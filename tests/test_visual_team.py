import json
from pathlib import Path
import tempfile
import threading
import unittest

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import TripRequest
from backend.app.task_plan import build_tasks
from backend.app.visual_team import VisualBranchResult, VisualTeam, merge_visual_results
from scripts.check_visual_team import mock_factories

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


class VisualTeamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.outputs = self.root / "outputs"
        self.settings = Settings(_env_file=None, nine_router_api_key="mock-key", nine_router_image_model="mock-image",
            photo_provider_order="wikimedia", image_cache_dir=self.root / "image-cache", data_cache_path=self.root / "api.db")
        read = lambda name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        places, days, budget = read("research.json"), read("itinerary.json"), read("budget.json")
        self.places, self.days = places[:1], days[:1]
        self.days[0]["stops"] = self.days[0]["stops"][:1]
        budget["items"] = [item for item in budget["items"] if item.get("day_number") in (None, 1)]
        fact = lambda value: {"value": value, "source": "fixture:visual-test", "confidence": 1}
        self.trip = TripRequest(destination=fact("Đà Nẵng"), days=fact(1), budget=budget["limit"], currency=budget["currency"], travelers=fact(1))
        self.paths = {}
        for role, data in (("places", self.places), ("itinerary", self.days), ("budget", budget)):
            path = self.root / (role + ".json")
            path.write_text(json.dumps(data), encoding="utf-8")
            self.paths[role] = str(path)
        self.db = Database(self.root / "test.db")
        self.db.init()

    def state(self, project_id, ready=True):
        self.db.create_project(project_id, "Visuals", "input", trip_request=self.trip)
        tasks = build_tasks(project_id)
        for task in tasks[:5]:
            task["status"] = "done" if ready else "todo"
        for task in tasks[5:8]:
            task["status"] = "ready"
        self.db.replace_tasks(project_id, tasks)
        return {"project_id": project_id, "trip_request": self.trip, "itinerary": self.days, "places": self.places,
                "map_input_paths": self.paths, "fixture_mode": True, "artifacts": []}

    def team(self, **kwargs):
        factories = mock_factories(self.db, self.settings, self.outputs, self.places, **kwargs)
        return VisualTeam(self.db, self.settings, self.outputs, agent_factories=factories)

    @staticmethod
    def report(result):
        return json.loads(Path(result["visual_report"].path).read_text(encoding="utf-8"))

    async def test_sequential_first_then_parallel_merges_same_output_counts(self):
        sequential = await self.team().run(self.state("seq"), mode="sequential")
        parallel = await self.team(barrier=threading.Barrier(3)).run(self.state("par"), mode="parallel")
        baseline, concurrent = self.report(sequential), self.report(parallel)
        self.assertTrue(sequential["visual_complete"])
        self.assertTrue(parallel["visual_complete"])
        self.assertFalse(baseline["all_three_overlap"])
        self.assertTrue(concurrent["all_three_overlap"])
        self.assertEqual(concurrent["counts"], {"photos": 1, "illustrations": 8, "maps": 1, "charts": 3})
        self.assertEqual(baseline["counts"], concurrent["counts"])
        self.assertEqual(len({branch["thread_id"] for branch in concurrent["branches"].values()}), 3)
        self.assertEqual({artifact.id for artifact in parallel["artifacts"]}, {artifact["id"] for artifact in self.db.get_artifacts("par")})
        self.assertEqual(len(parallel["artifacts"]), len({artifact.id for artifact in parallel["artifacts"]}))
        self.assertEqual(parallel["stage"], "reviewer")
        self.assertTrue(parallel["fixture_mode"])
        self.assertFalse(parallel["production_approved"])
        saved = self.db.get_project("par")["state"]
        self.assertEqual(len(saved["photos"]), 1)
        self.assertEqual(len(saved["illustrations"]), 8)
        self.assertEqual(len(saved["maps"]), 1)
        self.assertEqual(len(saved["charts"]), 3)

    async def test_only_fan_in_persists_global_state_and_releases_reviewer_once(self):
        observations = []
        def probe(role, project_id, managed):
            reviewer = next(task for task in self.db.get_tasks(project_id) if task["agent"] == "reviewer")
            observations.append((managed, self.db.get_project(project_id)["status"], reviewer["status"]))
        result = await self.team(barrier=threading.Barrier(3), probe=probe).run(self.state("barrier"))
        self.assertEqual(len(observations), 3)
        self.assertTrue(all(item == (True, "running", "todo") for item in observations))
        events = self.db.get_events("barrier")
        branch_ends = [event["id"] for event in events if event["event_type"] == "visual_branch_finished"]
        joins = [event for event in events if event["event_type"] == "visual_fan_in_completed"]
        self.assertEqual(len(branch_ends), 3)
        self.assertEqual(len(joins), 1)
        self.assertGreater(joins[0]["id"], max(branch_ends))
        self.assertEqual(self.report(result)["fan_in_count"], 1)

    async def test_failed_branch_keeps_successful_siblings_and_does_not_release_reviewer(self):
        result = await self.team(barrier=threading.Barrier(3), fail_role="illustrator").run(self.state("failure"))
        self.assertFalse(result["visual_complete"])
        self.assertEqual(len(result["photos"]), 1)
        self.assertEqual(len(result["maps"]), 1)
        self.assertEqual(len(result["charts"]), 3)
        self.assertEqual(result["visual_results"]["illustrator"].status, "failed")
        self.assertEqual(self.db.get_project("failure")["status"], "failed")
        reviewer = next(task for task in self.db.get_tasks("failure") if task["agent"] == "reviewer")
        self.assertEqual(reviewer["status"], "todo")
        self.assertEqual(len([event for event in self.db.get_events("failure") if event["event_type"] == "visual_branch_finished"]), 3)

    async def test_missing_photo_is_attention_and_not_a_false_complete(self):
        result = await self.team(barrier=threading.Barrier(3), empty_photos=True).run(self.state("no-photo"))
        self.assertFalse(result["visual_complete"])
        self.assertEqual(result["photos"], [])
        self.assertEqual(len(result["illustrations"]), 8)
        self.assertEqual(self.db.get_project("no-photo")["status"], "needs_attention")
        self.assertEqual(result["visual_results"]["image_curator"].status, "needs_attention")

    async def test_bad_dependency_or_concurrent_claim_cannot_start_a_second_run(self):
        with self.assertRaises(ValueError):
            await self.team().run(self.state("not-ready", ready=False))
        state = self.state("running")
        self.db.update_project("running", status="running")
        with self.assertRaises(ValueError):
            await self.team().run(state)
        self.assertFalse(self.outputs.exists())

    async def test_file_inputs_are_snapshotted_for_all_three_branches(self):
        state = self.state("snapshots")
        state["itinerary"] = []  # Supplied Planner file is authoritative, not this stale value.
        result = await self.team().run(state, mode="sequential")
        self.assertTrue(result["visual_complete"])
        for role, snapshot in result["map_input_paths"].items():
            self.assertEqual(Path(snapshot).read_bytes(), Path(self.paths[role]).read_bytes())
            self.assertIn("visual_inputs", snapshot)

    def test_result_reducer_is_idempotent_and_rejects_conflicting_replays(self):
        value = VisualBranchResult(agent="map_agent", status="done", started_ns=1, ended_ns=2, thread_id=3)
        first = merge_visual_results({}, {"map_agent": value})
        self.assertEqual(merge_visual_results(first, {"map_agent": value.model_dump(mode="json")}), first)
        with self.assertRaises(ValueError):
            merge_visual_results(first, {"map_agent": value.model_copy(update={"ended_ns": 9})})
        with self.assertRaises(ValueError):
            merge_visual_results({}, {"illustrator": value})
