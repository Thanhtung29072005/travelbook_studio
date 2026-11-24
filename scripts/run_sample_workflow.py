"""One-command offline StateGraph -> SQLite events -> PNGs -> travelbook.pdf."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import get_settings  # noqa: E402
from backend.app.db import Database  # noqa: E402
from backend.app.workflow import TravelBookWorkflow  # noqa: E402


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-id")
    parser.add_argument("--sample-dir", type=Path, default=ROOT / "backend/data/artifacts/danang_3days")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--database", type=Path)
    args = parser.parse_args()
    settings = get_settings()
    db = Database(args.database or settings.database_path)
    workflow = TravelBookWorkflow(db, sample_dir=args.sample_dir, artifacts_root=args.output_dir or settings.artifacts_dir, settings=settings)
    result = workflow.run(project_id=args.project_id)
    project_id = result["project_id"]
    events = db.get_events(project_id)
    nodes = [event["payload"]["node"] for event in events if event["event_type"] == "node_completed"]
    tasks = db.get_tasks(project_id)
    summary = {
        "project_id": project_id, "status": db.get_project(project_id)["status"],
        "fixture_mode": True, "production_approved": result["production_approved"],
        "nodes_completed": nodes, "node_count": len(nodes), "event_count": len(events),
        "tasks_done": sum(task["status"] == "done" for task in tasks),
        "artifact_count": len(db.get_artifacts(project_id)), "pdf": result["final_pdf"].path,
        "page_count": json.loads(Path(result["final_pdf"].path).with_suffix(".manifest.json").read_text(encoding="utf-8"))["page_count"],
    }
    if len(nodes) != 11 or summary["tasks_done"] != 11:
        raise RuntimeError("Graph did not execute every required node/task")
    (Path(summary["pdf"]).parent / "run_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
