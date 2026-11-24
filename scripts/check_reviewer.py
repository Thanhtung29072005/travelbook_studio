"""Run deterministic review; default fixture deliberately requires revision."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agents.reviewer import ReviewerAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Review, TripRequest
from backend.app.task_plan import build_tasks


async def check(args):
    settings = Settings(agent_reasoning_enabled=False, reviewer_visual_enabled=False)
    if args.max_distance is not None:
        settings = Settings.model_validate({**settings.model_dump(), "reviewer_max_daily_distance_km": args.max_distance})
    read = lambda name, override=None: json.loads((override or args.input_dir / name).read_text(encoding="utf-8-sig"))
    days, places, budget, photos = read("itinerary.json", args.itinerary), read("research.json", args.places), read("budget.json", args.budget), read("photos.json", args.photos)
    fact = lambda value: {"value": value, "source": "fixture:reviewer-cli-brief", "confidence": 1}
    trip = TripRequest.model_validate_json(args.trip_request.read_text(encoding="utf-8-sig")) if args.trip_request else TripRequest(
        destination=fact("Đà Nẵng"), days=fact(len(days)), budget=budget["limit"], currency=budget["currency"], travelers=fact(1))
    db = Database(settings.database_path)
    db.init()
    project_id = "reviewer-" + uuid4().hex[:12]
    db.create_project(project_id, "Reviewer code rules", "Review supplied JSON; no LLM/network calls", trip_request=trip)
    tasks = build_tasks(project_id)
    for task in tasks[:8]:
        task["status"] = "done"
    tasks[8]["status"] = "ready"
    db.replace_tasks(project_id, tasks)
    update = await ReviewerAgent(db, settings, settings.artifacts_dir).run({"project_id": project_id, "trip_request": trip,
        "itinerary": days, "places": places, "budget": budget, "photos": photos})
    review = Review.model_validate_json(Path(update["artifacts"][0].path).read_text(encoding="utf-8"))
    report = {"project_id": project_id, "schema_valid": True, "verdict": review.verdict, "issue_count": len(review.issues),
        "rules": sorted({issue.rule_code for issue in review.issues}), "revision_target": update["revision_target"],
        "production_approved": update["production_approved"], "artifacts": [artifact.path for artifact in update["artifacts"]]}
    (settings.artifacts_dir / project_id / "reviewer_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if review.verdict == "PASS" else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, default=ROOT / "backend/data/artifacts/danang_3days")
    parser.add_argument("--trip-request", type=Path)
    parser.add_argument("--itinerary", type=Path)
    parser.add_argument("--places", "--research", dest="places", type=Path)
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--photos", type=Path)
    parser.add_argument("--max-distance", type=float)
    sys.exit(asyncio.run(check(parser.parse_args())))
