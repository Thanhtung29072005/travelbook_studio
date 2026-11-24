import asyncio
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import threading
import unittest
import httpx

from langgraph.types import Send
from pypdf import PdfReader

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.manager import WorkflowManager
from backend.app.workflow import TravelBookWorkflow, VISUAL_NODES
from backend.app.checkpointer import SQLiteCheckpointer
from backend.app.checkpoint_codec import StateSerializer
from backend.app.schemas import Fact, Photo
from backend.app.main import create_app


class BlockingWorkflow(TravelBookWorkflow):
    def __init__(self,*args,blocked='planner',**kwargs):
        self.blocked,self.entered,self.release=blocked,threading.Event(),threading.Event()
        super().__init__(*args,**kwargs)
    def _hold(self,node):
        if node==self.blocked:
            self.entered.set()
            if not self.release.wait(15): raise TimeoutError('Test node was not released')
    def _planner(self,state,loaded):
        self._hold('planner')
        return super()._planner(state,loaded)
    def _photos(self,state,loaded):
        self._hold('image_curator')
        return super()._photos(state,loaded)


class PauseResumeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.settings=Settings(_env_file=None,data_dir=self.root/'data')
        self.db=Database(self.settings.database_path);self.db.init()
        self.db.create_project('pause-test','Pause test','Fixture test',state={'project_id':'pause-test','resume_node':'pm'})
        self.managers=[];self.workflows=[]
        self.addAsyncCleanup(self.cleanup)
    async def cleanup(self):
        for workflow in self.workflows:
            if isinstance(workflow,BlockingWorkflow): workflow.release.set()
        for manager in self.managers:
            for pid in list(manager.running): await manager.stop(pid)
    def manager(self,blocked=None):
        workflow=BlockingWorkflow(self.db,settings=self.settings,artifacts_root=self.settings.artifacts_dir,blocked=blocked) if blocked else TravelBookWorkflow(self.db,settings=self.settings,artifacts_root=self.settings.artifacts_dir)
        manager=WorkflowManager(self.db,workflow);manager.set_loop(asyncio.get_running_loop())
        self.managers.append(manager);self.workflows.append(workflow)
        return manager,workflow
    async def until(self,predicate):
        for _ in range(3000):
            if predicate(): return
            await asyncio.sleep(.01)
        self.fail('Timed out waiting for workflow')
    def completed(self):
        row=self.db.get_project('pause-test')
        self.assertEqual(row['status'],'completed',row['error'])
        with PdfReader(row['final_pdf']) as reader:
            self.assertEqual(len(reader.pages),11)
            self.assertIn('Đà Nẵng',reader.pages[0].extract_text())
        return row

    async def test_pause_mid_node_restart_manager_resume_native_checkpoint_to_pdf(self):
        first,workflow=self.manager('planner');first.start('pause-test')
        await self.until(workflow.entered.is_set)
        self.assertTrue(first.pause('pause-test'))
        self.assertEqual(self.db.get_project('pause-test')['status'],'pause_requested')
        self.assertTrue(self.db.get_project('pause-test')['pause_requested'])
        self.assertTrue(first.pause('pause-test'))
        workflow.release.set();await self.until(lambda:'pause-test' not in first.running)
        row=self.db.get_project('pause-test');self.assertEqual(row['status'],'paused',row['error'])
        info=first.checkpoint_info('pause-test');self.assertEqual(info['next_nodes'],['approve_planner'])
        before={item['path']:sha256(Path(item['path']).read_bytes()).hexdigest() for item in self.db.get_artifacts('pause-test')}
        # Corrupt the old manual routing hint. The graph's native checkpoint must win.
        self.db.update_project('pause-test',state={**row['state'],'resume_node':'pm'},status='pause_requested',pause_requested=True)
        second,_=self.manager();second.resume_pending()
        self.assertEqual(self.db.get_project('pause-test')['status'],'paused')
        self.assertEqual(second.checkpoint_info('pause-test')['checkpoint_id'],info['checkpoint_id'])
        second.start('pause-test');await self.until(lambda:'pause-test' not in second.running)
        final=self.completed()
        self.assertEqual(final['state'].get('execution_count',0),0)
        for path,digest in before.items(): self.assertEqual(sha256(Path(path).read_bytes()).hexdigest(),digest)
        events=self.db.get_events('pause-test')
        for node in ('pm','researcher','planner'):
            self.assertEqual(sum(item['event_type']=='node_completed' and item['payload'].get('node')==node for item in events),1)
        self.assertEqual(sum(item['event_type']=='pause_requested' for item in events),1)
        self.assertTrue(any(item['event_type']=='checkpoint_resumed' for item in events))

    async def test_parallel_send_group_finishes_before_pause_and_is_not_repeated(self):
        manager,workflow=self.manager('image_curator');manager.start('pause-test')
        await self.until(workflow.entered.is_set)
        manager.pause('pause-test')
        self.assertEqual(self.db.get_project('pause-test')['status'],'pause_requested')
        self.assertFalse(any(item['event_type']=='node_started' and item['payload'].get('node')=='reviewer' for item in self.db.get_events('pause-test')))
        workflow.release.set();await self.until(lambda:'pause-test' not in manager.running)
        self.assertEqual(self.db.get_project('pause-test')['status'],'paused',self.db.get_project('pause-test')['error'])
        self.assertEqual(manager.checkpoint_info('pause-test')['next_nodes'],['reviewer'])
        resumed,_=self.manager();resumed.start('pause-test');await self.until(lambda:'pause-test' not in resumed.running)
        self.completed()
        events=self.db.get_events('pause-test')
        for role in VISUAL_NODES:
            self.assertEqual(sum(item['event_type']=='node_completed' and item['payload'].get('node')==role for item in events),1)

    async def test_pause_requested_immediately_is_not_lost(self):
        manager,_=self.manager();manager.start('pause-test');manager.pause('pause-test')
        await self.until(lambda:'pause-test' not in manager.running)
        self.assertEqual(self.db.get_project('pause-test')['status'],'paused')
        self.assertEqual(manager.checkpoint_info('pause-test')['next_nodes'],['pm'])
        self.assertFalse(any(item['event_type']=='node_started' for item in self.db.get_events('pause-test')))
        manager.start('pause-test');await self.until(lambda:'pause-test' not in manager.running)
        self.completed()

    async def test_api_request_pending_pause_checkpoint_and_resume_to_pdf(self):
        workflow=BlockingWorkflow(self.db,settings=self.settings,artifacts_root=self.settings.artifacts_dir,blocked='planner')
        self.workflows.append(workflow)
        app=create_app(self.settings,database=self.db,workflow=workflow)
        async with app.router.lifespan_context(app),httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            manager=app.state.manager
            response=await client.post('/api/projects/pause-test/retry')
            self.assertEqual(response.status_code,200,response.text)
            await self.until(workflow.entered.is_set)
            response=await client.post('/api/projects/pause-test/pause')
            self.assertEqual(response.json()['status'],'pause_requested')
            self.assertEqual((await client.post('/api/projects/pause-test/resume')).status_code,409)
            checkpoint=(await client.get('/api/projects/pause-test/checkpoint')).json()
            self.assertTrue(checkpoint['exists'])
            workflow.release.set();await self.until(lambda:'pause-test' not in manager.running)
            self.assertEqual((await client.get('/api/projects/pause-test')).json()['status'],'paused')
            self.assertEqual((await client.get('/api/projects/pause-test/checkpoint')).json()['next_nodes'],['approve_planner'])
            self.assertEqual((await client.post('/api/projects/pause-test/resume')).status_code,200)
            await self.until(lambda:'pause-test' not in manager.running)
            self.completed()

    async def test_history_and_pending_writes_survive_new_saver_and_delete_cascades(self):
        manager,_=self.manager();manager.start('pause-test');manager.pause('pause-test')
        await self.until(lambda:'pause-test' not in manager.running)
        config=manager.config('pause-test');saver=SQLiteCheckpointer(Database(self.db.path))
        saved=saver.get_tuple(config);self.assertIsNotNone(saved)
        saver.put_writes(saved.config,[('test',{'value':1})],'task')
        saver.put_writes(saved.config,[('test',{'value':999})],'task')
        self.assertEqual(SQLiteCheckpointer(self.db).get_tuple(saved.config).pending_writes[0][2],{'value':1})
        self.assertEqual(len(list(saver.list(config,limit=1))),1)
        with self.db.connect() as connection: connection.execute("DELETE FROM projects WHERE id='pause-test'")
        self.assertIsNone(saver.get_tuple(config))
        with self.db.connect() as connection:
            self.assertEqual(connection.execute('SELECT COUNT(*) FROM graph_writes').fetchone()[0],0)

    async def test_failed_node_pending_error_is_saved_and_native_retry_completes(self):
        class BrokenPlanner(TravelBookWorkflow):
            def _planner(self,state,loaded): raise ValueError('intentional planner failure')
        broken=WorkflowManager(self.db,BrokenPlanner(self.db,settings=self.settings,artifacts_root=self.settings.artifacts_dir))
        self.managers.append(broken);broken.set_loop(asyncio.get_running_loop());broken.start('pause-test')
        await self.until(lambda:'pause-test' not in broken.running)
        row=self.db.get_project('pause-test')
        self.assertEqual(row['status'],'failed')
        self.assertIn('intentional planner failure',row['error'])
        self.assertEqual(broken.checkpoint_info('pause-test')['next_nodes'],['planner'])
        fixed,_=self.manager();fixed.start('pause-test');await self.until(lambda:'pause-test' not in fixed.running)
        self.completed()

    def test_codec_roundtrips_url_models_decimal_and_send_without_pickle(self):
        sample=Path(__file__).resolve().parents[1]/'backend/data/artifacts/danang_3days/photos.json'
        photo=Photo.model_validate(json.loads(sample.read_text(encoding='utf-8'))[0])
        from decimal import Decimal
        value={'photo':photo,'money':Fact[Decimal](value=Decimal('123.45'),source='test:codec',confidence=1),'task':Send('image_curator',{'photo':photo})}
        codec=StateSerializer();kind,blob=codec.dumps_typed(value);loaded=codec.loads_typed((kind,blob))
        self.assertEqual(loaded['photo'],photo);self.assertEqual(loaded['money'].value,Decimal('123.45'))
        self.assertEqual(loaded['task'].node,'image_curator');self.assertEqual(loaded['task'].arg['photo'],photo)
        with self.assertRaises(ValueError): codec.loads_typed(('pickle',b'anything'))
