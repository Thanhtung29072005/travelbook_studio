"""Real visual agents: sequential baseline or Send fan-out, with one durable fan-in."""

import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
import threading
import time
from typing import Annotated, Literal
from uuid import uuid4

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send
from pydantic import Field, TypeAdapter

from .agents.base import BaseAgent
from .agents.illustrator import IllustratorAgent
from .agents.image_curator import ImageCuratorAgent
from .agents.map_agent import MapAgent
from .db import utcnow
from .schemas import Artifact, Photo, Schema, State, TripRequest, merge_artifacts
from .services.illustrations import IllustrationService
from .services.photos import PhotoService
from .services.public_api import PublicAPIClient

VISUAL_ROLES = ("image_curator", "illustrator", "map_agent")
CHANNELS = {"image_curator": {"photos", "photo_warnings"}, "illustrator": {"illustrations", "illustration_warnings"},
            "map_agent": {"maps", "charts", "map_warnings", "map_input_paths"}}


class VisualBranchResult(Schema):
    agent: Literal["image_curator", "illustrator", "map_agent"]
    status: Literal["done", "needs_attention", "failed"]
    update: dict[str, object] = Field(default_factory=dict)
    started_ns: int
    ended_ns: int
    thread_id: int
    error_type: str | None = None


def merge_visual_results(left, right):
    result = dict(left or {})
    for role, raw in (right or {}).items():
        value = VisualBranchResult.model_validate(raw)
        if role != value.agent:
            raise ValueError("Visual branch key and agent disagree")
        if role in result and VisualBranchResult.model_validate(result[role]).model_dump(mode="json") != value.model_dump(mode="json"):
            raise ValueError("Conflicting visual branch replay: " + role)
        result[role] = value
    return result


class VisualTeamState(State, total=False):
    visual_run_id: str
    visual_mode: str
    visual_results: Annotated[dict[str, VisualBranchResult], merge_visual_results]
    visual_complete: bool
    visual_report: Artifact
    visual_input_files: dict[str, str]
    visual_input_hashes: dict[str, str]


class VisualTeam(BaseAgent):
    # Team orchestration belongs to PM; it does not introduce an eleventh domain agent.
    agent_id = "pm"
    display_name = "Visual Team"

    def __init__(self, db, settings, artifacts_root=None, *, agent_factories=None):
        super().__init__(db, None, artifacts_root or settings.artifacts_dir)
        self.settings = settings.model_copy(deep=True)
        self.factories = agent_factories or self._default_factories()
        if set(self.factories) != set(VISUAL_ROLES):
            raise ValueError("Provide exactly the three visual agent factories")
        self.graphs = {mode: self._build(mode) for mode in ("sequential", "parallel")}

    def _default_factories(self):
        @asynccontextmanager
        async def curator():
            http = PublicAPIClient(self.settings)
            try:
                yield ImageCuratorAgent(self.db, PhotoService(self.settings, http), self.artifacts_root)
            finally:
                await http.aclose()

        @asynccontextmanager
        async def illustrator():
            service = IllustrationService(self.settings)
            try:
                yield IllustratorAgent(self.db, service, self.artifacts_root)
            finally:
                await service.aclose()

        @asynccontextmanager
        async def maps():
            yield MapAgent(self.db, self.artifacts_root)

        return {"image_curator": curator, "illustrator": illustrator, "map_agent": maps}

    def _validate_update(self, role, update, project_id):
        common = {"artifacts", "stage", "fixture_mode", "production_approved", "error"}
        if set(update) - CHANNELS[role] - common:
            raise ValueError("Agent wrote another branch's channel")
        selected = {key: value for key, value in update.items() if key in CHANNELS[role] or key == "artifacts"}
        selected["artifacts"] = TypeAdapter(list[Artifact]).validate_python(selected.get("artifacts", []))
        root = self.artifacts_root / project_id
        for artifact in selected["artifacts"]:
            path = Path(artifact.path).resolve()
            if artifact.project_id != project_id or artifact.agent_id != role or not path.is_relative_to(root) or not path.is_file():
                raise ValueError("Invalid branch artifact ownership or path")
        if role == "image_curator":
            selected["photos"] = TypeAdapter(list[Photo]).validate_python(selected.get("photos", []))
        else:
            for channel in ("illustrations",) if role == "illustrator" else ("maps", "charts"):
                values = TypeAdapter(list[Artifact]).validate_python(selected.get(channel, []))
                if any(value.id not in {item.id for item in selected["artifacts"]} for value in values):
                    raise ValueError("Output channel references an unmerged artifact")
                selected[channel] = values
        return selected

    def _worker(self, role, state):
        # Each thread owns its event loop, agent and HTTP clients for the whole branch.
        async def invoke():
            project_id = state["project_id"]
            started, thread_id = time.perf_counter_ns(), threading.get_ident()
            self.db.add_event(project_id, role, "visual_branch_started", "Bắt đầu nhánh hình ảnh",
                {"visual_run_id": state["visual_run_id"], "role": role, "started_ns": started, "thread_id": thread_id})
            try:
                async with self.factories[role]() as agent:
                    update = await agent.run(deepcopy({key: value for key, value in state.items() if key != "visual_results"}), managed=True)
                selected = self._validate_update(role, update, project_id)
                owned = [task for task in self.db.get_tasks(project_id) if task["agent"] == role]
                status = "done" if owned and all(task["status"] == "done" for task in owned) else "needs_attention"
                result = VisualBranchResult(agent=role, status=status, update=selected,
                    started_ns=started, ended_ns=time.perf_counter_ns(), thread_id=thread_id)
            except Exception as error:
                for task in self.db.get_tasks(project_id):
                    if task["agent"] == role:
                        self.db.set_task_status(project_id, task["id"], "blocked")
                result = VisualBranchResult(agent=role, status="failed", error_type=type(error).__name__,
                    started_ns=started, ended_ns=time.perf_counter_ns(), thread_id=thread_id)
            self.db.add_event(project_id, role, "visual_branch_finished", "Đã thu kết quả nhánh hình ảnh",
                {"visual_run_id": state["visual_run_id"], "role": role, "status": result.status,
                 "ended_ns": result.ended_ns, "error_type": result.error_type})
            return {"visual_results": {role: result}}
        return asyncio.run(invoke())

    def _node(self, role):
        async def execute(state):
            return await asyncio.to_thread(self._worker, role, state)
        return execute

    @staticmethod
    def _dispatch(state):
        return [Send(role, state) for role in VISUAL_ROLES]

    def _build(self, mode):
        graph = StateGraph(VisualTeamState)
        graph.add_node("visual_dispatch", lambda state: {})
        graph.add_edge(START, "visual_dispatch")
        for role in VISUAL_ROLES:
            graph.add_node(role, self._node(role))
        graph.add_node("visual_fan_in", self._fan_in)
        if mode == "parallel":
            graph.add_conditional_edges("visual_dispatch", self._dispatch, list(VISUAL_ROLES))
            graph.add_edge(list(VISUAL_ROLES), "visual_fan_in")
        else:
            graph.add_edge("visual_dispatch", VISUAL_ROLES[0])
            for left, right in zip(VISUAL_ROLES, VISUAL_ROLES[1:]):
                graph.add_edge(left, right)
            graph.add_edge(VISUAL_ROLES[-1], "visual_fan_in")
        graph.add_edge("visual_fan_in", END)
        return graph.compile()

    def _fan_in(self, state):
        results = {role: VisualBranchResult.model_validate(value) for role, value in state.get("visual_results", {}).items()}
        if set(results) != set(VISUAL_ROLES):
            raise ValueError("Fan-in needs all three terminal results")
        for role, path in state["map_input_paths"].items():
            if sha256(Path(path).read_bytes()).hexdigest() != state["visual_input_hashes"][role]:
                raise ValueError("Visual input snapshot changed during execution")
        merged, artifacts = {}, []
        for role in VISUAL_ROLES:
            result = results[role]
            merged.update({key: value for key, value in result.update.items() if key in CHANNELS[role]})
            artifacts = merge_artifacts(artifacts, result.update.get("artifacts", []))
        complete = all(result.status == "done" for result in results.values())
        if complete:
            expected_places = {place.id if hasattr(place, "id") else place["id"] for place in state["places"]}
            photos = merged.get("photos", [])
            days = TripRequest.model_validate(state["trip_request"]).days.value
            if (len(photos) != len(expected_places) or {photo.place_id for photo in photos} != expected_places
                or len(merged.get("illustrations", [])) != days + 7
                or len(merged.get("maps", [])) != days or len(merged.get("charts", [])) != 3):
                raise ValueError("All agents finished but visual output coverage is incomplete")
        starts = [result.started_ns for result in results.values()]
        ends = [result.ended_ns for result in results.values()]
        report = {"mode": state["visual_mode"], "visual_run_id": state["visual_run_id"], "complete": complete,
            "input_files": state["visual_input_files"], "input_hashes": state["visual_input_hashes"],
            "branches": results, "all_three_overlap": max(starts) < min(ends),
            "wall_seconds": (max(ends)-min(starts))/1e9,
            "branch_seconds": {role: (result.ended_ns-result.started_ns)/1e9 for role, result in results.items()},
            "counts": {key: len(merged.get(key, [])) for key in ("photos", "illustrations", "maps", "charts")},
            "artifact_ids": [artifact.id for artifact in artifacts], "fan_in_count": 1}
        manifest = self.save_json(state["project_id"], state["visual_run_id"], "visual_team.json", report,
            kind="review", source="execution:VisualTeam; three-agent fan-in", confidence=1)
        artifacts.append(manifest)
        project_id = state["project_id"]
        for task in self.db.get_tasks(project_id):
            if task["agent"] == "reviewer":
                self.db.set_task_status(project_id, task["id"], "ready" if complete else "todo")
        failed = any(result.status == "failed" for result in results.values())
        stage = "reviewer" if complete else "needs_attention"
        update = {**merged, "artifacts": artifacts, "stage": stage, "visual_complete": complete,
                  "visual_report": manifest, "production_approved": False,
                  "error": "Visual branch failed; see visual_team.json" if failed else None}
        self.db.update_project(project_id, status="queued" if complete else "failed" if failed else "needs_attention", stage=stage,
            progress=60 if complete else 45, error=update["error"], state={**state, **update,
                "artifacts": merge_artifacts(state.get("artifacts", []), artifacts)})
        self.event(project_id, "visual_fan_in_completed", "Đã gộp kết quả của đủ ba nhánh",
            {"visual_run_id": state["visual_run_id"], "complete": complete, "counts": report["counts"], "all_three_overlap": report["all_three_overlap"]})
        return update

    async def run(self, state: State, *, mode="parallel"):
        if mode not in self.graphs:
            raise ValueError("Visual mode must be sequential or parallel")
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        TripRequest.model_validate(state.get("trip_request"))
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        for role in VISUAL_ROLES:
            owned = [task for task in tasks if task["agent"] == role]
            if not owned or any(task["status"] == "in_progress" for task in owned) or any(
                dep not in by_id or by_id[dep]["status"] != "done" for task in owned for dep in task["depends_on"]):
                raise ValueError("Visual tasks require completed Content Editor dependencies")
        if not any(task["agent"] == "reviewer" for task in tasks):
            raise ValueError("Visual team needs a Reviewer task")
        reader = MapAgent(self.db, self.artifacts_root)
        files = reader._resolve_inputs(state, None)
        loaded, days, places, budget, _, _, _ = reader._load(files, state["trip_request"])
        with self.db.connect() as connection:
            claimed = connection.execute("UPDATE projects SET status='running',stage='visuals',progress=45,error=NULL,updated_at=? "
                "WHERE id=? AND status IN ('queued','paused','needs_attention')", (utcnow(), project_id)).rowcount
            if claimed != 1:
                raise ValueError("Project is missing or already running/completed/failed")
        for task in tasks:
            if task["agent"] == "reviewer":
                self.db.set_task_status(project_id, task["id"], "todo")
        try:
            run_id = uuid4().hex[:16]
            snapshots = {}
            for role, (_, raw, _) in loaded.items():
                snapshot = self.artifacts_root / project_id / "visual_inputs" / run_id / (role + ".json")
                snapshot.parent.mkdir(parents=True, exist_ok=True)
                snapshot.write_bytes(raw)
                snapshots[role] = str(snapshot)
            initial = {**state, "visual_run_id": run_id, "visual_mode": mode, "visual_results": {},
                "visual_complete": False, "stage": "visuals", "production_approved": False, "error": None,
                "itinerary": days, "places": places, "budget": budget, "map_input_paths": snapshots,
                "visual_input_files": {role: str(path) for role, (path, _, _) in loaded.items()},
                "visual_input_hashes": {role: sha256(raw).hexdigest() for role, (_, raw, _) in loaded.items()}}
            self.event(project_id, "visual_team_started", "Chạy Visual Team", {"mode": mode, "visual_run_id": run_id})
            return await self.graphs[mode].ainvoke(initial, config={"max_concurrency": 3})
        except Exception as error:
            self.db.update_project(project_id, status="failed", stage="needs_attention", error="Visual Team failed: " + type(error).__name__)
            self.event(project_id, "visual_team_failed", "Không gộp được bộ kết quả hợp lệ", {"error_type": type(error).__name__})
            raise
