from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest

from pydantic import ValidationError

from backend.app.agents.budget import BudgetAgent
from backend.app.db import Database
from backend.app.schemas import Budget, TripRequest
from backend.app.task_plan import build_tasks


def fact(value, confidence=1, source="input:budget-unit-test"):
    return {"value": value, "source": source, "confidence": confidence}


def item(id, amount, price_kind="sourced", source="receipt:test", confidence=.8):
    return {"id": id, "category": "food", "description": fact("Full line cost"), "amount": fact(amount, confidence, source),
        "price_kind": price_kind, "day_number": 1}


class BudgetMathTests(unittest.TestCase):
    def setUp(self):
        self.agent = BudgetAgent(None, Path(tempfile.gettempdir()))
        self.trip = TripRequest(destination=fact("Test City"), days=fact(1), budget=fact("1.00"), currency=fact("USD"), travelers=fact(3))
        self.items = [item("a", "0.10"), item("b", "0.20", "estimated", "estimate:meal", .3)]

    def test_exact_decimal_total_split_and_signed_variance(self):
        result = self.agent.calculate(self.trip, self.items)
        budget = result.budget
        self.assertEqual(budget.total.value, Decimal("0.30"))
        self.assertEqual(budget.sourced_total.value, Decimal("0.10"))
        self.assertEqual(budget.estimated_total.value, Decimal("0.20"))
        self.assertEqual(budget.variance.value, Decimal("-0.70"))
        self.assertEqual(budget.remaining.value, Decimal("0.70"))
        self.assertTrue(budget.within_budget.value)
        self.assertEqual(budget.total.confidence, .3)
        self.assertEqual(budget.variance.confidence, .3)
        self.assertEqual(result.status, "ready")
        # traveler count does not multiply amounts already declared as full line totals.
        self.assertEqual(budget.total.value, Decimal("0.30"))

    def test_equal_limit_and_over_budget(self):
        equal = self.agent.calculate(self.trip, [item("a", "1.00")]).budget
        self.assertEqual(equal.variance.value, 0)
        self.assertTrue(equal.within_budget.value)
        result = self.agent.calculate(self.trip, [item("a", "1.01")])
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(result.budget.variance.value, Decimal(".01"))
        self.assertEqual(result.budget.remaining.value, Decimal("-.01"))

    def test_export_round_trip_accepts_computed_fields_but_not_tampering(self):
        budget = self.agent.calculate(self.trip, self.items).budget
        raw = json.loads(budget.model_dump_json())
        self.assertEqual(Budget.model_validate(raw).total.value, Decimal(".30"))
        for name, value in (("total", "999"), ("sourced_total", "0"), ("estimated_total", ".19"), ("variance", "100"), ("remaining", "0"), ("within_budget", False)):
            with self.subTest(name=name):
                altered = deepcopy(raw)
                altered[name]["value"] = value
                with self.assertRaises(ValidationError):
                    Budget.model_validate(altered)
        raw["total"]["confidence"] = 1
        with self.assertRaises(ValidationError):
            Budget.model_validate(raw)

    def test_legacy_budget_without_totals_still_valid_and_unknown_fields_rejected(self):
        data = {"currency": fact("USD"), "limit": fact("1"), "items": self.items}
        self.assertEqual(Budget.model_validate(data).total.value, Decimal(".30"))
        data["mystery"] = "value"
        with self.assertRaises(ValidationError):
            Budget.model_validate(data)

    def test_obviously_unsourced_claims_are_downgraded_without_changing_amount(self):
        for source in ("fixture:test", "mock:test", "llm:price", "estimate:taxi", "assumption:hotel"):
            with self.subTest(source=source):
                result = self.agent.calculate(self.trip, [item("a", ".5", "sourced", source)])
                self.assertEqual(result.budget.items[0].price_kind, "estimated")
                self.assertEqual(result.budget.items[0].amount.source, source)
                self.assertEqual(result.budget.sourced_total.value, 0)
                self.assertEqual(result.budget.estimated_total.value, Decimal(".5"))
                self.assertTrue(result.warnings)

    def test_empty_list_not_approved_as_free_trip(self):
        result = self.agent.calculate(self.trip, [])
        self.assertEqual(result.status, "needs_attention")
        self.assertEqual(result.budget.total.value, 0)

    def test_negative_nan_duplicate_and_outside_day_rejected(self):
        for amount in ("-1", "NaN", "Infinity"):
            with self.assertRaises(ValidationError):
                self.agent.calculate(self.trip, [item("a", amount)])
        with self.assertRaises(ValidationError):
            self.agent.calculate(self.trip, [item("a", ".1"), item("a", ".2")])
        outside = item("a", ".1")
        outside["day_number"] = 2
        with self.assertRaises(ValueError):
            self.agent.calculate(self.trip, [outside])


class BudgetLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.db = Database(self.root / "test.db")
        self.db.init()
        self.agent = BudgetAgent(self.db, self.root / "outputs")
        self.trip = TripRequest(destination=fact("Test City"), days=fact(1), budget=fact("10"), currency=fact("USD"), travelers=fact(1))
        self.days = [{"day_number": 1, "stops": [{"place_id": "a", "arrival": fact("09:00"), "departure": fact("10:00")}]}]

    def state(self, ready=True):
        self.db.create_project("budget-test", "budget", "input")
        tasks = build_tasks("budget-test")
        for task in tasks[:3]:
            task["status"] = "done" if ready else "todo"
        tasks[3]["status"] = "ready"
        self.db.replace_tasks("budget-test", tasks)
        return {"project_id": "budget-test", "trip_request": self.trip, "itinerary": self.days, "cost_items": [item("a", "3")], "artifacts": []}

    async def test_file_contains_all_totals_and_can_be_read_by_shared_budget_schema(self):
        update = await self.agent.run(self.state())
        raw = json.loads(Path(update["artifacts"][0].path).read_text(encoding="utf-8"))
        self.assertEqual(raw["total"]["value"], "3")
        self.assertEqual(raw["variance"]["value"], "-7")
        self.assertEqual(raw["sourced_total"]["value"], "3")
        self.assertEqual(raw["estimated_total"]["value"], "0")
        self.assertEqual(Budget.model_validate(raw).remaining.value, 7)
        self.assertEqual(update["stage"], "content_editor")
        tasks = {task["agent"]: task for task in self.db.get_tasks("budget-test")}
        self.assertEqual(tasks["content_editor"]["status"], "ready")
        self.assertTrue(any(event["event_type"] == "budget_ready" for event in self.db.get_events("budget-test")))

    async def test_over_budget_and_empty_costs_block_content_editor(self):
        state = self.state()
        state["cost_items"] = [item("a", "11")]
        result = await self.agent.run(state)
        self.assertEqual(result["stage"], "needs_attention")
        task = next(task for task in self.db.get_tasks("budget-test") if task["agent"] == "content_editor")
        self.assertEqual(task["status"], "blocked")

    async def test_incoming_budget_uses_request_limit_and_rejects_currency_conversion(self):
        state = self.state()
        state.pop("cost_items")
        state["budget"] = {"currency": fact("EUR"), "limit": fact("100"), "items": [item("a", "3")]}
        with self.assertRaises(ValueError):
            await self.agent.run(state)
        self.assertEqual(self.db.get_project("budget-test")["status"], "failed")

    async def test_missing_planner_dependencies_do_not_write_artifacts(self):
        with self.assertRaises(ValueError):
            await self.agent.run(self.state(ready=False))
        self.assertFalse((self.root / "outputs").exists())
