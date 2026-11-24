"""Run the full Reviewer lifecycle with mock vision HTTP or a supplied live state."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from PIL import Image

from backend.app.agents.reviewer import ReviewerAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Review
from backend.app.services.router import NineRouterClient
from backend.app.task_plan import build_tasks


def demo_state(root, project_id):
    fact = lambda value: {"value": value, "source": "fixture:visual-review-smoke", "confidence": 1}
    root.mkdir(parents=True, exist_ok=True)
    illustrations = []
    for index, color in enumerate(("red", "teal", "orange")):
        path = root / f"synthetic_{index}.png"
        Image.new("RGB", (320, 240), color).save(path)
        if index:
            illustrations.append({"id": f"art{index}", "project_id": project_id, "agent_id": "illustrator", "kind": "illustration",
                "name": f"Synthetic test image {index}", "path": str(path), "mime_type": "image/png", "source": "fixture:visual-review-smoke", "confidence": 1})
    return {"fixture_mode": True,
        "trip_request": {"destination": fact("Da Nang"), "days": fact(1), "budget": fact("1000000"), "currency": fact("VND"),
            "travelers": fact(1), "traveler_ages": [fact(8)], "start_date": fact("2026-12-07")},
        "places": [{"id": "museum", "name": fact("Museum"), "latitude": fact(16), "longitude": fact(108), "opening_hours": fact("24/7")}],
        "itinerary": [{"day_number": 1, "date": fact("2026-12-07"), "stops": [{"place_id": "museum", "arrival": fact("09:00"),
            "departure": fact("10:00"), "distance_km": fact(0), "travel_minutes": fact(0)}]}],
        "budget": {"currency": fact("VND"), "limit": fact("1000000"), "items": [{"id": "ticket", "category": "tickets", "description": fact("Ticket"), "amount": fact("100000"), "price_kind": "estimated"}]},
        "content": [fact("Museum visit with rest breaks and an adult")],
        "photos": [{"id": "photo1", "place_id": "museum", "provider": "wikimedia", "url": fact("https://upload.wikimedia.org/wikipedia/commons/a/a0/Test.jpg"),
            "source_url": fact("https://commons.wikimedia.org/wiki/File:Test.jpg"), "author": fact("Synthetic test only"), "license": fact("CC BY-SA 4.0"),
            "license_url": fact("https://creativecommons.org/licenses/by-sa/4.0/"), "local_path": str(root / "synthetic_0.png")}],
        "illustrations": illustrations}


def mock_decision(context):
    assessments = []
    for task in context["tasks"]:
        refs = [ref for ref in task["required_image_ids"] if ref in context["evidence"]]
        if task["rule"] == "photo_location":
            refs.append("input.places.0.name")
        if task["rule"] == "age_suitability":
            refs += [ref for ref in context["evidence"] if ref.startswith("input.trip_request.traveler_ages.")]
        fails = task["task_id"] in {"photo:photo1", "style", "age:itinerary"}
        assessments.append({"task_id": task["task_id"], "status": "fail" if fails else "pass",
            "classification": "warning" if task["task_id"] == "style" else "critical",
            "observation": {"photo:photo1": "MOCK wrong-place photo: request a verified replacement.", "style": "MOCK palette mismatch: align decorative assets.",
                "age:itinerary": "MOCK activity concern for age 8: revise the itinerary."}.get(task["task_id"], "MOCK assessment only; no real visual accuracy claim."),
            "evidence_ids": refs or ["policy:coverage"]})
    return {"assessments": assessments, "rationale": "Synthetic model response for lifecycle and schema validation"}


async def check(args):
    settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_models="mock-text", nine_router_vision_models="mock-vision",
        agent_reasoning_required=True) if args.mock else Settings(agent_reasoning_required=True)
    if not args.mock and (not args.state or not settings.nine_router_vision_models.strip()):
        raise ValueError("Live mode requires --state and NINE_ROUTER_VISION_MODELS")
    project_id = "visual-review-" + uuid4().hex[:12]
    output = settings.artifacts_dir / project_id
    state = json.loads(args.state.read_text(encoding="utf-8-sig")) if args.state else demo_state(output / "inputs", project_id)
    state["project_id"] = project_id
    db = Database(settings.database_path)
    db.init()
    db.create_project(project_id, "Visual and age LLM review", "Mock fixtures" if args.mock else "Review supplied state", trip_request=state.get("trip_request"))
    tasks = build_tasks(project_id)
    for task in tasks[:8]:
        task["status"] = "done"
    db.replace_tasks(project_id, tasks)
    requests = []
    def handler(request):
        payload = json.loads(request.content)
        requests.append(payload)
        content = payload["messages"][-1]["content"]
        text = content[0]["text"] if isinstance(content, list) else content
        context, _ = json.JSONDecoder().raw_decode(text)
        data = mock_decision(context) if "tasks" in context else {"issues": [], "rationale": "Preserve existing code issues"}
        return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler) if args.mock else None, trust_env=False) as http:
        router = NineRouterClient(settings, client=http)
        agent = ReviewerAgent(db, settings, settings.artifacts_dir, router=router)
        result = await agent.run(state)
        replay = await agent.visual_review(state)
    review = Review.model_validate_json(Path(result["artifacts"][0].path).read_text(encoding="utf-8"))
    report = {"project_id": project_id, "mode": "MOCK HTTP LLM; synthetic image pixels" if args.mock else "LIVE LLM; supplied state",
        "schema_valid": True, "verdict": review.verdict, "production_approved": False,
        "issues": [{"rule": issue.rule_code, "category": issue.category, "classification": issue.classification, "target_agent": issue.target_agent} for issue in review.issues],
        "review_path": result["artifacts"][0].path, "checks_path": result["artifacts"][1].path,
        "vision_replay_cache_hit": replay.reasoning[-1].cache_hit,
        "vision_pixels_sent": sum(block["type"] == "image_url" for request in requests for message in request["messages"] if isinstance(message["content"], list) for block in message["content"]) if args.mock else None}
    if args.mock and (report["vision_pixels_sent"] != 3 or not report["vision_replay_cache_hit"]):
        raise ValueError("Mock failed to send all images or reuse vision cache")
    output.mkdir(parents=True, exist_ok=True)
    (output / "visual_review_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--state", type=Path, help="State JSON with photos, illustrations and trip_request ages")
    asyncio.run(check(parser.parse_args()))
