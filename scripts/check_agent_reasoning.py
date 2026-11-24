"""Exercise four upgraded agents; tool APIs mocked, LLM mocked or live 9Router."""

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

from backend.app.agents.researcher import ResearcherAgent
from backend.app.agents.planner import PlannerAgent
from backend.app.agents.image_curator import ImageCuratorAgent
from backend.app.agents.reviewer import ReviewerAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Budget, Fact, TripRequest
from backend.app.services.places import PlacesService
from backend.app.services.weather import WeatherService
from backend.app.services.photos import PhotoService
from backend.app.services.routing import RoutingService
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.router import NineRouterClient
from scripts.check_researcher import mock_handler as research_http
from scripts.check_image_curator import mock_commons


def decision_payload(context):
    if "remaining_search_calls" in context:
        return {"action": "finish", "priority_place_ids": [place["id"] for place in context["places"]],
                "missing_information": ["Giờ mở cửa cần được xác minh."], "rationale": "Giữ dữ liệu công cụ, ghi phần còn thiếu."}
    if "tools" in context:
        return {"search_interests": ["biển", "bảo tàng"], "include_weather": False, "rationale": "Tìm địa điểm theo sở thích; chưa có ngày để dự báo."}
    if "eligible_places" in context:
        groups = [[] for _ in range(context["requested_days"])]
        for index, place in enumerate(context["eligible_places"]):
            groups[index % len(groups)].append(place["id"])
        return {"day_groups": groups, "rationale": "Nhóm các ID đã có, để code kiểm tra thời gian và độ tuổi."}
    if "available_providers" in context:
        return {"provider_priority": context["available_providers"], "rationale": "Ưu tiên provider có sẵn trong cấu hình."}
    if "candidates" in context:
        return {"photo_id": context["candidates"][0]["photo_id"], "rationale": "Metadata và tỷ lệ ảnh phù hợp; không xác nhận pixels."}
    if "code_verdict" in context:
        return {"issues": [], "rationale": "Giữ nguyên các lỗi do code phát hiện, không tự phê duyệt dữ liệu mock."}
    raise ValueError("Unexpected reasoning context")


async def check(args):
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_models="mock-reasoner",
            llm_cache_path=Path(temp) / "llm.db", data_cache_path=Path(temp) / "api.db",
            photo_provider_order="wikimedia", routing_provider_order="haversine", agent_reasoning_required=True) if args.mock else Settings(
                data_cache_path=Path(temp) / "api.db", photo_provider_order="wikimedia", routing_provider_order="haversine", agent_reasoning_required=True)
        llm_calls, tool_calls, current_places = [], [], []
        def llm_handler(request):
            payload = json.loads(request.content)
            llm_calls.append(payload["model"])
            user = next(message["content"] for message in payload["messages"] if message["role"] == "user")
            context, _ = json.JSONDecoder().raw_decode(user)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(decision_payload(context), ensure_ascii=False)}}]})
        def tools_handler(request):
            tool_calls.append(request.url.host)
            if request.url.host == "commons.wikimedia.org":
                query = request.url.params.get("gsrsearch", "")
                match = next(((index, place) for index, place in enumerate(current_places, 1) if query.startswith(place["name"]["value"])), None)
                return httpx.Response(200, json=mock_commons(match[1], match[0]) if match else {"query": {"pages": []}})
            return research_http(request)
        fact = lambda value: {"value": value, "source": "fixture:agent-reasoning-smoke", "confidence": 1}
        trip = TripRequest(destination=fact("Đà Nẵng"), days=fact(1), budget=fact("5000000"), currency=fact("VND"), travelers=fact(1), interests=[fact("biển")])
        db = Database(settings.database_path)
        db.init()
        project_id = ("reasoning-mock-" if args.mock else "reasoning-live-llm-") + uuid4().hex[:12]
        db.create_project(project_id, "Upgraded agent reasoning", "All tool data MOCK; LLM decision validation", trip_request=trip)
        async with httpx.AsyncClient(transport=httpx.MockTransport(llm_handler) if args.mock else None, trust_env=False) as model_http, httpx.AsyncClient(transport=httpx.MockTransport(tools_handler)) as data_http:
            router = NineRouterClient(settings, client=model_http)
            api = PublicAPIClient(settings, client=data_http)
            researcher = ResearcherAgent(db, PlacesService(settings, api), WeatherService(settings, api), settings.artifacts_dir, router=router)
            research = await researcher.research(trip, project_id=project_id, on_attempt=researcher.model_attempt(project_id))
            current_places.extend(place.model_dump(mode="json") for place in research.places)
            planner = PlannerAgent(db, RoutingService(settings, api), settings.artifacts_dir, router=router)
            plan = await planner.plan(trip, research.places, project_id=project_id, on_attempt=planner.model_attempt(project_id))
            curator = ImageCuratorAgent(db, PhotoService(settings, api), settings.artifacts_dir, router=router)
            photos = await curator.curate(research.places, trip.destination.value, project_id=project_id, on_attempt=curator.model_attempt(project_id))
            budget = Budget(currency=trip.currency, limit=trip.budget, items=[])
            reviewer = ReviewerAgent(db, settings, settings.artifacts_dir, router=router)
            review = await reviewer.qualitative({"trip_request": trip, "places": research.places, "itinerary": plan.itinerary,
                "budget": budget, "photos": photos.photos, "fixture_mode": True}, project_id=project_id, on_attempt=reviewer.model_attempt(project_id))
            # Exact replay demonstrates that the decision layer also uses existing router cache.
            replay = await planner.plan(trip, research.places)
        run_id = uuid4().hex[:16]
        artifacts = []
        for agent, filename, result in ((researcher, "research_reasoning.json", research), (planner, "planner_reasoning.json", plan),
                                       (curator, "curator_reasoning.json", photos), (reviewer, "reviewer_reasoning.json", review)):
            artifacts.append(agent.save_json(project_id, run_id, filename, result, kind="review", source="execution:reasoning-smoke; tool-data=MOCK", confidence=1))
        records = research.reasoning + plan.reasoning + photos.reasoning + review.reasoning
        counts = {role: sum(record.role == role and record.status == "llm" for record in records) for role in ("researcher", "planner", "image_curator", "reviewer")}
        if not all(counts.values()) or not all(record.status == "llm" for record in records):
            raise ValueError("Smoke did not execute all required LLM decisions")
        if not replay.reasoning[-1].cache_hit:
            raise ValueError("Identical Planner decision did not use the cache")
        report = {"project_id": project_id, "mode": "MOCK LLM and tool APIs" if args.mock else "LIVE 9Router LLM; all tool API data MOCK",
            "llm_decisions_by_role": counts, "llm_http_calls": len(llm_calls) if args.mock else None, "tool_http_calls": tool_calls,
            "planner_replay_cache_hit": replay.reasoning[-1].cache_hit, "place_count": len(research.places), "day_count": len(plan.itinerary),
            "photo_count": len(photos.photos), "review_verdict": review.review.verdict, "production_approved": False,
            "artifacts": [artifact.path for artifact in artifacts]}
        output = settings.artifacts_dir / project_id / "agent_reasoning_validation.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        db.update_project(project_id, status="needs_attention", stage="needs_attention")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true")
    asyncio.run(check(parser.parse_args()))
