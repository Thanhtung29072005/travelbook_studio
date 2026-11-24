"""Verify sequential baseline, then real Send/fan-in with three real visual agent classes."""

import argparse
import asyncio
import base64
from contextlib import asynccontextmanager
import json
from pathlib import Path
import sys
import tempfile
import threading
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from backend.app.agents.illustrator import IllustratorAgent
from backend.app.agents.image_curator import ImageCuratorAgent
from backend.app.agents.map_agent import MapAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import TripRequest
from backend.app.services.illustrations import IllustrationService
from backend.app.services.photos import PhotoService
from backend.app.services.public_api import PublicAPIClient
from backend.app.task_plan import build_tasks
from backend.app.visual_team import VisualTeam
from scripts.check_image_curator import mock_commons
from scripts.check_illustrator import mock_png


def mock_factories(db, settings, root, places, *, barrier=None, fail_role=None, empty_photos=False, probe=None):
    """Fresh per-thread HTTP clients; mocking providers, not the agent implementations."""
    places = [place.model_dump(mode="json") if hasattr(place, "model_dump") else place for place in places]
    class CheckedAgent:
        def __init__(self, role, agent):
            self.role, self.agent = role, agent

        async def run(self, state, *, managed=False):
            if barrier is not None:
                barrier.wait(timeout=10)
            await asyncio.sleep(.04)
            if self.role == fail_role:
                raise RuntimeError("Injected branch failure")
            update = await self.agent.run(state, managed=managed)
            if probe:
                probe(self.role, state["project_id"], managed)
            return update

    @asynccontextmanager
    async def curator():
        async def handler(request):
            await asyncio.sleep(.04)
            query = request.url.params.get("gsrsearch", "")
            match = next(((i, place) for i, place in enumerate(places, 1) if query.startswith(place["name"]["value"])), None)
            return httpx.Response(200, json=mock_commons(match[1], match[0]) if match and not empty_photos else {"query": {"pages": []}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            yield CheckedAgent("image_curator", ImageCuratorAgent(db, PhotoService(settings, PublicAPIClient(settings, client=http)), root))

    @asynccontextmanager
    async def illustrator():
        async def handler(request):
            await asyncio.sleep(.04)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(mock_png()).decode()}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            yield CheckedAgent("illustrator", IllustratorAgent(db, IllustrationService(settings, client=http), root))

    @asynccontextmanager
    async def maps():
        yield CheckedAgent("map_agent", MapAgent(db, root))

    return {"image_curator": curator, "illustrator": illustrator, "map_agent": maps}


async def check(args):
    if not args.mock and any(value is None for value in (args.itinerary, args.places, args.budget, args.trip_request)):
        raise ValueError("Live mode requires --itinerary, --places, --budget and --trip-request")
    sample = ROOT / "backend/data/artifacts/danang_3days"
    paths = {"itinerary": args.itinerary or sample / "itinerary.json", "places": args.places or sample / "research.json",
             "budget": args.budget or sample / "budget.json"}
    if args.weather:
        paths["weather"] = args.weather
    read = lambda role: json.loads(paths[role].read_text(encoding="utf-8-sig"))
    days, places, budget = read("itinerary"), read("places"), read("budget")
    fact = lambda value: {"value": value, "source": "fixture:visual-team-demo", "confidence": 1}
    trip = TripRequest.model_validate_json(args.trip_request.read_text(encoding="utf-8-sig")) if args.trip_request else TripRequest(
        destination=fact("Đà Nẵng"), days=fact(len(days)), budget=budget["limit"], currency=budget["currency"], travelers=fact(1))
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_image_model="mock-illustrator",
            agent_reasoning_enabled=False, photo_provider_order="wikimedia", data_cache_path=Path(temp) / "api.db", image_cache_dir=Path(temp) / "images") if args.mock else Settings()
        db = Database(settings.database_path)
        db.init()
        reports = []
        modes = ("sequential", "parallel") if args.mode == "both" else (args.mode,)
        for mode in modes:
            project_id = "visual-" + mode + "-" + uuid4().hex[:12]
            db.create_project(project_id, "Visual Team integration", "Three real agents; " + mode, trip_request=trip)
            tasks = build_tasks(project_id)
            for task in tasks[:5]:
                task["status"] = "done"
            for task in tasks[5:8]:
                task["status"] = "ready"
            db.replace_tasks(project_id, tasks)
            factories = mock_factories(db, settings, settings.artifacts_dir, places,
                barrier=threading.Barrier(3) if mode == "parallel" else None) if args.mock else None
            team = VisualTeam(db, settings, agent_factories=factories)
            result = await team.run({"project_id": project_id, "trip_request": trip, "places": places,
                "itinerary": days, "budget": budget, "map_input_paths": {key: str(value) for key, value in paths.items()},
                "fixture_mode": args.mock, "artifacts": []}, mode=mode)
            report = json.loads(Path(result["visual_report"].path).read_text(encoding="utf-8"))
            report["project_id"] = project_id
            report["manifest"] = result["visual_report"].path
            reports.append(report)
            if not result["visual_complete"]:
                break  # Baseline must work before promoting to the parallel demonstration.
        summary = {"mode": "MOCK provider HTTP; real agents/threads/StateGraph/SQLite/files" if args.mock else "LIVE provider APIs",
            "runs": [{key: report[key] for key in ("project_id", "mode", "complete", "counts", "all_three_overlap", "wall_seconds", "manifest")} for report in reports],
            "sequential_parallel_counts_equal": len(reports) == 2 and reports[0]["counts"] == reports[1]["counts"],
            "complete": len(reports) == len(modes) and all(report["complete"] for report in reports)}
        output = settings.artifacts_dir / reports[-1]["project_id"] / "visual_team_validation.json"
        output.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0 if summary["complete"] else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--mode", choices=("both", "sequential", "parallel"), default="both")
    parser.add_argument("--itinerary", type=Path)
    parser.add_argument("--places", type=Path)
    parser.add_argument("--budget", type=Path)
    parser.add_argument("--weather", type=Path)
    parser.add_argument("--trip-request", type=Path)
    sys.exit(asyncio.run(check(parser.parse_args())))
