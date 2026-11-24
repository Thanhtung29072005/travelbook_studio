"""Local day boundaries and explicit, provisional meal/lodging slots."""
from datetime import timedelta, timezone, time

from ..schemas import ActivitySlot, Fact

LOCAL = timezone(timedelta(hours=7))
POLICY = 'assumption:day-schedule-policy; transfer/luggage/terminal buffers, not reservations'


def local_stamp(fact):
    if fact is None:
        return None
    stamp = fact.value
    return stamp.astimezone(LOCAL) if stamp.tzinfo else stamp.replace(tzinfo=LOCAL)


def journey_questions(trip):
    if not trip.departure or trip.departure.value.casefold() == trip.destination.value.casefold():
        return []
    if trip.transport_times_flexible and trip.transport_times_flexible.value:
        return []
    missing = []
    if trip.destination_arrival_at is None:
        missing.append('giờ đến '+trip.destination.value+' ngày đầu')
    if trip.destination_departure_at is None:
        missing.append('giờ về (rời '+trip.destination.value+') ngày cuối')
    return ['Bạn cho biết ' + ' và '.join(missing) +
            '? Chỉ cần trả lời giờ, ví dụ: đến 10:00, về 18:00. Nếu chưa chốt, có thể dùng lịch tạm.'] if missing else []


def slot(kind, title, start, end, *, source=POLICY, confidence=.5):
    return ActivitySlot(kind=kind, title=Fact(value=title, source=source, confidence=confidence),
        start=Fact(value=time(start//60, start%60), source=source, confidence=confidence),
        end=Fact(value=time(end//60, end%60), source=source, confidence=confidence))


def minute(clock):
    return clock.hour*60 + clock.minute


def day_frame(trip, date, start, finish):
    """Never manufacture flight times; only transfer durations are policy estimates."""
    available_start, available_end = start, finish
    fixed = []
    arrival, departure = local_stamp(trip.destination_arrival_at), local_stamp(trip.destination_departure_at)
    hotel = trip.hotel_name.value if trip.hotel_name else 'nơi lưu trú dự kiến (chưa chọn khách sạn)'
    if arrival and date:
        if date < arrival.date():
            return finish, finish, [slot('free_time', 'Ngày di chuyển đến điểm đến; chưa có thời gian tham quan', start, finish)]
        if date == arrival.date():
            at = min(1438, minute(arrival.time()) + bool(arrival.second or arrival.microsecond))
            end = min(1439, at+60)
            fixed.append(slot('transfer', 'Đến nơi, nhận hành lý và di chuyển về nơi lưu trú (dự kiến 60 phút)', at, end))
            if end < 1439:
                fixed.append(slot('hotel', 'Gửi hành lý / làm thủ tục tại '+hotel+'; giờ nhận phòng cần xác nhận', end, min(1439,end+30)))
            available_start = max(start, at+90)
    if departure and date:
        if date > departure.date():
            return finish, finish, [slot('free_time', 'Đã rời điểm đến; không xếp tham quan', start, finish)]
        if date == departure.date():
            at = minute(departure.time())
            # Conservative air-travel buffers also safe for an unconfirmed mode.
            reserve = 180
            available_end = min(finish, max(0, at-reserve))
            for kind, title, a, b in (
                ('hotel', 'Nhận hành lý / trả phòng tại '+hotel, max(0,at-180),max(0,at-150)),
                ('transfer', 'Di chuyển tới sân bay / ga / bến xe (dự kiến 60 phút)',max(0,at-150),max(0,at-90)),
                ('terminal', 'Làm thủ tục và chờ chuyến về; khởi hành lúc '+departure.strftime('%H:%M'),max(0,at-90),at)):
                if b>a:
                    fixed.append(slot(kind,title,a,b))
    # Very short stopovers cannot accommodate both arrival and departure buffers.
    if any(min(minute(a.end.value),minute(b.end.value)) > max(minute(a.start.value),minute(b.start.value))
           for i,a in enumerate(fixed) for b in fixed[i+1:]):
        return finish, finish, [slot('terminal','Đến nơi và nối chuyến về; không đủ thời gian tham quan',
            min(minute(a.start.value) for a in fixed),max(minute(a.end.value) for a in fixed))]
    lunch_start = max(720,available_start)
    lunch_end = min(lunch_start+60,available_end)
    late_lunch = lunch_start < 900 and lunch_end-lunch_start >= 30
    rest_start = max(780,lunch_end if late_lunch else available_start)
    for kind,title,a,b in (
        ('breakfast','Ăn sáng (tự chọn địa điểm)',480,525),
        ('lunch','Ăn trưa (tự chọn địa điểm)',lunch_start,lunch_end if late_lunch else lunch_start),
        ('rest','Nghỉ trưa, tránh xếp tham quan liên tục',rest_start,min(rest_start+60,available_end) if rest_start<960 else rest_start),
        ('dinner','Ăn tối (tự chọn địa điểm)',1080,1140)):
        a,b = max(a,available_start),min(b,available_end)
        if b>a:
            fixed.append(slot(kind,title,a,b))
    if not (departure and date == departure.date()) and available_start < finish-60:
        fixed += [slot('transfer','Trở về nơi lưu trú (dự kiến 30 phút)',finish-60,finish-30),
                  slot('hotel','Nghỉ tối tại '+hotel,finish-30,finish)]
        available_end = finish-60
    return available_start, available_end, sorted(fixed,key=lambda s:s.start.value)


VISIT_MINUTES = {'museum':90,'gallery':75,'temple':60,'bridge':45,'beach':120,
    'park':90,'heritage':150,'heritage_site':150,'natural_area':180,'nature_reserve':180,'national_park':240,
    'hill_station':360,'theme_park':360,'amusement_park':360,'zoo':180,'aquarium':120}


def visit_duration(place, default):
    if place.visit_minutes:
        return place.visit_minutes.value
    kind = place.tourism_kind.value if place.tourism_kind else ''
    return VISIT_MINUTES.get(kind,default)


def fit_visit(current, travel, duration, windows, activities, finish):
    """Travel is contiguous before each visit; long venues may contain on-site meals."""
    candidates=[]
    for a,b in windows:
        arrival=max(current+travel,a)
        while arrival+duration <= min(b,finish):
            conflict=None
            for block in activities:
                x,y=minute(block.start.value),minute(block.end.value)
                if arrival-travel < y and arrival+duration > x:
                    on_site=(duration>=240 and block.kind in {'lunch','rest','dinner'} and arrival<=x and y<=arrival+duration)
                    if not on_site:
                        conflict=y
                        break
            if conflict is None:
                candidates.append((arrival,b))
                break
            arrival=max(arrival,conflict+travel)
    return min(candidates) if candidates else None


def complete_activities(stops, activities, start, finish):
    for block in activities:
        for stop in stops:
            if stop.arrival.value <= block.start.value < block.end.value <= stop.departure.value:
                block.within_place_id=stop.place_id
                block.title=block.title.model_copy(update={'value':block.title.value+' tại khu tham quan'})
    # Make spare time explicit rather than pretending extra attractions were researched.
    occupied=[(minute(s.arrival.value)-(s.travel_minutes.value if s.travel_minutes else 0),minute(s.departure.value)) for s in stops]
    occupied += [(minute(s.start.value),minute(s.end.value)) for s in activities]
    occupied = [(max(a,start),min(b,finish)) for a,b in occupied if a<finish and b>start]
    cursor=start
    for a,b in sorted(occupied):
        if a-cursor>=30:
            activities.append(slot('free_time','Thời gian tự do / nghỉ ngơi, có thể bổ sung hoạt động theo sở thích',cursor,min(a,finish)))
        cursor=max(cursor,b)
    if finish-cursor>=30:
        activities.append(slot('free_time','Thời gian tự do / nghỉ ngơi',cursor,finish))
    return sorted(activities,key=lambda s:s.start.value)
