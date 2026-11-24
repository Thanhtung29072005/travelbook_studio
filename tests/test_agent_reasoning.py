from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app.agents.researcher import ResearcherAgent
from backend.app.agents.planner import PlannerAgent
from backend.app.agents.image_curator import ImageCuratorAgent
from backend.app.agents.reviewer import ReviewerAgent
from backend.app.config import Settings
from backend.app.schemas import Place, TripRequest, WeatherReport
from backend.app.services.places import PlaceSearchResult
from backend.app.services.photos import PhotoService
from backend.app.services.routing import RoutingService
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.reasoning import DecisionEngine
from backend.app.services.router import StructuredResult
from scripts.check_agent_reasoning import decision_payload
from scripts.check_image_curator import mock_commons
from test_reviewer import valid_state


def fact(value):
    return {"value": value, "source": "input:reasoning-unit-test", "confidence": .8}


class ScriptedRouter:
    def __init__(self, mutate=None):
        self.calls, self.mutate = [], mutate

    async def chat_structured(self, **kwargs):
        context = json.loads(kwargs["prompt"])
        self.calls.append((kwargs["schema"].__name__, context, kwargs["system"]))
        data = decision_payload(context)
        if self.mutate:
            data = self.mutate(kwargs["schema"].__name__, context, data)
        return StructuredResult(data=kwargs["schema"].model_validate(data), model="unit-reasoner", cache_hit=False, cache_key="unit", usage={})


class ReasoningTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.settings = Settings(_env_file=None, routing_provider_order="haversine", photo_provider_order="wikimedia",
            data_cache_path=self.root / "api.db")
        self.trip = TripRequest(destination=fact("Test City"), days=fact(1), budget=fact("1000"), currency=fact("VND"), travelers=fact(1), traveler_ages=[fact(30)])
        self.places = [Place(id=id, name=fact("Museum " + id), latitude=fact(16 + index*.001), longitude=fact(108),
            opening_hours=fact("24/7"), visit_minutes=fact(30)) for index, id in enumerate(("a", "b"))]

    def researcher(self, router):
        owner = self
        class Search:
            settings = owner.settings
            def __init__(self):
                self.calls = []
            async def search(self, destination, interests, **kwargs):
                self.calls.append((destination, interests))
                return PlaceSearchResult(destination=owner.places[0], places=list(owner.places))
        class Weather:
            def __init__(self):
                self.calls = 0
            async def forecast(self, *args, **kwargs):
                self.calls += 1
                return WeatherReport(status="not_requested")
        search, weather = Search(), Weather()
        return ResearcherAgent(None, search, weather, self.root, router=router), search, weather

    async def test_researcher_llm_changes_tool_arguments_and_can_request_one_more_search(self):
        def mutate(name, context, data):
            if name == "ResearchFollowup":
                data.update(action="refine_search", search_interests=["museum"], priority_place_ids=["b", "a"])
            return data
        router = ScriptedRouter(mutate)
        agent, search, weather = self.researcher(router)
        result = await agent.research(self.trip)
        self.assertEqual(len(search.calls), 2)
        self.assertTrue(all(destination == "Test City" for destination, _ in search.calls))
        self.assertEqual(search.calls[1][1], ["museum"])
        self.assertEqual(weather.calls, 0)
        self.assertEqual([place.id for place in result.places], ["b", "a"])
        self.assertEqual([record.status for record in result.reasoning], ["llm", "llm"])
        self.assertTrue(all("Researcher" in system for _, _, system in router.calls))

    async def test_dated_trip_checks_weather_when_strategy_skips_it(self):
        self.trip = TripRequest.model_validate({**self.trip.model_dump(), "start_date": fact("2026-12-23")})
        agent, _, weather = self.researcher(ScriptedRouter())
        await agent.research(self.trip)
        self.assertEqual(weather.calls, 1)

    async def test_researcher_invented_priority_id_is_ignored_and_audited(self):
        def mutate(name, context, data):
            if name == "ResearchFollowup":
                data["priority_place_ids"] = ["invented"]
            return data
        agent, _, _ = self.researcher(ScriptedRouter(mutate))
        result = await agent.research(self.trip)
        self.assertEqual({place.id for place in result.places}, {"a", "b"})
        self.assertEqual(result.reasoning[-1].status, "fallback")

    async def planner(self, router):
        http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.addAsyncCleanup(http.aclose)
        return PlannerAgent(None, RoutingService(self.settings, PublicAPIClient(self.settings, client=http)), self.root, router=router)

    async def test_planner_llm_priority_changes_choice_but_times_still_valid(self):
        def mutate(name, context, data):
            data["day_groups"] = [["b", "a"]]
            return data
        agent = await self.planner(ScriptedRouter(mutate))
        result = await agent.plan(self.trip, self.places)
        self.assertEqual(result.itinerary[0].stops[0].place_id, "b")
        self.assertEqual(result.clusters, [["b", "a"]])
        self.assertEqual(result.reasoning[0].status, "llm")
        self.assertLess(result.itinerary[0].stops[0].departure.value, result.itinerary[0].stops[1].arrival.value)

    async def test_planner_unknown_or_duplicate_ids_cannot_replace_grounded_plan(self):
        agent = await self.planner(ScriptedRouter(lambda name, context, data: {"day_groups": [["a", "invented"]], "rationale": "invalid"}))
        result = await agent.plan(self.trip, self.places)
        self.assertEqual(result.reasoning[0].status, "fallback")
        self.assertNotIn("invented", [stop.place_id for day in result.itinerary for stop in day.stops])

    async def test_planner_llm_cannot_schedule_a_closed_venue(self):
        self.places[1].opening_hours = type(self.places[1].opening_hours).model_validate(fact("closed"))
        agent = await self.planner(ScriptedRouter(lambda name, context, data: {"day_groups": [["b", "a"]], "rationale": "test"}))
        result = await agent.plan(self.trip, self.places)
        self.assertNotIn("b", [stop.place_id for day in result.itinerary for stop in day.stops])

    async def curator(self, router):
        def handler(request):
            body = mock_commons(self.places[0].model_dump(mode="json"), 1)
            other = deepcopy(body["query"]["pages"][0])
            other["pageid"] = 2
            other["imageinfo"][0]["descriptionurl"] = "https://commons.wikimedia.org/wiki/File:MOCK_2.jpg"
            body["query"]["pages"].append(other)
            return httpx.Response(200, json=body)
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http.aclose)
        return ImageCuratorAgent(None, PhotoService(self.settings, PublicAPIClient(self.settings, client=http)), self.root, router=router)

    async def test_curator_llm_selects_non_default_candidate_without_rewriting_license(self):
        def mutate(name, context, data):
            if name == "PhotoChoice":
                data["photo_id"] = context["candidates"][-1]["photo_id"]
            return data
        agent = await self.curator(ScriptedRouter(mutate))
        result = await agent.curate(self.places[:1])
        self.assertIn("MOCK_2.jpg", str(result.photos[0].source_url.value))
        self.assertEqual(result.photos[0].license.value, "CC BY-SA 4.0")
        self.assertEqual(result.reasoning[-1].status, "llm")

    async def test_curator_unknown_id_falls_back_to_valid_candidate(self):
        def mutate(name, context, data):
            if name == "PhotoChoice":
                data["photo_id"] = "fake-image"
            return data
        agent = await self.curator(ScriptedRouter(mutate))
        result = await agent.curate(self.places[:1])
        self.assertNotEqual(result.photos[0].id, "fake-image")
        self.assertEqual(result.reasoning[-1].status, "fallback")

    async def test_curator_explicit_rejection_does_not_invent_replacement(self):
        def mutate(name, context, data):
            if name == "PhotoChoice":
                data["photo_id"] = None
            return data
        agent = await self.curator(ScriptedRouter(mutate))
        result = await agent.curate(self.places[:1])
        self.assertEqual(result.photos, [])
        self.assertEqual(result.missing_place_ids, ["a"])

    async def test_reviewer_llm_cannot_remove_code_budget_violation(self):
        state = valid_state()
        state["budget"]["items"][0]["amount"]["value"] = "1001"
        agent = ReviewerAgent(None, self.settings, self.root, router=ScriptedRouter())
        result = await agent.qualitative(state)
        self.assertEqual(result.review.verdict, "REVISE")
        self.assertTrue(any(issue.rule_code == "BUDGET_TOTAL" for issue in result.review.issues))

    async def test_reviewer_adds_supported_qualitative_issue_and_rejects_fake_reference(self):
        def mutate(name, context, data):
            data["issues"] = [{"category": "content", "severity": "major", "observation": "Cần giải thích nội dung theo số ngày.", "evidence_ids": ["input.trip_request.days"]}]
            return data
        agent = ReviewerAgent(None, self.settings, self.root, router=ScriptedRouter(mutate))
        result = await agent.qualitative(valid_state())
        self.assertEqual(result.review.verdict, "REVISE")
        self.assertEqual(result.review.issues[-1].target_agent, "content_editor")
        self.assertTrue(result.review.issues[-1].message.source.startswith("llm:"))
        def invalid(name, context, data):
            data = mutate(name, context, data)
            data["issues"][0]["evidence_ids"] = ["invented"]
            return data
        agent = ReviewerAgent(None, self.settings, self.root, router=ScriptedRouter(invalid))
        result = await agent.qualitative(valid_state())
        self.assertEqual(result.review.verdict, "PASS")
        self.assertEqual(result.reasoning[-1].status, "fallback")

    async def test_required_mode_does_not_silently_switch_to_code(self):
        settings = self.settings.model_copy(update={"agent_reasoning_required": True})
        class BrokenRouter:
            async def chat_structured(self, **kwargs):
                raise ValueError("invalid provider output")
        engine = DecisionEngine("planner", settings, router=BrokenRouter())
        from backend.app.agents.planner import PlannerProposal
        with self.assertRaises(RuntimeError):
            await engine.decide("test", "SYSTEM", PlannerProposal, {})
        self.assertEqual(engine.records[-1].status, "fallback")
