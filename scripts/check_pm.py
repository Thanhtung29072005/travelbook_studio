"""Run the real PM implementation with live 9Router, or an explicit --mock HTTP response."""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend.app.agents.pm import PMAgent  # noqa: E402
from backend.app.config import Settings  # noqa: E402
from backend.app.db import Database  # noqa: E402
from backend.app.schemas import TripRequest  # noqa: E402
from backend.app.services.router import NineRouterClient, RouterError  # noqa: E402

DEFAULT_PROMPT = "Tôi muốn đi Đà Nẵng 3 ngày với 2 người lớn, tổng ngân sách 5 triệu đồng, thích biển và ăn hải sản, khởi hành 26/11/2026."


def mock_draft():
    def field(value, evidence):
        return {"value": value, "evidence": evidence, "confidence": 0.95}
    return {"destination": field("Đà Nẵng", "Đà Nẵng"), "days": field(3, "3 ngày"),
            "budget": field("5000000", "5 triệu đồng"), "currency": field("VND", "đồng"),
            "travelers": field(2, "2 người lớn"), "start_date": field("2026-11-26", "26/11/2026"), "budget_scope": "total",
            "interests": [field("biển", "biển"), field("ăn hải sản", "ăn hải sản")]}


async def check(mock, prompt):
    if mock and prompt != DEFAULT_PROMPT:
        raise ValueError("--mock only demonstrates the default prompt; use live mode for arbitrary text")
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(_env_file=None, nine_router_api_key="mock-only-key", nine_router_models="mock-extractor",
                            llm_cache_path=Path(temp) / "cache.sqlite3") if mock else Settings()
        def handler(request):
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(mock_draft(), ensure_ascii=False)}}],
                "usage": {"prompt_tokens": 100, "completion_tokens": 80}})
        db = Database(settings.database_path)
        db.init()
        project_id = ("pm-mock-" if mock else "pm-live-") + uuid4().hex[:12]
        db.create_project(project_id, "PM chuẩn hóa yêu cầu", prompt)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler) if mock else None, trust_env=False) as http:
            router = NineRouterClient(settings, client=http)
            agent = PMAgent(db, router, settings.artifacts_dir)
            state = await agent.run({"project_id": project_id, "user_prompt": prompt})
        request = state["trip_request"]
        if request is not None:
            TripRequest.model_validate(request.model_dump(mode="json"))
        report = {"mode": "MOCK HTTP - no real LLM call" if mock else "LIVE 9Router",
                  "project_id": project_id, "project_status": db.get_project(project_id)["status"],
                  "model": state["model_usage"][-1]["model"],
                  "cache_hit": state["model_usage"][-1]["cache_hit"],
                  "trip_schema_valid": request is not None,
                  "trip_request": request.model_dump(mode="json") if request is not None else None,
                  "questions": [item.value for item in state["clarification_questions"]],
                  "task_count": len(db.get_tasks(project_id)),
                  "artifacts": [artifact.path for artifact in state["artifacts"]]}
        output = settings.artifacts_dir / project_id / "pm_validation.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", default=DEFAULT_PROMPT)
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()
    try:
        asyncio.run(check(args.mock, args.prompt))
    except (RouterError, ValueError) as error:
        print(f"PM check failed: {error}", file=sys.stderr)
        sys.exit(1)
