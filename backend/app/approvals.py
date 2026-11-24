"""Checkpoint-backed human decisions. Replayed gate creation is idempotent."""
import shutil

from langgraph.config import get_config
from langgraph.types import interrupt

from .schemas import Budget, Fact, TripRequest
from .workspace import APPROVAL_TARGETS, WorkspaceStore


class ManualApprovals:
    def __init__(self, workflow):
        self.workflow = workflow

    def node(self, node, following):
        def approve(state):
            pid = state["project_id"]
            store = WorkspaceStore(self.workflow.db)
            thread = get_config().get("configurable", {}).get("thread_id", pid)
            key = f"{thread}:{node}:e{state.get('execution_count', 0)}:r{state.get('revision_count', 0)}"
            if node=='reviewer' and state.get('budget') and state.get('revision_count',0)<self.workflow.settings.max_revisions:
                budget=Budget.model_validate(state['budget'])
                trip=TripRequest.model_validate(state['trip_request']) if state.get('trip_request') else None
                over=budget.total.value>(trip.budget.value if trip else budget.limit.value)
                pending=any(item.get('gate_key')==key and item['status']=='pending' for item in store.read(pid)['approvals'])
                if over and not pending:
                    # Finish automatic budget repairs before asking the user to review the result.
                    return {'approval_route':following}
            gate = store.gate(pid, node, state, key=key)
            if gate is None:
                return {"approval_route": following}
            # No generation or non-idempotent side effect before interrupt:
            # LangGraph reruns this function when Command(resume=...) arrives.
            decision = interrupt({"kind": "human_approval", "approval_id": gate["id"],
                "stage": gate["stage"], "node": node, "allowed_targets": gate["allowed_targets"]})
            if not isinstance(decision, dict) or decision.get("approval_id") != gate["id"]:
                raise ValueError("Decision does not match this interrupt")
            choice = decision.get("decision")
            feedback = decision.get("feedback", "").strip()
            target = decision.get("target_agent") or APPROVAL_TARGETS[gate["stage"]][0]
            if choice not in {"approve", "revise"} or (choice == "revise" and (not feedback or target not in APPROVAL_TARGETS[gate["stage"]])):
                raise ValueError("Invalid approval decision")
            resolution = {"approval_id": gate["id"], "decision": choice, "feedback": feedback, "target_agent": target, "node": node}
            if choice == "approve":
                return {"approval_route": following, "approval_resolution": resolution}
            # A separate output generation keeps previously reviewed artifacts intact.
            updated = {"execution_count": state.get("execution_count", 0)+1,
                "feedback": [Fact(value=feedback, source="user:approval:"+gate["id"], confidence=1)],
                "completed": False, "production_approved": False,
                "approval_route": "human_"+target if gate["stage"] == "visuals" else target,
                "approval_resolution": resolution}
            source = self.workflow._directory(state)
            destination = self.workflow._directory({**state, **updated})
            shutil.copytree(source, destination, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("executions", "revisions", "revision_reports"))
            return updated
        return approve
