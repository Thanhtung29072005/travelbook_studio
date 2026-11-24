import base64
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from PIL import Image

from backend.app.agents.illustrator import IllustrationSet, IllustratorAgent, plan_illustrations
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import TripRequest
from backend.app.services.illustrations import IllustrationService
from backend.app.services.style_lock import ILLUSTRATION_RULES, StyleLock
from backend.app.task_plan import build_tasks
from scripts.check_illustrator import mock_png

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


class IllustrationServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(_env_file=None, nine_router_api_key="image-secret-only",
            nine_router_image_model="primary-image", nine_router_image_fallback_models="fallback-image",
            image_cache_dir=self.root / "cache")
        self.calls, self.fail_primary, self.bad_image, self.url, self.api_error = [], False, False, None, False
        def handler(request):
            self.calls.append(request)
            if request.method == "GET":
                return httpx.Response(200, content=mock_png())
            payload = json.loads(request.content)
            if self.api_error or (self.fail_primary and payload["model"] == "primary-image"):
                return httpx.Response(503, json={"error": "provider failure"})
            if self.url:
                return httpx.Response(200, json={"data": [{"url": self.url}]})
            return httpx.Response(200, json={"data": [{"b64_json": "not base64" if self.bad_image else base64.b64encode(mock_png()).decode()}]})
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.service = IllustrationService(self.settings, client=self.http)
        self.style = StyleLock()

    async def test_style_lock_is_in_api_prompt_and_outputs_exact_png_geometry(self):
        output = await self.service.generate("Cover", (120, 180), self.style)
        self.assertFalse(output.is_placeholder)
        self.assertEqual(output.image.size, (120, 180))
        payload = json.loads(self.calls[0].content)
        self.assertIn(ILLUSTRATION_RULES, payload["prompt"])
        self.assertIn(self.style.description, payload["prompt"])
        self.assertIn(self.style.palette[0], payload["prompt"])
        self.assertEqual(payload["size"], "1024x1536")
        self.assertEqual(self.calls[0].headers["Authorization"], "Bearer image-secret-only")

    async def test_model_chain_and_image_cache(self):
        self.fail_primary = True
        first = await self.service.generate("Day opener", (180, 120), self.style)
        self.assertEqual(first.model, "fallback-image")
        self.assertEqual(len(self.calls), 2)
        second = await self.service.generate("Day opener", (180, 120), self.style)
        self.assertTrue(second.cache_hit)
        # Primary retried; successful fallback's cached image is reused.
        self.assertEqual(len(self.calls), 3)
        for path in self.root.rglob("*"):
            if path.is_file():
                self.assertNotIn(b"image-secret-only", path.read_bytes())

    async def test_comma_separated_image_models_are_tried_as_individual_ids(self):
        self.service.settings = self.settings.model_copy(update={"nine_router_image_model": "primary-image, fallback-image", "nine_router_image_fallback_models": ""})
        self.fail_primary = True
        output = await self.service.generate("Detailed day scene", (180, 120), self.style)
        self.assertFalse(output.is_placeholder)
        self.assertEqual([json.loads(call.content)["model"] for call in self.calls], ["primary-image", "fallback-image"])

    async def test_provider_error_and_invalid_base64_use_correct_background(self):
        self.api_error = True
        output = await self.service.generate("Icon", (32, 32), self.style)
        self.assertTrue(output.is_placeholder)
        self.assertEqual(output.image.getpixel((0, 0)), (243, 235, 221))
        self.assertFalse(list((self.root / "cache").glob("*.png")))
        self.api_error, self.bad_image = False, True
        self.assertTrue((await self.service.generate("Different icon", (40, 40), self.style)).is_placeholder)

    async def test_unconfigured_or_offline_never_calls_model(self):
        output = await self.service.generate("Background", (32, 32), self.style, force_fallback=True)
        self.assertTrue(output.is_placeholder)
        self.assertEqual(output.warning, "offline_requested")
        self.service.settings = self.settings.model_copy(update={"nine_router_image_model": "", "nine_router_image_fallback_models": ""})
        self.assertTrue((await self.service.generate("Cover", (32, 48), self.style)).is_placeholder)
        self.assertEqual(self.calls, [])

    async def test_url_download_uses_allowlist_and_does_not_forward_key(self):
        self.url = "https://oaidalleapiprodscus.blob.core.windows.net/art/image.png"
        output = await self.service.generate("Day", (80, 40), self.style)
        self.assertFalse(output.is_placeholder)
        self.assertNotIn("Authorization", self.calls[-1].headers)
        self.url = "http://127.0.0.1:9000/secret"
        start = len(self.calls)
        self.assertTrue((await self.service.generate("Other day", (80, 40), self.style)).is_placeholder)
        self.assertEqual(len(self.calls)-start, 2)  # only two POST model attempts, never GET localhost

    async def test_style_or_size_change_does_not_reuse_cache(self):
        await self.service.generate("Icon", (32, 32), self.style)
        await self.service.generate("Icon", (64, 64), self.style)
        await self.service.generate("Icon", (32, 32), StyleLock("Flat travel shapes"))
        self.assertEqual(len(self.calls), 3)

    async def test_bad_size_raises_before_request(self):
        with self.assertRaises(ValueError):
            await self.service.generate("Cover", (0, 32), self.style)
        self.assertEqual(self.calls, [])


class IllustratorAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(_env_file=None, image_cache_dir=self.root / "cache")
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.addAsyncCleanup(self.http.aclose)
        self.db = Database(self.root / "test.db")
        self.db.init()
        self.agent = IllustratorAgent(self.db, IllustrationService(self.settings, client=self.http), self.root / "outputs")
        read = lambda name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        self.days, self.places = read("itinerary.json"), read("research.json")
        fact = lambda value: {"value": value, "source": "input:test-illustrator", "confidence": 1}
        self.trip = TripRequest(destination=fact("Đà Nẵng"), days=fact(3), budget=fact("5000000"), currency=fact("VND"), travelers=fact(1))

    def state(self, others_done=False, blocked=False, ready=True):
        self.db.create_project("illustrator-test", "Illustrator", "input")
        tasks = build_tasks("illustrator-test")
        for task in tasks[:5]:
            task["status"] = "done" if ready else "todo"
        tasks[6]["status"] = "ready"
        if others_done:
            tasks[5]["status"] = tasks[7]["status"] = "done"
        if blocked:
            tasks[5]["status"] = "blocked"
        self.db.replace_tasks("illustrator-test", tasks)
        return {"project_id": "illustrator-test", "trip_request": self.trip, "itinerary": self.days, "places": self.places}

    def test_plan_contains_only_cover_and_daily_illustrations(self):
        requests = plan_illustrations(self.trip, self.days, self.places)
        self.assertEqual(len(requests), 4)
        self.assertEqual([request.day_number for request in requests if request.role == "day_opener"], [1, 2, 3])
        self.assertEqual({request.role for request in requests}, {'cover', 'day_opener'})
        with self.assertRaises(ValueError):
            plan_illustrations(self.trip, self.days[:1], self.places)

    def test_detailed_day_prompt_uses_the_actual_stops_schedule_and_descriptions(self):
        first_id = self.days[0]["stops"][0]["place_id"]
        place = next(item for item in self.places if item["id"] == first_id)
        place["description"] = {"value": "Mái ngói, lối đi lát đá và khu vườn cây xanh.", "source": "input:place-description", "confidence": .8}
        requests = plan_illustrations(self.trip, self.days, self.places)
        cover = next(item for item in requests if item.role == "cover")
        day = next(item for item in requests if item.key == "day_1")
        self.assertGreater(cover.width, cover.height)
        self.assertIn(place["name"]["value"], day.subject.value)
        self.assertIn(place["description"]["value"], day.subject.value)
        self.assertIn('"arrival"', day.subject.value)
        self.assertIn("highly detailed", day.subject.value)
        self.assertNotIn("Abstract impression", day.subject.value)
        self.assertIn("input:place-description", day.subject.source)

    async def test_fallback_set_has_one_style_exact_dimensions_and_db_artifacts(self):
        update = await self.agent.run(self.state())
        self.assertEqual(len(update["illustrations"]), 4)
        manifest = IllustrationSet.model_validate_json(Path(update["artifacts"][-1].path).read_text(encoding="utf-8"))
        self.assertEqual({asset.style_signature.value for asset in manifest.assets}, {manifest.style_signature.value})
        self.assertTrue(all(asset.is_placeholder.value for asset in manifest.assets))
        for asset in manifest.assets:
            with Image.open(asset.artifact.path) as image:
                self.assertEqual(image.size, (asset.width.value, asset.height.value))
                self.assertEqual(image.getpixel((0, 0)), (243, 235, 221))
            self.assertIn(ILLUSTRATION_RULES, asset.prompt.value)
        self.assertEqual(update["stage"], "visuals")
        self.assertFalse(update["production_approved"])
        self.assertEqual(len(self.db.get_artifacts("illustrator-test")), 5)
        tasks = {task["agent"]: task for task in self.db.get_tasks("illustrator-test")}
        self.assertEqual(tasks["illustrator"]["status"], "done")
        self.assertEqual(tasks["reviewer"]["status"], "todo")

    async def test_final_branch_can_open_reviewer(self):
        update = await self.agent.run(self.state(others_done=True), force_fallback=True)
        self.assertEqual(update["stage"], "reviewer")

    async def test_missing_photo_branch_remains_needs_attention(self):
        update = await self.agent.run(self.state(blocked=True), force_fallback=True)
        self.assertEqual(update["stage"], "needs_attention")
        self.assertEqual(self.db.get_project("illustrator-test")["status"], "needs_attention")

    async def test_incomplete_content_prevents_generation(self):
        with self.assertRaises(ValueError):
            await self.agent.run(self.state(ready=False))
        self.assertFalse((self.root / "outputs").exists())

    async def test_unavailable_provider_is_not_called_for_every_asset(self):
        self.agent.image_service.settings = Settings(_env_file=None, nine_router_api_key="mock-key", nine_router_image_model="broken",
            image_cache_dir=self.root / "cache")
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(503)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        self.agent.image_service.client = http
        result = await self.agent.run(self.state())
        self.assertEqual(len(result["illustrations"]), 4)
        self.assertEqual(len(calls), 1)

    def test_palette_is_part_of_signature_and_opaque(self):
        self.assertNotEqual(StyleLock().signature, StyleLock(palette=("white", "black")).signature)
        with self.assertRaises(ValueError):
            StyleLock(palette=("#FFFFFF00", "black"))
