"""Offline fixture demonstration of persisted human interrupts, no HTTP server."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.manager import WorkflowManager
from backend.app.workflow import TravelBookWorkflow
from backend.app.workspace import WorkspaceStore


async def main(args):
    settings = Settings()
    db = Database(settings.database_path)
    db.init()
    workflow = TravelBookWorkflow(db, settings=settings, artifacts_root=settings.artifacts_dir)
    store = WorkspaceStore(db)
    pid = args.resume or 'manual-approval-'+uuid4().hex[:12]
    if not args.resume:
        db.create_project(pid, 'Human interrupt demo', 'Da Nang 3 days', state={'project_id':pid,'resume_node':'pm'})
        store.settings(pid, {'approval_enabled':True, 'approval_stages':['itinerary','visuals','final']})
    elif db.get_project(pid) is None:
        raise ValueError('Project not found')

    async def settle(manager):
        task = manager.running.get(pid)
        if task: await task
        row = db.get_project(pid)
        if row['status']=='failed': raise RuntimeError(row['error'])
        return row

    def fresh(): return WorkflowManager(db, workflow)
    manager = fresh()
    if args.resume:
        pending = manager.pending_approval(pid)
        if not pending: raise ValueError('No pending human interrupt')
        await manager.decide(pid, pending[0].value['approval_id'], args.decision, args.feedback, args.target_agent)
    else:
        manager.start(pid)
    row = await settle(manager)
    if not args.pause_only and not args.resume:
        for stage, target in [('itinerary','planner'),('visuals','image_curator'),('final','layout_editor')]:
            for choice in ['revise','approve']:
                manager = fresh()  # Prove each pending Interrupt is read from SQLite.
                pending = manager.pending_approval(pid)
                assert len(pending)==1 and pending[0].value['stage']==stage
                await manager.decide(pid, pending[0].value['approval_id'], choice,
                    'Demo: regenerate this output' if choice=='revise' else '', target)
                row = await settle(manager)
        assert row['status']=='completed' and Path(row['final_pdf']).read_bytes().startswith(b'%PDF')
    manager = fresh()
    report = {'project_id':pid, 'mode':'OFFLINE JSON FIXTURES; no LLM called', 'status':row['status'],
        'checkpoint':manager.checkpoint_info(pid), 'final_pdf':row['final_pdf'],
        'decisions':[{key:gate.get(key) for key in ('id','stage','status','feedback','target_agent')}
                     for gate in store.read(pid)['approvals']]}
    directory = settings.artifacts_dir/pid
    directory.mkdir(parents=True, exist_ok=True)
    (directory/'manual_approval_validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(report,ensure_ascii=True,indent=2))


if __name__=='__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--pause-only',action='store_true',help='Stop at first itinerary interrupt')
    parser.add_argument('--resume',help='Existing project ID; resolve one pending interrupt')
    parser.add_argument('--decision',choices=['approve','revise'],default='approve')
    parser.add_argument('--feedback',default='')
    parser.add_argument('--target-agent',default=None)
    asyncio.run(main(parser.parse_args()))
