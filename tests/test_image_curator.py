import json
from pathlib import Path
import tempfile
import unittest

import httpx
from pydantic import TypeAdapter

from backend.app.agents.image_curator import ImageCuratorAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Photo
from backend.app.services.photos import PhotoService
from backend.app.services.public_api import PublicAPIClient
from backend.app.task_plan import build_tasks
from scripts.check_image_curator import mock_commons

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


class CuratorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.places = json.loads((FIXTURES / "research.json").read_text(encoding="utf-8"))
        self.empty = False
        def handler(request):
            if self.empty:
                return httpx.Response(200, json={"query": {"pages": []}})
            query = request.url.params["gsrsearch"]
            index, place = next((i, p) for i, p in enumerate(self.places, 1) if query.startswith(p["name"]["value"]))
            return httpx.Response(200, json=mock_commons(place, index))
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        settings = Settings(_env_file=None, photo_provider_order="wikimedia", data_cache_path=self.root / "cache.db")
        self.db = Database(self.root / "db.sqlite3")
        self.db.init()
        self.agent = ImageCuratorAgent(self.db, PhotoService(settings, PublicAPIClient(settings, client=self.http)), self.root / "outputs")

    def state(self, others_done=False, ready=True):
        self.db.create_project("curator-test", "curation", "input")
        tasks = build_tasks("curator-test")
        for task in tasks[:5]:
            task["status"] = "done" if ready else "todo"
        tasks[5]["status"] = "ready"
        if others_done:
            tasks[6]["status"] = tasks[7]["status"] = "done"
        self.db.replace_tasks("curator-test", tasks)
        return {"project_id": "curator-test", "places": self.places}

    async def test_every_place_has_exactly_one_photo_and_license(self):
        result = await self.agent.curate(self.places)
        self.assertEqual(len(result.photos), len(self.places))
        self.assertEqual({photo.place_id for photo in result.photos}, {place["id"] for place in self.places})
        self.assertEqual(len({photo.id for photo in result.photos}), len(self.places))
        self.assertEqual(len(result.attributions), len(result.photos))
        self.assertFalse(result.missing_place_ids)
        self.assertTrue(all(photo.license.value and photo.license_url and photo.author.value for photo in result.photos))

    async def test_run_persists_schema_and_does_not_skip_branch_barrier(self):
        update = await self.agent.run(self.state())
        self.assertEqual(update["stage"], "visuals")
        photos = TypeAdapter(list[Photo]).validate_json(Path(update["artifacts"][0].path).read_text(encoding="utf-8"))
        self.assertEqual(len(photos), len(self.places))
        tasks = {task["agent"]: task for task in self.db.get_tasks("curator-test")}
        self.assertEqual(tasks["image_curator"]["status"], "done")
        self.assertEqual(tasks["reviewer"]["status"], "todo")
        self.assertFalse(update["production_approved"])
        self.assertTrue(any(event["event_type"] == "photos_ready" for event in self.db.get_events("curator-test")))

    async def test_last_visual_branch_can_open_reviewer(self):
        update = await self.agent.run(self.state(others_done=True))
        self.assertEqual(update["stage"], "reviewer")
        reviewer = next(task for task in self.db.get_tasks("curator-test") if task["agent"] == "reviewer")
        self.assertEqual(reviewer["status"], "ready")

    async def test_missing_photos_block_curator_and_preserve_missing_ids(self):
        self.empty = True
        update = await self.agent.run(self.state())
        self.assertEqual(update["stage"], "needs_attention")
        self.assertEqual(update["photos"], [])
        report = json.loads(Path(update["artifacts"][2].path).read_text(encoding="utf-8"))
        self.assertEqual(len(report["missing_place_ids"]), len(self.places))
        tasks = {task["agent"]: task for task in self.db.get_tasks("curator-test")}
        self.assertEqual(tasks["image_curator"]["status"], "blocked")
        self.assertEqual(tasks["reviewer"]["status"], "todo")

    async def test_duplicate_places_and_incomplete_content_are_rejected(self):
        with self.assertRaises(ValueError):
            await self.agent.curate([self.places[0], self.places[0]])
        with self.assertRaises(ValueError):
            await self.agent.run(self.state(ready=False))
