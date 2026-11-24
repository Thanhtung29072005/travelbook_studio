"""Pause a real fixture graph during Planner, then resume from SQLite to PDF."""
import argparse
import asyncio
from hashlib import sha256
import json
from pathlib import Path
import sys
import threading
from uuid import uuid4

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.manager import WorkflowManager
from backend.app.workflow import TravelBookWorkflow


class PausableDemo(TravelBookWorkflow):
    def __init__(self,*args,**kwargs):
        self.entered,self.release=threading.Event(),threading.Event()
        super().__init__(*args,**kwargs)
    def _planner(self,state,loaded):
        self.entered.set()
        if not self.release.wait(30): raise TimeoutError('Demo Planner not released')
        return super()._planner(state,loaded)


async def wait_until(predicate):
    for _ in range(6000):
        if predicate(): return
        await asyncio.sleep(.01)
    raise TimeoutError('Workflow did not reach the expected state')


async def check(args):
    settings=Settings();db=Database(settings.database_path);db.init()
    project_id=args.resume
    if not project_id:
        project_id='pause-resume-'+uuid4().hex[:12]
        db.create_project(project_id,'Pause/resume SQLite demo','Fixture workflow with a safe pause',state={'project_id':project_id,'resume_node':'pm','fixture_mode':True})
        workflow=PausableDemo(db,settings=settings,artifacts_root=settings.artifacts_dir)
        manager=WorkflowManager(db,workflow);manager.set_loop(asyncio.get_running_loop())
        manager.start(project_id)
        try:
            await wait_until(workflow.entered.is_set)
            manager.pause(project_id)
            requested=db.get_project(project_id)['status']
            assert requested=='pause_requested'
        finally:
            workflow.release.set()
        await wait_until(lambda:project_id not in manager.running)
        row=db.get_project(project_id)
        assert row['status']=='paused',row['error']
        checkpoint=manager.checkpoint_info(project_id)
        assert checkpoint['next_nodes']==['approve_planner']
        folder=settings.artifacts_dir/project_id
        report={'project_id':project_id,'mode':'OFFLINE FIXTURE; SQLite LangGraph checkpoint',
            'requested_status':requested,'paused_status':row['status'],'checkpoint_before_resume':checkpoint,
            'files_before_resume':{item['path']:sha256(Path(item['path']).read_bytes()).hexdigest() for item in db.get_artifacts(project_id)}}
        (folder/'pause_resume_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        if args.pause_only:
            print(json.dumps(report,ensure_ascii=False,indent=2));return
    if not project_id or db.get_project(project_id) is None: raise ValueError('Unknown project')
    folder=settings.artifacts_dir/project_id
    report=json.loads((folder/'pause_resume_validation.json').read_text(encoding='utf-8'))
    # A fresh graph, saver and manager read only the persisted SQLite checkpoint.
    fresh=WorkflowManager(Database(settings.database_path),TravelBookWorkflow(db,settings=settings,artifacts_root=settings.artifacts_dir))
    fresh.set_loop(asyncio.get_running_loop());fresh.resume_pending()
    assert fresh.checkpoint_info(project_id)['checkpoint_id']==report['checkpoint_before_resume']['checkpoint_id']
    fresh.start(project_id)
    await wait_until(lambda:project_id not in fresh.running)
    row=db.get_project(project_id)
    assert row['status']=='completed',row['error']
    assert Path(row['final_pdf']).read_bytes().startswith(b'%PDF-')
    for path,digest in report['files_before_resume'].items():
        assert sha256(Path(path).read_bytes()).hexdigest()==digest
    counts={}
    for event in db.get_events(project_id,limit=10000):
        if event['event_type']=='node_completed':
            key=event['payload']['node'];counts[key]=counts.get(key,0)+1
    assert len(counts)==11 and all(count==1 for count in counts.values())
    with db.connect() as connection:
        checkpoint_count=connection.execute('SELECT COUNT(*) FROM graph_checkpoints WHERE project_id=?',(project_id,)).fetchone()[0]
    report.update(final_status=row['status'],final_pdf=row['final_pdf'],completed_node_counts=counts,checkpoint_count=checkpoint_count,
        checkpoint_after_resume=fresh.checkpoint_info(project_id),earlier_files_unchanged=True,production_approved=False)
    (folder/'pause_resume_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__':
    if hasattr(sys.stdout,'reconfigure'):sys.stdout.reconfigure(encoding='utf-8')
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pause-only',action='store_true')
    parser.add_argument('--resume',help='Resume a project produced by --pause-only, including from another process')
    asyncio.run(check(parser.parse_args()))
