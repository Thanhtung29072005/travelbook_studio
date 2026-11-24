"""Production graph adapters: real agents, shared checkpoints, no fixture reads."""
import asyncio
from datetime import datetime, timedelta, timezone
import base64
from contextlib import AsyncExitStack
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from langgraph.types import interrupt

from .workflow import TravelBookWorkflow, VISUAL_NODES
from .schemas import BudgetItem, Fact, TripRequest
from .services.router import NineRouterClient
from .services.public_api import PublicAPIClient
from .services.places import PlacesService
from .services.weather import WeatherService
from .services.routing import RoutingService
from .services.photos import PhotoService
from .services.illustrations import IllustrationService
from .services.visual_review import VisualEvidence, jpeg_evidence
from .agents.pm import PMAgent
from .agents.researcher import ResearcherAgent
from .agents.planner import PlannerAgent
from .agents.budget import BudgetAgent
from .services.budget_savings import repair_budget, savings_plan
from .agents.content_editor import ContentEditorAgent
from .agents.image_curator import ImageCuratorAgent
from .agents.illustrator import IllustratorAgent, plan_illustrations
from .agents.map_agent import MapAgent, MapInputFiles
from .agents.reviewer import ReviewerAgent
from .agents.layout_editor import LayoutEditorAgent


class LiveTravelBookWorkflow(TravelBookWorkflow):
    fixture_mode = False

    def _wrap(self, node, action, files, progress, stage):
        def real_action(state, unused):
            result = asyncio.run(self._execute(node, state))
            # Each visual branch owns distinct channels; only artifacts have a reducer.
            if node in VISUAL_NODES:
                for key in ('stage','fixture_mode','production_approved','error','model_usage'):
                    result.pop(key,None)
            return result
        return super()._wrap(node, real_action, (), progress, stage)

    def _export(self, state, node, result, documents):
        artifacts=list(result.get('artifacts',[]))
        for name, value, kind in documents:
            artifacts.append(self._save_json(state,name,value,node,kind,confidence=.5,source='derived:live-agent:'+node+'; see per-fact provenance'))
        result['artifacts']=artifacts
        return result

    async def _execute(self, node, state):
        pid=state['project_id']; root=self.artifacts_root; settings=self.settings
        self._directory(state).mkdir(parents=True,exist_ok=True)
        if node=='pm_finalize':
            pdf=state.get('final_pdf')
            if pdf is None or not Path(pdf.path).read_bytes().startswith(b'%PDF-'):
                raise ValueError('Final PDF missing')
            if state.get('fixture_mode'):
                raise ValueError('Live graph cannot finalize fixture state')
            self.db.add_event(pid,'pm','live_delivery_ready','Live workflow produced a PDF; inspect review and source confidence',
                {'pdf':pdf.path,'review_verdict':state['review'].verdict,'revision_exhausted':state.get('revision_exhausted',False)})
            return {'completed':True,'stage':'completed','production_approved':False,'error':None}
        async with AsyncExitStack() as stack:
            router=None; public=None
            if node in {'pm','researcher','planner','content_editor','image_curator','reviewer'}:
                router=await stack.enter_async_context(NineRouterClient(settings))
            if node in {'researcher','planner','image_curator','map_agent'}:
                public=PublicAPIClient(settings);stack.push_async_callback(public.aclose)
            def event(payload):
                kind='data_failover' if payload.get('status')=='failover' else 'data_cache_hit' if payload.get('cache_hit') else 'data_request'
                self.db.add_event(pid,node,kind,'Đổi máy chủ dữ liệu' if kind=='data_failover' else 'Accessed data provider',payload)
            trip=TripRequest.model_validate(state['trip_request']) if state.get('trip_request') else None
            context_trip=trip.model_copy(update={'requirements':[*trip.requirements,*state.get('feedback',[])]}) if trip else None
            if node=='pm':
                agent=PMAgent(self.db,router,root)
                prompt=state['user_prompt']
                user_feedback=[item for item in state.get('feedback',[]) if item.source.startswith('user:')]
                if user_feedback: prompt+='\n'+'\n'.join(item.value for item in user_feedback)
                from .services.day_schedule import journey_questions
                if trip is not None and trip.start_date is not None and not journey_questions(trip) and not user_feedback:
                    request=trip;plan=[Fact(value='Use the supplied validated trip request',source='user:trip_request',confidence=1)]
                else:
                    while True:
                        normalized=await agent.normalize(prompt,project_id=pid,on_attempt=agent.model_attempt(pid),confirmed_request=trip)
                        if normalized.status=='ready':
                            request=normalized.trip_request;plan=normalized.plan;break
                        answer=interrupt({'kind':'trip_clarification','node':'pm',
                            'questions':[question.model_dump(mode='json') for question in normalized.questions]})
                        if not isinstance(answer,dict) or not str(answer.get('answer','')).strip():
                            raise ValueError('A clarification answer is required')
                        prompt+='\nAdditional user answer:\n'+answer['answer'].strip()
                if request.days.value>31: raise ValueError('This workflow currently supports at most 31 days')
                self.db.update_project(pid,trip_request=request)
                result={'trip_request':request,'plan':plan,'clarification_questions':[],
                    'budget_baseline':None,'budget_savings':None,
                    'fixture_mode':False,'production_approved':False,'completed':False}
                return self._export(state,node,result,[('trip_request.json',request,'plan'),('plan.json',plan,'plan')])
            if trip is None: raise ValueError('PM must provide a complete TripRequest')
            if node=='researcher':
                agent=ResearcherAgent(self.db,PlacesService(settings,public),WeatherService(settings,public),root,router=router)
                found=await agent.research(context_trip,project_id=pid,on_event=event,on_attempt=agent.model_attempt(pid))
                if not found.places:
                    report=self._save_json(state,'diagnostics/'+uuid4().hex+'/research_diagnostics.json',found,'researcher','research',
                        confidence=1,source='execution:researcher-failure-diagnostics; provider warnings retained')
                    self.db.add_artifact(report)
                    details=[warning.value for warning in found.warnings]
                    self.db.add_event(pid,'researcher','research_unavailable','Không lấy được địa điểm thực tế',
                        {'destination':trip.destination.value,'destination_geocoded':found.destination is not None,
                         'warnings':details,'artifact_ids':[report.id]})
                    reason='; '.join(details) or 'Nguồn dữ liệu không trả địa điểm có tên và tọa độ hợp lệ.'
                    raise ValueError('Researcher chưa lấy được địa điểm tại '+trip.destination.value+': '+reason[:900])
                result={'places':found.places,'destination_place':found.destination,'weather':found.weather,
                    'research':found.notes,'research_warnings':found.warnings}
                if found.normalized_destination:
                    corrected=trip.model_copy(update={'destination':found.normalized_destination})
                    result['trip_request']=corrected
                    self.db.update_project(pid,trip_request=corrected)
                    self.db.add_event(pid,'researcher','destination_normalized','Đã chuẩn hóa tên điểm đến theo bản đồ',
                        {'original':trip.destination.value,'normalized':corrected.destination.value,'source':found.normalized_destination.source})
                return self._export(state,node,result,[('research.json',found.places,'research'),('weather.json',found.weather,'research'),('research_report.json',found,'research')])
            if node=='planner':
                agent=PlannerAgent(self.db,RoutingService(settings,public),root,router=router)
                planned=await agent.plan(context_trip,state['places'],project_id=pid,on_event=event,on_attempt=agent.model_attempt(pid))
                if planned.missing_days or len(planned.itinerary)!=trip.days.value:
                    raise ValueError('Insufficient real places or feasible visits for all requested days; refine the request or research')
                return self._export(state,node,{'itinerary':planned.itinerary,'planner_warnings':planned.warnings},
                    [('itinerary.json',planned.itinerary,'itinerary'),('planner_report.json',planned,'itinerary')])
            if node=='budget':
                items,warnings=self.costs(state,trip)
                calculated=BudgetAgent(self.db,root).calculate(trip,items,itinerary=state['itinerary'])
                baseline=state.get('budget_baseline')
                repairing=state.get('revision_pending',[])[:1]==['budget']
                if repairing and not calculated.budget.within_budget.value:
                    baseline=baseline or calculated.budget
                    revised,options=repair_budget(calculated.budget,trip,settings)
                    items=revised.items
                    calculated=BudgetAgent(self.db,root).calculate(trip,items,itinerary=state['itinerary'])
                    if options:
                        warnings.append(Fact(value='Đã chuyển khoản tạm tính đi-về/lưu trú sang mức tiết kiệm. Đây là giả định, cần xác nhận giá và khả năng thực hiện trước khi đặt.',
                            source='policy:budget-economy-assumptions',confidence=1))
                plan=savings_plan(calculated.budget,trip,settings,baseline=baseline) if baseline is not None else None
                result={'budget':calculated.budget,'cost_items':items,'budget_warnings':[*warnings,*calculated.warnings],
                    'budget_baseline':baseline,'budget_savings':plan}
                documents=[('budget.json',calculated.budget.model_dump(mode='json'),'budget'),('budget_warnings.json',result['budget_warnings'],'budget')]
                if plan:
                    documents.append(('budget_savings.json',plan,'budget'))
                return self._export(state,node,result,documents)
            if node=='content_editor':
                agent=ContentEditorAgent(self.db,router,root)
                edited=await agent.edit(context_trip,state['itinerary'],state['places'],budget=state['budget'],
                    research=[*state.get('research',[]),*state.get('feedback',[])],project_id=pid,on_attempt=agent.model_attempt(pid))
                result={'content_document':edited.document,'content':edited.document.as_facts()}
                return self._export(state,node,result,[('content.json',result['content'],'content'),('content_sections.json',edited.document,'content')])
            if node=='image_curator':
                agent=ImageCuratorAgent(self.db,PhotoService(settings,public),root,router=router)
                curated=await agent.curate(state['places'],trip.destination.value,project_id=pid,on_event=event,on_attempt=agent.model_attempt(pid),feedback=state.get('feedback',[]))
                result={'photos':curated.photos,'photo_warnings':curated.warnings}
                downloaded=[]
                for photo in curated.photos:
                    if photo.provider=='unsplash':
                        result['photo_warnings']=[*result['photo_warnings'],Fact(value='Unsplash photo retained as source link; PDF uses supported local images only: '+photo.id,source='policy:unsplash-hotlink',confidence=1)]
                        continue
                    try:
                        raw=await VisualEvidence(settings,root).read(url=str(photo.url.value),remote=True)
                        safe=base64.b64decode(jpeg_evidence(raw).split(',',1)[1],validate=True)
                        target=self._directory(state)/'photos'/(uuid4().hex+'.jpg')
                        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(safe)
                        photo.local_path=str(target)
                        artifact=self._artifact(state,target,node,'photo',source=str(photo.source_url.value)+'; processed:JPEG-preview',confidence=photo.url.confidence)
                        downloaded.append(artifact)
                    except Exception as error:
                        result['photo_warnings']=[*result['photo_warnings'],Fact(value='Photo download unavailable: '+photo.id+' ('+type(error).__name__+')',source='execution:photo-download',confidence=1)]
                result['artifacts']=downloaded
                if curated.missing_place_ids:
                    result['photo_warnings']=[*result['photo_warnings'],Fact(value='No verified photo found for: '+', '.join(curated.missing_place_ids),source='execution:photo-coverage',confidence=1)]
                return self._export(state,node,result,[('photos.json',curated.photos,'photo'),('photo_report.json',curated,'review'),('attribution.json',curated.attributions,'photo')])
            if node=='illustrator':
                images=IllustrationService(settings);stack.push_async_callback(images.aclose)
                agent=IllustratorAgent(self.db,images,root)
                requests=plan_illustrations(context_trip,state['itinerary'],state['places'],content_document=state.get('content_document'))
                generated=await agent.create_set(pid,uuid4().hex,requests,on_attempt=agent.model_attempt(pid))
                assets=[asset.artifact for asset in generated.assets]
                return self._export(state,node,{'illustrations':assets,'illustration_warnings':generated.warnings,'artifacts':assets},
                    [('illustrations.json',generated,'illustration')])
            if node=='map_agent':
                directory=self._directory(state)
                files=MapInputFiles(itinerary=str(directory/'itinerary.json'),places=str(directory/'research.json'),
                    budget=str(directory/'budget.json'),weather=str(directory/'weather.json'))
                agent=MapAgent(self.db,root)
                mapped,manifest=await agent.render_live(pid,files,settings=settings,public_api=public,trip_request=trip,on_event=event)
                return {'maps':mapped.maps,'charts':mapped.charts,'map_warnings':mapped.warnings,
                    'artifacts':[*mapped.maps,*mapped.charts,manifest]}
            if node=='reviewer':
                agent=ReviewerAgent(self.db,settings,root,router=router)
                reviewed=await agent.qualitative(state,project_id=pid,on_attempt=agent.model_attempt(pid))
                reviewed=await agent.visual_review(state,reviewed,project_id=pid,on_attempt=agent.model_attempt(pid))
                return self._export(state,node,{'review':reviewed.review},[('review.json',reviewed.review,'review'),('review_checks.json',reviewed,'review')])
            if node=='layout_editor':
                warning_keys=('revision_warnings','research_warnings','planner_warnings','budget_warnings','photo_warnings','illustration_warnings','map_warnings')
                layout_state={**state,'revision_warnings':[warning for key in warning_keys for warning in state.get(key,[])]}
                artifacts,manifest=LayoutEditorAgent(self.db,settings,root).render(layout_state)
                return {'final_pdf':next(item for item in artifacts if item.kind=='pdf'),'artifacts':artifacts}
            raise ValueError('Unknown live node: '+node)

    def costs(self,state,trip):
        # Only known ticket prices are sourced. Other numbers are explicit,
        # configurable VND assumptions, never inferred quotes or currency conversion.
        supplied=[BudgetItem.model_validate(item) for item in state.get('cost_items', [])]
        items=[item for item in supplied if item.amount.source.startswith('user:')]
        covered={item.category for item in items}
        warnings=[];known={place.id:place for place in state['places']}
        def add(key,category,label,amount,source,confidence,day=None,kind='estimated',evidence=None):
            items.append(BudgetItem(id=key,category=category,description=Fact(value=label,source=source,confidence=confidence),
                amount=Fact(value=amount,source=source,confidence=confidence),price_kind=kind,day_number=day,price_evidence=evidence))
        for day in state['itinerary']:
            for stop in day.stops:
                place=known[stop.place_id]
                if 'tickets' in covered:
                    continue
                priced=0
                quote=place.ticket_quote
                if quote:
                    reference=(trip.start_date.value+timedelta(days=day.day_number-1)) if trip.start_date else datetime.now(timezone.utc).date()
                    valid=(not quote.valid_from or reference>=quote.valid_from) and (not quote.valid_until or reference<=quote.valid_until)
                    if valid and quote.currency==trip.currency.value:
                        priced=trip.travelers.value if quote.audience=='all' or not trip.traveler_ages else sum(age.value>=18 for age in trip.traveler_ages)
                        if priced:
                            source=f'{quote.source_url}; retrieved={quote.retrieved_at.isoformat()}; official-admission; audience={quote.audience}'
                            label=f'Vé {place.name.value}: {quote.amount:,} {quote.currency}/người × {priced} người'
                            if quote.audience=='adult' and not trip.traveler_ages:
                                label+=' (tạm tính tất cả là người lớn; chưa có độ tuổi)'
                            add(f'ticket-{day.day_number}-{place.id}','tickets',label,quote.amount*priced,
                                source,.85,day.day_number,'sourced',evidence=quote)
                elif place.ticket_price is not None and place.currency.value==trip.currency.value:
                    # Legacy community fees lack age/eligibility information.
                    priced=trip.travelers.value if not trip.traveler_ages else 0
                    if priced:
                        add(f'ticket-{day.day_number}-{place.id}','tickets',f'Vé {place.name.value}: {place.ticket_price.value:,} {trip.currency.value}/người × {priced} người (giá cộng đồng, cần kiểm tra)',
                            place.ticket_price.value*priced,place.ticket_price.source+'; assumption:one ticket per traveler',
                            min(.5,place.ticket_price.confidence),day.day_number,'sourced')
                if priced<trip.travelers.value:
                    if trip.currency.value=='VND':
                        rate=self.settings.live_unknown_ticket_vnd_per_person
                        missing=trip.travelers.value-priced
                        add(f'ticket-reserve-{day.day_number}-{place.id}','tickets',
                            f'Tạm dự trù vé {place.name.value}: {rate:,}đ/người × {missing} người (chưa rõ giá áp dụng)',
                            Decimal(rate)*missing,'estimate:unknown-ticket-reserve; not an admission price',.25,day.day_number)
                    warnings.append(Fact(value='Chưa xác minh giá vé: '+place.name.value,source='assessment:missing-or-different-currency-ticket-price',confidence=1))
        if trip.currency.value=='VND':
            rates=self.settings
            people=trip.travelers.value
            cars=(people+3)//4
            for day in state['itinerary']:
                if 'food' not in covered:
                    add('food-'+str(day.day_number),'food',f'Ăn uống ngày {day.day_number}: {rates.live_food_vnd_per_person_day:,}đ/người × {people} người',Decimal(rates.live_food_vnd_per_person_day)*people,
                        'estimate:configuration:live_food_vnd_per_person_day; not a quotation',.25,day.day_number)
                if 'transport' not in covered:
                    add('transport-'+str(day.day_number),'transport',f'Di chuyển tại điểm đến ngày {day.day_number}: {rates.live_transport_vnd_per_day:,}đ/xe × {cars} xe (tối đa 4 người/xe)',Decimal(rates.live_transport_vnd_per_day)*cars,
                        'estimate:configuration:live_transport_vnd_per_day; assumption:4 travelers/car; not a route quote',.25,day.day_number)
            nights=max(0,trip.days.value-1);rooms=(trip.travelers.value+1)//2
            if nights and 'accommodation' not in covered:
                add('hotel','accommodation',f'Lưu trú: {rates.live_hotel_vnd_per_room_night:,}đ/phòng/đêm × {rooms} phòng × {nights} đêm (2 người/phòng)',Decimal(rates.live_hotel_vnd_per_room_night)*rooms*nights,
                    'estimate:configuration:live_hotel_vnd_per_room_night; assumption:2 travelers/room; days-1 nights',.25)
            if 'transport' not in covered and trip.departure and trip.departure.value.strip().casefold()!=trip.destination.value.strip().casefold():
                add('return-travel','transport',f'Tạm dự trù đi-về {trip.departure.value} - {trip.destination.value}: {rates.live_return_travel_vnd_per_person:,}đ/người × {people} người (chưa chọn phương tiện)',
                    Decimal(rates.live_return_travel_vnd_per_person)*people,'estimate:configuration:round-trip-allocation; not a route fare or booking quote',.2)
                warnings.append(Fact(value='Tiền đi-về chỉ là khoản tạm dự trù; cần chọn phương tiện, ngày đi và tra giá cho đúng tuyến.',source='policy:budget-coverage-disclosure',confidence=1))
            elif not trip.departure:
                warnings.append(Fact(value='Chưa có nơi khởi hành: tổng chưa gồm chi phí đi-về điểm đến.',source='policy:budget-coverage-disclosure',confidence=1))
            if 'contingency' not in covered:
                subtotal=sum((item.amount.value for item in items),Decimal(0))
                add('contingency','contingency',f'Dự phòng {rates.live_contingency_percent}% các khoản trên',
                    (subtotal*Decimal(rates.live_contingency_percent)/100).quantize(Decimal('1')),
                    'estimate:configuration:contingency-percent-of-subtotal',.5)
        else:
            warnings.append(Fact(value='No default non-VND allowances; add sourced or user-supplied costs before relying on the total',source='policy:no-implicit-currency-conversion',confidence=1))
        if covered:
            warnings.append(Fact(value='Khoản chi do bạn cung cấp được giữ nguyên và được hiểu là toàn bộ chi phí của nhóm cho hạng mục đó.',source='policy:user-cost-category-coverage',confidence=1))
        warnings.append(Fact(value='Dự toán cho toàn bộ nhóm. Giá ước tính là mức tạm tính, chưa phải báo giá; chưa gồm mua sắm và chi tiêu cá nhân ngoài lịch trình.',source='policy:budget-coverage-disclosure',confidence=1))
        return items,warnings
