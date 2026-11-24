import asyncio
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.db import Database
from backend.app.workflow import TravelBookWorkflow
from backend.app.workspace import WorkspaceStore
from backend.app.agents.base import BaseAgent
from backend.app.services.router import NineRouterClient
from backend.app.schemas import Fact


class WorkspaceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.settings = Settings(_env_file=None, data_dir=Path(self.temp.name)/"data", nine_router_api_key="mock", nine_router_models="default", llm_cache_path=Path(self.temp.name)/"cache.db")
        self.app = create_app(self.settings,workflow=TravelBookWorkflow(Database(self.settings.database_path),settings=self.settings,artifacts_root=self.settings.artifacts_dir))
        self.life = self.app.router.lifespan_context(self.app); await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__, None, None, None)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.db = self.app.state.db
        response = await self.client.post('/api/projects',json={'prompt':'Da Nang 3 days','start_workflow':False})
        self.pid=response.json()['id'];self.base='/api/projects/'+self.pid;self.store=WorkspaceStore(self.db)

    async def test_crew_persist_activate_edit_and_remove_without_losing_core(self):
        crew=(await self.client.get(self.base+'/agents')).json();self.assertEqual(len(crew),10)
        value=(await self.client.post(self.base+'/agents',json={'name':'Custom Research','role_key':'researcher','instructions':'Find museums','model':'custom','fallback_models':['backup']})).json()
        self.assertFalse(value['enabled'])
        result=await self.client.patch(self.base+'/agents/'+value['id'],json={'enabled':True,'temperature':.4})
        self.assertEqual(result.status_code,200,result.text)
        active=[item for item in self.store.crew(self.pid) if item['enabled'] and item['role_key']=='researcher']
        self.assertEqual([item['id'] for item in active],[value['id']])
        self.assertEqual((await self.client.delete(self.base+'/agents/pm')).status_code,422)
        self.assertEqual((await self.client.delete(self.base+'/agents/'+value['id'])).status_code,200)
        self.assertTrue(self.store.agent(self.pid,'researcher')['is_core'])

    async def test_settings_and_running_mutation_guards(self):
        result=await self.client.patch(self.base+'/settings',json={'approval_enabled':True,'approval_stages':['plan','final']})
        self.assertEqual(result.status_code,200)
        detail=(await self.client.get(self.base)).json();self.assertTrue(detail['approval_enabled'])
        self.db.update_project(self.pid,status='running')
        result=await self.client.patch(self.base+'/agents/pm',json={'name':'No'})
        self.assertEqual(result.status_code,409)
        self.assertEqual((await self.client.patch(self.base+'/settings',json={'approval_enabled':True,'approval_stages':['script']})).status_code,422)

    async def test_global_router_settings_persist_without_saving_api_keys(self):
        response=await self.client.put('/api/router/models',json={'models':['primary','backup'],'image_model':''})
        self.assertEqual(response.status_code,200,response.text)
        with self.db.connect() as connection:
            raw=connection.execute("SELECT value FROM app_settings WHERE key='router_models'").fetchone()[0]
        self.assertNotIn('api_key',raw)
        other=create_app(Settings(_env_file=None,data_dir=self.settings.data_dir))
        async with other.router.lifespan_context(other):
            self.assertEqual(other.state.settings.models,['primary','backup'])

    async def test_event_content_edit_is_validated_and_persistent(self):
        content=[{'value':'Original tip','source':'user:test','confidence':1}]
        row=self.db.get_project(self.pid);self.db.update_project(self.pid,state={**row['state'],'content':content},status='paused')
        eid=self.db.add_event(self.pid,'content_editor','node_completed','Done',{'node':'content_editor','artifact_ids':[]})
        url=self.base+f'/events/{eid}/content'
        response=await self.client.get(url);self.assertTrue(response.json()['editable'])
        self.assertEqual((await self.client.put(url,json={'content':{'content':[{'value':'missing evidence'}]}})).status_code,422)
        content[0]['value']='Updated tip'
        self.assertEqual((await self.client.put(url,json={'content':{'content':content}})).status_code,200)
        self.assertEqual(self.db.get_project(self.pid)['state']['content'][0]['value'],'Updated tip')
        self.assertIn('content_editor',self.store.read(self.pid)['overrides'])
        self.assertEqual((await self.client.get(self.base+'/events/999999/content')).status_code,404)

    async def test_orphan_approval_cannot_bypass_checkpoint(self):
        self.store.settings(self.pid,{'approval_enabled':True,'approval_stages':['plan']})
        gate=self.store.gate(self.pid,'pm',{})
        self.db.update_project(self.pid,status='awaiting_approval')
        url=self.base+'/approvals/'+gate['id']
        self.assertEqual((await self.client.post(url,json={'decision':'revise'})).status_code,422)
        response=await self.client.post(url,json={'decision':'approve'})
        self.assertEqual(response.status_code,422,response.text)
        self.assertEqual(self.store.read(self.pid)['approvals'][0]['status'],'pending')
        self.assertNotIn(self.pid,self.app.state.manager.running)

    async def test_role_model_instructions_reach_actual_router_request(self):
        self.store.update_agent(self.pid,'researcher',{'model':'custom','fallback_models':['backup'],'instructions':'Prefer museums','temperature':.4})
        payloads=[]
        def handler(request):
            payloads.append(json.loads(request.content))
            return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'value':'Museum','source':'fixture:test','confidence':.5})}}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            agent=BaseAgent(self.db,None,self.settings.artifacts_dir);agent.agent_id='researcher'
            await NineRouterClient(self.settings,client=http).chat_structured(system='Keep schema',prompt='Choose',schema=Fact[str],on_attempt=agent.model_attempt(self.pid))
        self.assertEqual(payloads[0]['model'],'custom');self.assertEqual(payloads[0]['temperature'],.4)
        self.assertIn('Prefer museums',payloads[0]['messages'][0]['content'])

    async def test_real_graph_approval_then_resume_does_not_reexecute_pm(self):
        self.store.settings(self.pid,{'approval_enabled':True,'approval_stages':['plan']})
        manager=self.app.state.manager;manager.start(self.pid)
        for _ in range(3000):
            await asyncio.sleep(.01)
            if self.pid not in manager.running: break
        row=self.db.get_project(self.pid)
        self.assertEqual(row['status'],'awaiting_approval',row.get('error'))
        self.assertEqual(row['state']['resume_node'],'approve_pm')
        gate=self.store.read(self.pid)['approvals'][0]
        response=await self.client.post(self.base+'/approvals/'+gate['id'],json={'decision':'approve'})
        self.assertEqual(response.status_code,200,response.text)
        for _ in range(3000):
            await asyncio.sleep(.01)
            if self.pid not in manager.running: break
        row=self.db.get_project(self.pid)
        self.assertEqual(row['status'],'completed',row.get('error'))
        self.assertTrue(Path(row['final_pdf']).is_file())
        events=self.db.get_events(self.pid)
        self.assertEqual(sum(e['event_type']=='node_completed' and e['payload'].get('node')=='pm' for e in events),1)
        manager.start(self.pid,{**row['state'],'resume_node':'image_curator'})
        for _ in range(3000):
            await asyncio.sleep(.01)
            if self.pid not in manager.running: break
        row=self.db.get_project(self.pid)
        self.assertEqual(row['status'],'completed',row.get('error'))

    async def test_cooperative_stop_leaves_no_worker_writing_and_retry_resumes(self):
        manager=self.app.state.manager
        manager.start(self.pid)
        await manager.stop(self.pid)
        row=self.db.get_project(self.pid)
        self.assertEqual(row['status'],'stopped')
        self.assertNotIn(self.pid,manager.running)
        self.assertIn(row['state']['resume_node'],('pm','approve_pm','researcher'))
        manager.start(self.pid,row['state'])
        for _ in range(3000):
            await asyncio.sleep(.01)
            if self.pid not in manager.running: break
        self.assertEqual(self.db.get_project(self.pid)['status'],'completed',self.db.get_project(self.pid)['error'])

    async def test_reviewer_code_findings_are_read_only(self):
        row=self.db.get_project(self.pid)
        self.db.update_project(self.pid,state={**row['state'],'review':{'verdict':'REVISE'}},status='paused')
        eid=self.db.add_event(self.pid,'reviewer','node_completed','Reviewed',{'node':'reviewer'})
        response=await self.client.get(self.base+f'/events/{eid}/content')
        self.assertFalse(response.json()['editable'])
        self.assertEqual((await self.client.put(self.base+f'/events/{eid}/content',json={'content':{'review':{'verdict':'PASS'}}})).status_code,422)
