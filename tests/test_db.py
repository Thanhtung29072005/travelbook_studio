"""Persistence and transaction tests against real temporary SQLite files."""

import sqlite3
import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from backend.app.db import Database
from backend.app.schemas import Artifact, Budget, Fact, TripRequest


def fact(value):
    return {"value": value, "source": "fixture:database-test", "confidence": 0.8}


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "travelbook.db"
        self.db = Database(self.path)
        self.db.init()

    def project(self, project_id="trip-1"):
        return self.db.create_project(project_id, "Đà Nẵng 3 ngày", "Đi Đà Nẵng với ngân sách 5 triệu")

    def artifact(self, project_id="trip-1"):
        return Artifact(
            id="artifact-1", project_id=project_id, agent_id="researcher", kind="research",
            name="research.json", path="artifacts/trip-1/research.json", mime_type="application/json",
            source="fixture:database-test", confidence=0.7,
        )

    def test_create_read_and_reopen_project(self):
        request = TripRequest(destination=fact("Đà Nẵng"), days=fact(3), budget=fact("5000000"),
                              currency=fact("VND"), travelers=fact(1))
        created = self.db.create_project(
            "trip-1", "Đà Nẵng 3 ngày", "Ngân sách 5 triệu",
            trip_request=request, state={"trip_request": request, "stage": "pm"},
        )
        reread = Database(self.path).get_project("trip-1")
        self.assertEqual(reread, created)
        self.assertEqual(reread["status"], "queued")
        self.assertEqual(reread["state"]["trip_request"]["destination"]["source"], "fixture:database-test")
        self.assertEqual(TripRequest.model_validate(reread["trip_request"]), request)
        self.assertEqual(reread["progress"], 0)
        self.assertFalse(reread["pause_requested"])
        self.assertEqual(self.db.get_events("trip-1")[0]["event_type"], "project_created")

    def test_init_idempotent_and_missing_project(self):
        self.project()
        self.db.init()
        self.assertEqual(len(self.db.list_projects()), 1)
        self.assertIsNone(self.db.get_project("missing"))
        self.assertFalse(self.db.update_project("missing", progress=10))

    def test_duplicate_does_not_overwrite_or_add_event(self):
        original = self.project()
        with self.assertRaises(sqlite3.IntegrityError):
            self.project()
        self.assertEqual(self.db.get_project("trip-1"), original)
        self.assertEqual(len(self.db.get_events("trip-1")), 1)

    def test_event_payload_pagination_and_foreign_keys(self):
        self.project()
        first = self.db.add_event("trip-1", "pm", "agent_started", "PM bắt đầu", {"destination": fact("Đà Nẵng")})
        last = self.db.add_event("trip-1", "pm", "handoff", "Giao Researcher", {"to": "researcher"})
        events = self.db.get_events("trip-1", after_id=first, limit=1)
        self.assertEqual([event["id"] for event in events], [last])
        self.assertEqual(self.db.get_events("trip-1")[1]["payload"]["destination"]["confidence"], 0.8)
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.add_event("missing", "pm", "started", "Không có dự án")

    def test_updates_validate_fields_and_preserve_nested_models(self):
        self.project()
        budget = Budget(currency=fact("VND"), limit=fact("5000000"))
        self.assertTrue(self.db.update_project("trip-1", status="running", progress=20,
                                               pause_requested=True, state={"budget": budget}))
        reread = self.db.get_project("trip-1")
        self.assertEqual(reread["status"], "running")
        self.assertTrue(reread["pause_requested"])
        self.assertEqual(Budget.model_validate(reread["state"]["budget"]), budget)
        for change in ({"progress": 101}, {"status": "bogus"}, {"id": "other"},
                       {"state_json": "[]"}, {"pause_requested": "false"},
                       {"title=?,progress=100 --": "injection"}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.db.update_project("trip-1", **change)

    def test_tasks_dependencies_and_parent_can_be_inserted_out_of_order(self):
        self.project()
        tasks = [
            {"id": "sub", "title": "Tìm địa điểm", "agent": "researcher", "parent_id": "main",
             "item_type": "subtask", "order_index": 1},
            {"id": "main", "title": "Nghiên cứu", "agent": "researcher", "order_index": 0},
            {"id": "plan", "title": "Lập lịch", "agent": "planner", "depends_on": ["main"], "order_index": 2},
        ]
        self.db.replace_tasks("trip-1", tasks)
        saved = self.db.get_tasks("trip-1")
        self.assertEqual([task["id"] for task in saved], ["main", "sub", "plan"])
        self.assertEqual(saved[2]["depends_on"], ["main"])
        self.assertTrue(self.db.set_task_status("trip-1", "sub", "testing"))
        self.assertFalse(self.db.set_task_status("other-project", "sub", "done"))

    def test_invalid_tasks_do_not_erase_previous_plan(self):
        self.project()
        original = [{"id": "old", "title": "Cũ", "agent": "pm"}]
        self.db.replace_tasks("trip-1", original)
        before = self.db.get_tasks("trip-1")
        bad_plans = [
            [{"id": "a", "title": "A", "agent": "pm", "depends_on": ["missing"]}],
            [{"id": "a", "title": "A", "agent": "pm", "depends_on": ["b"]},
             {"id": "b", "title": "B", "agent": "researcher", "depends_on": ["a"]}],
        ]
        for plan in bad_plans:
            with self.assertRaises(ValueError):
                self.db.replace_tasks("trip-1", plan)
            self.assertEqual(self.db.get_tasks("trip-1"), before)
        self.project("trip-2")
        self.db.replace_tasks("trip-2", [{"id": "reserved", "title": "Khác", "agent": "pm"}])
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.replace_tasks("trip-1", [{"id": "reserved", "title": "Trùng", "agent": "pm"}])
        self.assertEqual(self.db.get_tasks("trip-1"), before)

    def test_artifact_round_trip_requires_provenance(self):
        self.project()
        artifact = self.artifact()
        self.assertEqual(self.db.add_artifact(artifact), artifact.id)
        self.assertEqual(Artifact.model_validate(self.db.get_artifact("trip-1", artifact.id)), artifact)
        self.assertEqual(len(self.db.get_artifacts("trip-1")), 1)
        self.assertIsNone(self.db.get_artifact("trip-2", artifact.id))
        invalid = artifact.model_dump()
        invalid.pop("source")
        with self.assertRaises(ValidationError):
            self.db.add_artifact(invalid)

    def test_duplicate_artifact_registration_is_idempotent_and_keeps_original_time(self):
        self.project()
        first=self.artifact();self.db.add_artifact(first)
        self.db.add_artifact(first)
        retried=self.artifact()
        self.db.add_artifact(retried)
        self.assertEqual(len(self.db.get_artifacts('trip-1')),1)
        self.assertEqual(Artifact.model_validate(self.db.get_artifact('trip-1',first.id)).created_at,first.created_at)

    def test_artifact_id_cannot_overwrite_other_project_or_file_or_provenance(self):
        self.project();self.project('trip-2')
        first=self.artifact();self.db.add_artifact(first)
        for changes in ({'project_id':'trip-2'},{'path':'another.json'},{'source':'different-source'},{'confidence':.1}):
            with self.assertRaisesRegex(ValueError,'different metadata'):
                self.db.add_artifact(first.model_copy(update=changes))
        self.assertEqual(Artifact.model_validate(self.db.get_artifact('trip-1',first.id)),first)
        self.assertEqual(self.db.get_artifacts('trip-2'),[])

    def test_concurrent_registration_of_same_artifact_creates_one_row(self):
        from concurrent.futures import ThreadPoolExecutor
        self.project();artifact=self.artifact()
        with ThreadPoolExecutor(max_workers=4) as pool:
            result=list(pool.map(lambda _:self.db.add_artifact(artifact),range(8)))
        self.assertEqual(result,[artifact.id]*8)
        self.assertEqual(len(self.db.get_artifacts('trip-1')),1)

    def test_connect_rolls_back_transaction_and_closes_file(self):
        self.project()
        with self.assertRaises(RuntimeError):
            with self.db.connect() as connection:
                connection.execute("UPDATE projects SET title=? WHERE id=?", ("Không commit", "trip-1"))
                raise RuntimeError("abort")
        self.assertEqual(self.db.get_project("trip-1")["title"], "Đà Nẵng 3 ngày")
        # Windows rejects a rename if this process left a SQLite connection open.
        renamed = self.path.with_name("renamed.db")
        self.path.rename(renamed)
        renamed.rename(self.path)

    def test_parameterized_sql_and_project_isolation(self):
        project_id = "trip'; DROP TABLE projects; --"
        self.project(project_id)
        self.project("other")
        self.db.add_event(project_id, "pm", "note", "O'Brien và tiếng Việt")
        self.assertEqual(self.db.get_project(project_id)["id"], project_id)
        self.assertEqual(len(self.db.list_projects()), 2)
        self.assertEqual(len(self.db.get_events("other")), 1)


if __name__ == "__main__":
    unittest.main()
