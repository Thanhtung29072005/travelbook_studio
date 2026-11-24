import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import tempfile
import threading
import unittest

import httpx
from pypdf import PdfReader

from backend.app.agents.supervisor import HybridSupervisor
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.manager import WorkflowManager
from backend.app.services.router import NineRouterClient, RouterExhaustedError
from backend.app.task_plan import DEPENDENCIES, build_tasks
from backend.app.schemas import Fact, Review, ReviewIssue
from backend.app.workflow import TravelBookWorkflow, VISUAL_NODES
from backend.app.workspace import WorkspaceStore


class SupervisorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, data_dir=self.root/'data', supervisor_enabled=True,
            nine_router_api_key='mock',nine_router_models='primary,backup',llm_max_attempts_per_model=1,
            llm_retry_delay_seconds=0,llm_cache_path=self.root/'cache.db')
        self.db = Database(self.settings.database_path)
        self.db.init()
        self.pid = 'supervisor-test'
        self.db.create_project(self.pid,'Supervisor','Da Nang 3 days',state={'project_id':self.pid,'resume_node':'pm'})
        self.db.replace_tasks(self.pid,build_tasks(self.pid))
        self.calls = []

    def router_factory(self, policy='all'):
        def handler(request):
            payload = json.loads(request.content)
            text = payload['messages'][1]['content']
            eligible = json.loads(re.search(r'"eligible_agents":\s*(\[[^\]]+\])',text).group(1))
            self.calls.append({'model':payload['model'],'eligible':eligible,'system':payload['messages'][0]['content']})
            if policy == 'invalid': selected = ['layout_editor'] if eligible != ['layout_editor'] else ['pm']
            elif policy == 'duplicate': selected = [eligible[0],eligible[0]]
            elif policy == 'empty': selected = []
            elif policy == 'single': selected = [eligible[-1]]
            else: selected = eligible
            if policy == 'primary_invalid' and payload['model']=='primary': selected = ['made_up_agent']
            if policy == 'revoke': self.db.set_task_status(self.pid,self.pid+':'+selected[0],'blocked')
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({
                'agents':selected,'reason':{'value':'Run legal independent work','source':'policy:eligible_agents','confidence':1}})}}]})
        @asynccontextmanager
        async def factory():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                async with NineRouterClient(self.settings,client=http) as router:
                    yield router
        return factory

    def workflow(self, policy='all'):
        return TravelBookWorkflow(self.db,settings=self.settings,artifacts_root=self.settings.artifacts_dir,
            supervisor_router_factory=self.router_factory(policy))

    async def settle(self, manager):
        task = manager.running.get(self.pid)
        if task: await task
        row = self.db.get_project(self.pid)
        self.assertNotEqual(row['status'],'failed',row['error'])
        return row

    def assert_order(self):
        events = self.db.get_events(self.pid,limit=10000)
        completed = set()
        for event in events:
            node = event['payload'].get('node')
            if event['event_type']=='node_started':
                self.assertTrue(set(DEPENDENCIES[node]).issubset(completed),(node,completed))
            if event['event_type']=='node_completed':
                self.assertNotIn(node,completed)
                completed.add(node)
            if event['event_type']=='supervisor_decision':
                self.assertTrue(set(event['payload']['selected_agents']).issubset(event['payload']['eligible_agents']))
        self.assertEqual(completed,set(DEPENDENCIES))

    async def test_model_selects_parallel_visual_batch_graph_completes_in_dependency_order(self):
        manager = WorkflowManager(self.db,self.workflow())
        manager.start(self.pid)
        row = await self.settle(manager)
        self.assertEqual(row['status'],'completed')
        self.assertEqual(len(PdfReader(row['final_pdf']).pages),11)
        self.assert_order()
        batch = next(step for step in row['state']['supervisor_trace'] if len(step['eligible_agents'])==3)
        self.assertEqual(set(batch['selected_agents']),set(VISUAL_NODES))
        self.assertTrue(all(step['mode']=='llm' for step in row['state']['supervisor_trace']))
        self.assertTrue(self.calls)
        self.assertIn('ONLY',self.calls[0]['system'])

    async def test_partial_visual_batches_wait_for_all_roles_before_reviewer(self):
        manager = WorkflowManager(self.db,self.workflow('single'))
        manager.start(self.pid)
        row = await self.settle(manager)
        self.assertEqual(row['status'],'completed')
        self.assert_order()
        trace = row['state']['supervisor_trace']
        visual = [step for step in trace if set(step['eligible_agents']).intersection(VISUAL_NODES)]
        self.assertEqual([len(step['eligible_agents']) for step in visual],[3,2,1])
        self.assertEqual([step['selected_agents'][0] for step in visual],list(reversed(VISUAL_NODES)))

    async def test_out_of_enum_and_duplicate_outputs_fall_back_without_illegal_dispatch(self):
        state = {'project_id':self.pid,'supervisor_frontier':['pm']}
        for policy in ['invalid','duplicate','empty']:
            supervisor = HybridSupervisor(self.db,self.settings,router_factory=self.router_factory(policy))
            result = await supervisor.choose(state)
            self.assertEqual(result['supervisor_selected'],['pm'])
            self.assertEqual(result['supervisor_trace'][-1]['mode'],'code_fallback')

    async def test_model_primary_invalid_uses_router_fallback_model(self):
        supervisor = HybridSupervisor(self.db,self.settings,router_factory=self.router_factory('primary_invalid'))
        result = await supervisor.choose({'project_id':self.pid,'supervisor_frontier':['pm']})
        self.assertEqual([call['model'] for call in self.calls],['primary','backup'])
        self.assertEqual(result['supervisor_trace'][-1]['model'],'backup')
        self.assertEqual(result['supervisor_trace'][-1]['mode'],'llm')

    async def test_code_eligibility_cannot_be_weakened_by_task_metadata(self):
        supervisor = HybridSupervisor(self.db,self.settings,router_factory=self.router_factory())
        with self.db.connect() as connection:
            connection.execute("UPDATE tasks SET depends_on='[]' WHERE id=?",(self.pid+':planner',))
        state = {'project_id':self.pid,'supervisor_frontier':['planner']}
        self.assertEqual(supervisor.eligible(state),[])
        with self.assertRaisesRegex(ValueError,'No eligible'):
            await supervisor.choose(state)
        self.assertEqual(self.calls,[])
        for frontier in [[],['pm','pm'],['unknown']]:
            with self.assertRaises(ValueError): supervisor.eligible({'project_id':self.pid,'supervisor_frontier':frontier})

    async def test_eligibility_rechecked_after_model_returns(self):
        supervisor = HybridSupervisor(self.db,self.settings,router_factory=self.router_factory('revoke'))
        with self.assertRaisesRegex(ValueError,'no longer eligible'):
            await supervisor.choose({'project_id':self.pid,'supervisor_frontier':['pm']})
        self.assertFalse(any(e['event_type']=='supervisor_decision' for e in self.db.get_events(self.pid)))

    async def test_automatic_repair_stays_mandatory_then_returns_to_supervisor(self):
        workflow = self.workflow()
        original = workflow._review
        def review(state,loaded):
            result = original(state,loaded)
            if not state.get('revision_count'):
                result['review'] = Review(verdict='REVISE',issues=[ReviewIssue(category='photo',severity='major',
                    message=Fact(value='Injected wrong-place photo',source='test:injected-review',confidence=1),
                    target_agent='image_curator',rule_code='LLM_PHOTO_LOCATION')])
            return result
        workflow._review = review
        manager = WorkflowManager(self.db,workflow)
        manager.start(self.pid)
        row = await self.settle(manager)
        self.assertEqual(row['status'],'completed')
        self.assertEqual(row['state']['revision_count'],1)
        events = self.db.get_events(self.pid,limit=10000)
        repairs = [e for e in events if e['event_type']=='repair_completed']
        self.assertEqual([e['payload']['agent'] for e in repairs],['image_curator'])
        self.assertEqual(row['state']['supervisor_trace'][-2]['selected_agents'],['layout_editor'])
    async def test_strict_mode_stops_when_model_cannot_select(self):
        self.settings.supervisor_fallback_enabled=False
        supervisor = HybridSupervisor(self.db,self.settings,router_factory=self.router_factory('invalid'))
        with self.assertRaises(RouterExhaustedError):
            await supervisor.choose({'project_id':self.pid,'supervisor_frontier':['pm']})
        self.assertFalse(any(e['event_type']=='node_started' for e in self.db.get_events(self.pid)))

    async def test_direct_resume_hint_cannot_skip_required_nodes(self):
        manager = WorkflowManager(self.db,self.workflow())
        manager.start(self.pid,{'project_id':self.pid,'resume_node':'reviewer'})
        task = manager.running.get(self.pid)
        if task: await task
        row = self.db.get_project(self.pid)
        self.assertEqual(row['status'],'failed')
        self.assertIn('Unmet dependencies for reviewer',row['error'])
        self.assertFalse(any(e['event_type']=='node_started' for e in self.db.get_events(self.pid)))
        self.assertEqual(self.calls,[])

    async def test_checkpoint_and_three_human_gates_preserved_with_supervisor(self):
        workflow = self.workflow()
        store = WorkspaceStore(self.db)
        store.settings(self.pid,{'approval_enabled':True,'approval_stages':['itinerary','visuals','final']})
        manager = WorkflowManager(self.db,workflow)
        manager.start(self.pid)
        await self.settle(manager)
        for stage in ['itinerary','visuals','final']:
            manager = WorkflowManager(self.db,workflow)
            pending = manager.pending_approval(self.pid)
            self.assertEqual(len(pending),1)
            self.assertEqual(pending[0].value['stage'],stage)
            before = len(self.calls)
            manager.start(self.pid)
            await self.settle(manager)
            self.assertEqual(len(self.calls),before)
            await manager.decide(self.pid,pending[0].value['approval_id'],'approve','',None)
            await self.settle(manager)
        self.assertEqual(self.db.get_project(self.pid)['status'],'completed')
        self.assert_order()

    async def test_manual_revision_reruns_planner_then_continues_supervised_graph(self):
        workflow = self.workflow('single')
        WorkspaceStore(self.db).settings(self.pid,{'approval_enabled':True,'approval_stages':['itinerary']})
        manager = WorkflowManager(self.db,workflow)
        manager.start(self.pid)
        await self.settle(manager)
        gate = manager.pending_approval(self.pid)[0]
        await manager.decide(self.pid,gate.value['approval_id'],'revise','Adjust arrival times','planner')
        await self.settle(manager)
        gate = manager.pending_approval(self.pid)[0]
        await manager.decide(self.pid,gate.value['approval_id'],'approve','',None)
        row = await self.settle(manager)
        self.assertEqual(row['status'],'completed')
        events = self.db.get_events(self.pid,limit=10000)
        self.assertEqual(sum(e['event_type']=='node_completed' and e['payload'].get('node')=='planner' for e in events),2)
        self.assertEqual(sum(e['event_type']=='node_completed' and e['payload'].get('node')=='pm' for e in events),1)

    async def test_pause_in_partial_visual_batch_then_new_manager_preserves_remaining(self):
        workflow = self.workflow('single')
        entered, release = threading.Event(), threading.Event()
        original = workflow._maps
        def maps(state,loaded):
            entered.set()
            if not release.wait(15): raise RuntimeError('Missing test release')
            return original(state,loaded)
        workflow._maps = maps
        manager = WorkflowManager(self.db,workflow)
        manager.start(self.pid)
        try:
            for _ in range(1000):
                if entered.is_set(): break
                await asyncio.sleep(.01)
            self.assertTrue(entered.is_set())
            manager.pause(self.pid)
        finally:
            release.set()
        row = await self.settle(manager)
        self.assertEqual(row['status'],'paused')
        self.assertEqual(row['state']['supervisor_remaining'],['image_curator','illustrator'])
        recovered = WorkflowManager(self.db,self.workflow('single'))
        await recovered.resume(self.pid)
        row = await self.settle(recovered)
        self.assertEqual(row['status'],'completed')
        self.assert_order()
