"""Real StateGraph integration: isolated JSON fixtures, SQLite, PNGs and PDF."""

from hashlib import sha256
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from pypdf import PdfReader

from backend.app.db import Database
from backend.app.schemas import Budget
from backend.app.services.style_lock import StyleLock
from backend.app.workflow import DEPENDENCIES, TravelBookWorkflow, VISUAL_NODES

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"
FILES = ("research.json", "itinerary.json", "budget.json", "content.json", "photos.json")


class WorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.root = Path(cls.directory.name)
        cls.sample = cls.root / "fixtures"
        cls.sample.mkdir()
        for name in FILES:
            shutil.copyfile(FIXTURES / name, cls.sample / name)
        cls.original_hashes = {name: sha256((cls.sample / name).read_bytes()).hexdigest() for name in FILES}
        cls.db = Database(cls.root / "travelbook.db")
        cls.workflow = TravelBookWorkflow(cls.db, sample_dir=cls.sample, artifacts_root=cls.root / "outputs",
                                          style_lock=StyleLock())
        cls.result = cls.workflow.run(project_id="test-complete")
        cls.events = cls.db.get_events("test-complete")

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_one_run_finishes_graph_and_persists_project(self):
        self.assertTrue(self.result["completed"])
        project = Database(self.db.path).get_project("test-complete")
        self.assertEqual(project["status"], "completed")
        self.assertEqual(project["progress"], 100)
        self.assertEqual(project["stage"], "completed")
        self.assertTrue(project["state"]["completed"])
        self.assertEqual(Budget.model_validate(project["state"]["budget"]).total.value, 3700000)
        reader = PdfReader(project["final_pdf"])
        try:
            manifest=json.loads(Path(project['final_pdf']).with_suffix('.manifest.json').read_text(encoding='utf-8'))
            self.assertEqual(len(reader.pages), manifest['page_count'])
            self.assertTrue({'day_1','day_2','day_3','timeline'} <= {item['id'] for item in manifest['sections']})
            self.assertIn("Đà Nẵng", reader.pages[0].extract_text())
        finally:
            reader.close()

    def test_every_node_reads_hashed_json_and_completes_once(self):
        completed = [event["payload"]["node"] for event in self.events if event["event_type"] == "node_completed"]
        self.assertEqual(set(completed), set(DEPENDENCIES))
        self.assertEqual(len(completed), 11)
        reads = [event for event in self.events if event["event_type"] == "fixture_read"]
        self.assertEqual(len(reads), 11)
        for event in reads:
            for item in event["payload"]["files"]:
                self.assertEqual(item["sha256"], self.original_hashes[item["file"]])
        self.assertEqual(self.original_hashes, {
            name: sha256((self.sample / name).read_bytes()).hexdigest() for name in FILES
        })

    def test_fan_in_waits_for_all_three_roles_and_merges_artifacts(self):
        branch_ends = [event["id"] for event in self.events
                       if event["event_type"] == "node_completed" and event["payload"]["node"] in VISUAL_NODES]
        reviewer_starts = [event["id"] for event in self.events
                           if event["event_type"] == "node_started" and event["payload"]["node"] == "reviewer"]
        self.assertEqual(len(branch_ends), 3)
        self.assertEqual(len(reviewer_starts), 1)
        self.assertGreater(reviewer_starts[0], max(branch_ends))
        self.assertEqual(len(self.result["maps"]), 3)
        self.assertEqual(len(self.result["charts"]), 3)
        self.assertEqual(len(self.result["illustrations"]), 4)
        self.assertEqual(len(self.result["photos"]), 8)
        saved = self.db.get_artifacts("test-complete")
        self.assertEqual({artifact["id"] for artifact in saved}, {artifact.id for artifact in self.result["artifacts"]})
        self.assertTrue(all(Path(artifact["path"]).is_file() for artifact in saved))

    def test_tasks_represent_document_dependencies_and_finish(self):
        tasks = {task["title"]: task for task in self.db.get_tasks("test-complete")}
        self.assertEqual(len(tasks), 11)
        self.assertTrue(all(task["status"] == "done" for task in tasks.values()))
        self.assertEqual(set(tasks["reviewer"]["depends_on"]), {f"test-complete:{node}" for node in VISUAL_NODES})
        self.assertEqual(tasks["pm_finalize"]["agent"], "pm")

    def test_completion_is_explicitly_fixture_only_not_production_approval(self):
        self.assertTrue(self.result["fixture_mode"])
        self.assertFalse(self.result["production_approved"])
        event = next(event for event in self.events if event["event_type"] == "fixture_review")
        self.assertEqual(event["payload"]["scope"], "structure_only")
        self.assertFalse(event["payload"]["production_approved"])
        manifest = json.loads(Path(self.result["final_pdf"].path).with_suffix(".manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["embedded_photo_ids"], [])

    def test_duplicate_id_and_unsafe_path_rejected_without_touching_run(self):
        count = len(self.db.get_events("test-complete"))
        with self.assertRaises(ValueError):
            self.workflow.run(project_id="test-complete")
        with self.assertRaises(ValueError):
            self.workflow.run(project_id="../escape")
        self.assertEqual(len(self.db.get_events("test-complete")), count)

    def test_missing_fixture_blocks_node_and_marks_project_failed(self):
        broken = self.root / "broken-fixtures"
        broken.mkdir()
        for name in FILES:
            if name != "photos.json":
                shutil.copyfile(FIXTURES / name, broken / name)
        workflow = TravelBookWorkflow(self.db, sample_dir=broken, artifacts_root=self.root / "outputs", style_lock=StyleLock())
        with self.assertRaises(FileNotFoundError):
            workflow.run(project_id="test-failure")
        failed = self.db.get_project("test-failure")
        self.assertEqual(failed["status"], "failed")
        self.assertIsNone(failed["final_pdf"])
        self.assertFalse((self.root / "outputs/test-failure/travelbook.pdf").exists())
        events = self.db.get_events("test-failure")
        self.assertTrue(any(event["event_type"] == "node_failed" for event in events))
        self.assertEqual(events[-1]["event_type"], "workflow_failed")
        self.assertEqual(next(task for task in self.db.get_tasks("test-failure") if task["agent"] == "image_curator")["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
