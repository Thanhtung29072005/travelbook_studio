"""Compute and round-trip budget.json with explicit totals and signed variance."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agents.budget import BudgetAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Budget, TripRequest
from backend.app.task_plan import build_tasks


async def check(args):
    settings = Settings()
    incoming = Budget.model_validate_json(args.input_budget.read_text(encoding="utf-8-sig"))
    days = json.loads(args.itinerary.read_text(encoding="utf-8-sig"))
    fact = lambda value: {"value": value, "source": "fixture:budget-cli-brief", "confidence": 1}
    trip = TripRequest.model_validate_json(args.trip_request.read_text(encoding="utf-8-sig")) if args.trip_request else TripRequest(
        destination=fact("Đà Nẵng"), days=fact(len(days)), budget=incoming.limit, currency=incoming.currency, travelers=fact(1))
    db = Database(settings.database_path)
    db.init()
    project_id = "budget-" + uuid4().hex[:12]
    db.create_project(project_id, "Budget code totals", "Sum provided full-line costs with Decimal", trip_request=trip)
    tasks = build_tasks(project_id)
    for task in tasks[:3]:
        task["status"] = "done"
    tasks[3]["status"] = "ready"
    db.replace_tasks(project_id, tasks)
    update = await BudgetAgent(db, settings.artifacts_dir).run({"project_id": project_id, "trip_request": trip,
        "itinerary": days, "budget": incoming})
    path = Path(update["artifacts"][0].path)
    exported = json.loads(path.read_text(encoding="utf-8"))
    validated = Budget.model_validate(exported)
    names = ("total", "sourced_total", "estimated_total", "variance", "remaining", "within_budget")
    if not all(name in exported for name in names):
        raise ValueError("Computed fields missing from budget.json")
    report = {"project_id": project_id, "schema_valid": True, "stage": update["stage"],
        "currency": validated.currency.value, "limit": str(validated.limit.value),
        **{name: exported[name]["value"] for name in names}, "warnings": [fact.value for fact in update["budget_warnings"]],
        "artifacts": [artifact.path for artifact in update["artifacts"]]}
    (settings.artifacts_dir / project_id / "budget_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if update["stage"] == "content_editor" else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    sample = ROOT / "backend/data/artifacts/danang_3days"
    parser.add_argument("--input-budget", type=Path, default=sample / "budget.json")
    parser.add_argument("--itinerary", type=Path, default=sample / "itinerary.json")
    parser.add_argument("--trip-request", type=Path)
    sys.exit(asyncio.run(check(parser.parse_args())))
