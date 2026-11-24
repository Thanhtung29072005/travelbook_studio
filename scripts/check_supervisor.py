"""Run the hybrid supervisor on fixture workers; mock LLM by default."""
import argparse
import asyncio
from contextlib import asynccontextmanager
import json
from pathlib import Path
import re
import sys
from uuid import uuid4

import httpx

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.manager import WorkflowManager
from backend.app.services.router import NineRouterClient
from backend.app.task_plan import DEPENDENCIES
from backend.app.workflow import TravelBookWorkflow


async def main(args):
    settings = Settings(supervisor_enabled=True)
    if not args.live:
        settings = settings.model_copy(update={'nine_router_api_key':Settings(_env_file=None,nine_router_api_key='mock').nine_router_api_key,
            'nine_router_models':'mock-supervisor','llm_cache_enabled':False})
    def handler(request):
        payload = json.loads(request.content)
        text = payload['messages'][1]['content']
        ready = json.loads(re.search(r'"eligible_agents":\s*(\[[^\]]+\])',text).group(1))
        selected = [ready[-1]] if args.serial else ready
        data = {'agents':selected,'reason':{'value':'Run independent eligible tasks','source':'mock:supervisor-policy','confidence':1}}
        return httpx.Response(200,json={'choices':[{'finish_reason':'stop','message':{'content':json.dumps(data)}}]})
    @asynccontextmanager
    async def mock_factory():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            async with NineRouterClient(settings,client=http) as router: yield router
    db = Database(settings.database_path)
    db.init()
    pid = 'supervisor-'+uuid4().hex[:12]
    db.create_project(pid,'Hybrid supervisor demo','Da Nang 3 days',state={'project_id':pid,'resume_node':'pm'})
    workflow = TravelBookWorkflow(db,settings=settings,artifacts_root=settings.artifacts_dir,
        supervisor_router_factory=None if args.live else mock_factory)
    manager = WorkflowManager(db,workflow)
    manager.start(pid)
    await manager.running[pid]
    row = db.get_project(pid)
    if row['status']!='completed': raise RuntimeError(row.get('error') or row['status'])
    finished = set()
    order = []
    for event in db.get_events(pid,limit=10000):
        node = event['payload'].get('node')
        if event['event_type']=='node_started':
            assert set(DEPENDENCIES[node]).issubset(finished),f'Invalid dependency order: {node}'
        if event['event_type']=='node_completed':
            assert node not in finished,f'Duplicate execution: {node}'
            finished.add(node)
            order.append(node)
    assert finished==set(DEPENDENCIES)
    assert Path(row['final_pdf']).read_bytes().startswith(b'%PDF')
    report = {'project_id':pid,'status':row['status'],'fixture_workers':True,
        'supervisor_model':'live' if args.live else 'mock HTTP structured output',
        'order_valid':True,'completed_nodes':order,'decisions':row['state']['supervisor_trace'],'pdf':row['final_pdf']}
    target=settings.artifacts_dir/pid/'supervisor_validation.json'
    target.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',help='Use configured 9Router for supervisor decisions')
    parser.add_argument('--serial',action='store_true',help='Mock chooses one visual worker per batch')
    asyncio.run(main(parser.parse_args()))
