"""Sum full-line trip costs in code; preserve price provenance and explicit estimates."""

from typing import Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from ..schemas import Budget, BudgetItem, DayPlan, Fact, Schema, State, Text, TripRequest
from .base import BaseAgent


class BudgetResult(Schema):
    budget: Budget
    status: Literal["ready", "needs_attention"]
    warnings: list[Fact[Text]] = Field(default_factory=list)


class BudgetAgent(BaseAgent):
    agent_id = "budget"
    display_name = "Budget"

    def __init__(self, db, artifacts_root):
        super().__init__(db, None, artifacts_root)

    def calculate(self, trip_request, items, *, itinerary=None) -> BudgetResult:
        trip = TripRequest.model_validate(trip_request)
        items = TypeAdapter(list[BudgetItem]).validate_python(items)
        if itinerary is not None:
            days = TypeAdapter(list[DayPlan]).validate_python(itinerary)
            if [day.day_number for day in days] != list(range(1, trip.days.value + 1)):
                raise ValueError("Budget needs complete ordered Planner days")
        warnings, normalized = [], []
        for item in items:
            if item.day_number is not None and item.day_number > trip.days.value:
                raise ValueError("Cost item references a day outside the trip")
            source = item.amount.source.casefold()
            unreliable = any(marker in source for marker in ("fixture:", "mock:", "mock-http", "synthetic:", "llm:", "estimate:", "assumption:", "unverified", "placeholder"))
            if 'official-admission' in source:
                quote = item.price_evidence
                unreliable = unreliable or quote is None
                if quote is not None:
                    unreliable = unreliable or quote.currency != trip.currency.value or str(quote.source_url) not in item.amount.source
                    if quote.amount == 0:
                        unreliable = unreliable or item.amount.value != 0
                    else:
                        unreliable = unreliable or item.amount.value % quote.amount != 0 or item.amount.value / quote.amount > trip.travelers.value
            if item.price_kind == "sourced" and (unreliable or item.amount.confidence <= 0):
                # Keep the declared amount and original source; only downgrade classification.
                item = item.model_copy(update={"price_kind": "estimated"})
                warnings.append(Fact[Text](value=f"{item.id}: nguồn chưa đủ để gắn giá có nguồn; chuyển sang giá ước tính.",
                    source="assessment:budget-price-kind; " + item.amount.source, confidence=1))
            normalized.append(item)
        budget = Budget(currency=trip.currency, limit=trip.budget, items=normalized)
        if not normalized:
            warnings.append(Fact[Text](value="Chưa có khoản chi. Tổng 0 chỉ là phép cộng danh sách rỗng, chưa đủ để duyệt dự toán.",
                source="validation:budget-empty", confidence=1))
        if any(item.price_kind == "estimated" for item in normalized):
            warnings.append(Fact[Text](value="Có giá ước tính; tổng dự toán không phải báo giá đã xác minh.", source="validation:budget-has-estimates", confidence=1))
        if not budget.within_budget.value:
            warnings.append(Fact[Text](value=f"Vượt ngân sách {budget.variance.value} {trip.currency.value}; cần sửa dự toán hoặc lịch trình.",
                source=budget.variance.source, confidence=budget.variance.confidence))
        return BudgetResult(budget=budget, status="ready" if normalized and budget.within_budget.value else "needs_attention", warnings=warnings)

    async def run(self, state: State) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        if project is None or project["status"] not in {"queued", "paused", "needs_attention"}:
            raise ValueError("Budget Agent needs a runnable project")
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        owned = [task for task in tasks if task["agent"] == self.agent_id]
        if not owned or any(dep not in by_id or by_id[dep]["status"] != "done" for task in owned for dep in task["depends_on"]):
            raise ValueError("Complete Planner dependencies before Budget Agent")
        self.db.update_project(project_id, status="running", stage="budget", progress=30, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Budget Agent cộng các khoản chi bằng Decimal")
        try:
            trip = TripRequest.model_validate(state["trip_request"])
            if "cost_items" in state:
                items = state["cost_items"]
            elif state.get("budget") is not None:
                incoming = Budget.model_validate(state["budget"])
                if incoming.currency.value != trip.currency.value:
                    raise ValueError("Budget Agent does not convert currencies")
                items = incoming.items
            else:
                items = []
            result = self.calculate(trip, items, itinerary=state["itinerary"])
            run_id = uuid4().hex[:16]
            # Supply a plain dict so BaseAgent's generic serializer does NOT omit totals.
            exported = result.budget.model_dump(mode="json")
            Budget.model_validate(exported)  # Round-trip every computed field before publishing.
            artifacts = [self.save_json(project_id, run_id, "budget.json", exported, kind="budget",
                source="calculation:BudgetAgent Decimal totals", confidence=result.budget.total.confidence),
                self.save_json(project_id, run_id, "budget_report.json", {"status": result.status, "warnings": result.warnings,
                    "variance_sign": Fact[Text](value="variance=total-limit; dương là vượt, âm là còn dư", source="policy:budget-sign-convention", confidence=1)},
                    kind="review", source="execution:BudgetAgent audit", confidence=1)]
            ready = result.status == "ready"
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done" if ready else "blocked")
            for task in tasks:
                if task["agent"] == "content_editor":
                    self.db.set_task_status(project_id, task["id"], "ready" if ready else "blocked")
            update: State = {"budget": result.budget, "budget_warnings": result.warnings, "artifacts": artifacts,
                "stage": "content_editor" if ready else "needs_attention", "production_approved": False, "error": None}
            self.db.update_project(project_id, status="queued" if ready else "needs_attention", stage=update["stage"], progress=35 if ready else 30,
                state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "budget_ready" if ready else "budget_needs_attention", "Đã lưu tổng và độ lệch ngân sách",
                {"total": str(result.budget.total.value), "sourced_total": str(result.budget.sourced_total.value),
                 "estimated_total": str(result.budget.estimated_total.value), "variance": str(result.budget.variance.value), "within_budget": result.budget.within_budget.value})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            self.db.update_project(project_id, status="failed", error="Budget Agent failed: " + type(error).__name__)
            self.event(project_id, "agent_failed", "Budget Agent chưa tính được dự toán hợp lệ", {"error_type": type(error).__name__})
            raise
