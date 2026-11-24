"""HTTP-boundary tests: real LangChain pipeline and SQLite, simulated 9Router."""

import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import httpx
from pydantic import ValidationError

from backend.app.config import Settings
from backend.app.schemas import Budget, Fact
from backend.app.services.router import NineRouterClient, RouterError, RouterExhaustedError


class RouterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.settings = Settings(
            _env_file=None, nine_router_api_key="test-only-secret",
            nine_router_models="primary,fallback", llm_retry_delay_seconds=0,
            llm_cache_path=Path(self.directory.name) / "cache.sqlite3",
        )
        self.kwargs = dict(system="Return a sourced fact.", prompt="Name this project.", schema=Fact[str])

    @staticmethod
    def response(data=None):
        if data is None:
            data = {"value": "TravelBook Studio", "source": "fixture:test", "confidence": 1}
        return httpx.Response(200, json={
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5},
        })

    async def test_second_prompt_uses_cache_and_persists_after_new_client(self):
        calls = []

        def handler(request):
            calls.append(json.loads(request.content))
            self.assertEqual(request.url.path, "/v1/chat/completions")
            self.assertEqual(request.headers["Authorization"], "Bearer test-only-secret")
            self.assertEqual(calls[-1]["response_format"], {"type": "json_object"})
            return self.response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            router = NineRouterClient(self.settings, client=http)
            first = await router.chat_structured(**self.kwargs)
            second = await router.chat_structured(**self.kwargs)
            third = await NineRouterClient(self.settings, client=http).chat_structured(**self.kwargs)
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertTrue(third.cache_hit)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.data, second.data)
        self.assertEqual(second.usage["prompt_tokens"], 0)
        self.assertEqual(second.usage["original_usage"]["prompt_tokens"], 10)
        with closing(sqlite3.connect(self.settings.llm_cache_path)) as db, db:
            row = db.execute("SELECT cache_key,payload FROM llm_cache").fetchone()
        self.assertEqual(len(row[0]), 64)
        self.assertNotIn("test-only-secret", str(row))
        self.assertNotIn(self.kwargs["prompt"], str(row))

    async def test_quota_falls_back_then_caches_success(self):
        models = []
        events = []

        def handler(request):
            model = json.loads(request.content)["model"]
            models.append(model)
            return httpx.Response(429) if model == "primary" else self.response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            router = NineRouterClient(self.settings, client=http)
            result = await router.chat_structured(**self.kwargs, on_attempt=lambda *args: events.append(args))
            cached = await router.chat_structured(**self.kwargs)
        self.assertEqual(models, ["primary", "fallback"])
        self.assertEqual(result.model, "fallback")
        self.assertTrue(cached.cache_hit)
        self.assertEqual(events, [("primary", 1, False), ("fallback", 1, True)])

    async def test_invalid_schema_is_never_cached(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return self.response({"value": "missing source and confidence"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            router = NineRouterClient(self.settings, client=http)
            for _ in range(2):
                with self.assertRaises(RouterExhaustedError):
                    await router.chat_structured(**self.kwargs, models=["primary"])
        self.assertEqual(count, 4)
        with closing(sqlite3.connect(self.settings.llm_cache_path)) as db, db:
            self.assertEqual(db.execute("SELECT count(*) FROM llm_cache").fetchone()[0], 0)

    async def test_unauthorized_does_not_fallback_or_expose_body(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return httpx.Response(401, text="test-only-secret echoed by provider")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            router = NineRouterClient(self.settings, client=http)
            with self.assertRaises(RouterError) as error:
                await router.chat_structured(**self.kwargs)
        self.assertEqual(count, 1)
        self.assertNotIn("test-only-secret", str(error.exception))

    async def test_hash_separates_prompt_system_schema_models_temperature_and_mode(self):
        # Hash-only tests must not construct proxy/TLS transports from host env.
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: self.response()))
        self.addAsyncCleanup(http.aclose)
        router = NineRouterClient(self.settings, client=http)
        self.addAsyncCleanup(router.aclose)
        base = dict(**self.kwargs, models=["primary"], temperature=0.3)
        key = router.cache_key(**base)
        for field, value in [
            ("prompt", "Another prompt"), ("system", "Another role"),
            ("schema", Fact[int]), ("models", ["fallback"]), ("temperature", 0.4),
        ]:
            with self.subTest(field=field):
                self.assertNotEqual(key, router.cache_key(**{**base, field: value}))
        changed = self.settings.model_copy(update={"llm_structured_output_mode": "prompt"})
        other = NineRouterClient(changed, client=http)
        self.addAsyncCleanup(other.aclose)
        self.assertNotEqual(key, other.cache_key(**base))

    async def test_ttl_expiry_and_corrupt_row_trigger_fresh_request(self):
        count = 0

        def handler(request):
            nonlocal count
            count += 1
            return self.response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            router = NineRouterClient(self.settings, client=http)
            await router.chat_structured(**self.kwargs)
            with closing(sqlite3.connect(self.settings.llm_cache_path)) as db, db:
                db.execute("UPDATE llm_cache SET created_at=0")
            self.assertFalse((await router.chat_structured(**self.kwargs)).cache_hit)
            with closing(sqlite3.connect(self.settings.llm_cache_path)) as db, db:
                db.execute("UPDATE llm_cache SET payload='broken'")
            self.assertFalse((await router.chat_structured(**self.kwargs)).cache_hit)
        self.assertEqual(count, 3)

    async def test_concurrent_same_prompt_sends_one_request(self):
        count = 0

        async def handler(request):
            nonlocal count
            count += 1
            await asyncio.sleep(0.01)
            return self.response()

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            router = NineRouterClient(self.settings, client=http)
            results = await asyncio.gather(*(router.chat_structured(**self.kwargs) for _ in range(5)))
        self.assertEqual(count, 1)
        self.assertEqual(sum(result.cache_hit for result in results), 4)
        self.assertEqual(router._inflight, {})

    async def test_computed_budget_fields_round_trip_through_cache(self):
        data = {"currency": {"value": "VND", "source": "fixture:test", "confidence": 1},
                "limit": {"value": "5000000", "source": "fixture:test", "confidence": 1},
                "items": []}
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: self.response(data))) as http:
            router = NineRouterClient(self.settings, client=http)
            args = {**self.kwargs, "schema": Budget}
            await router.chat_structured(**args)
            result = await router.chat_structured(**args)
        self.assertTrue(result.cache_hit)
        self.assertEqual(result.data.total.value, 0)

    def test_settings_validate_and_redact_secrets(self):
        self.assertNotIn("test-only-secret", repr(self.settings))
        self.assertTrue(self.settings.llm_cache_path.is_absolute())
        self.assertEqual(Settings(_env_file=None, nine_router_models="a, b,a,,").models, ["a", "b"])
        with self.assertRaises(ValidationError):
            Settings(_env_file=None, llm_cache_ttl_seconds=0)


if __name__ == "__main__":
    unittest.main()
