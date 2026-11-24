"""Deterministic Review routing and bounded repair loops for LangGraph."""

import json
import inspect
import re
from pathlib import Path
from uuid import uuid4

from .schemas import Artifact, Budget, Fact, Review, ReviewIssue, TripRequest
from .services.budget_savings import savings_plan
from .workspace import WorkspaceStore

# Explicit rule overrides precede category defaults; the model cannot pick arbitrary nodes.
RULE_ROUTES = {"LLM_PHOTO_LOCATION": "image_curator", "LLM_STYLE_CONSISTENCY": "illustrator",
               "SCHEMA_TRIP_REQUEST": "pm"}
CATEGORY_ROUTES = {"research": "researcher", "itinerary": "planner", "budget": "budget", "content": "content_editor",
                   "photo": "image_curator", "style": "illustrator", "map": "map_agent", "layout": "layout_editor"}
ALLOWED_OWNERS = {"research": {"pm", "researcher"}, "itinerary": {"pm", "researcher", "planner"},
    "budget": {"budget"}, "content": {"content_editor"}, "photo": {"image_curator"}, "style": {"illustrator"},
    "map": {"map_agent"}, "layout": {"layout_editor"}}
# Recompute affected data after upstream edits; visual repair remains one branch, then re-review.
REPAIR_PLANS = {
    "pm": ["pm", "researcher", "planner", "budget", "content_editor", "image_curator", "illustrator", "map_agent"],
    "researcher": ["researcher", "planner", "budget", "content_editor", "image_curator", "illustrator", "map_agent"],
    "planner": ["planner", "budget", "content_editor", "image_curator", "illustrator", "map_agent"],
    "budget": ["budget", "content_editor", "map_agent"],
    "content_editor": ["content_editor", "image_curator", "illustrator", "map_agent"],
    "image_curator": ["image_curator"], "illustrator": ["illustrator"], "map_agent": ["map_agent"],
    "layout_editor": ["layout_editor"],
}


def responsible_agent(issue):
    if issue.rule_code in RULE_ROUTES:
        return RULE_ROUTES[issue.rule_code]
    if issue.target_agent in ALLOWED_OWNERS[issue.category]:
        return issue.target_agent
    return CATEGORY_ROUTES[issue.category]


class RevisionController:
    def __init__(self, db, settings, artifacts_root, *, prepare_revision=None):
        self.db, self.settings, self.root = db, settings, Path(artifacts_root).resolve()
        self.prepare_revision = prepare_revision

    def _event(self, state, kind, message, payload):
        self.db.add_event(state["project_id"], "reviewer", kind, message, payload)

    def decide(self, state):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", state["project_id"]):
            raise ValueError("Unsafe project_id")
        review = Review.model_validate(state["review"])
        budget = Budget.model_validate(state['budget']) if state.get('budget') is not None else None
        trip = TripRequest.model_validate(state['trip_request']) if state.get('trip_request') is not None else None
        over_budget = bool(budget and (trip is None or budget.currency.value==trip.currency.value)
                           and budget.total.value > (trip.budget.value if trip else budget.limit.value))
        review_update = {}
        if over_budget:
            limit = trip.budget.value if trip else budget.limit.value
            issue = ReviewIssue(category='budget',severity='blocker',target_agent='budget',rule_code='BUDGET_TOTAL',
                message=Fact(value=f'Dự toán {budget.total.value:,.0f} vượt ngân sách {limit:,.0f}; cần giảm {budget.total.value-limit:,.0f} {budget.currency.value}.',
                    source='calculation:budget-total-minus-limit',confidence=1))
            review = review.model_copy(update={'verdict':'REVISE','issues':[issue,*[item for item in review.issues if item.rule_code!='BUDGET_TOTAL']]})
            review_update={'review':review}
        count = state.get("revision_count", 0)
        if type(count) is not int or count < 0:
            raise ValueError("revision_count must be a nonnegative integer")
        unresolved = sorted([issue for issue in review.issues if issue.severity in {"major", "blocker"}],
                            key=lambda issue: (issue.rule_code!='BUDGET_TOTAL', issue.severity != "blocker"))
        if unresolved and not over_budget and WorkspaceStore(self.db).review_approved(state["project_id"], state):
            approval_id = state["approval_resolution"]["approval_id"]
            warning = Fact[str](value=f"Người dùng đã duyệt kết quả kiểm duyệt có {len(unresolved)} vấn đề cần lưu ý.",
                                source="user:approval:" + approval_id, confidence=1)
            warnings = [Fact[str].model_validate(item) for item in state.get("revision_warnings", [])]
            if not any(item.source == warning.source for item in warnings):
                warnings.append(warning)
            self._ready(state, ["layout_editor"])
            self._event(state, "review_approved_by_user", "Người dùng đã duyệt; chuyển sang dàn trang PDF",
                        {"approval_id": approval_id, "revision_count": count, "unresolved_count": len(unresolved)})
            return {"review_route": "layout_editor", "revision_target": None, "revision_pending": [],
                    "revision_exhausted": False, "revision_warnings": warnings, "production_approved": False}
        if not unresolved:
            self._event(state, "review_ready", "No critical review errors; continue to layout", {"revision_count": count, "warning_count": len(review.issues)})
            self._ready(state, ["layout_editor"])
            return {**review_update,"review_route": "layout_editor", "revision_target": None, "revision_pending": [], "revision_exhausted": False}
        if count >= self.settings.max_revisions:
            warning = Fact[str](value=f"MAX_REVISIONS={self.settings.max_revisions} reached after {count} repairs; {len(unresolved)} critical issues remain. Continue as a draft requiring manual review.",
                source="policy:MAX_REVISIONS; execution:RevisionController", confidence=1)
            warnings = [*state.get("revision_warnings", []), warning]
            report = {"revision_count": count, "max_revisions": self.settings.max_revisions, "warning": warning.model_dump(mode="json"),
                "review": review.model_dump(mode="json"), "production_approved": False, "continue_as_draft": True}
            folder = self.root / state["project_id"] / "revision_reports"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / (uuid4().hex + ".json")
            path.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
            artifact = Artifact(id=uuid4().hex, project_id=state["project_id"], agent_id="reviewer", kind="review", name="revision_limit_warning.json",
                path=str(path), mime_type="application/json", source=warning.source, confidence=1)
            self.db.add_artifact(artifact)
            self._event(state, "revision_limit_reached", warning.value, report)
            self._ready(state, ["layout_editor"])
            savings = savings_plan(budget,trip,self.settings,baseline=state.get('budget_baseline')) if over_budget else state.get('budget_savings')
            return {**review_update,"review_route": "layout_editor", "revision_target": None, "revision_pending": [], "revision_exhausted": True,
                'budget_savings':savings,
                "revision_warnings": warnings, "production_approved": False, "artifacts": [artifact]}
        target = responsible_agent(unresolved[0])
        plan = REPAIR_PLANS[target][:]
        if self.prepare_revision:
            self.prepare_revision(state, count+1)
        self._ready(state, plan)
        self._event(state, "revision_requested", "Return to responsible agent: " + target,
            {"revision_count": count+1, "max_revisions": self.settings.max_revisions, "target_agent": target, "repair_plan": plan,
             "rule_code": unresolved[0].rule_code, "issue": unresolved[0].model_dump(mode="json")})
        return {**review_update,"revision_count": count+1, "revision_target": target, "revision_pending": plan,
            "review_route": "repair_"+target, "revision_exhausted": False, "production_approved": False,
            "feedback": [issue.message for issue in unresolved]}

    def _ready(self, state, roles):
        for task in self.db.get_tasks(state["project_id"]):
            # Final PM acceptance is not a repair task.
            if task["agent"] in roles and not task["id"].endswith(":pm_finalize"):
                self.db.set_task_status(state["project_id"], task["id"], "ready")

    def repaired(self, state, role):
        pending = state.get("revision_pending", [])
        if not pending or pending[0] != role:
            raise ValueError("Repair agent does not match pending repair plan")
        remaining = pending[1:]
        if not remaining:
            self._ready(state, ["reviewer"])
        self._event(state, "repair_completed", "Repair agent finished: " + role,
            {"revision_count": state["revision_count"], "agent": role, "remaining": remaining})
        return {"revision_pending": remaining, "review_route": "repair_"+remaining[0] if remaining else "reviewer"}

    def attach(self, graph, repair_actions, *, reviewer="reviewer", layout="layout_editor", review_gate=None):
        """Attach real conditional edges; actions can be fixture wrappers or real agent nodes."""
        if set(repair_actions) != set(REPAIR_PLANS):
            raise ValueError("Provide a repair action for each routed agent")
        graph.add_node("revision_gate", self.decide)
        if review_gate is None:
            graph.add_edge(reviewer, "revision_gate")
        destinations = {"repair_"+role: "repair_"+role for role in REPAIR_PLANS}
        graph.add_conditional_edges("revision_gate", lambda state: state["review_route"], {**destinations, "layout_editor": layout})
        for role, action in repair_actions.items():
            if inspect.iscoroutinefunction(action):
                async def repair(state, role=role, action=action):
                    result = await action(state)
                    return {**result, **self.repaired(state, role)}
            else:
                def repair(state, role=role, action=action):
                    result = action(state)
                    return {**result, **self.repaired(state, role)}
            graph.add_node("repair_"+role, repair)
            graph.add_conditional_edges("repair_"+role, lambda state: state["review_route"], {**destinations, "reviewer": reviewer})
