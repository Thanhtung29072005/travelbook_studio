import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy

import httpx
from PIL import Image
from pydantic import ValidationError

from backend.app.agents.reviewer import ReviewerAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Review, ReviewIssue
from backend.app.services.router import NineRouterClient
from backend.app.services.visual_review import VisualEvidence, VisualReviewDecision
from test_reviewer import valid_state, fact


def response(context):
    evidence = context["evidence"]
    ages = [key for key in evidence if key.startswith("input.trip_request.traveler_ages.")]
    assessments = []
    for task in context["tasks"]:
        refs = list(task["required_image_ids"])
        if task["rule"] == "photo_location":
            refs.append("input.places.0.name")
        if task["rule"] == "age_suitability":
            refs += ages
        assessments.append({"task_id": task["task_id"], "status": "pass", "classification": "warning",
            "observation": "Mock model assessment; not a verified visual conclusion", "evidence_ids": refs or ["policy:coverage"]})
    return {"assessments": assessments, "rationale": "Mock HTTP test of structured visual assessment"}


class VisualReviewTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_models="text-mock",
            nine_router_vision_models="vision-mock", llm_max_attempts_per_model=1, llm_retry_delay_seconds=0,
            llm_cache_path=self.root / "cache.db")
        self.state = valid_state()
        self.state["trip_request"]["traveler_ages"] = [fact(8)]
        self.state["content"] = [fact("Rest and visit the museum with a parent")]
        self.state["illustrations"] = []
        for index, color in enumerate(("red", "teal", "orange")):
            path = self.root / f"image{index}.png"
            Image.new("RGB", (200, 150), color).save(path)
            if index == 0:
                self.state["photos"][0]["local_path"] = str(path)
            else:
                self.state["illustrations"].append({"id": f"art{index}", "project_id": "visual-test", "agent_id": "illustrator",
                    "kind": "illustration", "name": f"Image {index}", "path": str(path), "mime_type": "image/png",
                    "source": "input:visual-test", "confidence": 1})
        self.calls, self.mutate = [], None
        def handler(request):
            payload = json.loads(request.content)
            self.calls.append(payload)
            content = payload["messages"][-1]["content"]
            text = content[0]["text"] if isinstance(content, list) else content
            context, _ = json.JSONDecoder().raw_decode(text)
            data = response(context) if "tasks" in context else {"issues": [], "rationale": "Mock text review"}
            if self.mutate and "tasks" in context:
                self.mutate(data)
            return httpx.Response(200, json={"choices": [{"finish_reason": "stop", "message": {"content": json.dumps(data)}}]})
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.db = Database(self.root / "db.sqlite3")
        self.db.init()
        self.router = NineRouterClient(self.settings, client=self.http)
        self.agent = ReviewerAgent(self.db, self.settings, self.root, router=self.router)

    async def test_actual_pixels_sent_and_replay_cached(self):
        first = await self.agent.visual_review(self.state)
        second = await self.agent.visual_review(self.state)
        self.assertEqual(first.review.verdict, "PASS")
        self.assertEqual(second.review.verdict, "PASS")
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.calls[0]["model"], "vision-mock")
        blocks = self.calls[0]["messages"][-1]["content"]
        pixels = [block for block in blocks if block["type"] == "image_url"]
        self.assertEqual(len(pixels), 3)
        self.assertTrue(all(block["image_url"]["url"].startswith("data:image/jpeg;base64,") for block in pixels))
        self.assertTrue(second.reasoning[-1].cache_hit)

    async def test_image_changes_invalidate_cache(self):
        await self.agent.visual_review(self.state)
        Image.new("RGB", (200, 150), "black").save(self.state["photos"][0]["local_path"])
        await self.agent.visual_review(self.state)
        self.assertEqual(len(self.calls), 2)

    async def test_three_error_types_have_classification_and_fixed_owner(self):
        def mutate(data):
            for item in data["assessments"]:
                if item["task_id"] in {"photo:photo1", "style", "age:itinerary"}:
                    item.update(status="fail", classification="warning" if item["task_id"] == "style" else "critical")
        self.mutate = mutate
        result = await self.agent.visual_review(self.state)
        issues = {issue.rule_code: issue for issue in result.review.issues}
        for code, owner, level in (("LLM_PHOTO_LOCATION", "image_curator", "critical"), ("LLM_STYLE_CONSISTENCY", "illustrator", "warning"), ("LLM_AGE_SUITABILITY", "planner", "critical")):
            self.assertEqual((issues[code].target_agent, issues[code].classification), (owner, level))
            self.assertTrue(issues[code].evidence_ids)
            self.assertIn("llm:", issues[code].message.source)
        self.assertEqual(Review.model_validate_json(result.review.model_dump_json()).verdict, "REVISE")

    async def test_warning_only_does_not_block_layout(self):
        self.mutate = lambda data: next(item for item in data["assessments"] if item["task_id"] == "style").update(status="fail", classification="warning")
        result = await self.agent.visual_review(self.state)
        self.assertEqual(result.review.verdict, "PASS")
        self.assertEqual(result.review.issues[0].severity, "minor")

    async def test_unknown_assessment_cannot_pass_as_warning(self):
        self.mutate = lambda data: next(item for item in data["assessments"] if item["task_id"] == "photo:photo1").update(status="unknown", classification="warning")
        result = await self.agent.visual_review(self.state)
        issue = next(issue for issue in result.review.issues if issue.rule_code == "LLM_PHOTO_LOCATION")
        self.assertEqual(issue.classification, "critical")
        self.assertEqual(result.review.verdict, "REVISE")

    async def test_invalid_photo_schema_still_returns_actionable_review(self):
        self.state["photos"][0].pop("author")
        result = await self.agent.visual_review(self.state)
        self.assertEqual(result.review.verdict, "REVISE")
        self.assertTrue(any(issue.rule_code == "SCHEMA_PHOTOS" for issue in result.review.issues))

    async def test_remote_source_streamed_without_redirects(self):
        image = Path(self.state["photos"][0]["local_path"]).read_bytes()
        seen = []
        def handler(request):
            seen.append(str(request.url))
            self.assertEqual(request.headers["User-Agent"], self.settings.data_user_agent)
            return httpx.Response(200, content=image, headers={"content-type": "image/png"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            loader = VisualEvidence(self.settings, self.root, client=client)
            raw = await loader.read(url="https://images.pexels.com/test.png", remote=True)
            thumbnail = await loader.read(url="https://thumb.wikimedia.org/test.png", remote=True)
        self.assertEqual(raw, image)
        self.assertEqual(thumbnail, image)
        self.assertEqual(len(seen), 2)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(302, headers={"location": "http://127.0.0.1/private"}))) as client:
            with self.assertRaises(httpx.HTTPStatusError):
                await VisualEvidence(self.settings, self.root, client=client).read(url="https://images.pexels.com/test.png", remote=True)

    async def test_missing_pixels_cannot_be_passed_by_model(self):
        self.state["photos"][0]["local_path"] = str(self.root / "missing.png")
        result = await self.agent.visual_review(self.state)
        issue = next(issue for issue in result.review.issues if issue.rule_code == "LLM_PHOTO_LOCATION")
        self.assertEqual(issue.classification, "critical")
        self.assertIn("unknown", issue.message.value)
        self.assertEqual(result.review.verdict, "REVISE")

    async def test_missing_ages_routes_to_pm(self):
        del self.state["trip_request"]["traveler_ages"]
        result = await self.agent.visual_review(self.state)
        age = [issue for issue in result.review.issues if issue.rule_code == "LLM_AGE_SUITABILITY"]
        self.assertTrue(age)
        self.assertTrue(all(issue.target_agent == "pm" for issue in age))

    async def test_fake_reference_or_omitted_task_rejected(self):
        for mode in ("ref", "omit", "pixels"):
            with self.subTest(mode=mode):
                def mutate(data):
                    if mode == "ref": data["assessments"][0]["evidence_ids"] = ["invented"]
                    elif mode == "omit": data["assessments"].pop()
                    else: next(item for item in data["assessments"] if item["task_id"] == "style")["evidence_ids"] = ["policy:style"]
                self.mutate = mutate
                self.settings.llm_cache_enabled = False
                result = await self.agent.visual_review(self.state)
                self.assertEqual(result.reasoning[-1].status, "fallback")
                self.assertEqual(result.review.verdict, "REVISE")

    async def test_all_pass_cannot_clear_code_budget_failure(self):
        self.state["budget"]["items"][0]["amount"] = fact("2000")
        result = await self.agent.visual_review(self.state)
        self.assertEqual(result.review.verdict, "REVISE")
        self.assertTrue(any(issue.target_agent == "budget" for issue in result.review.issues))

    async def test_artifact_limit_is_unknown_not_silent_sampling(self):
        self.agent.visual_evidence.settings.reviewer_max_images = 1
        result = await self.agent.visual_review(self.state)
        self.assertEqual(result.review.verdict, "REVISE")
        self.assertTrue(any("batch limit" in issue.message.value for issue in result.review.issues))

    async def test_outside_root_and_untrusted_remote_sources_rejected(self):
        loader = VisualEvidence(self.settings, self.root)
        with self.assertRaises(ValueError):
            await loader.read(Path(__file__))
        for url in ("http://127.0.0.1/image.jpg", "https://example.com/x.png", "https://images.pexels.com:8443/a.jpg"):
            with self.assertRaises(ValueError):
                await loader.read(url=url, remote=True)

    def test_classification_must_match_severity(self):
        with self.assertRaises(ValidationError):
            ReviewIssue(category="photo", severity="major", classification="warning", message=fact("Mismatch"), target_agent="image_curator")
