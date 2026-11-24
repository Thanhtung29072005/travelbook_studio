"""Management acceptance tests; isolated SQLite and mocked 9Router HTTP only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.workspace import AgentInput, WorkspaceStore


class AgentModelManagementTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.settings=Settings(_env_file=None,data_dir=Path(self.temp.name)/'data',nine_router_api_key='mock-key')
        self.app=create_app(self.settings)
        self.life=self.app.router.lifespan_context(self.app);await self.life.__aenter__()
        self.addAsyncCleanup(self.life.__aexit__,None,None,None)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app),base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)
        response=await self.client.post('/api/projects',json={'prompt':'Management acceptance'})
        self.pid=response.json()['id'];self.base='/api/projects/'+self.pid
        self.store=WorkspaceStore(self.app.state.db)

    async def test_support_agent_add_enable_disable_remove_and_pm_protection(self):
        response=await self.client.post(self.base+'/agents',json={'name':'Helper','role_key':'researcher'})
        self.assertEqual(response.status_code,201,response.text)
        agent=response.json();self.assertFalse(agent['enabled'])
        url=self.base+'/agents/'+agent['id']
        self.assertEqual((await self.client.patch(url,json={'enabled':True})).status_code,200)
        self.assertEqual(self.store.agent(self.pid,'researcher')['id'],agent['id'])
        self.assertEqual((await self.client.patch(url,json={'enabled':False})).status_code,200)
        self.assertIsNone(self.store.agent(self.pid,'researcher'))
        self.assertEqual((await self.client.delete(url)).status_code,200)
        self.assertEqual((await self.client.patch(self.base+'/agents/pm',json={'enabled':False})).status_code,422)
        self.assertEqual((await self.client.delete(self.base+'/agents/pm')).status_code,422)
        self.assertEqual((await self.client.post(self.base+'/agents',json={'name':'Duplicate PM','role_key':'pm'})).status_code,422)
        self.assertTrue(self.store.agent(self.pid,'pm')['enabled'])

    async def test_core_support_role_can_be_disabled_deleted_and_replaced(self):
        url=self.base+'/agents/researcher'
        self.assertEqual((await self.client.patch(url,json={'enabled':False})).status_code,200)
        self.assertEqual((await self.client.delete(url)).status_code,200)
        response=await self.client.post(self.base+'/agents',json={'name':'Replacement','role_key':'researcher'})
        agent=response.json()
        self.assertEqual((await self.client.patch(self.base+'/agents/'+agent['id'],json={'enabled':True})).status_code,200)
        self.assertEqual(self.store.agent(self.pid,'researcher')['id'],agent['id'])

    async def test_agent_model_fields_normalized_and_survive_reload(self):
        response=await self.client.patch(self.base+'/agents/reviewer',json={'model':' text-main ',
            'fallback_models':['backup',' backup ','','last'],'image_model':' vision-main '})
        self.assertEqual(response.status_code,200,response.text)
        saved=WorkspaceStore(self.app.state.db).agent(self.pid,'reviewer')
        self.assertEqual(saved['model'],'text-main')
        self.assertEqual(saved['fallback_models'],['backup','last'])
        self.assertEqual(saved['image_model'],'vision-main')

    async def test_text_and_image_fallback_configuration_persists(self):
        body={'models':['main','fallback','main'],'image_model':'image-main','image_fallback_models':['image-backup']}
        response=await self.client.put('/api/router/models',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['models'],['main','fallback'])
        other=create_app(Settings(_env_file=None,data_dir=self.settings.data_dir))
        async with other.router.lifespan_context(other):
            self.assertEqual(other.state.settings.models,['main','fallback'])
            self.assertEqual(other.state.settings.nine_router_image_fallback_models,'image-backup')
        with self.app.state.db.connect() as connection:
            raw=connection.execute("SELECT value FROM app_settings WHERE key='router_models'").fetchone()[0]
        self.assertNotIn('mock-key',raw)
        self.assertEqual((await self.client.put('/api/router/models',json={'models':[' ']})).status_code,422)

    async def test_explicit_sync_retains_last_catalog_when_provider_fails(self):
        actual_client=httpx.AsyncClient
        def factory(status, data):
            return lambda **kwargs:actual_client(transport=httpx.MockTransport(lambda request:httpx.Response(status,json=data)))
        with patch('backend.app.model_management.httpx.AsyncClient',factory(200,{'data':[{'id':'m1'},{'id':'m2'},{'id':'m1'}]})):
            response=await self.client.post('/api/router/models/sync')
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['available_models'],['m1','m2'])
        synced=response.json()['synced_at']
        with patch('backend.app.model_management.httpx.AsyncClient',factory(401,{'error':'secret provider body'})):
            failed=await self.client.post('/api/router/models/sync')
        self.assertEqual(failed.status_code,502)
        self.assertNotIn('secret provider body',failed.text)
        saved=(await self.client.get('/api/router/models')).json()
        self.assertEqual(saved['available_models'],['m1','m2'])
        self.assertEqual(saved['synced_at'],synced)

    async def test_running_project_rejects_agent_changes(self):
        self.app.state.db.update_project(self.pid,status='running')
        self.assertEqual((await self.client.patch(self.base+'/agents/researcher',json={'enabled':False})).status_code,409)
        self.assertEqual((await self.client.delete(self.base+'/agents/researcher')).status_code,409)
