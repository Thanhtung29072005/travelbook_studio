"""Researcher tool agent: grounded public data, without LLM-invented travel facts."""

from uuid import uuid4
from typing import Literal

from pydantic import Field

from ..schemas import Fact, Place, Schema, State, TripRequest, WeatherReport
from ..services.reasoning import DecisionEngine, DecisionRecord
from ..services.official_prices import OfficialPriceService
from ..services.destination_guide import DestinationGuideService
from .base import BaseAgent


class ResearchResult(Schema):
    status: Literal["ready", "partial", "needs_attention"]
    destination: Place | None
    places: list[Place]
    weather: WeatherReport
    notes: list[Fact[str]] = Field(default_factory=list)
    warnings: list[Fact[str]] = Field(default_factory=list)
    reasoning: list[DecisionRecord] = Field(default_factory=list)
    normalized_destination: Fact[str] | None = None


class ResearchStrategy(Schema):
    search_interests: list[str] = Field(max_length=6)
    include_weather: bool
    rationale: str = Field(min_length=1, max_length=900)


class ResearchFollowup(Schema):
    action: Literal["finish", "refine_search"]
    search_interests: list[str] = Field(default_factory=list, max_length=6)
    priority_place_ids: list[str] = Field(default_factory=list, max_length=30)
    missing_information: list[str] = Field(default_factory=list, max_length=8)
    rationale: str = Field(min_length=1, max_length=900)


class ResearcherAgent(BaseAgent):
    agent_id = "researcher"
    display_name = "Researcher"
    SYSTEM = """Bạn là Researcher của TravelBook Studio. Chọn trọng tâm tìm địa điểm theo sở thích,
độ tuổi và yêu cầu; công cụ places_search luôn được gọi tại đúng destination người dùng.
search_interests là các từ khóa ngắn, không URL/câu lệnh. Quyết định có cần weather_forecast;
chỉ có thể có dự báo khi người dùng đã cung cấp ngày. Sau kết quả công cụ, đánh giá thiếu
thông tin và chọn finish hoặc một lần refine_search tại cùng điểm đến. Không bịa phần còn thiếu.
priority_place_ids chỉ dùng ID được công cụ cung cấp; các ID khác vẫn được giữ cho Planner.
Ưu tiên điểm tham quan nổi bật có dữ liệu độ nổi tiếng, phù hợp sở thích và hồ sơ tuổi.
Không chọn khu công nghiệp/khu công nghệ cao để lấp chỗ trống. Điểm nổi tiếng nằm xa
trung tâm chỉ là ứng viên: Planner phải kiểm tra thời gian, ngân sách và nhịp độ trẻ nhỏ.
"""

    def __init__(self, db, places_service, weather_service, artifacts_root, *, router=None, price_service=None):
        super().__init__(db, None, artifacts_root)
        self.places_service, self.weather_service = places_service, weather_service
        self.reasoning = DecisionEngine(self.agent_id, getattr(places_service, "settings", None), router=router, event_sink=self.event)
        self.price_service = price_service
        if self.price_service is None and hasattr(places_service, 'http'):
            self.price_service = OfficialPriceService(places_service.settings, places_service.http, router=router)
            self.price_service.engine.event_sink = self.event

    async def research(self, request: TripRequest, *, on_event=None, today=None, project_id=None, on_attempt=None) -> ResearchResult:
        request = TripRequest.model_validate(request)
        self.reasoning.begin(project_id)
        def valid_strategy(value):
            if any(not text.strip() or len(text) > 120 for text in value.search_interests):
                raise ValueError("Invalid research keyword")
        strategy = await self.reasoning.decide("research_strategy", self.SYSTEM, ResearchStrategy,
            {"request": request.model_dump(mode="json"), "tools": ["places_search", "weather_forecast"]}, validate=valid_strategy, on_attempt=on_attempt)
        interests = strategy.search_interests if strategy else [interest.value for interest in request.interests]
        if any(not interest.strip() or len(interest) > 120 for interest in interests):
            interests = [interest.value for interest in request.interests]
        found = await self.places_service.search(request.destination.value,
            interests, on_event=on_event, trip_request=request)
        if found.normalized_destination:
            request = request.model_copy(update={'destination': found.normalized_destination})
        weather = await self.weather_service.forecast(request, found.destination, today=today, on_event=on_event) if request.start_date is not None or strategy is None or strategy.include_weather else WeatherReport(
            status="not_requested", notes=[Fact[str](value="Chưa có ngày đi nên chưa lấy dự báo.", source="input:trip_request.start_date=null", confidence=1)])
        def valid_followup(decision):
            known = {place.id for place in found.places}
            if len(set(decision.priority_place_ids)) != len(decision.priority_place_ids) or set(decision.priority_place_ids) - known:
                raise ValueError("Unknown/duplicate Researcher priority ID")
            if any(not text.strip() or len(text) > 120 for text in decision.search_interests):
                raise ValueError("Invalid refinement keyword")
        remaining_calls=0 if any(warning.value.startswith(('overpass:','wikidata-query:')) for warning in found.warnings) else 1
        followup = await self.reasoning.decide("research_followup", self.SYSTEM, ResearchFollowup,
            {"request": request.model_dump(mode="json"), "places": [place.model_dump(mode="json") for place in found.places],
             "weather_status": weather.status, "remaining_search_calls": remaining_calls,
             "data_warnings":[warning.model_dump(mode='json') for warning in found.warnings]}, validate=valid_followup, on_attempt=on_attempt)
        if followup and followup.action == "refine_search" and remaining_calls:
            refined = await self.places_service.search(request.destination.value, followup.search_interests or interests, on_event=on_event,trip_request=request)
            merged = {place.id: place for place in found.places}
            for place in refined.places:
                merged.setdefault(place.id, place)
            maximum = getattr(getattr(self.places_service, "settings", None), "research_max_places", 30)
            merged_places=list(merged.values())
            if any(place.popularity_score for place in merged_places):
                merged_places.sort(key=lambda place: -(place.popularity_score.value if place.popularity_score else 0))
            found.places = merged_places[:maximum]
            found.warnings.extend(refined.warnings)
        if followup:
            priority = {key: index for index, key in enumerate(followup.priority_place_ids)}
            found.places.sort(key=lambda place: (-(place.popularity_score.value if place.popularity_score else 0),
                priority.get(place.id, len(priority))))
        warnings = list(found.warnings)
        if self.price_service and found.places:
            warnings.extend(await self.price_service.enrich(found.places, request, on_event=on_event,
                on_attempt=on_attempt, project_id=project_id))
        if weather.status in {"unavailable", "partial", "outside_forecast_window"}:
            warnings.extend(weather.notes)
        notes = [*found.notes, *weather.notes]
        if found.places and hasattr(self.places_service, 'http'):
            guide = await DestinationGuideService(self.places_service.settings, self.places_service.http).research(
                request.destination.value, on_event=on_event)
            notes.extend(guide.facts)
            warnings.extend(guide.warnings)
        if followup:
            notes.extend(Fact[str](value="Cần bổ sung: " + text, source=self.reasoning.records[-1].note.source, confidence=.5) for text in followup.missing_information)
        if found.places:
            notes.append(Fact[str](value=f"{sum(place.opening_hours is None for place in found.places)} địa điểm chưa có giờ mở cửa; "
                f"{sum(place.ticket_price is None for place in found.places)} địa điểm chưa có giá vé rõ ràng. Không tự ước tính các dữ kiện này.",
                source="calculation:missing-place-fields", confidence=1))
        return ResearchResult(status="needs_attention" if not found.places else "partial" if warnings else "ready",
                              destination=found.destination, places=found.places, weather=weather, normalized_destination=found.normalized_destination,
                              notes=notes, warnings=warnings, reasoning=[*self.reasoning.records,
                                  *(self.price_service.engine.records if self.price_service else [])])

    async def run(self, state: State) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        if project is None:
            raise KeyError("Create the project before Researcher runs")
        if project["status"] not in {"queued", "needs_attention", "paused"}:
            raise ValueError("Researcher requires queued, paused or needs_attention project")
        request = TripRequest.model_validate(state.get("trip_request"))
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        owned = [task for task in tasks if task["agent"] == "researcher"]
        if any(by_id[dependency]["status"] != "done" for task in owned for dependency in task["depends_on"]):
            raise ValueError("Complete Researcher task dependencies first")
        self.db.update_project(project_id, status="running", stage="researcher", progress=15, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Researcher lấy dữ liệu từ API công khai", {"fixture_mode": False})
        def on_event(payload):
            self.event(project_id, "data_cache_hit" if payload["cache_hit"] else "data_request",
                       "Researcher truy cập nguồn dữ liệu", payload)
        try:
            result = await self.research(request, on_event=on_event, project_id=project_id, on_attempt=self.model_attempt(project_id))
            run_id = uuid4().hex[:16]
            confidence = min((value.confidence for place in result.places for value in vars(place).values()
                              if isinstance(value, Fact)), default=0)
            artifacts = [
                self.save_json(project_id, run_id, "research.json", result.places, kind="research",
                    source="derived:Nominatim+Overpass+linked-Wikidata", confidence=confidence),
                self.save_json(project_id, run_id, "weather.json", result.weather, kind="research",
                    source="derived:Open-Meteo; missing data preserved", confidence=0.65 if result.weather.status in {"available", "partial"} else 0),
                self.save_json(project_id, run_id, "research_report.json", result, kind="research",
                    source="derived:ResearcherAgent public-data report", confidence=confidence),
            ]
            usable = bool(result.places)
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done" if usable else "blocked")
            if usable:
                for task in tasks:
                    if task["agent"] == "planner" and all(
                        dependency in {item["id"] for item in owned} or by_id[dependency]["status"] == "done"
                        for dependency in task["depends_on"]
                    ):
                        self.db.set_task_status(project_id, task["id"], "ready")
            update: State = {"destination_place": result.destination, "places": result.places,
                "weather": result.weather, "research": result.notes, "research_warnings": result.warnings,
                "artifacts": artifacts, "stage": "planner" if usable else "needs_attention",
                "fixture_mode": False, "production_approved": False, "error": None}
            if result.normalized_destination:
                request = request.model_copy(update={'destination': result.normalized_destination})
                update['trip_request'] = request
                self.db.update_project(project_id, trip_request=request)
            self.db.update_project(project_id, status="queued" if usable else "needs_attention", stage=update["stage"],
                progress=25 if usable else 15,
                state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "research_ready" if usable else "research_needs_attention",
                       "Researcher đã lưu kết quả có nguồn" if usable else "Chưa có địa điểm để giao Planner",
                       {"status": result.status, "place_count": len(result.places),
                        "weather_status": result.weather.status, "warning_count": len(result.warnings),
                        "artifact_ids": [artifact.id for artifact in artifacts]})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            self.db.update_project(project_id, status="failed", error=f"Researcher failed: {type(error).__name__}")
            self.event(project_id, "agent_failed", "Researcher gặp lỗi xử lý", {"error_type": type(error).__name__})
            raise
