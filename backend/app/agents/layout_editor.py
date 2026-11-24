"""Deterministic final book layout from the real agent State and local image assets."""

import json
from pathlib import Path
import re
from uuid import uuid4

from pydantic import TypeAdapter

from ..schemas import Artifact, Budget, ContentDocument, DayPlan, Fact, Photo, Place, Review, State, TripRequest, WeatherReport
from ..services.layout import LayoutService
from ..workspace import WorkspaceStore
from ..services.budget_savings import savings_plan
from .base import BaseAgent


class LayoutEditorAgent(BaseAgent):
    agent_id = "layout_editor"
    display_name = "Layout Editor"

    def __init__(self, db, settings, artifacts_root=None, *, service=None):
        super().__init__(db, None, artifacts_root or settings.artifacts_dir)
        self.settings, self.service = settings, service or LayoutService()

    def _asset(self, artifact, project_id, kind):
        artifact = Artifact.model_validate(artifact)
        path = Path(artifact.path).resolve()
        if artifact.project_id != project_id or artifact.kind != kind or not path.is_relative_to(self.artifacts_root):
            raise ValueError("Layout asset must belong to this project and artifact root")
        if not path.is_file():
            raise FileNotFoundError(path)
        return artifact, path

    def render(self, state):
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        trip = TripRequest.model_validate(state["trip_request"])
        days = TypeAdapter(list[DayPlan]).validate_python(state["itinerary"])
        places = TypeAdapter(list[Place]).validate_python(state["places"])
        budget = Budget.model_validate(state["budget"])
        if budget.currency.value != trip.currency.value or budget.limit.value != trip.budget.value:
            raise ValueError('Budget currency and limit must match the confirmed TripRequest')
        over_budget = budget.total.value > trip.budget.value
        if over_budget and not (state.get('revision_exhausted') and state.get('revision_count',0)>=self.settings.max_revisions and state.get('revision_warnings')):
            raise ValueError('Over-budget itinerary must complete the budget repair loop before PDF export')
        baseline = state.get('budget_baseline')
        baseline_over = baseline is not None and Budget.model_validate(baseline).total.value > trip.budget.value
        plan = savings_plan(budget,trip,self.settings,baseline=baseline) if over_budget or baseline_over else None
        photos = TypeAdapter(list[Photo]).validate_python(state.get("photos", []))
        if [day.day_number for day in days] != list(range(1, trip.days.value+1)):
            raise ValueError("Layout needs a complete ordered itinerary matching TripRequest")
        editorial = ContentDocument.model_validate(state["content_document"]) if state.get("content_document") else None
        content = TypeAdapter(list[Fact[str]]).validate_python(state.get("content", []))
        if not editorial and not content:
            raise ValueError("Layout needs editorial content")
        weather = WeatherReport.model_validate(state["weather"]) if state.get("weather") else None
        maps, charts = {}, {}
        for raw in state.get("maps", []):
            artifact, path = self._asset(raw, project_id, "map")
            match = re.fullmatch(r"day_?(\d+)", path.stem)
            if not match or int(match[1]) in maps:
                raise ValueError("Maps require unique dayN.png names")
            maps[int(match[1])] = path
        for raw in state.get("charts", []):
            artifact, path = self._asset(raw, project_id, "chart")
            if path.stem not in {"budget", "weather", "timeline"} or path.stem in charts:
                raise ValueError("Charts require unique budget/weather/timeline names")
            charts[path.stem] = path
        if set(maps) != {day.day_number for day in days}:
            raise ValueError("Exactly one map is required for each day")
        day_images, cover = {}, None
        for raw in state.get("illustrations", []):
            artifact, path = self._asset(raw, project_id, "illustration")
            if artifact.source.startswith("fallback:"):
                continue
            if path.stem == "cover":
                cover = path
            match = re.fullmatch(r"day_?(\d+)", path.stem)
            if match:
                day_images[int(match[1])] = path
        for photo in photos:
            if photo.local_path:
                local = Path(photo.local_path)
                local = local if local.is_absolute() else self.artifacts_root / local
                if not local.resolve().is_relative_to(self.artifacts_root):
                    raise ValueError("Photo path is outside artifact root")
                photo.local_path = str(local.resolve())
        run_id = uuid4().hex[:16]
        folder = self.artifacts_root / project_id / self.agent_id / run_id
        folder.mkdir(parents=True, exist_ok=True)
        output = self.service.build_pdf(folder / "travelbook.pdf", days=days, places=places, budget=budget,
            content=content, photos=photos, maps=maps, charts=charts, title=f"{trip.destination.value} - Cẩm nang {trip.days.value} ngày",
            image_root=self.artifacts_root, warnings=state.get("revision_warnings", []), with_toc=True, editorial=editorial,
            weather=weather, day_images=day_images, cover_image=cover, low_confidence_threshold=self.settings.layout_low_confidence_threshold,
            illustration_credits=state.get("illustrations", []), trip_request=trip, budget_savings=plan)
        manifest_path = output.with_suffix(".manifest.json")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        artifacts = []
        for path, kind in ((output, "pdf"), (manifest_path, "review")):
            artifact = Artifact(id=uuid4().hex, project_id=project_id, agent_id=self.agent_id, kind=kind, name=path.name,
                path=str(path), mime_type="application/pdf" if kind == "pdf" else "application/json",
                source="derived:LayoutEditorAgent; source facts retained in manifest", confidence=manifest["confidence"] if kind == "pdf" else 1)
            self.db.add_artifact(artifact)
            artifacts.append(artifact)
        return artifacts, manifest

    async def run(self, state: State) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        tasks = self.db.get_tasks(project_id)
        owned = [task for task in tasks if task["agent"] == self.agent_id]
        by_id = {task["id"]: task for task in tasks}
        if project is None or project["status"] not in {"queued", "running", "paused", "needs_attention"}:
            raise ValueError("Layout needs a runnable project")
        if not owned or any(dep not in by_id or by_id[dep]["status"] != "done" for task in owned for dep in task["depends_on"]):
            raise ValueError("Complete Reviewer before Layout")
        review = Review.model_validate(state["review"])
        if review.verdict == "REVISE" and not (state.get("revision_exhausted") and state.get("revision_warnings")) and not WorkspaceStore(self.db).review_approved(project_id, state):
            raise ValueError("Unresolved Review requires repair or an explicit revision-limit warning")
        self.db.update_project(project_id, status="running", stage="layout_editor", progress=85, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Layout Editor creates the paginated travel book")
        try:
            artifacts, manifest = self.render(state)
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done")
            for task in tasks:
                if task["id"].endswith(":pm_finalize"):
                    self.db.set_task_status(project_id, task["id"], "ready")
            update: State = {"final_pdf": artifacts[0], "artifacts": artifacts, "stage": "layout_editor", "production_approved": False, "error": None}
            self.db.update_project(project_id, status="queued", stage="layout_editor", progress=95, final_pdf=artifacts[0].path,
                state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "layout_ready", "PDF and layout audit saved", {"page_count": manifest["page_count"],
                "toc_entries": len(manifest["toc"]), "low_confidence_count": len(manifest["low_confidence_facts"]),
                "text_image_overlap_count": manifest["text_image_overlap_count"], "pdf": artifacts[0].path})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            self.db.update_project(project_id, status="failed", error="Layout failed: " + type(error).__name__)
            self.event(project_id, "agent_failed", "Layout could not create a readable PDF", {"error_type": type(error).__name__})
            raise
