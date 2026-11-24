from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from backend.app.agents.reviewer import ReviewerAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Review
from backend.app.task_plan import build_tasks


def fact(value, source="input:reviewer-unit-test", confidence=1):
    return {"value": value, "source": source, "confidence": confidence}


def valid_state():
    # Synthetic unit-test inputs with valid metadata shape; not a real photo verification.
    state = {"trip_request": {"destination": fact("Test City"), "days": fact(1), "budget": fact("1000"), "currency": fact("VND"),
        "travelers": fact(1), "start_date": fact("2026-12-07")},
        "places": [{"id": "a", "name": fact("Museum"), "latitude": fact(16), "longitude": fact(108), "opening_hours": fact("Mo-Fr 09:00-17:00; Sa-Su off")}],
        "itinerary": [{"day_number": 1, "date": fact("2026-12-07"), "stops": [{"place_id": "a", "arrival": fact("09:00"),
            "departure": fact("10:00"), "distance_km": fact(0), "travel_minutes": fact(0)}]}],
        "budget": {"currency": fact("VND"), "limit": fact("1000"), "items": [{"id": "tickets", "category": "tickets", "description": fact("Ticket"),
            "amount": fact("999.99"), "price_kind": "sourced", "day_number": 1}]},
        "photos": [{"id": "photo1", "place_id": "a", "provider": "wikimedia", "url": fact("https://upload.wikimedia.org/wikipedia/commons/a/a0/Unit_test.jpg"),
            "source_url": fact("https://commons.wikimedia.org/wiki/File:Unit_test.jpg"), "author": fact("Test photographer"),
            "license": fact("CC BY-SA 4.0"), "license_url": fact("https://creativecommons.org/licenses/by-sa/4.0/")}],
        "fixture_mode": False}
    from backend.app.schemas import TripRequest
    from backend.app.services.day_schedule import day_frame
    state['itinerary'][0]['activities'] = [item.model_dump(mode='json') for item in day_frame(
        TripRequest.model_validate(state['trip_request']),None,480,1260)[2]]
    return state


class ReviewerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "test.db")
        self.db.init()
        self.settings = Settings(_env_file=None, reviewer_visual_enabled=False)
        self.agent = ReviewerAgent(self.db, self.settings, self.root / "outputs")
        self.state = valid_state()

    def codes(self):
        result = self.agent.evaluate(self.state)
        return result, {issue.rule_code for issue in result.review.issues}

    def seed(self, ready=True):
        self.db.create_project("review-test", "review", "input")
        tasks = build_tasks("review-test")
        for task in tasks[:8]:
            task["status"] = "done" if ready else "todo"
        tasks[8]["status"] = "ready"
        self.db.replace_tasks("review-test", tasks)
        self.state["project_id"] = "review-test"

    def test_valid_rule_inputs_pass_without_proving_real_world_authenticity(self):
        result = self.agent.evaluate(self.state)
        self.assertEqual(result.review.verdict, "PASS")
        self.assertEqual(result.review.issues, [])
        self.assertTrue(all(check.status == "pass" for check in result.checks))
        self.state["photos"][0]["author"] = fact("Anna Mock")
        self.assertEqual(self.agent.evaluate(self.state).review.verdict, "PASS")

    def test_official_price_claim_requires_matching_evidence_and_arithmetic(self):
        item = self.state['budget']['items'][0]
        item['amount'] = fact('100', source='https://museum.example/tickets; official-admission')
        self.assertIn('BUDGET_PRICE_EVIDENCE', self.codes()[1])
        item['price_evidence'] = {'amount': '100', 'currency': 'VND', 'audience': 'adult',
            'source_url': 'https://museum.example/tickets', 'retrieved_at': '2026-11-23T00:00:00+00:00',
            'excerpt': 'Vé người lớn: 100 đồng/người', 'conditions': 'Test only'}
        self.assertNotIn('BUDGET_PRICE_EVIDENCE', self.codes()[1])
        item['amount']['value'] = '101'
        self.assertIn('BUDGET_PRICE_EVIDENCE', self.codes()[1])

    def test_closed_day_or_arrival_before_open_or_departure_after_close(self):
        original = deepcopy(self.state)
        for change in ("closed", "early", "late", "seconds"):
            with self.subTest(change=change):
                self.state = deepcopy(original)
                self.state['itinerary'][0]['activities'] = []
                if change == "closed":
                    self.state["places"][0]["opening_hours"] = fact("Mo off; Tu-Su 09:00-17:00")
                elif change == "early":
                    self.state["itinerary"][0]["stops"][0]["arrival"] = fact("08:59")
                elif change == "seconds":
                    self.state["places"][0]["opening_hours"] = fact("09:00-10:00")
                    self.state["itinerary"][0]["stops"][0]["departure"] = fact("10:00:01")
                else:
                    self.state["itinerary"][0]["stops"][0]["departure"] = fact("17:01")
                result, codes = self.codes()
                self.assertEqual(result.review.verdict, "REVISE")
                self.assertIn("OPENING_WINDOW", codes)

    def test_visit_cannot_span_lunch_closure(self):
        self.state['itinerary'][0]['activities'] = []
        self.state["places"][0]["opening_hours"] = fact("09:00-12:00,13:00-17:00")
        self.state["itinerary"][0]["stops"][0].update(arrival=fact("11:30"), departure=fact("13:30"))
        self.assertIn("OPENING_WINDOW", self.codes()[1])

    def test_missing_meals_and_rest_are_sent_to_planner(self):
        self.state['itinerary'][0]['activities'] = []
        result,codes=self.codes()
        self.assertTrue({'DAILY_LUNCH','DAILY_REST','DAILY_DINNER','DAILY_HOTEL'} <= codes)
        self.assertEqual(result.review.verdict,'REVISE')
        self.assertTrue(all(issue.target_agent=='planner' for issue in result.review.issues if issue.rule_code.startswith('DAILY_')))

    def test_reviewer_rejects_visits_before_actual_arrival(self):
        self.state['trip_request']['destination_arrival_at']=fact('2026-12-07T11:00:00+07:00')
        self.assertIn('JOURNEY_BOUNDARY',self.codes()[1])

    def test_missing_date_hours_or_unsupported_expression_are_unknown(self):
        self.state["trip_request"].pop("start_date")
        self.state["itinerary"][0].pop("date")
        self.assertIn("OPENING_DATE_REQUIRED", self.codes()[1])
        self.state["places"][0]["opening_hours"] = None
        self.assertIn("OPENING_HOURS_MISSING", self.codes()[1])
        self.state["places"][0]["opening_hours"] = fact("PH off; Mo-Su 09:00-17:00")
        self.assertIn("OPENING_HOURS_UNSUPPORTED", self.codes()[1])

    def test_request_date_can_be_derived_but_mismatching_date_is_rejected(self):
        self.state["itinerary"][0].pop("date")
        self.assertEqual(self.codes()[0].review.verdict, "PASS")
        self.state["itinerary"][0]["date"] = fact("2026-12-08")
        self.assertIn("DAY_DATE_MISMATCH", self.codes()[1])

    def test_decimal_budget_exact_limit_and_one_cent_over(self):
        self.state["budget"]["items"][0]["amount"] = fact("1000.00")
        self.assertNotIn("BUDGET_TOTAL", self.codes()[1])
        self.state["budget"]["items"][0]["amount"] = fact("1000.01")
        self.assertIn("BUDGET_TOTAL", self.codes()[1])

    def test_budget_cannot_raise_limit_or_change_currency(self):
        self.state["budget"]["limit"] = fact("999999")
        self.assertIn("BUDGET_LIMIT", self.codes()[1])
        self.state["budget"]["currency"] = fact("USD")
        self.assertIn("BUDGET_CURRENCY", self.codes()[1])

    def test_empty_budget_and_out_of_trip_day_are_not_approved(self):
        self.state["budget"]["items"][0]["day_number"] = 2
        self.assertIn("BUDGET_DAY", self.codes()[1])
        self.state["budget"]["items"] = []
        self.assertIn("BUDGET_EMPTY", self.codes()[1])

    def test_daily_distance_sum_limit_and_missing_leg(self):
        stop = deepcopy(self.state["itinerary"][0]["stops"][0])
        stop.update(arrival=fact("11:00"), departure=fact("12:00"), distance_km=fact(101))
        self.state["itinerary"][0]["stops"].append(stop)
        self.assertIn("DAILY_DISTANCE", self.codes()[1])
        stop["distance_km"] = None
        result, codes = self.codes()
        self.assertIn("DAILY_DISTANCE", codes)
        self.assertTrue(any(check.code == "DAILY_DISTANCE" and check.status == "unknown" for check in result.checks))

    def test_transport_policy_and_haversine_warning(self):
        self.agent.settings = Settings(_env_file=None, routing_mode="walking")
        self.state["itinerary"][0]["stops"][0]["distance_km"] = fact(16)
        self.assertIn("DAILY_DISTANCE", self.codes()[1])
        self.agent.settings = Settings(_env_file=None, reviewer_max_daily_distance_km=20)
        self.state["itinerary"][0]["stops"][0]["distance_km"] = fact(16, "estimate:haversine", .25)
        result, codes = self.codes()
        self.assertIn("DISTANCE_ESTIMATED", codes)
        self.assertEqual(result.review.verdict, "PASS")

    def test_missing_duplicate_or_generated_photo_is_rejected(self):
        photo = self.state["photos"][0]
        photo["url"]["source"] = "generation:model-illustrator"
        self.assertIn("PHOTO_METADATA", self.codes()[1])
        self.state["photos"].append(deepcopy(photo))
        self.assertIn("PHOTO_DUPLICATE_ID", self.codes()[1])
        self.assertIn("PHOTO_COVERAGE", self.codes()[1])
        self.state["photos"] = []
        self.assertIn("PHOTO_COVERAGE", self.codes()[1])

    def test_unknown_license_url_mismatch_zero_confidence_and_bad_local_file(self):
        original = deepcopy(self.state)
        for change in ("license", "author", "confidence", "file", "url", "root_page"):
            with self.subTest(change=change):
                self.state = deepcopy(original)
                photo = self.state["photos"][0]
                if change == "license":
                    photo["license_url"] = fact("https://creativecommons.org/licenses/by-nc/4.0/")
                elif change == "author":
                    photo["author"] = fact("unknown")
                elif change == "confidence":
                    photo["license"]["confidence"] = 0
                elif change == "file":
                    photo["local_path"] = str(self.root / "missing.jpg")
                elif change == "url":
                    photo["source_url"] = fact("https://example.com/photo")
                else:
                    photo["source_url"] = fact("https://commons.wikimedia.org/")
                self.assertIn("PHOTO_METADATA", self.codes()[1])

    def test_pexels_and_unsplash_metadata_require_author_link(self):
        for provider, page, image, name, license_url in (("pexels", "https://www.pexels.com", "https://images.pexels.com/photo.jpg", "Pexels License", "https://www.pexels.com/license/"),
                ("unsplash", "https://unsplash.com", "https://images.unsplash.com/photo-id", "Unsplash License", "https://unsplash.com/license")):
            with self.subTest(provider=provider):
                photo = self.state["photos"][0]
                photo.update(provider=provider, url=fact(image), source_url=fact(page + ("/photos/id" if provider == "unsplash" else "/photo/id")), license=fact(name), license_url=fact(license_url))
                photo.pop("author_url", None)
                self.assertIn("PHOTO_METADATA", self.codes()[1])
                photo["author_url"] = fact(page + "/@author")
                self.assertNotIn("PHOTO_METADATA", self.codes()[1])

    def test_schema_errors_are_actionable_review_issues(self):
        self.state["itinerary"][0]["stops"][0]["departure"] = fact("08:00")
        self.state["photos"][0].pop("license")
        result, codes = self.codes()
        self.assertIn("SCHEMA_ITINERARY", codes)
        self.assertIn("SCHEMA_PHOTOS", codes)
        self.assertEqual(result.review.verdict, "REVISE")

    def test_fixture_flag_or_embedded_fixture_source_cannot_pass(self):
        self.state["fixture_mode"] = True
        self.assertIn("INPUT_UNVERIFIED", self.codes()[1])
        self.state["fixture_mode"] = False
        self.state["places"][0]["name"]["source"] = "derived:fixture:test"
        self.assertIn("INPUT_UNVERIFIED", self.codes()[1])

    async def test_run_pass_saves_schema_and_opens_layout(self):
        self.seed()
        result = await self.agent.run(self.state)
        self.assertEqual(result["stage"], "layout_editor")
        self.assertFalse(result["production_approved"])
        Review.model_validate_json(Path(result["artifacts"][0].path).read_text(encoding="utf-8"))
        tasks = {task["agent"]: task for task in self.db.get_tasks("review-test")}
        self.assertEqual(tasks["layout_editor"]["status"], "ready")
        self.assertEqual(tasks["reviewer"]["status"], "done")

    async def test_run_revise_blocks_layout_and_targets_responsible_agent(self):
        self.seed()
        self.state["budget"]["items"][0]["amount"] = fact("1001")
        result = await self.agent.run(self.state)
        self.assertEqual(result["review"].verdict, "REVISE")
        self.assertEqual(result["revision_target"], "budget")
        self.assertEqual(result["stage"], "needs_attention")
        tasks = {task["agent"]: task for task in self.db.get_tasks("review-test")}
        self.assertEqual(tasks["layout_editor"]["status"], "blocked")
        self.assertTrue(any(event["event_type"] == "review_revise" for event in self.db.get_events("review-test")))

    async def test_visual_dependencies_must_finish_before_review(self):
        self.seed(ready=False)
        with self.assertRaises(ValueError):
            await self.agent.run(self.state)
        self.assertFalse((self.root / "outputs").exists())
