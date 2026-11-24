"""Runnable fixture-backed LangGraph workflow, following TravelBook's ten roles.

This is an offline integration demo: all nodes read sample JSON, no LLM/API is
called. Review checks pipeline structure only; it never approves travel facts.
"""

from __future__ import annotations

from functools import wraps
from hashlib import sha256
import json
from pathlib import Path
import re
import shutil
from uuid import NAMESPACE_URL, uuid4, uuid5

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from PIL import Image
from pydantic import BaseModel, TypeAdapter

from .db import Database, utcnow
from .config import Settings
from .revisions import RevisionController, REPAIR_PLANS
from .workspace import WorkspaceStore
from .approvals import ManualApprovals
from .agents.supervisor import HybridSupervisor
from .task_plan import DEPENDENCIES, build_tasks
from .schemas import Artifact, Budget, ContentDocument, DayPlan, Fact, Photo, Place, Review, ReviewIssue, State, TripRequest
from .services.charts import render_budget_chart, render_timeline_chart, render_weather_chart
from .services.images import placeholder_image
from .services.layout import LayoutService
from .services.maps import render_day_maps
from .services.style_lock import StyleLock, get_style_lock

ROOT = Path(__file__).resolve().parents[2]
VISUAL_NODES = ("image_curator", "illustrator", "map_agent")


def json_value(value):
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_computed_fields=True)
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    return value


class TravelBookWorkflow:
    fixture_mode = True
    def __init__(
        self, db: Database, *, sample_dir: str | Path | None = None,
        artifacts_root: str | Path | None = None, style_lock: StyleLock | None = None,
        settings: Settings | None = None, supervisor_router_factory=None,
    ):
        self.db = db
        self.sample_dir = Path(sample_dir or ROOT / "backend/data/artifacts/danang_3days").resolve()
        self.artifacts_root = Path(artifacts_root or ROOT / "backend/data/artifacts").resolve()
        self.style_lock = style_lock or get_style_lock()
        self.layout = LayoutService()
        self.settings = settings or Settings()
        self.supervisor = HybridSupervisor(db, self.settings, router_factory=supervisor_router_factory)
        self.revisions = RevisionController(db, self.settings, self.artifacts_root, prepare_revision=self._prepare_revision)
        self.graph = self._build()

    def _directory(self, state):
        root = self.artifacts_root / state["project_id"]
        if state.get("execution_count", 0):
            root = root / "executions" / str(state["execution_count"])
        return root / "revisions" / f"r{state['revision_count']}" if state.get("revision_count", 0) else root

    def _prepare_revision(self, state, revision):
        source = self._directory(state)
        target = self._directory({**state, "revision_count": revision})
        # Exclude nested revision history when copying revision zero into its descendant.
        shutil.copytree(source, target, ignore=shutil.ignore_patterns("revisions", "revision_reports"))

    def _artifact(self, state, path, agent, kind, *, source, confidence):
        path = Path(path).resolve()
        relative = path.relative_to(self._directory(state))
        artifact_id=str(uuid5(NAMESPACE_URL, f"travelbook:{state['project_id']}:e{state.get('execution_count', 0)}:r{state.get('revision_count', 0)}:{relative.as_posix()}"))
        existing=self.db.get_artifact(state['project_id'],artifact_id)
        return Artifact(
            id=artifact_id,
            project_id=state["project_id"], agent_id=agent, kind=kind, name=relative.as_posix(),
            path=str(path), mime_type={".png": "image/png", ".jpg":"image/jpeg", ".jpeg":"image/jpeg", ".webp":"image/webp", ".pdf": "application/pdf"}.get(path.suffix, "application/json"),
            source=source, confidence=confidence,
            **({'created_at':existing['created_at']} if existing else {}),
        )

    def _save_json(self, state, name, data, agent, kind, *, confidence, source=None):
        path = self._directory(state) / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(json_value(data), ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        return self._artifact(state, path, agent, kind, source=source or f"fixture:{name}", confidence=confidence)

    @staticmethod
    def _fact(value, source, confidence=1.0):
        return Fact(value=value, source=source, confidence=confidence)

    def _wrap(self, node, action, files, progress, stage):
        agent = "pm" if node == "pm_finalize" else node

        @wraps(action)
        def execute(state):
            project_id = state["project_id"]
            if WorkspaceStore(self.db).agent(project_id, agent) is None:
                self.db.add_event(project_id,"system","agent_unavailable","Required role is disabled or removed",{"node":node,"role":agent})
                raise ValueError(f"Required agent is disabled or removed: {agent}")
            if self.settings.supervisor_enabled:
                self.supervisor.require_dependencies(project_id, node)
            task_id = f"{project_id}:{node}"
            self.db.set_task_status(project_id, task_id, "in_progress")
            with self.db.connect() as connection:
                connection.execute("UPDATE tasks SET start_at=COALESCE(start_at,?) WHERE project_id=? AND id=?", (utcnow(), project_id, task_id))
            self.db.update_project(project_id, stage=stage, progress=progress)
            self.db.add_event(project_id, agent, "node_started", f"Bắt đầu {node}", {"node": node, "fixture_mode": self.fixture_mode})
            try:
                loaded, fingerprints = {}, []
                for name in files:
                    raw = (self.sample_dir / name).read_bytes()
                    loaded[name] = json.loads(raw.decode("utf-8-sig"))
                    fingerprints.append({"file": name, "sha256": sha256(raw).hexdigest()})
                if files:
                    self.db.add_event(project_id, agent, "fixture_read", "Đã đọc JSON mẫu", {"node": node, "files": fingerprints})
                result = action(state, loaded)
                workspace = WorkspaceStore(self.db)
                override = workspace.read(project_id)["overrides"].get(agent, {})
                if override:
                    validators = {"trip_request": TripRequest, "places": list[Place], "itinerary": list[DayPlan], "budget": Budget,
                        "content": list[Fact[str]], "content_document": ContentDocument, "photos": list[Photo], "review": Review}
                    for key, value in override.items():
                        if key not in validators: continue
                        value = TypeAdapter(validators[key]).validate_python(value)
                        result[key] = value
                        if key == "trip_request":
                            self.db.update_project(project_id, trip_request=value)
                        if key == "content_document":
                            result["content"] = value.as_facts()
                            content_artifact = self._save_json(state, "content.json", value.as_facts(), agent, "content", confidence=.5, source="user:validated-editorial-edit")
                            result["artifacts"] = [item for item in result.get("artifacts", []) if item.id != content_artifact.id]+[content_artifact]
                        filename = {"places": "research.json", "trip_request": "trip_request.json"}.get(key, key+".json")
                        kind = {"places":"research","trip_request":"plan","content_document":"content"}.get(key,key)
                        artifact = self._save_json(state, filename, value, agent, kind, confidence=1, source="user:validated-content-edit")
                        result["artifacts"] = [item for item in result.get("artifacts", []) if item.id != artifact.id] + [artifact]
                if node not in VISUAL_NODES:
                    result.setdefault("stage", stage)
                for artifact in result.get("artifacts", []):
                    self.db.add_artifact(artifact)
                self.db.set_task_status(project_id, task_id, "done")
                with self.db.connect() as connection:
                    connection.execute("UPDATE tasks SET due_at=COALESCE(due_at,?) WHERE project_id=? AND id=?", (utcnow(), project_id, task_id))
                self.db.add_event(project_id, agent, "node_completed", f"Hoàn tất {node}",
                                  {"node": node, "artifact_ids": [artifact.id for artifact in result.get("artifacts", [])]})
                notifications = getattr(self,'notifications',None)
                if notifications is not None and not self.fixture_mode:
                    notifications.agent_completed(state,node,result)
                return result
            except Exception as error:
                self.db.set_task_status(project_id, task_id, "blocked")
                self.db.add_event(project_id, agent, "node_failed", f"Lỗi {node}",
                                  {"node": node, "error_type": type(error).__name__})
                raise
        return execute

    def _build(self, *, checkpointer=None):
        graph = StateGraph(State)
        definitions = [
            ("pm", self._pm, ("content.json", "itinerary.json", "budget.json"), 5, "pm"),
            ("researcher", self._researcher, ("research.json",), 15, "researcher"),
            ("planner", self._planner, ("itinerary.json",), 25, "planner"),
            ("budget", self._budget, ("budget.json",), 35, "budget"),
            ("content_editor", self._content, ("content.json",), 45, "content_editor"),
            ("image_curator", self._photos, ("photos.json",), 55, "visuals"),
            ("illustrator", self._illustrator, ("content.json",), 55, "visuals"),
            ("map_agent", self._maps, ("research.json", "itinerary.json", "budget.json"), 55, "visuals"),
            ("reviewer", self._review, ("research.json", "itinerary.json", "budget.json", "photos.json"), 80, "reviewer"),
            ("layout_editor", self._layout, ("research.json", "itinerary.json", "budget.json", "content.json", "photos.json"), 90, "layout_editor"),
            ("pm_finalize", self._finalize, ("content.json",), 98, "layout_editor"),
        ]
        for node, action, files, progress, stage in definitions:
            graph.add_node(node, self._wrap(node, action, files, progress, stage))
        supervised = self.settings.supervisor_enabled
        def scheduled(role):
            return "schedule_"+role if supervised else role
        if supervised:
            graph.add_node("supervisor", self.supervisor)
            graph.add_conditional_edges("supervisor", self.supervisor.dispatch, list(DEPENDENCIES))
            for role in (*DEPENDENCIES, "visuals"):
                frontier = list(VISUAL_NODES) if role == "visuals" else [role]
                graph.add_node("schedule_"+role, lambda state, frontier=frontier: {"supervisor_frontier":frontier})
                graph.add_edge("schedule_"+role, "supervisor")
            graph.add_node("visuals_done", lambda state: {"supervisor_frontier":state.get("supervisor_remaining") or ["reviewer"]})
            graph.add_edge("visuals_done", "supervisor")
        def entry(state):
            node = state.get("resume_node", "pm")
            if node == "visuals": return "schedule_visuals" if supervised else self._dispatch(state)
            if node == "pm" and supervised: return "schedule_pm"
            if node == "revision_gate" or node.startswith("repair_"): return node
            if node not in {item[0] for item in definitions}: raise ValueError("Invalid resume node")
            return node
        graph.add_conditional_edges(START, entry, [item[0] for item in definitions]+["revision_gate"]+["repair_"+role for role in REPAIR_PLANS]+(["schedule_pm","schedule_visuals"] if supervised else []))
        manual = ManualApprovals(self)
        for node, following in (("pm", "researcher"), ("planner", "budget"),
                                ("reviewer", "revision_gate"), ("layout_editor", "pm_finalize")):
            gate = "approve_"+node
            graph.add_node(gate, manual.node(node, following))
            graph.add_edge(node, gate)
            targets = {following: scheduled(following) if following in DEPENDENCIES else following, node: node}
            if node == "reviewer":
                targets = {following: following, **{"human_"+role: "human_"+role for role in VISUAL_NODES}}
            graph.add_conditional_edges(gate, lambda state: state["approval_route"], targets)
        graph.add_edge("researcher", scheduled("planner"))
        graph.add_edge("budget", scheduled("content_editor"))
        if supervised:
            graph.add_edge("content_editor", "schedule_visuals")
        else:
            graph.add_conditional_edges("content_editor", self._dispatch, list(VISUAL_NODES))
        # A barrier waits for all three different roles, not just the first result.
        if supervised:
            # Each selected batch joins after its superstep. The supervisor keeps
            # unselected visual roles pending; Reviewer needs all three done.
            for role in VISUAL_NODES:
                graph.add_edge(role, "visuals_done")
        else:
            graph.add_edge(list(VISUAL_NODES), "reviewer")
        actions = {node: self._wrap(node, action, files, progress, stage)
                   for node, action, files, progress, stage in definitions if node in REPAIR_PLANS}
        # Visual requests repair one chosen role, then re-review the merged result.
        for role in VISUAL_NODES:
            graph.add_node("human_"+role, actions[role])
            graph.add_edge("human_"+role, "reviewer")
        # The approval gate already owns its conditional edge to revision_gate.
        self.revisions.attach(graph, actions, review_gate="approve_reviewer", layout=scheduled("layout_editor"))
        graph.add_edge("pm_finalize", END)
        return graph.compile(checkpointer=checkpointer)

    @staticmethod
    def _dispatch(state):
        return [Send(node, state) for node in VISUAL_NODES]

    def _pm(self, state, loaded):
        content = TypeAdapter(list[Fact[str]]).validate_python(loaded["content.json"])
        days = TypeAdapter(list[DayPlan]).validate_python(loaded["itinerary.json"])
        budget = Budget.model_validate(loaded["budget.json"])
        # Explicit fixed brief for this Da Nang fixture, not extraction by an LLM.
        request = TripRequest(destination=self._fact("Đà Nẵng", "fixture:sample-brief"),
                              days=self._fact(len(days), "calculation:len(itinerary.json)"),
                              budget=budget.limit, currency=budget.currency,
                              travelers=self._fact(1, "fixture:sample-brief; one adult"))
        plan = [*content[:1], self._fact("Chạy offline từ JSON mẫu; không xác minh dữ kiện thực tế.", "fixture:workflow-scope")]
        artifact = self._save_json(state, "plan.json", {"trip_request": request, "plan": plan}, "pm", "plan", confidence=1)
        self.db.update_project(state["project_id"], trip_request=request)
        return {"trip_request": request, "plan": plan, "artifacts": [artifact]}

    def _researcher(self, state, loaded):
        places = TypeAdapter(list[Place]).validate_python(loaded["research.json"])
        artifact = self._save_json(state, "research.json", places, "researcher", "research", confidence=0.3)
        return {"places": places, "research": [self._fact("Địa điểm lấy từ fixture chưa xác minh.", "fixture:research.json", 0.3)],
                "artifacts": [artifact]}

    def _planner(self, state, loaded):
        days = TypeAdapter(list[DayPlan]).validate_python(loaded["itinerary.json"])
        known = {place.id for place in state["places"]}
        if not days or len({day.day_number for day in days}) != len(days) or any(
            stop.place_id not in known for day in days for stop in day.stops
        ):
            raise ValueError("Fixture itinerary has duplicate days or unknown places")
        artifact = self._save_json(state, "itinerary.json", days, "planner", "itinerary", confidence=0.3)
        return {"itinerary": days, "artifacts": [artifact]}

    def _budget(self, state, loaded):
        budget = Budget.model_validate(loaded["budget.json"])
        artifact = self._save_json(state, "budget.json", budget, "budget", "budget", confidence=budget.total.confidence)
        return {"budget": budget, "artifacts": [artifact]}

    def _content(self, state, loaded):
        content = TypeAdapter(list[Fact[str]]).validate_python(loaded["content.json"])
        artifact = self._save_json(state, "content.json", content, "content_editor", "content",
                                   confidence=min((fact.confidence for fact in content), default=0))
        return {"content": content, "artifacts": [artifact]}

    def _photos(self, state, loaded):
        photos = TypeAdapter(list[Photo]).validate_python(loaded["photos.json"])
        artifact = self._save_json(state, "photos.json", photos, "image_curator", "photo", confidence=0)
        return {"photos": photos, "artifacts": [artifact]}

    def _illustrator(self, state, loaded):
        content = TypeAdapter(list[Fact[str]]).validate_python(loaded["content.json"])
        prompts = {"cover": self.style_lock.apply(content[0].value if content else "Travel guide cover")}
        prompts.update({f"day{day.day_number}": self.style_lock.apply(
            day.title.value if day.title else f"Day {day.day_number} illustration") for day in state["itinerary"]})
        artifacts = []
        for name in prompts:
            path = self._directory(state) / f"illustrations/{name}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            placeholder_image((1024, 1536) if name == "cover" else (1200, 800)).save(path)
            artifacts.append(self._artifact(state, path, "illustrator", "illustration",
                                            source="generated:solid-background-placeholder; no image model called", confidence=0))
        prompt_artifact = self._save_json(state, "illustrations/prompts.json",
            {"style_signature": self.style_lock.signature, "prompts": prompts, "placeholder_only": True},
            "illustrator", "illustration", confidence=0)
        return {"illustrations": artifacts, "artifacts": [*artifacts, prompt_artifact]}

    def _maps(self, state, loaded):
        places = TypeAdapter(list[Place]).validate_python(loaded["research.json"])
        days = TypeAdapter(list[DayPlan]).validate_python(loaded["itinerary.json"])
        budget = Budget.model_validate(loaded["budget.json"])
        folder = self._directory(state)
        map_paths = render_day_maps(days, places, folder / "maps")
        chart_paths = [render_budget_chart(budget, folder / "charts/budget.png"),
                       render_weather_chart(days, folder / "charts/weather.png"),
                       render_timeline_chart(days, places, folder / "charts/timeline.png")]
        def artifact(path, kind):
            with Image.open(path) as image:
                confidence = float(image.info["Confidence"])
            return self._artifact(state, path, "map_agent", kind, source="derived:validated-fixture-json; offline-render", confidence=confidence)
        maps, charts = [artifact(path, "map") for path in map_paths], [artifact(path, "chart") for path in chart_paths]
        return {"maps": maps, "charts": charts, "artifacts": [*maps, *charts]}

    def _review(self, state, loaded):
        # Read and validate all reviewer fixtures; review also verifies the merged branches.
        TypeAdapter(list[Place]).validate_python(loaded["research.json"])
        TypeAdapter(list[DayPlan]).validate_python(loaded["itinerary.json"])
        Budget.model_validate(loaded["budget.json"])
        photos = TypeAdapter(list[Photo]).validate_python(loaded["photos.json"])
        if not state.get("illustrations") or len(state.get("maps", [])) != len(state["itinerary"]) or len(state.get("charts", [])) != 3:
            raise ValueError("Fan-in did not receive all visual branches")
        if len(state.get("photos", [])) != len(photos):
            raise ValueError("Photo branch result is missing")
        for artifact in state["artifacts"]:
            if not Path(artifact.path).is_file():
                raise FileNotFoundError(artifact.path)
            if artifact.mime_type == "image/png":
                with Image.open(artifact.path) as image:
                    image.verify()
        summary = self._fact("Chỉ đạt kiểm tra cấu trúc luồng demo; chưa duyệt dữ kiện du lịch hoặc giấy phép ảnh.",
                             "validation:fixture-structure-only")
        review = Review(source="validation:fixture-structure-only", confidence=1, verdict="PASS", summary=summary,
                        issues=[ReviewIssue(category="photo", severity="minor", target_agent="image_curator",
                            message=self._fact("Ảnh vẫn là placeholder và không được nhúng vào PDF demo.", "fixture:photos.json"))])
        artifact = self._save_json(state, f"review_reports/fixture_review_r{state.get('revision_count', 0)}.json", review, "reviewer", "review", confidence=1,
                                   source="validation:fixture-structure-only")
        self.db.add_event(state["project_id"], "reviewer", "fixture_review", summary.value,
                          {"scope": "structure_only", "production_approved": False})
        return {"review": review, "production_approved": False, "artifacts": [artifact]}

    def _layout(self, state, loaded):
        # Wrapper reads original fixtures; layout consumes the copies produced by earlier nodes.
        for name in ("research.json", "itinerary.json", "budget.json", "content.json", "photos.json"):
            if not isinstance(loaded[name], (dict, list)):
                raise ValueError("Invalid fixture document")
        # Repairing layout and final delivery must not overwrite the same artifact IDs.
        output = self._directory(state) / "layout_repair" / "travelbook.pdf" if state.get("revision_pending", [None])[:1] == ["layout_editor"] else None
        path = self.layout.build_from_json(self._directory(state), output, warnings=state.get("revision_warnings", []))
        pdf = self._artifact(state, path, "layout_editor", "pdf", source="derived:fixture-PDF; draft-only", confidence=0)
        manifest = self._artifact(state, path.with_suffix(".manifest.json"), "layout_editor", "review",
                                  source="validation:layout-manifest", confidence=1)
        return {"final_pdf": pdf, "artifacts": [pdf, manifest]}

    def _finalize(self, state, loaded):
        TypeAdapter(list[Fact[str]]).validate_python(loaded["content.json"])
        pdf = state.get("final_pdf")
        if pdf is None or not Path(pdf.path).read_bytes().startswith(b"%PDF-"):
            raise ValueError("Final PDF missing or invalid")
        self.db.add_event(state["project_id"], "pm", "demo_accepted", "PM xác nhận hoàn tất đồ thị demo và PDF",
                          {"pdf": pdf.path, "production_approved": False})
        return {"completed": True, "stage": "completed", "error": None}

    def run(self, *, project_id: str | None = None, user_prompt: str = "Demo Đà Nẵng 3 ngày, một người lớn, ngân sách 5 triệu") -> State:
        project_id = project_id or "demo-" + uuid4().hex[:16]
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", project_id):
            raise ValueError("project_id must be a safe directory name")
        self.db.init()
        if self.db.get_project(project_id) is not None:
            raise ValueError("Project already exists; use a new project_id")
        folder = self.artifacts_root / project_id
        if folder == self.sample_dir or (folder.exists() and any(folder.iterdir())):
            raise ValueError("Output directory must be empty and separate from fixtures")
        folder.mkdir(parents=True, exist_ok=True)
        initial: State = {"project_id": project_id, "user_prompt": user_prompt, "fixture_mode": True,
                          "production_approved": False, "stage": "pm", "artifacts": [],
                          "revision_count": 0, "revision_pending": [], "revision_exhausted": False, "revision_warnings": [], "completed": False, "error": None}
        self.db.create_project(project_id, "Đà Nẵng - demo StateGraph", user_prompt, state=initial)
        tasks = build_tasks(project_id, descriptive_titles=False)
        latest = initial
        try:
            self.db.replace_tasks(project_id, tasks)
            self.db.update_project(project_id, status="running")
            for snapshot in self.graph.stream(initial, {"recursion_limit": 150 + 24*self.settings.max_revisions}, stream_mode="values"):
                latest = dict(snapshot)
                self.db.update_project(project_id, state=latest)
            if not latest.get("completed") or latest.get("final_pdf") is None:
                raise RuntimeError("Graph ended without a completed PDF")
            self.db.update_project(project_id, status="completed", stage="completed", progress=100,
                                   final_pdf=latest["final_pdf"].path, state=latest, error=None)
            self.db.add_event(project_id, "system", "workflow_completed", "Đã đi hết đồ thị JSON mẫu", {"fixture_mode": True})
            return latest
        except Exception as error:
            self.db.update_project(project_id, status="failed", error=f"{type(error).__name__}: {error}", state=latest)
            self.db.add_event(project_id, "system", "workflow_failed", "Đồ thị demo thất bại", {"error_type": type(error).__name__})
            raise
