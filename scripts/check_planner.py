"""Schedule sourced places into days; live routing, --offline, or --mock HTTP."""

import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from backend.app.agents.planner import PlannerAgent  # noqa: E402
from backend.app.config import Settings  # noqa: E402
from backend.app.db import Database  # noqa: E402
from backend.app.schemas import DayPlan, TripRequest  # noqa: E402
from backend.app.services.public_api import PublicAPIClient  # noqa: E402
from backend.app.services.routing import RoutingService, haversine_km  # noqa: E402
from backend.app.task_plan import build_tasks  # noqa: E402


def mock_handler(request):
    coordinates = request.url.path.rsplit("/", 1)[1]
    locations = [tuple(map(float, value.split(","))) for value in coordinates.split(";")]
    distances = [[haversine_km(a[1], a[0], b[1], b[0]) * 1300 for b in locations] for a in locations]
    return httpx.Response(200, json={"code": "Ok", "distances": distances,
                                    "durations": [[distance/30000*3600 for distance in row] for row in distances]})


async def check(args):
    settings = Settings(routing_provider_order="haversine") if args.offline else Settings()
    if args.offline or args.mock:
        settings = settings.model_copy(update={"agent_reasoning_enabled": False})
    def fact(value):
        return {"value": value, "source": "input:planner-cli-demo", "confidence": 1}
    trip = TripRequest.model_validate(json.loads(args.trip_request.read_text(encoding="utf-8"))) if args.trip_request else TripRequest(
        destination=fact("Đà Nẵng"), days=fact(3), budget=fact("5000000"), currency=fact("VND"), travelers=fact(2))
    values = trip.model_dump(mode="json")
    if args.start_date:
        values.update(start_date=fact(args.start_date), end_date=None)
    if args.ages:
        ages = [int(value) for value in args.ages.split(",")]
        values.update(travelers=fact(len(ages)), traveler_ages=[fact(age) for age in ages])
    trip = TripRequest.model_validate(values)
    places = json.loads(args.research.read_text(encoding="utf-8"))
    db = Database(settings.database_path)
    db.init()
    project_id = ("planner-mock-" if args.mock else "planner-offline-" if args.offline else "planner-live-") + uuid4().hex[:12]
    db.create_project(project_id, "Planner scheduling demo", "Planner CLI from JSON research", trip_request=trip)
    tasks = build_tasks(project_id)
    tasks[0]["status"], tasks[1]["status"], tasks[2]["status"] = "done", "done", "ready"
    db.replace_tasks(project_id, tasks)
    async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler) if args.mock else None, trust_env=False) as http:
        agent = PlannerAgent(db, RoutingService(settings, PublicAPIClient(settings, client=http)), settings.artifacts_dir)
        update = await agent.run({"project_id": project_id, "trip_request": trip, "places": places})
    for day in update["itinerary"]:
        DayPlan.model_validate_json(day.model_dump_json())
    report = {"mode": "MOCK HTTP" if args.mock else "OFFLINE haversine" if args.offline else "LIVE routing with fallback",
        "project_id": project_id, "project_status": db.get_project(project_id)["status"],
        "day_count": len(update["itinerary"]), "days": [{"day_number": day.day_number,
        "stops": [{"place_id": stop.place_id, "arrival": stop.arrival.value.isoformat(),
                   "departure": stop.departure.value.isoformat(), "travel_minutes": stop.travel_minutes.value}
                  for stop in day.stops]} for day in update["itinerary"]],
        "warnings": [fact.value for fact in update["planner_warnings"]],
        "artifacts": [artifact.path for artifact in update["artifacts"]]}
    (settings.artifacts_dir / project_id / "planner_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["day_count"] == trip.days.value else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--research", type=Path, default=ROOT / "backend/data/artifacts/danang_3days/research.json")
    parser.add_argument("--trip-request", type=Path)
    parser.add_argument("--start-date", type=date.fromisoformat)
    parser.add_argument("--ages", help="Complete age list, e.g. 35,34,5")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--mock", action="store_true")
    mode.add_argument("--offline", action="store_true")
    sys.exit(asyncio.run(check(parser.parse_args())))
