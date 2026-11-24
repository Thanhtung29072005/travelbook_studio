"""Create a real-agent project using the same manager as the UI (no fixtures)."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.live_workflow import LiveTravelBookWorkflow
from backend.app.manager import WorkflowManager


async def main(prompt):
    settings=Settings();db=Database(settings.database_path);db.init()
    pid='live-'+uuid4().hex[:12]
    db.create_project(pid,'Live travel guide',prompt,state={'project_id':pid,'user_prompt':prompt,'fixture_mode':False,'resume_node':'pm'})
    workflow=LiveTravelBookWorkflow(db,settings=settings,artifacts_root=settings.artifacts_dir)
    manager=WorkflowManager(db,workflow);manager.start(pid)
    await manager.running[pid]
    row=db.get_project(pid)
    print(json.dumps({'id':pid,'status':row['status'],'error':row['error'],'pdf':row['final_pdf'],
        'questions':row['state'].get('clarification_questions',[]),'fixture_mode':row['state'].get('fixture_mode')},ensure_ascii=True,indent=2))
    if row['status']=='failed': raise SystemExit(1)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prompt',required=True)
    asyncio.run(main(parser.parse_args().prompt))
