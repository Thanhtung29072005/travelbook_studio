from pathlib import Path
import asyncio
import json
import tempfile
import unittest

from langgraph.graph import END, START, StateGraph

from backend.app.config import Settings
from backend.app.db import Database
from backend.app.revisions import CATEGORY_ROUTES, REPAIR_PLANS, RevisionController, responsible_agent
from backend.app.schemas import Fact, Review, ReviewIssue, State
from backend.app.task_plan import build_tasks
from backend.app.workspace import WorkspaceStore


def issue(category="photo", owner="image_curator", severity="major", rule="LLM_PHOTO_LOCATION"):
    return ReviewIssue(category=category, severity=severity, target_agent=owner, rule_code=rule,
        message=Fact[str](value="Intentional test error", source="test:revision-routing", confidence=1))


def review(issues):
    return Review(source="test:revision-routing", confidence=1, verdict="REVISE" if any(i.severity != "minor" for i in issues) else "PASS",
        summary=Fact[str](value="Injected review for routing test", source="test:revision-routing", confidence=1), issues=issues)


class RevisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = Database(self.root / "db.sqlite3")
        self.db.init()
        self.db.create_project("revision-test", "Routing test", "intentional error")
        tasks = build_tasks("revision-test")
        for task in tasks: task["status"] = "done"
        self.db.replace_tasks("revision-test", tasks)
        self.visits, self.reviews, self.layout_visits = [], [], []

    def graph(self, injected, maximum=2, clears_after=None):
        controller = RevisionController(self.db, Settings(_env_file=None, max_revisions=maximum), self.root)
        graph = StateGraph(State)
        def reviewer(state):
            count = state.get("revision_count", 0)
            self.reviews.append(count)
            return {"review": review([] if clears_after is not None and count >= clears_after else injected)}
        def layout(state):
            self.layout_visits.append(state.get("revision_count", 0))
            return {"completed": True, "production_approved": False}
        def action(role):
            def execute(state):
                self.visits.append(role)
                return {}
            return execute
        graph.add_node("reviewer", reviewer)
        graph.add_node("layout_editor", layout)
        graph.add_edge(START, "reviewer")
        graph.add_edge("layout_editor", END)
        controller.attach(graph, {role: action(role) for role in REPAIR_PLANS})
        return graph.compile()

    def invoke(self, graph):
        return graph.invoke({"project_id": "revision-test", "revision_count": 0, "artifacts": [], "revision_warnings": []}, {"recursion_limit": 100})

    def approved_state(self):
        state = {"project_id": "revision-test", "review": review([issue("research", "researcher", rule="OPENING_HOURS_MISSING")]),
                 "revision_count": 0, "revision_warnings": []}
        store = WorkspaceStore(self.db)
        store.settings("revision-test", {"approval_enabled": True, "approval_stages": ["visuals"]})
        gate = store.gate("revision-test", "reviewer", state, key=f"test-{len(store.read('revision-test')['approvals'])}:reviewer:e0:r0")
        store.decide("revision-test", gate["id"], "approve", "", "image_curator")
        state["approval_resolution"] = {"approval_id": gate["id"], "node": "reviewer", "decision": "approve"}
        return state

    def test_human_approval_of_current_review_moves_to_layout_without_research(self):
        state = self.approved_state()
        result = RevisionController(self.db, Settings(_env_file=None), self.root).decide(state)
        self.assertEqual(result["review_route"], "layout_editor")
        self.assertEqual(result["revision_pending"], [])
        self.assertFalse(result["revision_exhausted"])
        self.assertTrue(result["revision_warnings"])
        self.assertEqual(state["review"].verdict, "REVISE")
        self.assertFalse(any(e["event_type"] == "revision_requested" for e in self.db.get_events("revision-test")))

    def test_human_approval_cannot_skip_over_budget_repair(self):
        from test_budget_savings import example
        trip,budget=example()
        state=self.approved_state()
        state.update(trip_request=trip,budget=budget)
        result=RevisionController(self.db,Settings(_env_file=None),self.root).decide(state)
        self.assertEqual(result['review_route'],'repair_budget')
        self.assertEqual(result['revision_count'],1)
        self.assertEqual(result['review'].issues[0].rule_code,'BUDGET_TOTAL')
        self.assertEqual(result['review'].issues[0].severity,'blocker')

    def test_budget_check_cannot_be_missed_even_if_review_says_pass(self):
        from test_budget_savings import example
        trip,budget=example()
        graph=self.graph([],maximum=2)
        result=graph.invoke({'project_id':'revision-test','review':review([]),'trip_request':trip,'budget':budget,
            'revision_count':0,'artifacts':[],'revision_warnings':[]},{'recursion_limit':100})
        self.assertEqual(self.visits,REPAIR_PLANS['budget']*2)
        self.assertEqual(self.layout_visits,[2])
        self.assertTrue(result['revision_exhausted'])
        self.assertEqual(result['review'].verdict,'REVISE')
        self.assertEqual(result['budget_savings'].current_total,13365000)
        self.assertEqual(len(result['budget_savings'].options),2)

    def test_budget_repairs_happen_before_requesting_new_human_review_approval(self):
        from types import SimpleNamespace
        from backend.app.approvals import ManualApprovals
        from test_budget_savings import example
        trip,budget=example()
        store=WorkspaceStore(self.db)
        store.settings('revision-test',{'approval_enabled':True,'approval_stages':['visuals']})
        workflow=SimpleNamespace(db=self.db,settings=Settings(_env_file=None,max_revisions=2))
        graph=StateGraph(State)
        graph.add_node('gate',ManualApprovals(workflow).node('reviewer','revision_gate'))
        graph.add_edge(START,'gate')
        graph.add_edge('gate',END)
        result=graph.compile().invoke({'project_id':'revision-test','trip_request':trip,'budget':budget,'revision_count':0})
        self.assertEqual(result['approval_route'],'revision_gate')
        self.assertFalse(store.read('revision-test')['approvals'])

    def test_old_approval_does_not_skip_a_new_review_or_revision(self):
        controller = RevisionController(self.db, Settings(_env_file=None), self.root)
        state = self.approved_state()
        state["review"] = review([issue("budget", "budget", rule="BUDGET_LIMIT")])
        self.assertEqual(controller.decide(state)["review_route"], "repair_budget")
        state = self.approved_state()
        state["revision_count"] = 1
        self.assertEqual(controller.decide(state)["review_route"], "repair_researcher")

    def test_unrecorded_human_approval_does_not_skip_repairs(self):
        state = self.approved_state()
        state["approval_resolution"]["approval_id"] = "not-a-saved-approval"
        result = RevisionController(self.db, Settings(_env_file=None), self.root).decide(state)
        self.assertEqual(result["review_route"], "repair_researcher")

    def test_persistent_photo_error_returns_to_curator_twice_then_continues(self):
        result = self.invoke(self.graph([issue()]))
        self.assertEqual(self.visits, ["image_curator", "image_curator"])
        self.assertEqual(self.reviews, [0, 1, 2])
        self.assertEqual(self.layout_visits, [2])
        self.assertEqual(result["revision_count"], 2)
        self.assertTrue(result["revision_exhausted"])
        self.assertTrue(result["completed"])
        self.assertEqual(result["review"].verdict, "REVISE")
        self.assertFalse(result["production_approved"])
        events = self.db.get_events("revision-test")
        self.assertEqual(sum(e["event_type"] == "revision_limit_reached" for e in events), 1)
        self.assertEqual(sum(e["event_type"] == "revision_requested" for e in events), 2)
        report = json.loads(Path(result["artifacts"][0].path).read_text(encoding="utf-8"))
        self.assertTrue(report["continue_as_draft"])
        self.assertEqual(report["review"]["verdict"], "REVISE")

    def test_zero_revisions_does_not_attempt_any_repair(self):
        result = self.invoke(self.graph([issue()], maximum=0))
        self.assertEqual(self.visits, [])
        self.assertEqual(self.reviews, [0])
        self.assertTrue(result["revision_exhausted"])

    def test_error_fixed_early_stops_without_limit_warning(self):
        result = self.invoke(self.graph([issue()], maximum=3, clears_after=1))
        self.assertEqual(self.visits, ["image_curator"])
        self.assertEqual(self.reviews, [0, 1])
        self.assertFalse(result["revision_exhausted"])
        self.assertEqual(result["review"].verdict, "PASS")
        self.assertFalse(result["revision_warnings"])

    def test_fixed_on_last_allowed_attempt_is_not_exhaustion(self):
        result = self.invoke(self.graph([issue()], maximum=2, clears_after=2))
        self.assertFalse(result["revision_exhausted"])
        self.assertEqual(result["revision_count"], 2)

    def test_minor_warning_goes_straight_to_layout(self):
        result = self.invoke(self.graph([issue(severity="minor")]))
        self.assertFalse(self.visits)
        self.assertEqual(result["revision_count"], 0)
        self.assertEqual(len(result["review"].issues), 1)

    def test_upstream_planner_error_refreshes_dependencies_once_per_round(self):
        result = self.invoke(self.graph([issue("itinerary", "planner", rule="OPENING_WINDOW")], maximum=1))
        self.assertEqual(self.visits, REPAIR_PLANS["planner"])
        self.assertEqual(self.reviews, [0, 1])
        self.assertEqual(result["revision_count"], 1)

    def test_blocker_takes_priority_over_major_without_reordering_original_review(self):
        failures = [issue("budget", "budget", rule="BUDGET_LIMIT"), issue("research", "researcher", "blocker", "INPUT_UNVERIFIED")]
        result = self.invoke(self.graph(failures, maximum=1))
        self.assertEqual(self.visits, REPAIR_PLANS["researcher"])
        self.assertEqual(result["review"].issues[0].target_agent, "budget")

    def test_table_rejects_wrong_owner_and_handles_age_and_schema_exceptions(self):
        self.assertEqual(responsible_agent(issue(owner="budget")), "image_curator")
        for category, target in CATEGORY_ROUTES.items():
            self.assertEqual(responsible_agent(issue(category, "reviewer", rule="OTHER")), target)
        self.assertEqual(responsible_agent(issue("itinerary", "pm", rule="LLM_AGE_SUITABILITY")), "pm")
        self.assertEqual(responsible_agent(issue("itinerary", "planner", rule="SCHEMA_TRIP_REQUEST")), "pm")

    def test_missing_review_and_invalid_revision_counter_fail(self):
        controller = RevisionController(self.db, Settings(_env_file=None), self.root)
        with self.assertRaises(KeyError): controller.decide({"project_id": "revision-test"})
        for count in (-1, True, "2"):
            with self.assertRaises(ValueError): controller.decide({"project_id": "revision-test", "review": review([issue()]), "revision_count": count})

    def test_wrong_repair_cannot_consume_an_attempt(self):
        controller = RevisionController(self.db, Settings(_env_file=None), self.root)
        with self.assertRaises(ValueError):
            controller.repaired({"project_id": "revision-test", "revision_count": 1, "revision_pending": ["illustrator"]}, "image_curator")

    def test_async_real_agent_entry_points_can_be_attached(self):
        controller = RevisionController(self.db, Settings(_env_file=None, max_revisions=1), self.root)
        graph = StateGraph(State)
        async def reviewer(state):
            return {"review": review([issue("style", "illustrator", rule="LLM_STYLE_CONSISTENCY")])}
        async def repair(state):
            self.visits.append("illustrator")
            return {}
        graph.add_node("reviewer", reviewer)
        graph.add_node("layout_editor", lambda state: {"completed": True})
        graph.add_edge(START, "reviewer")
        graph.add_edge("layout_editor", END)
        controller.attach(graph, {role: repair for role in REPAIR_PLANS})
        result = asyncio.run(graph.compile().ainvoke({"project_id": "revision-test", "revision_count": 0, "artifacts": []}))
        self.assertEqual(self.visits, ["illustrator"])
        self.assertTrue(result["revision_exhausted"])
