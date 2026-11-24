"""Generate content.json through 9Router, or explicitly mocked HTTP."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from pydantic import TypeAdapter

from backend.app.agents.content_editor import ContentEditorAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import ContentDocument, Fact, Text, TripRequest
from backend.app.services.router import NineRouterClient
from backend.app.task_plan import build_tasks


def mock_draft(context):
    def text(value, refs=(), suggestion=False):
        return {"text": value, "evidence_ids": list(refs), "suggestion": suggestion}
    trip, days = context["trip"], context["itinerary"]
    return {"introduction": text(f"Cùng khám phá {trip['destination']['value']} trong {trip['days']['value']} ngày.",
                                ["input.trip.destination", "input.trip.days"]),
        "days": [{"day_number": day["day_number"], "introduction": text("Hôm nay, lần lượt ghé " +
            ", ".join(context["places"][stop["place_id"]]["name"]["value"] for stop in day["stops"]) + ".",
            [f"input.places.{stop['place_id']}.name" for stop in day["stops"]][:12])} for day in days],
        "tips": [text("Hãy kiểm tra lại giờ mở cửa trước khi khởi hành.", suggestion=True)],
        "food": [text("Có thể cân nhắc mì Quảng; chưa xác minh quán, giá và thành phần, hãy hỏi trước khi gọi món.", suggestion=True)],
        "culture": [text("Nên xem nội quy tại điểm đến và hỏi trước khi chụp ảnh người khác.", suggestion=True)]}


async def check(args):
    read = lambda name: json.loads((args.input_dir / name).read_text(encoding="utf-8"))
    itinerary, places, budget = read("itinerary.json"), read("research.json"), read("budget.json")
    fact = lambda value: {"value": value, "source": "input:content-cli-demo", "confidence": 1}
    trip = TripRequest.model_validate(json.loads(args.trip_request.read_text(encoding="utf-8"))) if args.trip_request else TripRequest(
        destination=fact("Đà Nẵng"), days=fact(len(itinerary)), budget=budget["limit"], currency=budget["currency"], travelers=fact(1))
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_models="mock-content",
            llm_cache_path=Path(temp) / "cache.db") if args.mock else Settings()
        calls = []
        def handler(request):
            calls.append(1)
            payload = json.loads(request.content)
            user = next(message["content"] for message in payload["messages"] if message["role"] == "user")
            data, _ = json.JSONDecoder().raw_decode(user)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(mock_draft(data["context"]), ensure_ascii=False)}}]})
        db = Database(settings.database_path)
        db.init()
        project_id = ("content-mock-" if args.mock else "content-live-") + uuid4().hex[:12]
        db.create_project(project_id, "Content Editor demo", "Biên tập từ JSON", trip_request=trip)
        tasks = build_tasks(project_id)
        # CLI seeds upstream demo tasks only; does not execute real Budget/Planner.
        for task in tasks[:4]:
            task["status"] = "done"
        tasks[4]["status"] = "ready"
        db.replace_tasks(project_id, tasks)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler) if args.mock else None, trust_env=False) as http:
            router = NineRouterClient(settings, client=http)
            agent = ContentEditorAgent(db, router, settings.artifacts_dir)
            state = {"project_id": project_id, "trip_request": trip, "itinerary": itinerary, "places": places, "budget": budget}
            update = await agent.run(state)
            again = await agent.edit(trip, itinerary, places, budget=budget)
        content = Path(update["artifacts"][0].path)
        facts = TypeAdapter(list[Fact[Text]]).validate_json(content.read_text(encoding="utf-8"))
        sections = ContentDocument.model_validate_json(Path(update["artifacts"][1].path).read_text(encoding="utf-8"))
        report = {"mode": "MOCK HTTP, not real LLM" if args.mock else "LIVE 9Router; fixture inputs unverified",
            "project_id": project_id, "schema_valid": True, "day_count": len(sections.days), "fact_count": len(facts),
            "model": again.model, "second_call_cache_hit": again.cache_hit,
            "mock_http_calls": len(calls) if args.mock else None, "artifacts": [item.path for item in update["artifacts"]]}
        output = settings.artifacts_dir / project_id / "content_validation.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "backend/data/artifacts/danang_3days")
    parser.add_argument("--trip-request", type=Path)
    asyncio.run(check(parser.parse_args()))
