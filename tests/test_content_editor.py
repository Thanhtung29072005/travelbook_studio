import copy
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from pydantic import TypeAdapter, ValidationError

from backend.app.agents.content_editor import ContentEditorAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import ContentDocument, Fact, Text, TripRequest
from backend.app.services.router import NineRouterClient
from backend.app.task_plan import build_tasks
from scripts.check_content_editor import mock_draft

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


class ContentEditorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_models="content-primary,content-fallback",
            llm_cache_path=self.root / "cache.db", llm_max_attempts_per_model=1)
        self.calls, self.mutate, self.fail_primary = [], lambda value: value, False
        self.prompts = []
        def handler(request):
            payload = json.loads(request.content)
            self.calls.append(payload["model"])
            if self.fail_primary and payload["model"] == "content-primary":
                return httpx.Response(503)
            user = next(item["content"] for item in payload["messages"] if item["role"] == "user")
            self.prompts.append(user)
            data, _ = json.JSONDecoder().raw_decode(user)
            draft = self.mutate(mock_draft(data["context"]))
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(draft)}}]})
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.db = Database(self.root / "db.sqlite3")
        self.db.init()
        self.agent = ContentEditorAgent(self.db, NineRouterClient(self.settings, client=self.http), self.root / "outputs")
        read = lambda name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        self.days, self.places, self.budget = read("itinerary.json"), read("research.json"), read("budget.json")
        fact = lambda value: {"value": value, "source": "input:test", "confidence": 1}
        self.trip = TripRequest(destination=fact("Đà Nẵng"), days=fact(3), budget=fact("5000000"), currency=fact("VND"), travelers=fact(1))

    async def edit(self):
        return await self.agent.edit(self.trip, self.days, self.places, budget=self.budget)

    def state(self, project_id="content-test", ready=True):
        self.db.create_project(project_id, "content", "input", trip_request=self.trip)
        tasks = build_tasks(project_id)
        for task in tasks[:4]:
            task["status"] = "done" if ready else "todo"
        tasks[4]["status"] = "ready"
        self.db.replace_tasks(project_id, tasks)
        return {"project_id": project_id, "trip_request": self.trip, "itinerary": self.days, "places": self.places, "budget": self.budget}

    async def test_sections_flat_json_provenance_and_cache(self):
        result = await self.edit()
        self.assertEqual([day.day_number for day in result.document.days], [1, 2, 3])
        facts = result.document.as_facts()
        TypeAdapter(list[Fact[Text]]).validate_json(json.dumps([fact.model_dump(mode="json") for fact in facts]))
        self.assertTrue(all("llm:content-primary" in fact.source for fact in facts[:-1]))
        self.assertTrue(all(fact.confidence <= .6 for fact in facts[:-1]))
        self.assertTrue(all("chưa xác minh" in fact.value for fact in result.document.food + result.document.culture))
        self.assertTrue((await self.edit()).cache_hit)
        self.assertEqual(len(self.calls), 1)

    async def test_long_source_chains_are_not_duplicated_in_model_context(self):
        long_source=('https://example.org/original; '+('calculation:upstream-chain; '*300)).rstrip()
        def extend(value):
            if isinstance(value,dict):
                if {'value','source','confidence'} <= value.keys():
                    value['source']=long_source
                else:
                    for item in value.values(): extend(item)
            elif isinstance(value,list):
                for item in value: extend(item)
        extend(self.days)
        extend(self.places)
        extend(self.budget)
        self.assertGreater(len(json.dumps([self.days,self.places,self.budget])),180000)
        result=await self.edit()
        self.assertLess(len(self.prompts[0]),180000)
        self.assertIn(long_source,result.document.days[0].introduction.source)
        self.assertNotIn(long_source,self.prompts[0])

    async def test_available_local_food_evidence_is_used_without_generic_placeholder(self):
        def mutate(draft):
            draft['food']=[{'text':'Bún bò Huế là món bún địa phương.', 'evidence_ids':['input.research.0'], 'suggestion':False}]
            return draft
        self.mutate=mutate
        result=await self.agent.edit(self.trip,self.days,self.places,budget=self.budget,research=[
            {'value':'Bún bò Huế là món bún địa phương.', 'source':'https://vi.wikipedia.org/wiki/Hue; scope=destination-guide:food','confidence':.7}])
        self.assertIn('Bún bò Huế',result.document.food[0].value)
        self.assertNotIn('chưa xác minh',result.document.food[0].value)
        self.assertIn('scope=destination-guide:food',result.document.food[0].source)

    async def test_editor_retries_if_local_evidence_was_replaced_by_generic_advice(self):
        def mutate(draft):
            if len(self.calls)>1:
                draft['food']=[{'text':'Bún bò Huế là món bún địa phương.', 'evidence_ids':['input.research.0'], 'suggestion':False}]
            return draft
        self.mutate=mutate
        result=await self.agent.edit(self.trip,self.days,self.places,budget=self.budget,research=[
            {'value':'Bún bò Huế là món bún địa phương.', 'source':'https://vi.wikipedia.org/wiki/Hue; scope=destination-guide:food','confidence':.7}])
        self.assertEqual(len(self.calls),2)
        self.assertIn('Bún bò Huế',result.document.food[0].value)

    async def test_unknown_evidence_cannot_be_published(self):
        def mutate(draft):
            draft["food"][0]["evidence_ids"] = ["https://invented.example/source"]
            return draft
        self.mutate = mutate
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            await self.edit()

    async def test_unsupported_factual_claim_without_evidence_is_rejected(self):
        def mutate(draft):
            draft["culture"][0]["suggestion"] = False
            return draft
        self.mutate = mutate
        with self.assertRaisesRegex(ValueError, "requires input evidence"):
            await self.edit()

    async def test_missing_or_duplicate_day_is_rejected(self):
        def mutate(draft):
            draft["days"][1]["day_number"] = 1
            return draft
        self.mutate = mutate
        with self.assertRaisesRegex(ValueError, "content day"):
            await self.edit()

    async def test_bad_input_fails_before_model_call(self):
        with self.assertRaises(ValueError):
            await self.agent.edit(self.trip, self.days[:1], self.places)
        with self.assertRaises(ValueError):
            await self.agent.edit(self.trip, self.days, [])
        self.assertEqual(self.calls, [])

    async def test_model_fallback_uses_existing_router(self):
        self.fail_primary = True
        result = await self.edit()
        self.assertEqual(result.model, "content-fallback")
        self.assertEqual(self.calls, ["content-primary", "content-fallback"])

    async def test_run_writes_valid_artifacts_events_and_opens_visual_tasks(self):
        update = await self.agent.run(self.state())
        self.assertEqual(update["stage"], "visuals")
        self.assertFalse(update["production_approved"])
        paths = [Path(artifact.path) for artifact in update["artifacts"]]
        self.assertEqual(paths[0].name, "content.json")
        facts = TypeAdapter(list[Fact[Text]]).validate_json(paths[0].read_text(encoding="utf-8"))
        document = ContentDocument.model_validate_json(paths[1].read_text(encoding="utf-8"))
        self.assertEqual([fact.value for fact in facts], [fact.value for fact in document.as_facts()])
        tasks = {task["agent"]: task for task in self.db.get_tasks("content-test")}
        self.assertEqual(tasks["content_editor"]["status"], "done")
        for role in ("image_curator", "illustrator", "map_agent"):
            self.assertEqual(tasks[role]["status"], "ready")
        self.assertTrue(any(event["event_type"] == "content_ready" for event in self.db.get_events("content-test")))

    async def test_failed_generation_blocks_task_without_content_artifact(self):
        state = self.state()
        def mutate(draft):
            draft["days"].pop()
            return draft
        self.mutate = mutate
        with self.assertRaises(ValueError):
            await self.agent.run(state)
        self.assertEqual(self.db.get_project("content-test")["status"], "failed")
        self.assertFalse(list((self.root / "outputs").rglob("content.json")))
        tasks = {task["agent"]: task for task in self.db.get_tasks("content-test")}
        self.assertEqual(tasks["content_editor"]["status"], "blocked")
        self.assertEqual(tasks["image_curator"]["status"], "todo")

    async def test_unfinished_budget_dependency_does_not_call_llm(self):
        with self.assertRaises(ValueError):
            await self.agent.run(self.state(ready=False))
        self.assertEqual(self.calls, [])

    def test_shared_section_schema_rejects_invalid_confidence_and_empty_categories(self):
        with self.assertRaises(ValidationError):
            ContentDocument.model_validate({"introduction": {"value": "a", "source": "b", "confidence": 2}})
