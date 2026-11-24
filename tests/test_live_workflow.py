"""Live wiring contracts: providers are mocked; graph/SQLite/PNG/PDF are real."""
import json
from datetime import date, time, timedelta
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pypdf import PdfReader

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.schemas import TripRequest, Place, DayPlan, PlanStop, WeatherReport, ContentDocument, DayContent, Fact
from backend.app.agents.pm import PMResult, PMTask
from backend.app.agents.researcher import ResearchResult
from backend.app.agents.planner import PlannerResult
from backend.app.agents.content_editor import ContentResult
from backend.app.agents.image_curator import CurationResult
from backend.app.task_plan import build_tasks


def f(value): return Fact(value=value,source='user:integration-contract',confidence=.7)


class LiveWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(_env_file=None,data_dir=Path(self.temp.name)/'data',nine_router_api_key='',nine_router_models='',
            agent_reasoning_enabled=False,reviewer_visual_enabled=False,max_revisions=0,map_basemap_enabled=False)
        self.app=create_app(self.settings)
        self.life=self.app.router.lifespan_context(self.app);await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__,None,None,None)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        self.trip=TripRequest(destination=f('Phú Quốc'),days=f(3),budget=f(10000000),currency=f('VND'),travelers=f(2),start_date=f(date(2026,11,26)))

    async def wait(self,pid):
        task=self.app.state.manager.running.get(pid)
        if task: await task
        return self.app.state.db.get_project(pid)

    async def test_budget_repair_node_changes_only_economy_assumptions_and_keeps_baseline(self):
        from test_budget_savings import example
        trip,budget=example()
        response=await self.client.post('/api/projects',json={'prompt':'Sửa dự toán','trip_request':trip.model_dump(mode='json')})
        pid=response.json()['id']
        workflow=self.app.state.manager.workflow
        state={'project_id':pid,'trip_request':trip,'budget':budget,'revision_count':1,'revision_pending':['budget'],
            'itinerary':[DayPlan(day_number=i,stops=[PlanStop(place_id='a',arrival=f(time(9)),departure=f(time(10)))]) for i in range(1,4)]}
        with patch.object(workflow,'costs',return_value=(budget.items,[])):
            result=await workflow._execute('budget',state)
        self.assertEqual(result['budget'].total.value,9845000)
        self.assertEqual(result['budget_baseline'].total.value,13365000)
        self.assertEqual(result['budget_savings'].current_total,9845000)
        self.assertTrue(any(item.name=='budget_savings.json' for item in result['artifacts']))

    async def test_no_model_fails_instead_of_publishing_danang_fixture(self):
        response=await self.client.post('/api/projects',json={'prompt':'Phú Quốc 3 ngày cho 2 người, 10 triệu đồng','start_workflow':True})
        pid=response.json()['id'];row=await self.wait(pid)
        self.assertEqual(row['status'],'failed')
        self.assertFalse(row['state']['fixture_mode'])
        self.assertIsNone(row['final_pdf'])
        self.assertFalse(any(e['event_type']=='fixture_read' for e in self.app.state.db.get_events(pid)))

    async def test_api_uses_live_workers_and_requested_destination_through_pdf(self):
        places=[Place(id='pq-'+str(i),name=f('Phú Quốc place '+str(i)),latitude=f(10.2+i*.01),longitude=f(103.95+i*.01)) for i in range(3)]
        days=[DayPlan(day_number=i+1,date=f(date(2026,11,26)+timedelta(days=i)),title=f('Phú Quốc day '+str(i+1)),stops=[PlanStop(place_id=place.id,arrival=f(time(9)),departure=f(time(10)),travel_minutes=f(0),distance_km=f(0))]) for i,place in enumerate(places)]
        document=ContentDocument(introduction=f('Phú Quốc travel guide'),days=[DayContent(day_number=i+1,introduction=f('Phú Quốc day introduction')) for i in range(3)],tips=[f('Check opening hours')],food=[f('Check local menus')],culture=[f('Respect local customs')])
        research=AsyncMock(return_value=ResearchResult(status='partial',destination=places[0],places=places,weather=WeatherReport(status='not_requested')))
        planner=AsyncMock(return_value=PlannerResult(status='ready',itinerary=days))
        editor=AsyncMock(return_value=ContentResult(document=document,model='mock-editor',cache_hit=False))
        curator=AsyncMock(return_value=CurationResult(missing_place_ids=[place.id for place in places]))
        with patch('backend.app.live_workflow.ResearcherAgent.research',research),patch('backend.app.live_workflow.PlannerAgent.plan',planner),patch('backend.app.live_workflow.ContentEditorAgent.edit',editor),patch('backend.app.live_workflow.ImageCuratorAgent.curate',curator):
            response=await self.client.post('/api/projects',json={'prompt':'Phú Quốc','trip_request':self.trip.model_dump(mode='json'),'start_workflow':True})
            pid=response.json()['id'];row=await self.wait(pid)
        self.assertEqual(row['status'],'completed',row['error'])
        self.assertFalse(row['state']['fixture_mode'])
        self.assertEqual(row['state']['trip_request']['destination']['value'],'Phú Quốc')
        self.assertEqual(research.await_args.args[0].destination.value,'Phú Quốc')
        self.assertEqual(planner.await_args.args[0].destination.value,'Phú Quốc')
        self.assertEqual(len(row['state']['maps']),3)
        self.assertTrue(row['state']['budget_warnings'])
        pages=PdfReader(row['final_pdf']).pages
        self.assertIn('Phú Quốc',''.join(page.extract_text() for page in pages))
        self.assertFalse(any(e['event_type']=='fixture_read' for e in self.app.state.db.get_events(pid,limit=10000)))

    async def test_pm_clarification_resumes_same_checkpoint_then_calls_research(self):
        response=await self.client.post('/api/projects',json={'prompt':'Phú Quốc'})
        pid=response.json()['id'];manager=self.app.state.manager
        missing=PMResult(status='needs_clarification',trip_request=None,questions=[f('Bao nhiêu ngày, người và ngân sách?')],model='mock-pm',cache_hit=False)
        ready=PMResult(status='ready',trip_request=self.trip,plan=[f('Plan Phú Quốc')],tasks=[PMTask.model_validate(task) for task in build_tasks(pid)],model='mock-pm',cache_hit=False)
        async def normalize(prompt,**kwargs): return ready if 'Additional user answer:' in prompt else missing
        with patch('backend.app.live_workflow.PMAgent.normalize',side_effect=normalize),patch('backend.app.live_workflow.ResearcherAgent.research',side_effect=ValueError('Injected provider outage')):
            manager.start(pid);row=await self.wait(pid)
            self.assertEqual(row['status'],'awaiting_input',row['error'])
            thread=manager.config(pid)['configurable']['thread_id']
            reply=await self.client.post('/api/projects/'+pid+'/answer',json={'answer':'3 ngày, 2 người lớn, tổng 10 triệu đồng'})
            self.assertEqual(reply.status_code,200,reply.text)
            row=await self.wait(pid)
        self.assertEqual(manager.config(pid)['configurable']['thread_id'],thread)
        self.assertEqual(row['state']['trip_request']['destination']['value'],'Phú Quốc')
        self.assertEqual(row['status'],'failed')
        self.assertIn('Injected provider outage',row['error'])
        self.assertIsNone(row['final_pdf'])

    async def test_preconfirmed_undated_trip_waits_for_date_before_research(self):
        undated=self.trip.model_copy(update={'start_date':None})
        response=await self.client.post('/api/projects',json={'prompt':'Phú Quốc','trip_request':undated.model_dump(mode='json')})
        pid=response.json()['id'];manager=self.app.state.manager
        missing=PMResult(status='needs_clarification',trip_request=None,questions=[f('Bạn dự kiến khởi hành ngày nào?')],model='mock-pm',cache_hit=False)
        ready=PMResult(status='ready',trip_request=self.trip,plan=[f('Plan Phú Quốc')],tasks=[PMTask.model_validate(task) for task in build_tasks(pid)],model='mock-pm',cache_hit=False)
        async def normalize(prompt,**kwargs):
            self.assertEqual(kwargs['confirmed_request'].budget,undated.budget)
            return ready if '26/11/2026' in prompt else missing
        research=AsyncMock(side_effect=ValueError('Injected provider outage'))
        with patch('backend.app.live_workflow.PMAgent.normalize',side_effect=normalize),patch('backend.app.live_workflow.ResearcherAgent.research',research):
            manager.start(pid);row=await self.wait(pid)
            self.assertEqual(row['status'],'awaiting_input',row['error'])
            research.assert_not_awaited()
            thread=manager.config(pid)['configurable']['thread_id']
            reply=await self.client.post('/api/projects/'+pid+'/answer',json={'answer':'Khởi hành 26/11/2026'})
            self.assertEqual(reply.status_code,200,reply.text)
            row=await self.wait(pid)
        self.assertEqual(manager.config(pid)['configurable']['thread_id'],thread)
        self.assertEqual(row['state']['trip_request']['start_date']['value'],'2026-11-26')
        research.assert_awaited_once()

    async def test_legacy_fixture_cannot_be_silently_resumed_as_live(self):
        response=await self.client.post('/api/projects',json={'prompt':'Phú Quốc'})
        pid=response.json()['id'];db=self.app.state.db
        row=db.get_project(pid);db.update_project(pid,state={**row['state'],'fixture_mode':True},status='paused')
        self.assertEqual((await self.client.post('/api/projects/'+pid+'/retry')).status_code,409)
        self.assertEqual((await self.client.post('/api/projects/'+pid+'/resume')).status_code,409)
