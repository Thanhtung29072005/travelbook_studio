"""Create a real demo project, write an event, then read from a new Database."""

import json
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.config import get_settings  # noqa: E402
from backend.app.db import Database  # noqa: E402
from backend.app.schemas import TripRequest  # noqa: E402


def fact(value):
    return {"value": value, "source": "fixture:database-smoke", "confidence": 1.0}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    path = get_settings().database_path
    db = Database(path)
    db.init()
    project_id = "db-smoke-" + uuid4().hex
    request = TripRequest(
        destination=fact("Đà Nẵng"), days=fact(3), budget=fact("5000000"),
        currency=fact("VND"), travelers=fact(1),
    )
    created = db.create_project(
        project_id, "Đà Nẵng 3 ngày — kiểm tra SQLite", "Demo: một người lớn, ngân sách 5 triệu",
        trip_request=request, state={"project_id": project_id, "trip_request": request, "stage": "pm"},
    )
    db.add_event(project_id, "pm", "smoke_test", "Đã ghi và đọc dự án", {"mode": "database_demo"})
    reopened = Database(path)
    reread = reopened.get_project(project_id)
    if reread != created:
        raise RuntimeError("Project read-back differs from the created project")
    events = reopened.get_events(project_id)
    if len(events) != 2 or events[-1]["event_type"] != "smoke_test":
        raise RuntimeError("Event read-back failed")
    print(json.dumps({
        "database": str(path), "project_id": project_id, "title": reread["title"],
        "status": reread["status"], "created_and_read": True,
        "source_preserved": reread["trip_request"]["destination"]["source"],
        "events": len(events),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
