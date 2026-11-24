"""Named meal/lodging suggestions from map records; never invented reservations or prices."""
import asyncio
from datetime import datetime
import math

from pydantic import ValidationError

from ..schemas import Fact, VenueRecommendation
from .opening_hours import parse_opening_hours, UnsupportedHours
from .public_api import DataServiceError
from .routing import haversine_km


def f(value,source,confidence=.75):
    return Fact(value=value,source=source,confidence=confidence)


def distance(venue,anchor):
    return haversine_km(venue.latitude.value,venue.longitude.value,*anchor)


class TripVenueService:
    def __init__(self,settings,http):
        self.settings,self.http=settings,http
        self.cache={}
        self.unavailable_endpoints=set()

    @staticmethod
    def parse(row,retrieved,kind):
        tags=row.get('tags',{})
        if not isinstance(tags,dict): return None
        name=tags.get('name:vi') or tags.get('name') or tags.get('name:en')
        osm_type=row.get('type')
        if not name or osm_type not in {'node','way','relation'} or not isinstance(row.get('id'),int) or row['id']<=0:
            return None
        if kind=='restaurant' and tags.get('amenity')!='restaurant':
            return None
        lodging=tags.get('tourism')
        if kind=='hotel' and lodging not in {'hotel','guest_house','hostel'}:
            return None
        coordinate=row.get('center',row)
        url=f"https://www.openstreetmap.org/{osm_type}/{row['id']}"
        source=url+'; retrieved='+retrieved+'; map-record, not booking confirmation'
        address=tags.get('addr:full') or ', '.join(str(tags[key]) for key in (
            'addr:housenumber','addr:street','addr:suburb','addr:city') if tags.get(key))
        try:
            stars=int(tags['stars']) if str(tags.get('stars','')).isdigit() else None
            return VenueRecommendation(id=f"osm:{osm_type}:{row['id']}",kind=kind,
                name=f(name,source),latitude=f(float(coordinate['lat']),source),longitude=f(float(coordinate['lon']),source),
                source_url=f(url,source),address=f(address,source) if address else None,
                opening_hours=f(tags['opening_hours'],source) if tags.get('opening_hours') else None,
                accommodation_type=lodging if kind=='hotel' else None,stars=stars if stars is not None and stars<=5 else None)
        except (KeyError,TypeError,ValueError,ValidationError):
            return None

    async def nearby(self,anchor,kind,*,on_event=None):
        key=(round(anchor[0],4),round(anchor[1],4),kind)
        if key in self.cache:
            return self.cache[key]
        radius=700 if kind=='restaurant' else 6000
        condition='["amenity"="restaurant"]["name"]' if kind=='restaurant' else '["tourism"~"^(hotel|guest_house|hostel)$"]["name"]'
        query=f'[out:json][timeout:12];nwr(around:{radius},{anchor[0]:.6f},{anchor[1]:.6f}){condition};out body center 60;'
        endpoints=list(dict.fromkeys([self.settings.overpass_url,*[url.strip() for url in self.settings.overpass_fallback_urls.split(',') if url.strip()]]))
        found=[]
        for endpoint in endpoints:
            if endpoint in self.unavailable_endpoints:
                continue
            try:
                response=await asyncio.wait_for(self.http.request('overpass',endpoint,form={'data':query},interval=1,on_event=on_event),timeout=8)
                if not isinstance(response.data,dict) or not isinstance(response.data.get('elements'),list) or response.data.get('remark'):
                    raise DataServiceError('Incomplete venue lookup')
                found=[venue for row in response.data['elements'] if isinstance(row,dict)
                       and (venue:=self.parse(row,response.retrieved_at,kind)) is not None]
                break
            except (DataServiceError,asyncio.TimeoutError) as error:
                self.unavailable_endpoints.add(endpoint)
                status=getattr(error,'status_code',None)
                if status and status not in {500,502,503,504}:
                    break
        if not found:
            # Independent map search if Overpass is unavailable or has no named records.
            lat,lon=anchor
            dy=radius/111000
            dx=dy/max(.1,math.cos(math.radians(lat)))
            try:
                response=await asyncio.wait_for(self.http.request('nominatim',self.settings.nominatim_base_url+'/search',
                    params={'amenity':'restaurant' if kind=='restaurant' else 'hotel','format':'jsonv2',
                        'viewbox':f'{lon-dx},{lat+dy},{lon+dx},{lat-dy}','bounded':1,'limit':10,
                        'addressdetails':1,'namedetails':1,'extratags':1},
                    interval=self.settings.nominatim_min_interval_seconds,on_event=on_event),timeout=8)
                for row in response.data if isinstance(response.data,list) else []:
                    if not isinstance(row,dict): continue
                    tags={**(row.get('extratags') or {}),'name':row.get('name') or (row.get('namedetails') or {}).get('name'),
                          'addr:full':row.get('display_name')}
                    category='amenity' if kind=='restaurant' else 'tourism'
                    tags[category]=row.get('type')
                    try:
                        record={'id':int(row['osm_id']),'type':row['osm_type'],'lat':row['lat'],'lon':row['lon'],'tags':tags}
                        venue=self.parse(record,response.retrieved_at,kind)
                        if venue: found.append(venue)
                    except (KeyError,TypeError,ValueError):
                        continue
            except (DataServiceError,asyncio.TimeoutError):
                pass
        self.cache[key]=found
        return found

    async def enrich(self,trip,days,places,*,on_event=None):
        warnings=[]
        if not any(activity.kind in {'hotel','breakfast','lunch','dinner'} for day in days for activity in day.activities):
            return warnings
        indexed={place.id:place for place in places}
        stops=[indexed[stop.place_id] for day in days for stop in day.stops]
        if not stops:
            return warnings
        center=(sum(p.latitude.value for p in stops)/len(stops),sum(p.longitude.value for p in stops)/len(stops))
        async def work():
            hotel=None
            if trip.days.value>1 and not trip.hotel_name:
                candidates=await self.nearby(center,'hotel',on_event=on_event)
                rooms=math.ceil(trip.travelers.value/2)
                nightly=float(trip.budget.value)*.25/max(1,rooms*(trip.days.value-1)) if trip.currency.value=='VND' else None
                # A tier preference is not a verified room quote or proof of affordability.
                candidates=[v for v in candidates if distance(v,center)<=6 and not (nightly is not None and nightly<800000 and (v.stars or 0)>=4)]
                cheap=nightly is not None and nightly<600000
                candidates.sort(key=lambda v:(0 if not cheap or v.accommodation_type in {'guest_house','hostel'} else 1,
                    distance(v,center),v.id))
                hotel=candidates[0] if candidates else None
                if hotel is None:
                    warnings.append(f('Chưa tìm được nơi lưu trú có tên và nguồn phù hợp khu vực; không tự đặt tên khách sạn.','execution:venue-lookup',1))
            lookups=0
            used=set()
            for day in days:
                for activity in sorted(day.activities,key=lambda item:{'hotel':0,'lunch':1,'dinner':2,'breakfast':3}.get(item.kind,4)):
                    if activity.kind=='hotel' and hotel:
                        activity.venue=hotel
                        old=activity.title.value
                        activity.title=activity.title.model_copy(update={'value':old.replace('nơi lưu trú dự kiến (chưa chọn khách sạn)',hotel.name.value+' (đề xuất, chưa đặt phòng)')})
                    if activity.kind not in {'breakfast','lunch','dinner'}:
                        continue
                    if activity.within_place_id:
                        parent=indexed.get(activity.within_place_id)
                        if parent:
                            activity.title=f(('Ăn trưa' if activity.kind=='lunch' else 'Ăn tối')+' trong khu '+parent.name.value+'; quầy/nhà hàng trong khu cần xác nhận',parent.name.source,.5)
                        continue
                    previous=[s for s in day.stops if s.departure.value<=activity.start.value]
                    following=[s for s in day.stops if s.arrival.value>=activity.end.value]
                    stop=previous[-1] if previous else following[0] if following else None
                    if stop is None and not (activity.kind=='breakfast' and hotel):
                        continue
                    place=hotel if hotel and (activity.kind=='breakfast' or not previous) else indexed[stop.place_id]
                    anchor=(place.latitude.value,place.longitude.value)
                    if lookups>=16:
                        continue
                    lookups+=1
                    candidates=await self.nearby(anchor,'restaurant',on_event=on_event)
                    available=[]
                    duration=(datetime.combine(datetime.min.date(),activity.end.value)-datetime.combine(datetime.min.date(),activity.start.value)).total_seconds()/60
                    a=activity.start.value.hour*60+activity.start.value.minute
                    b=activity.end.value.hour*60+activity.end.value.minute
                    for venue in candidates:
                        km=distance(venue,anchor)
                        # Meal slot includes an estimated walk there and back, not free teleportation.
                        transfer=math.ceil(km*1.35/4*60)*2
                        if km>.5 or duration-transfer<30:
                            continue
                        if venue.opening_hours:
                            try:
                                rule=parse_opening_hours(venue.opening_hours.value)
                                if not any(x<=a and b<=y for x,y in rule.windows(day.date.value if day.date else None)):
                                    continue
                            except UnsupportedHours:
                                continue
                        available.append((venue.id in used,km,venue.id,venue,transfer))
                    if available:
                        _,_,_,venue,transfer=min(available,key=lambda row:row[:3])
                        used.add(venue.id)
                        activity.venue=venue
                        activity.venue_transfer_minutes=f(transfer,'assumption:roundtrip-walk; haversine*1.35, 4km/h',.5)
                        meal={'breakfast':'Ăn sáng','lunch':'Ăn trưa','dinner':'Ăn tối'}[activity.kind]
                        activity.title=f(meal+' tại '+venue.name.value,venue.name.source,.7)
                    else:
                        warnings.append(f(f'Ngày {day.day_number}: chưa tìm thấy nhà hàng có nguồn, đủ gần và phù hợp khung {activity.start.value:%H:%M}–{activity.end.value:%H:%M}.','execution:venue-lookup',1))
        try:
            await asyncio.wait_for(work(),timeout=100)
        except asyncio.TimeoutError:
            warnings.append(f('Hết thời gian tra nhà hàng/nơi ở; các mục chưa tra được không được tự đặt tên.','execution:venue-lookup-timeout',1))
        return warnings
