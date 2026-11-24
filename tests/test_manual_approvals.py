import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from pypdf import PdfReader

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.db import Database
from backend.app.workflow import TravelBookWorkflow
from backend.app.manager import WorkflowManager
from backend.app.workspace import WorkspaceStore
from backend.app.schemas import Fact, Review, ReviewIssue


class ManualApprovalTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        settings=Settings(_env_file=None,data_dir=Path(self.temp.name)/'data')
        self.app = create_app(settings,workflow=TravelBookWorkflow(Database(settings.database_path),settings=settings,artifacts_root=settings.artifacts_dir))
        self.life = self.app.router.lifespan_context(self.app)
        await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        response = await self.client.post('/api/projects', json={'prompt':'Da Nang 3 days'})
        self.pid = response.json()['id']
        self.base = '/api/projects/'+self.pid
        self.manager = self.app.state.manager
        self.db = self.app.state.db
        self.store = WorkspaceStore(self.db)
        self.store.settings(self.pid, {'approval_enabled':True, 'approval_stages':['itinerary','visuals','final']})

    async def settled(self, manager=None):
        manager = manager or self.manager
        for _ in range(3000):
            if self.pid not in manager.running: break
            await asyncio.sleep(.01)
        self.assertNotIn(self.pid, manager.running)
        row = self.db.get_project(self.pid)
        self.assertNotEqual(row['status'], 'failed', row.get('error'))
        return row

    def pending(self, stage, manager=None):
        manager = manager or self.manager
        pending = manager.pending_approval(self.pid)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0].value['stage'], stage)
        gates = [gate for gate in self.store.read(self.pid)['approvals'] if gate['status']=='pending']
        self.assertEqual(len(gates), 1)
        self.assertEqual(gates[0]['id'], pending[0].value['approval_id'])
        return gates[0]

    async def choose(self, gate, decision='approve', target=None):
        response = await self.client.post(self.base+'/approvals/'+gate['id'], json={
            'decision':decision, 'feedback':'Please adjust this output' if decision=='revise' else '',
            'target_agent':target})
        self.assertEqual(response.status_code, 200, response.text)
        return await self.settled()

    async def test_three_interrupts_survive_new_manager_and_approve_without_reruns(self):
        self.manager.start(self.pid)
        await self.settled()
        thread = self.manager.config(self.pid)['configurable']['thread_id']
        for stage in ['itinerary','visuals','final']:
            # New graph + saver instance reads persisted pending Interrupt/Photo/URL.
            manager = WorkflowManager(self.db, self.manager.workflow)
            gate = self.pending(stage, manager)
            row = self.db.get_project(self.pid)
            self.assertEqual(row['status'], 'awaiting_approval')
            self.assertFalse(row['state'].get('completed'))
            if stage == 'visuals':
                self.assertTrue(gate['content']['photos'])
                self.assertTrue(gate['content']['illustrations'])
                self.assertTrue(gate['content']['maps'])
            await manager.decide(self.pid, gate['id'], 'approve', '', None)
            await self.settled(manager)
            self.assertEqual(manager.config(self.pid)['configurable']['thread_id'], thread)
        row = self.db.get_project(self.pid)
        self.assertEqual(row['status'],'completed')
        manifest = json.loads(Path(row['final_pdf']).with_suffix('.manifest.json').read_text(encoding='utf-8'))
        self.assertEqual(len(PdfReader(row['final_pdf']).pages),manifest['page_count'])
        events = [event for event in self.db.get_events(self.pid,limit=10000) if event['event_type']=='node_completed']
        self.assertEqual(len(events),11)
        self.assertEqual(len(self.store.read(self.pid)['approvals']),3)

    async def test_approving_reviewer_with_missing_hours_goes_to_final_without_research_rerun(self):
        workflow = self.manager.workflow
        original_review = workflow._review
        def missing_hours(state, loaded):
            result = original_review(state, loaded)
            result['review'] = Review(source='test:manual-review', confidence=1, verdict='REVISE',
                summary=Fact(value='Missing opening hours', source='test:manual-review', confidence=1),
                issues=[ReviewIssue(category='research', severity='major', target_agent='researcher', rule_code='OPENING_HOURS_MISSING',
                    message=Fact(value='Opening hours unavailable', source='test:manual-review', confidence=1))])
            return result
        workflow._review = missing_hours
        manager = WorkflowManager(self.db, workflow)
        manager.start(self.pid)
        await self.settled(manager)
        gate = self.pending('itinerary', manager)
        await manager.decide(self.pid, gate['id'], 'approve', '', None)
        await self.settled(manager)
        gate = self.pending('visuals', manager)
        self.assertEqual(gate['content']['review']['verdict'], 'REVISE')
        before = len([e for e in self.db.get_events(self.pid) if e['agent']=='researcher' and e['event_type']=='node_started'])
        await manager.decide(self.pid, gate['id'], 'approve', '', None)
        row = await self.settled(manager)
        self.pending('final', manager)
        self.assertGreaterEqual(row['progress'], 80)
        self.assertEqual(row['state']['revision_count'], 0)
        after = len([e for e in self.db.get_events(self.pid) if e['agent']=='researcher' and e['event_type']=='node_started'])
        self.assertEqual(before, after)
        self.assertFalse(any(e['event_type']=='revision_requested' for e in self.db.get_events(self.pid)))

    async def test_api_revise_each_stage_then_approve_and_download_pdf(self):
        self.manager.start(self.pid)
        await self.settled()
        original = self.pending('itinerary')
        thread = self.manager.config(self.pid)['configurable']['thread_id']
        bad = await self.client.post(self.base+'/approvals/'+original['id'],json={'decision':'revise','feedback':'','target_agent':'planner'})
        self.assertEqual(bad.status_code,422)
        bad = await self.client.post(self.base+'/approvals/'+original['id'],json={'decision':'revise','feedback':'fix','target_agent':'illustrator'})
        self.assertEqual(bad.status_code,422)
        for stage, targets in [('itinerary',['planner']),('visuals',['image_curator','illustrator','map_agent']),('final',['layout_editor'])]:
            for target in targets:
                gate = self.pending(stage)
                before = len([e for e in self.db.get_events(self.pid,limit=10000) if e['event_type']=='node_completed' and e['payload'].get('node')==target])
                old_pdf = Path(gate['content']['final_pdf']['path']).read_bytes() if stage=='final' else None
                await self.choose(gate,'revise',target)
                new = self.pending(stage)
                self.assertNotEqual(new['id'],gate['id'])
                self.assertEqual(self.manager.config(self.pid)['configurable']['thread_id'],thread)
                after = len([e for e in self.db.get_events(self.pid,limit=10000) if e['event_type']=='node_completed' and e['payload'].get('node')==target])
                self.assertEqual(after,before+1)
                self.assertEqual(self.db.get_project(self.pid)['state']['feedback'][0]['source'],'user:approval:'+gate['id'])
                if old_pdf is not None:
                    self.assertEqual(Path(gate['content']['final_pdf']['path']).read_bytes(),old_pdf)
                    self.assertNotEqual(new['content']['final_pdf']['id'],gate['content']['final_pdf']['id'])
                duplicate = await self.client.post(self.base+'/approvals/'+gate['id'],json={'decision':'approve'})
                self.assertEqual(duplicate.status_code,422)
            await self.choose(self.pending(stage))
        row = self.db.get_project(self.pid)
        self.assertEqual(row['status'],'completed')
        artifact = row['state']['final_pdf']
        response = await self.client.get(self.base+'/artifacts/'+artifact['id']+'/download')
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.content.startswith(b'%PDF'))
        gates = self.store.read(self.pid)['approvals']
        self.assertEqual(sum(g['status']=='revision_requested' for g in gates),5)
        self.assertEqual(sum(g['status']=='approved' for g in gates),3)

    async def test_recovery_mirrors_committed_decision_and_deduplicates_gate(self):
        self.manager.start(self.pid)
        await self.settled()
        gate = self.pending('itinerary')
        # A retry without a decision must not bypass or duplicate the interrupt.
        self.manager.start(self.pid)
        await self.settled()
        self.assertEqual(self.pending('itinerary')['id'], gate['id'])
        original = self.manager._sync_approval
        def fail_mirror(pid, values):
            if values.get('approval_resolution'):
                raise RuntimeError('Simulated process failure after graph commit')
            return original(pid, values)
        self.manager._sync_approval = fail_mirror
        await self.manager.decide(self.pid,gate['id'],'approve','',None)
        task = self.manager.running.get(self.pid)
        if task: await task
        self.assertEqual(self.db.get_project(self.pid)['status'],'failed')
        self.assertEqual(self.store.read(self.pid)['approvals'][0]['status'],'pending')
        recovered = WorkflowManager(self.db,self.manager.workflow)
        recovered.start(self.pid)
        await self.settled(recovered)
        self.assertEqual(self.store.read(self.pid)['approvals'][0]['status'],'approved')
        self.pending('visuals',recovered)
        events = self.db.get_events(self.pid,limit=10000)
        self.assertEqual(sum(e['event_type']=='node_completed' and e['payload'].get('node')=='planner' for e in events),1)
        self.db.update_project(self.pid,status='running')
        recovered.resume_pending()
        self.assertEqual(self.db.get_project(self.pid)['status'],'awaiting_approval')
