from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from PIL import Image

from backend.app.agents.map_agent import MapAgent, MapInputFiles, MapResult
from backend.app.db import Database
from backend.app.config import Settings
from backend.app.schemas import Artifact
from backend.app.task_plan import build_tasks

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


def fact(value):
    return {"value": value, "source": "user:test-json-input", "confidence": 0.8}


def user_sources(value):
    if isinstance(value, dict):
        if {"value", "source", "confidence"} <= value.keys():
            value["source"], value["confidence"] = "user:test-json-input", .8
        else:
            for child in value.values():
                user_sources(child)
    elif isinstance(value, list):
        for child in value:
            user_sources(child)
    return value


class MapAgentTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.inputs_dir = self.root / "inputs"
        self.inputs_dir.mkdir()
        read = lambda name: user_sources(json.loads((FIXTURES / name).read_text(encoding="utf-8")))
        self.days, self.places, self.budget = read("itinerary.json"), read("research.json"), read("budget.json")
        for i, day in enumerate(self.days):
            day["date"] = fact((date(2026, 12, 7) + timedelta(days=i)).isoformat())
        # Change the actual FILE order/values; the node must not use stale State or demo data.
        a, b = self.days[0]["stops"][:2]
        a["place_id"], b["place_id"] = b["place_id"], a["place_id"]
        self.budget["items"][0]["amount"] = fact("123456.78")
        self.weather = {"status": "partial", "daily": [{"day_number": 1, "date": fact("2026-12-07"),
            "temperature_min": fact(24.2), "temperature_max": fact(31.7), "rain_probability": fact(55.5)}]}
        self.paths = MapInputFiles(itinerary=self.write("custom-plan.json", self.days), places=self.write("custom-places.json", self.places),
            budget=self.write("custom-budget.json", self.budget), weather=self.write("custom-weather.json", self.weather))
        self.db = Database(self.root / "test.db")
        self.db.init()
        self.agent = MapAgent(self.db, self.root / "outputs")

    def write(self, name, data):
        path = self.inputs_dir / name
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return path

    def state(self, others_done=False, blocked=False, ready=True):
        self.db.create_project("map-test", "Map files", "input")
        tasks = build_tasks("map-test")
        for task in tasks[:5]:
            task["status"] = "done" if ready else "todo"
        tasks[7]["status"] = "ready"
        if others_done:
            tasks[5]["status"] = tasks[6]["status"] = "done"
        if blocked:
            tasks[5]["status"] = "blocked"
        self.db.replace_tasks("map-test", tasks)
        return {"project_id": "map-test", "map_input_paths": self.paths.model_dump(mode="json"),
                "itinerary": json.loads((FIXTURES / "itinerary.json").read_text(encoding="utf-8"))}

    @staticmethod
    def png_data(artifact):
        with Image.open(artifact.path) as image:
            image.load()
            return json.loads(image.info["Data"])

    async def test_reads_real_file_values_and_keeps_input_hashes(self):
        update = await self.agent.run(self.state())
        self.assertEqual(len(update["maps"]), 3)
        self.assertEqual(len(update["charts"]), 3)
        data = self.png_data(update["maps"][0])
        self.assertEqual(data["place_ids"], [stop["place_id"] for stop in self.days[0]["stops"]])
        self.assertEqual(data["coordinates"][0], [self.places[1]["longitude"]["value"], self.places[1]["latitude"]["value"]])
        charts = {artifact.name: self.png_data(artifact) for artifact in update["charts"]}
        self.assertEqual(charts["budget.png"]["total"], "2623456.78")
        self.assertEqual(charts["weather.png"]["forecasts"][0]["temperature_max"]["value"], 31.7)
        self.assertEqual(charts["timeline.png"]["stops"][0]["place_id"], self.days[0]["stops"][0]["place_id"])
        manifest = MapResult.model_validate_json(Path(update["artifacts"][-1].path).read_text(encoding="utf-8"))
        self.assertFalse(manifest.contains_demo_input.value)
        for snapshot in manifest.inputs:
            self.assertEqual(sha256(Path(snapshot.path).read_bytes()).hexdigest(), snapshot.sha256.value)
            self.assertEqual(Path(snapshot.path).read_bytes(), Path(snapshot.snapshot_path).read_bytes())
        with Image.open(update["maps"][0].path) as image:
            self.assertEqual({item["role"] for item in json.loads(image.info["Inputs"])}, {"itinerary", "places"})
        self.assertFalse(update["production_approved"])
        self.assertEqual(update["stage"], "visuals")

    async def test_live_render_embeds_prepared_basemap_with_input_provenance(self):
        from backend.app.services.basemaps import MapLayer, viewport
        state = self.state()
        bundle = self.agent._load(self.paths)
        day, places = bundle[1][0], bundle[2]
        indexed = {place.id: place for place in places}
        coords = [(indexed[stop.place_id].longitude.value, indexed[stop.place_id].latitude.value) for stop in day.stops]
        view = viewport(coords)
        layer = MapLayer(Image.new('RGB', (view.width, view.height), '#dce8d2'), view, [], 'straight_segments',
            ['https://www.openstreetmap.org/copyright'], [])
        with patch('backend.app.services.basemaps.BasemapService.prepare', AsyncMock(return_value=({1: layer}, []))):
            result, _ = await self.agent.render_live('map-test', self.paths, settings=Settings(_env_file=None), public_api=None)
        self.assertEqual(self.png_data(result.maps[0])['basemap'], 'openstreetmap')
        self.assertEqual(self.png_data(result.maps[0])['place_ids'], [stop['place_id'] for stop in self.days[0]['stops']])
        self.assertEqual(len(result.inputs), 4)

    async def test_file_changes_are_reflected_in_next_render(self):
        state = self.state()
        result, _ = self.agent.render("map-test", self.paths)
        before = self.png_data(result.charts[0])["total"]
        self.budget["items"][0]["amount"] = fact("10.01")
        self.write("custom-budget.json", self.budget)
        after, _ = self.agent.render("map-test", self.paths)
        self.assertNotEqual(before, self.png_data(after.charts[0])["total"])
        self.assertNotEqual(result.inputs[2].sha256.value, after.inputs[2].sha256.value)

    async def test_missing_weather_renders_no_invented_numeric_forecast(self):
        self.paths.weather = None
        update = await self.agent.run(self.state())
        weather = next(artifact for artifact in update["charts"] if artifact.name == "weather.png")
        self.assertFalse(self.png_data(weather)["has_numeric_forecast"])
        self.assertEqual(self.png_data(weather)["forecasts"], [])
        self.assertEqual(weather.confidence, 0)
        self.assertTrue(any("weather.json" in fact.value for fact in update["map_warnings"]))

    async def test_forecast_date_confidence_is_not_lost(self):
        self.weather["daily"][0]["date"]["confidence"] = 0.1
        self.write("custom-weather.json", self.weather)
        update = await self.agent.run(self.state())
        weather = next(artifact for artifact in update["charts"] if artifact.name == "weather.png")
        self.assertEqual(weather.confidence, 0.1)

    async def test_missing_itinerary_date_does_not_attach_wrong_forecast(self):
        self.days[0]["date"] = None
        self.write("custom-plan.json", self.days)
        update = await self.agent.run(self.state())
        weather = next(artifact for artifact in update["charts"] if artifact.name == "weather.png")
        self.assertFalse(self.png_data(weather)["has_numeric_forecast"])

    async def test_weather_date_mismatch_fails_before_any_png(self):
        self.weather["daily"][0]["date"] = fact("2026-12-08")
        self.write("custom-weather.json", self.weather)
        with self.assertRaisesRegex(ValueError, "Weather date"):
            await self.agent.run(self.state())
        self.assertEqual(list((self.root / "outputs").rglob("*.png")), [])
        self.assertEqual(self.db.get_project("map-test")["status"], "failed")

    async def test_bad_stop_and_budget_day_are_rejected(self):
        self.days[0]["stops"][0]["place_id"] = "missing-point"
        self.write("custom-plan.json", self.days)
        with self.assertRaises(ValueError):
            self.agent._load(self.paths)
        self.days[0]["stops"][0]["place_id"] = self.places[0]["id"]
        self.write("custom-plan.json", self.days)
        self.budget["items"][0]["day_number"] = 99
        self.write("custom-budget.json", self.budget)
        with self.assertRaisesRegex(ValueError, "Budget references"):
            self.agent._load(self.paths)

    async def test_no_implicit_fixture_fallback(self):
        state = self.state()
        state.pop("map_input_paths")
        with self.assertRaises(ValueError):
            await self.agent.run(state)
        self.assertFalse((self.root / "outputs").exists())

    async def test_can_resolve_same_project_upstream_artifacts(self):
        state = self.state()
        state.pop("map_input_paths")
        definitions = [("planner", "itinerary.json", "itinerary", self.paths.itinerary), ("researcher", "research.json", "research", self.paths.places),
                       ("budget", "budget.json", "budget", self.paths.budget)]
        state["artifacts"] = [Artifact(id=str(i), project_id="map-test", agent_id=agent, kind=kind, name=name,
            path=str(path), mime_type="application/json", source="input:test", confidence=.8) for i, (agent, name, kind, path) in enumerate(definitions)]
        result = await self.agent.run(state)
        self.assertEqual(len(result["maps"]), 3)
        self.assertEqual(self.png_data(result["maps"][0])["place_ids"], [stop["place_id"] for stop in self.days[0]["stops"]])

    async def test_last_branch_opens_reviewer_and_logs_events(self):
        result = await self.agent.run(self.state(others_done=True))
        self.assertEqual(result["stage"], "reviewer")
        tasks = {task["agent"]: task for task in self.db.get_tasks("map-test")}
        self.assertEqual(tasks["map_agent"]["status"], "done")
        self.assertEqual(tasks["reviewer"]["status"], "ready")
        self.assertTrue(any(event["event_type"] == "maps_charts_ready" for event in self.db.get_events("map-test")))

    async def test_blocked_sibling_keeps_needs_attention(self):
        result = await self.agent.run(self.state(blocked=True))
        self.assertEqual(result["stage"], "needs_attention")

    async def test_incomplete_content_prevents_render(self):
        with self.assertRaises(ValueError):
            await self.agent.run(self.state(ready=False))
        self.assertFalse((self.root / "outputs").exists())

    async def test_fixture_markers_are_kept_as_unverified(self):
        self.places[0]["name"]["source"] = "derived:research; fixture:unverified-place"
        self.write("custom-places.json", self.places)
        update = await self.agent.run(self.state())
        manifest = MapResult.model_validate_json(Path(update["artifacts"][-1].path).read_text(encoding="utf-8"))
        self.assertTrue(manifest.contains_demo_input.value)
        with Image.open(update["maps"][0].path) as image:
            self.assertIn("demo/fixture", image.info["Notes"])
