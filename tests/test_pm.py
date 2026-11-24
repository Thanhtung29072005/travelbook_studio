"""PM extraction tests use real router/schema logic with simulated HTTP replies."""

from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app.agents.pm import PMAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import TripRequest
from backend.app.services.router import NineRouterClient
from backend.app.task_plan import DEPENDENCIES


def field(value, evidence):
    return {"value": value, "evidence": evidence, "confidence": 0.9}


PROMPT = "Đi Đà Nẵng 3 ngày, 2 người, tổng ngân sách 5 triệu đồng, thích biển, khởi hành 26/11/2026."


def draft():
    return {"destination": field("Đà Nẵng", "Đà Nẵng"), "days": field(3, "3 ngày"),
            "budget": field("5000000", "5 triệu đồng"), "currency": field("VND", "đồng"),
            "travelers": field(2, "2 người"), "start_date": field("2026-11-26", "26/11/2026"), "budget_scope": "total", "interests": [field("biển", "biển")]}


class PMTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.db = Database(root / "test.db")
        self.db.init()
        self.settings = Settings(_env_file=None, nine_router_api_key="test-only", nine_router_models="extractor",
                                 llm_max_attempts_per_model=1, llm_cache_path=root / "cache.sqlite3")
        self.payload = draft()
        self.calls = 0
        def handler(request):
            self.calls += 1
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps(self.payload, ensure_ascii=False)}}], "usage": {"prompt_tokens": 10, "completion_tokens": 20}})
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.agent = PMAgent(self.db, NineRouterClient(self.settings, client=self.http), root / "artifacts")

    async def test_free_text_returns_valid_trip_schema_and_fixed_tasks(self):
        result = await self.agent.normalize(PROMPT, reference_date=date(2026, 11, 21))
        self.assertEqual(result.status, "ready")
        trip = TripRequest.model_validate(result.trip_request.model_dump(mode="json"))
        self.assertEqual(trip.destination.value, "Đà Nẵng")
        self.assertEqual(trip.days.value, 3)
        self.assertEqual(trip.budget.value, 5000000)
        self.assertEqual(trip.travelers.value, 2)
        self.assertEqual(len(result.tasks), 11)
        self.assertTrue(trip.budget.source.startswith("user:sha256:"))
        for task, (node, dependencies) in zip(result.tasks, DEPENDENCIES.items()):
            self.assertEqual(task.id, f"draft:{node}")
            self.assertEqual(task.depends_on, [f"draft:{item}" for item in dependencies])

    async def test_same_request_uses_router_cache(self):
        first = await self.agent.normalize(PROMPT, reference_date=date(2026, 11, 21))
        second = await self.agent.normalize(PROMPT, reference_date=date(2026, 11, 21))
        self.assertFalse(first.cache_hit)
        self.assertTrue(second.cache_hit)
        self.assertEqual(first.trip_request, second.trip_request)
        self.assertEqual(self.calls, 1)

    async def test_intercity_request_asks_journey_times(self):
        self.payload['departure']=field('Hà Nội','Hà Nội')
        result=await self.agent.normalize(PROMPT+' Từ Hà Nội.')
        self.assertEqual(result.status,'needs_clarification')
        self.assertEqual(len(result.questions),1)
        self.assertTrue(any('giờ đến' in q.value and 'giờ về' in q.value for q in result.questions))
        self.assertEqual(result.tasks,[])

    async def test_two_destination_times_are_enough_despite_old_model_questions(self):
        self.payload['departure']=field('Hà Nội','Hà Nội')
        self.payload['questions']=['Bạn về đến Hà Nội lúc mấy giờ?', 'Phương tiện đi lại là gì?']
        result=await self.agent.normalize(PROMPT+' Từ Hà Nội. Đến 10h, về 18h.')
        self.assertEqual(result.status,'ready')
        trip=result.trip_request
        self.assertEqual(trip.destination_arrival_at.value.isoformat(),'2026-11-26T10:00:00+07:00')
        self.assertEqual(trip.destination_departure_at.value.isoformat(),'2026-11-28T18:00:00+07:00')
        self.assertIsNone(trip.outbound_departure_at)
        self.assertIsNone(trip.return_arrival_at)

    async def test_partial_arrival_is_kept_and_only_return_time_is_asked(self):
        self.payload['departure']=field('Hà Nội','Hà Nội')
        first=await self.agent.normalize(PROMPT+' Từ Hà Nội. Đến 10h.')
        self.assertEqual(first.status,'needs_clarification')
        self.assertEqual(len(first.questions),1)
        self.assertNotIn('giờ đến',first.questions[0].value)
        self.payload={'questions':['Bạn đến Đà Nẵng lúc mấy giờ?']}
        second=await self.agent.normalize('Về 6h chiều.',partial_request=first.partial_request)
        self.assertEqual(second.status,'ready')
        self.assertEqual(second.trip_request.destination_arrival_at.value.hour,10)
        self.assertEqual(second.trip_request.destination_departure_at.value.hour,18)

    async def test_latest_short_answer_overrides_old_conflicting_times(self):
        self.payload['departure']=field('Hà Nội','Hà Nội')
        self.payload['destination_arrival_at']=field('2026-11-26T18:00:00+07:00','đến 18h')
        self.payload['return_arrival_at']=field('2026-11-28T10:00:00+07:00','về đến Hà Nội 10h')
        result=await self.agent.normalize(PROMPT+' Từ Hà Nội, đến 18h, về đến Hà Nội 10h.\nAdditional user answer:\nĐến 10:00, về 18:00.')
        self.assertEqual(result.status,'ready')
        self.assertEqual(result.trip_request.destination_arrival_at.value.hour,10)
        self.assertEqual(result.trip_request.destination_departure_at.value.hour,18)

    async def test_explicit_full_destination_dates_are_preserved(self):
        self.payload['departure']=field('Hà Nội','Hà Nội')
        self.payload['destination_arrival_at']=field('2026-11-27T00:30:00+07:00','đến ngày 27/11 lúc 00:30')
        self.payload['destination_departure_at']=field('2026-11-28T18:00:00+07:00','rời ngày 28/11 lúc 18:00')
        result=await self.agent.normalize(PROMPT+' Từ Hà Nội, đến ngày 27/11 lúc 00:30, rời ngày 28/11 lúc 18:00.')
        self.assertEqual(result.status,'ready')
        self.assertEqual(result.trip_request.destination_arrival_at.value.day,27)

    async def test_explicit_flexible_ticket_times_allow_provisional_plan(self):
        self.payload['departure']=field('Hà Nội','Hà Nội')
        self.payload['transport_times_flexible']=field(True,'chưa chốt vé, dùng lịch tạm')
        result=await self.agent.normalize(PROMPT+' Từ Hà Nội, chưa chốt vé, dùng lịch tạm.')
        self.assertEqual(result.status,'ready')
        self.assertIsNone(result.trip_request.destination_arrival_at)
        self.assertTrue(result.trip_request.transport_times_flexible.value)

    async def test_missing_departure_date_always_asks_even_if_model_has_no_questions(self):
        self.payload.pop("start_date")
        self.payload["questions"] = []
        result = await self.agent.normalize(PROMPT.replace(", khởi hành 26/11/2026", ""))
        self.assertEqual(result.status, "needs_clarification")
        self.assertIsNone(result.trip_request)
        self.assertEqual(result.tasks, [])
        self.assertTrue(any("khởi hành" in item.value for item in result.questions))

    async def test_date_answer_keeps_previously_confirmed_trip_values(self):
        fields = {name: {"value": item["value"], "source": "user:confirmed", "confidence": 1}
                  for name, item in draft().items() if isinstance(item, dict) and name != "start_date"}
        request = TripRequest.model_validate(fields)
        self.payload = {"start_date": field("2026-12-23", "23/12/2026")}
        result = await self.agent.normalize("Khởi hành 23/12/2026", confirmed_request=request)
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.trip_request.start_date.value, date(2026, 12, 23))
        self.assertEqual(result.trip_request.destination, request.destination)
        self.assertEqual(result.trip_request.budget, request.budget)
        self.assertEqual(result.trip_request.travelers, request.travelers)

    async def test_missing_information_questions_without_fabricated_trip(self):
        self.payload = {"destination": field("Đà Nẵng", "Đà Nẵng")}
        result = await self.agent.normalize("Tôi muốn đi Đà Nẵng")
        self.assertEqual(result.status, "needs_clarification")
        self.assertIsNone(result.trip_request)
        self.assertEqual(result.tasks, [])
        self.assertGreaterEqual(len(result.questions), 4)

    async def test_per_person_budget_is_calculated_with_decimal(self):
        self.payload = draft()
        self.payload.update(budget=field("2500000.25", "2.500.000,25 đồng mỗi người"), budget_scope="per_person")
        result = await self.agent.normalize("Đà Nẵng 3 ngày, 2 người, 2.500.000,25 đồng mỗi người, thích biển, khởi hành 26/11/2026")
        self.assertEqual(str(result.trip_request.budget.value), "5000000.50")
        self.assertTrue(result.trip_request.budget.source.startswith("calculation:"))

    async def test_unsupported_evidence_cannot_reach_trip_request(self):
        self.payload["destination"] = field("Huế", "Huế")
        with self.assertRaisesRegex(ValueError, "evidence"):
            await self.agent.normalize(PROMPT)
        self.assertEqual(self.db.list_projects(), [])

    async def test_currency_not_explicitly_stated_needs_confirmation(self):
        self.payload["budget"] = field("5000000", "5 triệu")
        self.payload["currency"] = field("VND", "5 triệu")
        result = await self.agent.normalize("Đà Nẵng 3 ngày, 2 người, tổng ngân sách 5 triệu, thích biển, khởi hành 26/11/2026")
        self.assertEqual(result.status, "needs_clarification")
        self.assertTrue(any("tiền tệ" in item.value for item in result.questions))

    async def test_partial_child_age_is_preserved_without_inventing_adult_ages(self):
        self.payload["travelers"] = field(3, "2 người lớn và 1 bé 5 tuổi")
        self.payload["traveler_ages"] = [field(5, "5 tuổi")]
        result = await self.agent.normalize("Đà Nẵng 3 ngày, 2 người lớn và 1 bé 5 tuổi, tổng ngân sách 5 triệu đồng, thích biển, khởi hành 26/11/2026")
        self.assertEqual(result.status, "ready")
        self.assertEqual(result.trip_request.traveler_ages, [])
        self.assertTrue(any("5 tuổi" in item.value for item in result.trip_request.requirements))

    async def test_conflicting_dates_require_clarification(self):
        self.payload["start_date"] = field("2026-12-01", "01/12/2026")
        self.payload["end_date"] = field("2026-12-02", "02/12/2026")
        result = await self.agent.normalize(PROMPT + " Từ 01/12/2026 đến 02/12/2026.")
        self.assertEqual(result.status, "needs_clarification")
        self.assertIsNone(result.trip_request)

    async def test_run_persists_trip_plan_tasks_and_model_events(self):
        self.db.create_project("pm-test", "Test PM", PROMPT)
        state = await self.agent.run({"project_id": "pm-test", "user_prompt": PROMPT})
        project = self.db.get_project("pm-test")
        self.assertEqual(project["status"], "queued")
        self.assertEqual(project["stage"], "researcher")
        self.assertEqual(TripRequest.model_validate(project["trip_request"]), state["trip_request"])
        tasks = self.db.get_tasks("pm-test")
        self.assertEqual(len(tasks), 11)
        self.assertEqual(tasks[0]["status"], "done")
        self.assertEqual(tasks[1]["status"], "ready")
        self.assertEqual(len(self.db.get_artifacts("pm-test")), 2)
        self.assertTrue(all(Path(artifact.path).is_file() for artifact in state["artifacts"]))
        self.assertFalse(state["fixture_mode"])
        self.assertTrue(any(event["event_type"] == "model_call" for event in self.db.get_events("pm-test")))

    async def test_incomplete_run_waits_for_input_without_dispatching_tasks(self):
        self.payload = {"destination": field("Đà Nẵng", "Đà Nẵng")}
        self.db.create_project("pm-incomplete", "Test", "Đi Đà Nẵng")
        state = await self.agent.run({"project_id": "pm-incomplete", "user_prompt": "Đi Đà Nẵng"})
        self.assertIsNone(state["trip_request"])
        self.assertEqual(self.db.get_project("pm-incomplete")["status"], "awaiting_input")
        self.assertEqual(self.db.get_tasks("pm-incomplete"), [])
        self.assertTrue(state["clarification_questions"])


if __name__ == "__main__":
    unittest.main()
