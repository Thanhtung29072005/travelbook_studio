"""Run a live 9Router request twice, or use --mock for an offline HTTP demo."""

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from backend.app.config import Settings  # noqa: E402
from backend.app.schemas import Fact  # noqa: E402
from backend.app.services.router import NineRouterClient, RouterError  # noqa: E402


async def check(mock: bool):
    with tempfile.TemporaryDirectory() as directory:
        calls = 0

        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(200, json={
                "choices": [{"message": {"content": json.dumps({
                    "value": "TravelBook Studio", "source": "fixture:router-smoke", "confidence": 1,
                })}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            })

        if mock:
            settings = Settings(
                _env_file=None, nine_router_api_key="offline-test-key",
                nine_router_models="mock-primary,mock-fallback",
                llm_cache_path=Path(directory) / "cache.sqlite3",
            )
        else:
            settings = Settings()
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler) if mock else None) as http:
            async with NineRouterClient(settings, client=http) as router:
                args = dict(
                    system="Return a JSON fact with value, source, confidence. Do not add other fields.",
                    prompt='Use value="TravelBook Studio", source="user:router-smoke", confidence=1.',
                    schema=Fact[str], temperature=0,
                )
                first = await router.chat_structured(**args)
                second = await router.chat_structured(**args)
        print("Mode:", "MOCK (no real LLM call)" if mock else "LIVE 9Router")
        print(f"Call 1: cache_hit={first.cache_hit}; model={first.model}")
        print(f"Call 2: cache_hit={second.cache_hit}; model={second.model}")
        print("Same cache hash:", first.cache_key == second.cache_key)
        print("Same data:", first.data == second.data)
        if mock:
            print("HTTP requests to simulated 9Router:", calls)
            if first.cache_hit or calls != 1:
                raise RuntimeError("Expected a cache miss then exactly one HTTP call")
        if not second.cache_hit or first.data != second.data:
            raise RuntimeError("Cache verification failed; check LLM_CACHE_ENABLED")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true", help="Simulate 9Router without API access")
    options = parser.parse_args()
    try:
        asyncio.run(check(options.mock))
    except RouterError as error:
        print(f"Router check failed: {error}", file=sys.stderr)
        sys.exit(1)
