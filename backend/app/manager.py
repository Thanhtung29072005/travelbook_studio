from __future__ import annotations

import asyncio
import shutil
import threading
from pydantic import TypeAdapter
from langgraph.graph import START
from langgraph.types import Command

from .checkpointer import SQLiteCheckpointer
from .schemas import Artifact, TripRequest, Place, DayPlan, Budget, BudgetItem, BudgetSavingsPlan, Photo, Fact, Review, ContentDocument, WeatherReport
from .task_plan import build_tasks
from .workspace import WorkspaceStore

class WorkflowManager:
    def __init__(self, db, workflow=None):
        if workflow is None:
            from .live_workflow import LiveTravelBookWorkflow
            workflow = LiveTravelBookWorkflow(db)
        self.db, self.workflow = db, workflow
        self.checkpointer = SQLiteCheckpointer(db)
        self.graph = workflow._build(checkpointer=self.checkpointer)
        self.running, self.stops, self.loop = {}, {}, None

    def set_loop(self, loop): self.loop = loop

    def config(self, project_id):
        return {"configurable": {"thread_id": self.checkpointer.thread_for(project_id)}, "recursion_limit": 150+24*self.workflow.settings.max_revisions}

    def checkpoint_info(self, project_id):
        saved = self.checkpointer.get_tuple(self.config(project_id))
        if saved is None: return {"exists": False, "checkpoint_id": None, "next_nodes": []}
        snapshot = self.graph.get_state(self.config(project_id))
        return {"exists": True, "checkpoint_id": saved.checkpoint["id"], "next_nodes": list(snapshot.next),
            "created_at": snapshot.created_at, "step": saved.metadata.get("step"),
            "interrupts": [{"id": item.id, "value": item.value} for task in snapshot.tasks for item in task.interrupts]}

    def start(self, project_id, state=None, *, restart=False, command=None):
        if project_id in self.running and not self.running[project_id].done(): return
        row = self.db.get_project(project_id)
        if row is None: raise KeyError(project_id)
        if not self.workflow.fixture_mode and row['state'].get('fixture_mode'):
            raise ValueError('This project contains legacy demo data. Create a new project for live execution.')
        # Explicit interventions branch from START; normal pause/resume never does.
        restart = restart or bool(state and state.get("restart_requested"))
        restart = restart or bool(state and state.get("resume_node") != row["state"].get("resume_node"))
        loop = self.loop or asyncio.get_running_loop()
        self.stops[project_id] = threading.Event()
        self.db.update_project(project_id, status="running", pause_requested=False, error=None)
        self.running[project_id] = loop.create_task(self._run(project_id, state, restart=restart, command=command))

    def pending_approval(self, project_id):
        snapshot = self.graph.get_state(self.config(project_id))
        return [item for task in snapshot.tasks for item in task.interrupts]

    async def answer(self, project_id, answer):
        task=self.running.get(project_id)
        if task and not task.done(): await asyncio.shield(task)
        row=self.db.get_project(project_id)
        pending=self.pending_approval(project_id)
        item=next((item for item in pending if item.value.get('kind')=='trip_clarification'),None)
        if row is None or row['status']!='awaiting_input' or item is None:
            raise ValueError('Project is not awaiting a PM clarification')
        if not answer.strip(): raise ValueError('Answer required')
        self.db.add_event(project_id,'user','clarification_answered','User supplied additional trip information',{'answer':answer.strip()})
        self.start(project_id,command=Command(resume={item.id:{'answer':answer.strip()}}))

    async def decide(self, project_id, approval_id, decision, feedback, target):
        task = self.running.get(project_id)
        if task and not task.done(): await asyncio.shield(task)
        pending = self.pending_approval(project_id)
        item = next((item for item in pending if isinstance(item.value, dict) and item.value.get("approval_id") == approval_id), None)
        if item is None: raise ValueError("No matching checkpoint interrupt")
        gate = next((gate for gate in WorkspaceStore(self.db).read(project_id)["approvals"] if gate["id"] == approval_id), None)
        if gate is None or gate["status"] != "pending": raise ValueError("Approval already resolved")
        target = target or item.value["allowed_targets"][0]
        if decision == "revise" and (not feedback.strip() or target not in item.value["allowed_targets"]):
            raise ValueError("Select a responsible agent and supply feedback")
        payload = {"approval_id": approval_id, "decision": decision, "feedback": feedback.strip(), "target_agent": target}
        self.db.add_event(project_id, "user", "approval_decided", "Human decision submitted", payload)
        self.start(project_id, command=Command(resume={item.id: payload}))
        return {**gate, "status": "approved" if decision == "approve" else "revision_requested", "feedback": feedback.strip(), "target_agent": target}

    def pause(self, project_id):
        row = self.db.get_project(project_id)
        if not row or row["status"] not in {"running", "queued", "pause_requested"}: return False
        if row["status"] == "pause_requested": return True
        active = project_id in self.running and not self.running[project_id].done()
        self.db.update_project(project_id, status="pause_requested" if active else "paused", pause_requested=active)
        self.db.add_event(project_id, "system", "pause_requested", "Yêu cầu tạm dừng; chờ bước đang chạy và checkpoint an toàn")
        if not active:
            self.db.add_event(project_id, "system", "workflow_paused", "Đã tạm dừng; không có bước đang chạy", self.checkpoint_info(project_id))
        return True

    async def stop(self, project_id):
        task = self.running.get(project_id)
        if task and not task.done():
            self.stops[project_id].set()
            await task
        self.db.update_project(project_id, status="stopped", pause_requested=False)
        self.db.add_event(project_id, "system", "workflow_stopped", "Đã dừng an toàn", self.checkpoint_info(project_id))
        return True

    async def resume(self, project_id):
        # The worker can publish paused just before its asyncio wrapper unwinds.
        # Join that wrapper instead of silently dropping an immediate resume click.
        task = self.running.get(project_id)
        if task and not task.done():
            await asyncio.shield(task)
        row = self.db.get_project(project_id)
        if row is None or row["status"] != "paused":
            raise ValueError("Project is not safely paused")
        checkpoint = self.checkpoint_info(project_id)
        self.start(project_id, row["state"])
        return checkpoint

    def _hydrate(self, state):
        for key, schema in {"trip_request": TripRequest, "places": list[Place], "itinerary": list[DayPlan], "budget": Budget,
            "photos": list[Photo], "artifacts": list[Artifact], "maps": list[Artifact], "charts": list[Artifact], "illustrations": list[Artifact],
            "content": list[Fact[str]], "review": Review, "feedback":list[Fact[str]],
            "research":list[Fact[str]], "weather":WeatherReport, "content_document":ContentDocument,
            "cost_items":list[BudgetItem],"budget_baseline":Budget,"budget_savings":BudgetSavingsPlan}.items():
            if state.get(key) is not None: state[key] = TypeAdapter(schema).validate_python(state[key])
        return state

    def _initialize(self, project_id, state):
        row = self.db.get_project(project_id)
        initial = self._hydrate(dict(state or row["state"] or {}))
        initial.update(project_id=project_id, fixture_mode=self.workflow.fixture_mode, production_approved=False, completed=False, restart_requested=False)
        initial.setdefault("user_prompt", row["prompt"])
        for key,value in {"artifacts": [], "revision_count": 0, "revision_pending": [], "revision_warnings": [], "resume_node": "pm"}.items():
            initial.setdefault(key,value)
        if initial["resume_node"] in {"image_curator", "illustrator", "map_agent"}:
            role = initial["resume_node"]
            initial["resume_node"], initial["revision_pending"] = "repair_"+role, [role]
        if initial.get("artifacts"):
            old_folder = self.workflow._directory(initial)
            initial["execution_count"] = initial.get("execution_count", 0)+1
            folder = self.workflow._directory(initial)
            while folder.exists():
                initial["execution_count"] += 1
                folder = self.workflow._directory(initial)
            if old_folder.exists():
                shutil.copytree(old_folder, folder, ignore=shutil.ignore_patterns("executions", "revisions", "revision_reports"))
        if not self.db.get_tasks(project_id): self.db.replace_tasks(project_id, build_tasks(project_id))
        self.graph.update_state(self.config(project_id), initial, as_node=START)
        return initial

    def _values(self, snapshot):
        values = dict(snapshot.values)
        values["resume_node"] = "visuals" if len(snapshot.next)>1 else snapshot.next[0] if snapshot.next else "done"
        return values

    def _sync_approval(self, project_id, values):
        # Mirror decisions only AFTER the graph checkpoint commits. On recovery,
        # replay this mirror from State rather than losing an in-flight decision.
        value = values.get("approval_resolution")
        if not value: return
        WorkspaceStore(self.db).decide(project_id, value["approval_id"], value["decision"],
            value["feedback"], value["target_agent"], replay=True)
        self.db.set_task_status(project_id, f"{project_id}:{value['node']}", "done")

    def _halt(self, project_id, values):
        current = self.db.get_project(project_id)
        if self.stops[project_id].is_set():
            self.db.update_project(project_id, status="stopped", pause_requested=False, state=values)
            return True
        if current["pause_requested"]:
            info = self.checkpoint_info(project_id)
            self.db.update_project(project_id, status="paused", pause_requested=False, state=values)
            self.db.add_event(project_id, "system", "workflow_paused", "Đã lưu checkpoint SQLite; tạm dừng ở bước an toàn", info)
            return True
        pending = self.pending_approval(project_id)
        if pending:
            clarification = next((item for item in pending if item.value.get('kind')=='trip_clarification'),None)
            if clarification:
                self.db.update_project(project_id,status='awaiting_input',state={**values,'clarification_questions':clarification.value['questions']})
                self.db.add_event(project_id,'pm','clarification_requested','PM needs additional trip information',clarification.value)
                return True
            self.db.update_project(project_id, status="awaiting_approval", state=values)
            for item in pending:
                gate = item.value
                self.db.set_task_status(project_id, f"{project_id}:{gate['node']}", "awaiting_approval")
                self.db.add_event(project_id, "system", "approval_requested", "Waiting for human approval", {**gate, "interrupt_id": item.id})
            return True
        return False

    async def _run(self, project_id, state=None, *, restart=False, command=None):
        try:
            def execute():
                config = self.config(project_id)
                saved = self.checkpointer.get_tuple(config)
                if saved is None or restart:
                    if restart:
                        self.checkpointer.fork_thread(project_id)
                        config = self.config(project_id)
                    self._initialize(project_id, state)
                snapshot = self.graph.get_state(config)
                values = self._values(snapshot)
                self._sync_approval(project_id, values)
                self.db.update_project(project_id, state=values)
                self.db.add_event(project_id, "system", "checkpoint_resumed" if saved and not restart else "workflow_started",
                    "Tiếp tục từ checkpoint SQLite" if saved and not restart else ("Khởi động đồ thị JSON mẫu" if self.workflow.fixture_mode else "Khởi động workflow agent thật"), self.checkpoint_info(project_id))
                if command is None and self._halt(project_id, values): return
                graph_input = command
                while snapshot.next:
                    completed = []
                    # Exhaust this generator: all writes for the superstep must be
                    # durably committed before the project can be marked paused.
                    for mode, update in self.graph.stream(graph_input, config, stream_mode=["updates", "values"],
                            interrupt_after="*", durability="sync"):
                        if mode == "updates": completed.extend(key for key in update if not key.startswith("__"))
                    graph_input = None
                    snapshot = self.graph.get_state(config)
                    values = self._values(snapshot)
                    self._sync_approval(project_id, values)
                    self.db.update_project(project_id, state=values)
                    self.db.add_event(project_id, "system", "checkpoint_saved", "Đã lưu bước an toàn", {**self.checkpoint_info(project_id), "completed_nodes": completed})
                    if self._halt(project_id, values): return
                if values.get("completed"):
                    pdf = values.get("final_pdf")
                    self.db.update_project(project_id, status="completed", stage="completed", progress=100, pause_requested=False,
                        state=values, final_pdf=pdf.path if hasattr(pdf,"path") else pdf.get("path") if isinstance(pdf,dict) else None)
                    self.db.add_event(project_id, "system", "workflow_completed", "Đã tiếp tục đến PDF", self.checkpoint_info(project_id))
                else:
                    raise RuntimeError("Graph ended without completion or pause/approval")
            await asyncio.to_thread(execute)
        except Exception as error:
            self.db.update_project(project_id, status="failed", error=type(error).__name__+": "+str(error)[:400])
            self.db.add_event(project_id, "system", "workflow_failed", "Workflow failed", {"error_type": type(error).__name__})
        finally:
            self.running.pop(project_id, None)

    def resume_pending(self):
        for row in self.db.list_projects(limit=10000):
            if row["status"] in {"running", "pause_requested"}:
                saved = self.checkpointer.get_tuple(self.config(row["id"]))
                fields = {"status": "paused", "pause_requested": False}
                if saved:
                    fields["state"] = self._values(self.graph.get_state(self.config(row["id"])))
                    self._sync_approval(row["id"], fields["state"])
                    pending = self.pending_approval(row['id'])
                    if pending:
                        fields['status']='awaiting_input' if pending[0].value.get('kind')=='trip_clarification' else 'awaiting_approval'
                        if fields['status']=='awaiting_input': fields['state']['clarification_questions']=pending[0].value['questions']
                self.db.update_project(row["id"], **fields)
                self.db.add_event(row["id"], "system", "workflow_recovered", "Khôi phục checkpoint; chờ người dùng tiếp tục", self.checkpoint_info(row["id"]))
