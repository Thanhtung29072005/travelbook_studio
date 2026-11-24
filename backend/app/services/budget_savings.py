"""Transparent economy assumptions; never discount sourced/confirmed prices in-place."""
from decimal import Decimal
import unicodedata

from ..schemas import Budget, BudgetItem, BudgetSavingOption, BudgetSavingsPlan, Fact, TripRequest


def normalized(value):
    value=unicodedata.normalize('NFD', value.casefold().replace('đ','d'))
    return ''.join(char for char in value if not unicodedata.combining(char))


def limits(settings=None):
    return (Decimal(getattr(settings,'budget_economy_hotel_vnd_per_room_night',400000)),
            Decimal(getattr(settings,'budget_economy_return_vnd_per_person',1200000)))


def alternative_items(budget, trip, settings=None, *, scenario=False):
    budget=Budget.model_validate(budget)
    trip=TripRequest.model_validate(trip)
    if budget.currency.value!='VND':
        return list(budget.items), []
    hotel_rate,return_rate=limits(settings)
    requirements=normalized(' '.join(item.value for item in trip.requirements))
    fixed_booking=any(word in requirements for word in ('khong hoan huy','khong hoan tien','non-refundable','nonrefundable','da dat'))
    fixed_hotel=any(word in requirements for word in ('4 sao','5 sao','four star','five star','cao cap','luxury'))
    fixed_flight=any(word in requirements for word in ('may bay','flight','thuong gia','business class'))
    changes={}
    options=[]
    rooms=(trip.travelers.value+1)//2
    nights=max(0,trip.days.value-1)
    for category,key,rate,quantity,title,assumption in (
        ('accommodation','hotel',hotel_rate,rooms*nights,'Đổi mức khách sạn',
         f'Mức mục tiêu {hotel_rate:,.0f} VND/phòng/đêm × {rooms} phòng × {nights} đêm, tối đa 2 người/phòng. Cần khách sạn xác nhận phòng trống và giá phù hợp.'),
        ('transport','return-travel',return_rate,trip.travelers.value,'So sánh phương án đi-về tiết kiệm',
         f'Mức mục tiêu {return_rate:,.0f} VND/người khứ hồi × {trip.travelers.value} người. So sánh vé giá thấp, tàu/xe hoặc phương án kết hợp nếu phù hợp tuyến; cần kiểm tra thời gian di chuyển với lịch trình, không mặc định có tuyến khả thi.'),
    ):
        if fixed_booking or (key=='hotel' and fixed_hotel) or (key=='return-travel' and fixed_flight) or not quantity:
            continue
        candidates=[item for item in budget.items if item.category==category and (
            item.id==key or scenario and key=='hotel')]
        if not scenario:
            candidates=[item for item in candidates if item.price_kind=='estimated' and
                item.amount.source.startswith(('estimate:configuration:live_hotel_vnd_per_room_night',
                    'estimate:configuration:round-trip-allocation','estimate:budget-economy:'))]
        original=sum((item.amount.value for item in candidates),Decimal(0))
        target=rate*quantity
        if not candidates or target>=original:
            continue
        options.append(BudgetSavingOption(title=title,original_amount=original,target_amount=target,assumption=assumption))
        # Aggregate only in hypothetical scenarios; normal repairs own one default line.
        for index,item in enumerate(candidates):
            amount=target if index==0 else Decimal(0)
            source=('estimate:budget-saving-scenario:' if scenario else 'estimate:budget-economy:')+key+'; not a quotation'
            changes[item.id]=item.model_copy(update={
                'description':Fact(value=title+' (tạm tính): '+assumption,source=source,confidence=.25),
                'amount':Fact(value=amount,source=source,confidence=.25),'price_kind':'estimated','price_evidence':None})
    items=[changes.get(item.id,item) for item in budget.items]
    if changes:
        # Recompute percentage reserves only when the application generated them.
        for index,item in enumerate(items):
            if item.id=='contingency' and item.price_kind=='estimated' and item.amount.source.startswith('estimate:configuration:contingency-percent-of-subtotal'):
                previous=sum((value.amount.value for value in budget.items if value.id!=item.id),Decimal(0))
                subtotal=sum((value.amount.value for value in items if value.id!=item.id),Decimal(0))
                reserve=(subtotal*item.amount.value/previous).quantize(Decimal('1')) if previous else item.amount.value
                items[index]=item.model_copy(update={'amount':Fact(value=reserve,source=item.amount.source,confidence=item.amount.confidence)})
    return items,options


def repair_budget(budget,trip,settings=None):
    budget=Budget.model_validate(budget)
    items,options=alternative_items(budget,trip,settings)
    return Budget(currency=budget.currency,limit=TripRequest.model_validate(trip).budget,items=items),options


def savings_plan(budget,trip=None,settings=None,*,baseline=None):
    current=Budget.model_validate(budget)
    initial=Budget.model_validate(baseline) if baseline is not None else current
    if initial.currency.value!=current.currency.value or initial.limit.value!=current.limit.value or initial.total.value<current.total.value:
        initial=current
    options=[]
    proposed=current
    if trip is not None:
        items,options=alternative_items(initial,trip,settings,scenario=True)
        proposed=Budget(currency=initial.currency,limit=current.limit,items=items)
    notes=['Các mức mục tiêu là giả định để so sánh, chưa phải báo giá hay phương án đã đặt. Cần xác nhận giá, điều kiện đổi/hủy và thời gian di chuyển trước khi chọn.']
    reserve_reduction=initial.total.value-proposed.total.value-sum((option.saving for option in options),Decimal(0))
    if reserve_reduction>0:
        notes.insert(0,f'Khoản dự phòng được tính lại theo tỷ lệ ban đầu trên chi phí mới, giảm thêm {reserve_reduction:,.0f} {current.currency.value}.')
    if proposed.total.value>current.limit.value:
        notes.append(f'Ngay cả kịch bản tiết kiệm vẫn vượt {proposed.total.value-current.limit.value:,.0f} {current.currency.value}. Cần giảm thêm hạng mục tùy chọn, điều chỉnh thời lượng hoặc trao đổi lại ngân sách; không coi phương án này là đã đạt ngân sách.')
    if not options:
        notes.append('Chưa có khoản đi-về hoặc lưu trú có thể giảm bằng dữ liệu hiện tại. Hãy lấy báo giá phương tiện khác/phòng mức thấp hơn nếu phù hợp yêu cầu; không tự giảm giá đã xác minh hoặc khoản chi đã chốt.')
    return BudgetSavingsPlan(currency=current.currency.value,initial_total=initial.total.value,current_total=current.total.value,
        limit=current.limit.value,proposed_total=proposed.total.value,options=options,notes=notes)
