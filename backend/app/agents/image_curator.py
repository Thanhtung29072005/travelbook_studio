"""Select licensed photo metadata and credits; never fill missing results with fake photos."""

from uuid import uuid4

from pydantic import Field, TypeAdapter

from ..attribution import AttributionRecord, attribution_record
from ..schemas import Fact, Photo, Place, Schema, State, Text, TripRequest
from ..services.reasoning import DecisionEngine, DecisionRecord
from typing import Literal
from .base import BaseAgent


class CurationResult(Schema):
    photos: list[Photo] = Field(default_factory=list)
    attributions: list[AttributionRecord] = Field(default_factory=list)
    missing_place_ids: list[Text] = Field(default_factory=list)
    warnings: list[Fact[Text]] = Field(default_factory=list)
    reasoning: list[DecisionRecord] = Field(default_factory=list)


class CurationStrategy(Schema):
    provider_priority: list[Literal["wikimedia", "unsplash", "pexels"]] = Field(min_length=1, max_length=3)
    rationale: str = Field(min_length=1, max_length=900)


class PhotoChoice(Schema):
    photo_id: str | None
    rationale: str = Field(min_length=1, max_length=900)


class ImageCuratorAgent(BaseAgent):
    agent_id = "image_curator"
    display_name = "Image Curator"
    SYSTEM = """Bạn là Image Curator. Chọn thứ tự công cụ tìm ảnh và một ảnh phù hợp từng địa điểm
theo caption, tên, tỷ lệ kích thước và chủ đề. Backend đã lọc license/kích thước/mức khớp tên;
chỉ chọn photo_id trong candidates hoặc null để thử provider khác. Không sửa license/tác giả/URL.
Bạn chỉ xem metadata văn bản, không nhìn pixels; không khẳng định đã xác minh cảnh thật.
"""

    def __init__(self, db, photos_service, artifacts_root, *, router=None):
        super().__init__(db, None, artifacts_root)
        self.photos_service = photos_service
        self.reasoning = DecisionEngine(self.agent_id, photos_service.settings, router=router, event_sink=self.event)

    async def curate(self, places, destination="", *, on_event=None, project_id=None, on_attempt=None, feedback=()):
        places = TypeAdapter(list[Place]).validate_python(places)
        if not places or len(places) > 30 or len({place.id for place in places}) != len(places):
            raise ValueError("Curator needs 1..30 unique places")
        result, used = CurationResult(), set()
        self.reasoning.begin(project_id)
        available = self.photos_service.settings.photo_provider_order.split(",")
        def valid_strategy(value):
            if len(value.provider_priority) != len(set(value.provider_priority)) or set(value.provider_priority) - set(available):
                raise ValueError("Curator tool not available")
        strategy = await self.reasoning.decide("curation_strategy", self.SYSTEM, CurationStrategy,
            {"destination": destination, "places": [place.model_dump(mode="json") for place in places], "available_providers": available,
             "feedback":[item.model_dump(mode='json') for item in feedback]},
            validate=valid_strategy, on_attempt=on_attempt)
        providers = list(dict.fromkeys([*(strategy.provider_priority if strategy else []), *available]))
        for place in places:
            async def choose(provider, candidates):
                def valid_choice(value):
                    if value.photo_id is not None and value.photo_id not in {candidate.photo.id for candidate in candidates}:
                        raise ValueError("Unknown photo choice")
                choice = await self.reasoning.decide("photo_choice:" + place.id + ":" + provider, self.SYSTEM, PhotoChoice,
                    {"place": place.model_dump(mode="json"), "provider": provider, "feedback":[item.model_dump(mode='json') for item in feedback], "candidates": [
                        {"photo_id": candidate.photo.id, "title": candidate.title.value, "width": candidate.width.value,
                         "height": candidate.height.value, "license": candidate.photo.license.value, "relevance": candidate.relevance.value}
                        for candidate in candidates]}, validate=valid_choice, on_attempt=on_attempt)
                return choice.photo_id if choice is not None else candidates[0].photo.id
            found = await self.photos_service.select(place, destination, used=used, on_event=on_event,
                provider_order=providers, candidate_selector=choose if self.reasoning.enabled else None)
            result.warnings.extend(found.warnings)
            if found.selected is None:
                result.missing_place_ids.append(place.id)
            else:
                result.attributions.append(attribution_record(found.selected))
                result.photos.append(found.selected)
        result.reasoning = self.reasoning.records
        return result

    async def run(self, state: State, *, managed=False) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        if project is None or project["status"] not in {"queued", "paused", "needs_attention", "running"}:
            raise ValueError("Image Curator requires a runnable project")
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        owned = [task for task in tasks if task["agent"] == self.agent_id]
        if not owned or any(dep not in by_id or by_id[dep]["status"] != "done" for task in owned for dep in task["depends_on"]):
            raise ValueError("Complete Content Editor dependencies first")
        if not managed:
            self.db.update_project(project_id, status="running", stage="visuals", progress=50, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Image Curator tìm ảnh và metadata giấy phép")
        def on_event(payload):
            self.event(project_id, "photo_cache_hit" if payload["cache_hit"] else "photo_request", "Truy cập nguồn ảnh", payload)
        try:
            trip = TripRequest.model_validate(state["trip_request"]) if state.get("trip_request") is not None else None
            result = await self.curate(state["places"], trip.destination.value if trip else "", on_event=on_event,
                project_id=project_id, on_attempt=self.model_attempt(project_id))
            confidence = min((min(photo.author.confidence, photo.license.confidence, photo.selection_note.confidence)
                for photo in result.photos), default=0)
            run_id = uuid4().hex[:16]
            artifacts = [self.save_json(project_id, run_id, "photos.json", result.photos, kind="photo",
                source="derived:ImageCurator provider metadata; semantic match unverified", confidence=confidence),
                self.save_json(project_id, run_id, "attribution.json", result.attributions, kind="photo",
                source="derived:provider attribution metadata", confidence=confidence),
                self.save_json(project_id, run_id, "photo_report.json", result, kind="review",
                source="derived:ImageCurator coverage report", confidence=confidence)]
            complete = not result.missing_place_ids
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done" if complete else "blocked")
            # Re-read branch statuses: this branch alone must not release Reviewer.
            fresh = self.db.get_tasks(project_id)
            completed = {task["id"] for task in fresh if task["status"] == "done"}
            reviewer_ready = False
            if complete and not managed:
                for task in fresh:
                    if task["agent"] == "reviewer" and all(dep in completed for dep in task["depends_on"]):
                        self.db.set_task_status(project_id, task["id"], "ready")
                        reviewer_ready = True
            stage = "needs_attention" if not complete else "reviewer" if reviewer_ready else "visuals"
            update: State = {"photos": result.photos, "photo_warnings": result.warnings, "stage": stage,
                "artifacts": artifacts, "fixture_mode": False, "production_approved": False, "error": None}
            if not managed:
                self.db.update_project(project_id, status="queued" if complete else "needs_attention", stage=stage,
                    progress=55 if complete else 45, state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "photos_ready" if complete else "photos_needs_attention", "Đã lưu ảnh được chọn và hồ sơ ghi công",
                {"photo_count": len(result.photos), "missing_place_ids": result.missing_place_ids, "reviewer_ready": reviewer_ready})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            if not managed:
                self.db.update_project(project_id, status="failed", error=f"Image Curator failed: {type(error).__name__}")
            self.event(project_id, "agent_failed", "Image Curator chưa hoàn tất", {"error_type": type(error).__name__})
            raise
