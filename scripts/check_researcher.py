"""Run Researcher with public APIs, or --mock for deterministic HTTP testing."""

import argparse
import asyncio
from datetime import date
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402
from backend.app.agents.researcher import ResearcherAgent  # noqa: E402
from backend.app.config import Settings  # noqa: E402
from backend.app.db import Database  # noqa: E402
from backend.app.schemas import TripRequest  # noqa: E402
from backend.app.services.places import PlacesService  # noqa: E402
from backend.app.services.public_api import PublicAPIClient  # noqa: E402
from backend.app.services.weather import WeatherService  # noqa: E402
from backend.app.task_plan import build_tasks  # noqa: E402


def mock_handler(request):
    if request.url.host == "nominatim.openstreetmap.org":
        body = [{"osm_type": "relation", "osm_id": 1, "name": "Đà Nẵng", "lat": "16.05", "lon": "108.2",
                 "display_name": "Đà Nẵng, Việt Nam"}]
    elif request.url.host == "overpass-api.de":
        body = {"elements": [{"type": "node", "id": 10, "lat": 16.054, "lon": 108.247,
                 "tags": {"name": "Bãi biển mẫu (MOCK)", "natural": "beach", "wikidata": "Q10"}},
                {"type": "way", "id": 11, "center": {"lat": 16.06, "lon": 108.22},
                 "tags": {"name": "Bảo tàng mẫu (MOCK)", "tourism": "museum", "charge": "40000 VND", "opening_hours": "Tu-Su 08:00-17:00"}}]}
    elif request.url.host == "www.wikidata.org":
        body = {"entities": {"Q10": {"id": "Q10", "descriptions": {"vi": {"value": "Mô tả giả lập để kiểm thử."}}}}}
    else:
        start = date.fromisoformat(request.url.params["start_date"])
        from datetime import timedelta
        end = date.fromisoformat(request.url.params["end_date"])
        dates = [(start + timedelta(days=index)).isoformat() for index in range((end-start).days + 1)]
        body = {"latitude": 16.05, "longitude": 108.2, "timezone": "Asia/Ho_Chi_Minh",
                "daily_units": {"temperature_2m_min": "°C", "temperature_2m_max": "°C", "precipitation_probability_max": "%", "precipitation_sum": "mm"},
                "daily": {"time": dates, "temperature_2m_min": [24] * len(dates), "temperature_2m_max": [29] * len(dates),
                          "precipitation_probability_max": [0] * len(dates), "precipitation_sum": [0] * len(dates)}}
    return httpx.Response(200, json=body)


async def check(args):
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(agent_reasoning_enabled=False, data_cache_path=Path(temp) / "cache.sqlite3") if args.mock else Settings()
        def fact(value):
            return {"value": value, "source": "input:researcher-cli-demo", "confidence": 1}
        request = TripRequest.model_validate(json.loads(args.trip_request.read_text(encoding="utf-8"))) if args.trip_request else TripRequest(
            destination=fact(args.destination), days=fact(args.days), budget=fact("5000000"), currency=fact("VND"),
            travelers=fact(2), interests=[fact("biển")])
        if args.start_date:
            values = request.model_dump(mode="json")
            values["start_date"] = fact(args.start_date)
            values["end_date"] = None
            request = TripRequest.model_validate(values)
        db = Database(settings.database_path)
        db.init()
        project_id = ("research-mock-" if args.mock else "research-live-") + uuid4().hex[:12]
        db.create_project(project_id, "Researcher public data", "Researcher CLI demo", trip_request=request)
        tasks = build_tasks(project_id)
        tasks[0]["status"], tasks[1]["status"] = "done", "ready"
        db.replace_tasks(project_id, tasks)
        async with httpx.AsyncClient(transport=httpx.MockTransport(mock_handler) if args.mock else None,
                                     trust_env=False, follow_redirects=True) as http:
            api = PublicAPIClient(settings, client=http)
            agent = ResearcherAgent(db, PlacesService(settings, api), WeatherService(settings, api), settings.artifacts_dir)
            result = await agent.run({"project_id": project_id, "trip_request": request})
        report = {"mode": "MOCK HTTP" if args.mock else "LIVE public APIs", "project_id": project_id,
                  "project_status": db.get_project(project_id)["status"], "place_count": len(result["places"]),
                  "place_names": [place.name.value for place in result["places"]],
                  "weather_status": result["weather"].status, "forecast_days": len(result["weather"].daily),
                  "warnings": [warning.value for warning in result["research_warnings"]],
                  "artifacts": [artifact.path for artifact in result["artifacts"]]}
        (settings.artifacts_dir / project_id / "researcher_validation.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if result["places"] else 2


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trip-request", type=Path)
    parser.add_argument("--destination", default="Đà Nẵng")
    parser.add_argument("--days", type=int, default=3)
    parser.add_argument("--start-date", help="YYYY-MM-DD, optional; no date means no invented forecast")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    sys.exit(asyncio.run(check(args)))
