"""LLM grouping with code-validated spatial/time-window scheduling."""

from datetime import time, timedelta
import math
import re
from typing import Literal
from uuid import uuid4

from pydantic import Field, TypeAdapter

from ..schemas import DayPlan, Fact, Place, PlanStop, Schema, State, TripRequest
from ..services.opening_hours import UnsupportedHours, parse_opening_hours
from ..services.routing import RouteMatrix, haversine_km
from ..services.reasoning import DecisionEngine, DecisionRecord
from ..services.place_popularity import tourism_candidate
from ..services.day_schedule import day_frame, fit_visit, visit_duration, complete_activities, journey_questions
from .base import BaseAgent


class UnplannedPlace(Schema):
    place_id: str
    reason: Fact[str]


class PlannerResult(Schema):
    status: Literal["ready", "partial", "needs_attention"]
    itinerary: list[DayPlan] = Field(default_factory=list)
    clusters: list[list[str]] = Field(default_factory=list)
    routing: RouteMatrix | None = None
    unplanned: list[UnplannedPlace] = Field(default_factory=list)
    missing_days: list[int] = Field(default_factory=list)
    warnings: list[Fact[str]] = Field(default_factory=list)
    reasoning: list[DecisionRecord] = Field(default_factory=list)


class PlannerProposal(Schema):
    day_groups: list[list[str]] = Field(min_length=1, max_length=366)
    rationale: str = Field(min_length=1, max_length=1200)


def _distance(a, b):
    return haversine_km(a.latitude.value, a.longitude.value, b.latitude.value, b.longitude.value)


def cluster_places(places, count):
    """Balanced farthest-first geographic groups; no duplicate/repeated venues."""
    if not places or count < 1:
        return []
    count = min(count, len(places))
    ordered = sorted(places, key=lambda place: (place.latitude.value, place.longitude.value, place.id))
    seeds = [ordered[0]]
    while len(seeds) < count:
        choices = [place for place in ordered if place.id not in {seed.id for seed in seeds}]
        seeds.append(max(choices, key=lambda place: (min(_distance(place, seed) for seed in seeds), place.id)))
    groups = [[seed] for seed in seeds]
    maximum = math.ceil(len(places) / count)
    for place in sorted([p for p in ordered if p.id not in {seed.id for seed in seeds}],
                        key=lambda place: (-min(_distance(place, seed) for seed in seeds), place.id)):
        index = min((i for i in range(count) if len(groups[i]) < maximum), key=lambda i: (_distance(place, seeds[i]), i))
        groups[index].append(place)
    return groups


def _f(value, source, confidence=1):
    return {"value": value, "source": source, "confidence": confidence}


def _clock(minutes):
    return time(minutes // 60, minutes % 60)


class PlannerAgent(BaseAgent):
    agent_id = "planner"
    display_name = "Planner"
    SYSTEM = """Bạn là Planner. Phân nhóm ID địa điểm theo ngày, cân nhắc sở thích, độ tuổi,
vị trí, ma trận di chuyển và giờ mở cửa. day_groups có đúng số nhóm requested_days,
có thể có nhóm rỗng nếu thiếu điểm; mọi eligible ID phải xuất hiện đúng một lần.
Thứ tự ID trong nhóm thể hiện ưu tiên. Không tạo ID, giờ đến, giá hoặc quãng đường.
Backend sẽ xếp giờ và loại lựa chọn không đáp ứng giới hạn tuổi/giờ mở cửa/nghỉ/ngày.
Ưu tiên các điểm có popularity_score cao khi phải chọn trong cùng cụm; không đánh
đồng nổi tiếng với phù hợp trẻ nhỏ. Điểm xa trung tâm cần đủ thời gian di chuyển,
không cố ghép nhiều điểm xa vào một ngày của nhóm có trẻ em.
Phân bổ đủ hoạt động sáng, chiều và tối trong khung 08:00–21:00; dành giờ ăn và nghỉ trưa.
Ngày đầu chỉ có thời gian sau khi đến nơi; ngày cuối dành thời gian trả phòng, ra ga/sân bay và làm thủ tục.
Khu vui chơi/Bà Nà cần gần trọn ngày, bảo tàng khoảng 90 phút, bãi biển khoảng 120 phút;
không coi mọi điểm đều 45 phút, không tạo nhà hàng/khách sạn hoặc giờ vé chưa có nguồn.
"""

    def __init__(self, db, routing_service, artifacts_root, *, router=None):
        super().__init__(db, None, artifacts_root)
        self.routing = routing_service
        self.settings = routing_service.settings
        self.reasoning = DecisionEngine(self.agent_id, self.settings, router=router, event_sink=self.event)

    async def plan(self, request, places, *, on_event=None, project_id=None, on_attempt=None) -> PlannerResult:
        request = TripRequest.model_validate(request)
        self.reasoning.begin(project_id)
        places = TypeAdapter(list[Place]).validate_python(places)
        if len({place.id for place in places}) != len(places) or request.days.value > 366:
            raise ValueError("Places must have unique IDs; planning supports at most 366 days")
        start = self.settings.planner_day_start_hour * 60
        finish = self.settings.planner_day_end_hour * 60
        if finish <= start:
            raise ValueError("Planner day must end after it starts")
        result = PlannerResult(status="needs_attention")
        if journey_questions(request):
            result.missing_days = list(range(1, request.days.value+1))
            result.warnings.extend(Fact[str](**_f(q, 'input:missing-journey-times')) for q in journey_questions(request))
            return result
        if request.transport_times_flexible and request.transport_times_flexible.value:
            result.warnings.append(Fact[str](**_f('Chưa chốt giờ đi/về: lịch tạm dùng khung ngày, cần xếp lại sau khi có vé; không phải giờ chuyến đã xác nhận.', 'assumption:flexible-journey-times', .5)))
        ages = [age.value for age in request.traveler_ages]
        complete_ages = bool(ages) and len(ages) == request.travelers.value
        if not complete_ages:
            ages += [int(age) for requirement in request.requirements
                     for age in re.findall(r"\b(\d{1,3})\s*tuổi\b", requirement.value.casefold())]
        needs = " ".join(requirement.value for requirement in request.requirements).casefold()
        gentle = any(age < 12 or age >= 65 for age in ages) or any(
            phrase in needs for phrase in ("trẻ nhỏ", "trẻ em", "cao tuổi", "người già", "xe lăn", "wheelchair"))
        children = any(age < 12 for age in ages) or any(phrase in needs for phrase in ("trẻ nhỏ", "trẻ em"))
        seniors = any(age >= 65 for age in ages) or any(phrase in needs for phrase in ("cao tuổi", "người già"))
        if not complete_ages:
            result.warnings.append(Fact[str](**_f("Chưa biết tuổi toàn nhóm; không mặc định tuổi để vượt qua giới hạn tuổi của địa điểm.", "input:trip_request.age-profile")))
        if gentle:
            result.warnings.append(Fact[str](**_f("Dùng nhịp độ nhẹ: tối đa 4 điểm/ngày, có ăn uống, nghỉ trưa và thời gian chuyển tiếp.", "assumption:gentle-planning-policy", 0.5)))
        limit = min(self.settings.planner_max_stops, 4) if gentle else self.settings.planner_max_stops
        buffer = self.settings.planner_transition_minutes + (5 if gentle else 0)
        rules, eligible = {}, []
        excluded = set()
        def exclude(place, message, source):
            if place.id not in excluded:
                result.unplanned.append(UnplannedPlace(place_id=place.id, reason=_f(message, source)))
                excluded.add(place.id)
        for place in places:
            if not tourism_candidate(place,trip=request):
                exclude(place,'Không chọn địa điểm không phù hợp tham quan hoặc hồ sơ độ tuổi của nhóm.','policy:tourism-suitability-filter')
                continue
            low = place.minimum_age.value if place.minimum_age is not None else None
            high = place.maximum_age.value if place.maximum_age is not None else None
            advisory_excluded = False
            for note in place.age_notes:
                text = note.value.casefold()
                child_caution = any(phrase in text for phrase in ("không phù hợp trẻ", "không dành cho trẻ", "not suitable for children"))
                senior_caution = any(phrase in text for phrase in ("không phù hợp người cao tuổi", "not suitable for elderly"))
                if (children and child_caution) or (seniors and senior_caution):
                    exclude(place, "Không chọn do lưu ý độ tuổi của địa điểm không phù hợp nhóm.", note.source)
                    advisory_excluded = True
                if "chỉ người lớn" in text or "adults only" in text:
                    low = max(low or 0, 18)
                match = re.search(r"(?:từ|dưới)\s+(\d{1,3})\s*tuổi", text)
                if match and ("từ" in match.group() or "không" in text):
                    low = max(low or 0, int(match[1]))
                for pattern, side in ((r"\bmin_age\s*[:=]\s*(\d+)", "min"), (r"\bmax_age\s*[:=]\s*(\d+)", "max")):
                    match = re.search(pattern, note.value.casefold())
                    if match:
                        if side == "min":
                            low = max(low or 0, int(match[1]))
                        else:
                            high = min(high if high is not None else int(match[1]), int(match[1]))
            if advisory_excluded:
                continue
            if (low is not None or high is not None) and (not complete_ages or any(
                (low is not None and age < low) or (high is not None and age > high) for age in ages)):
                exclude(place, "Không thỏa giới hạn tuổi hoặc chưa đủ tuổi toàn nhóm để xác nhận.", "validation:place-age-limits")
                continue
            try:
                rules[place.id] = parse_opening_hours(place.opening_hours.value) if place.opening_hours is not None else None
            except UnsupportedHours:
                exclude(place, "Giờ mở cửa vượt phạm vi parser hỗ trợ; cần kiểm tra trước khi tự xếp lịch.", place.opening_hours.source)
                continue
            eligible.append(place)
        if not eligible:
            result.missing_days = list(range(1, request.days.value + 1))
            return result
        groups = cluster_places(eligible, request.days.value)
        result.clusters = [[place.id for place in group] for group in groups]
        matrix = await self.routing.matrix(eligible, on_event=on_event)
        result.routing = matrix
        result.warnings.extend(matrix.warnings)
        def validate_proposal(proposal):
            flattened = [id for group in proposal.day_groups for id in group]
            if len(proposal.day_groups) != request.days.value or len(flattened) != len(set(flattened)) or set(flattened) != {place.id for place in eligible}:
                raise ValueError("Planner proposal must cover eligible IDs exactly once")
        proposal = await self.reasoning.decide("planner_proposal", self.SYSTEM, PlannerProposal,
            {"request": request.model_dump(mode="json"), "requested_days": request.days.value,
             "eligible_places": [place.model_dump(mode="json") for place in eligible], "routing": matrix.model_dump(mode="json")},
            validate=validate_proposal, on_attempt=on_attempt)
        if proposal:
            result.clusters = proposal.day_groups
        priorities = {id: index for group in (proposal.day_groups if proposal else []) for index, id in enumerate(group)}
        result.reasoning = self.reasoning.records
        remaining = {place.id: place for place in eligible}
        assumptions = False
        for day_index in range(request.days.value):
            day_date = request.start_date.value + timedelta(days=day_index) if request.start_date is not None else None
            primary = set(result.clusters[day_index]) if day_index < len(result.clusters) else set()
            target = min(limit, math.ceil(len(remaining) / (request.days.value - day_index)))
            day_start, day_finish, activities = day_frame(request, day_date, start, finish)
            current, previous, stops = day_start, None, []
            while remaining and len(stops) < target:
                options = []
                for place in remaining.values():
                    rule = rules[place.id]
                    provisional = rule is None or (day_date is None and rule.varies_by_weekday)
                    windows = [(day_start, day_finish)] if rule is None else rule.windows(day_date)
                    duration = visit_duration(place, self.settings.planner_default_visit_minutes)
                    leg = matrix.leg(previous.id, place.id) if previous is not None else None
                    if leg is not None and not leg.reachable:
                        continue
                    travel = leg.duration_minutes.value + buffer if leg is not None else 30
                    earliest = current + travel
                    fit = fit_visit(current, travel, duration, windows, activities, day_finish)
                    if not fit:
                        continue
                    arrival, deadline = fit
                    options.append((place.id not in primary, deadline, -(place.popularity_score.value if place.popularity_score else 0), priorities.get(place.id, 0), travel + arrival-earliest, place.id,
                                    place, arrival, duration, travel, leg, provisional))
                if not options:
                    break
                _, _, _, _, _, _, place, arrival, duration, travel, leg, provisional = min(options, key=lambda row: row[:6])
                notes = []
                if provisional:
                    assumptions = True
                    notes.append(Fact[str](**_f("Khung giờ tham quan tạm tính; thiếu giờ mở cửa hoặc ngày cụ thể để kiểm tra thứ trong tuần.",
                                                "assumption:unverified-opening-window; " + place.id, 0.3)))
                if place.visit_minutes is None:
                    assumptions = True
                    notes.append(Fact[str](**_f(f"Thời lượng tham quan {duration} phút là giả định lập lịch.", "assumption:default-visit-duration", 0.5)))
                notes.extend(place.age_notes)
                hours_conf = 0.3 if provisional else place.opening_hours.confidence
                duration_conf = place.visit_minutes.confidence if place.visit_minutes is not None else 0.5
                route_conf = leg.duration_minutes.confidence if leg is not None else .5
                confidence = min(hours_conf, duration_conf, route_conf)
                source = "calculation:planner-time-window; " + (place.opening_hours.source if place.opening_hours else "opening-hours-unknown")
                if leg is None:
                    travel_source = "assumption:accommodation-to-first-stop-30min; accommodation-not-geocoded"
                    distance = None
                    notes.append(Fact[str](**_f("Dự phòng 30 phút từ nơi lưu trú tới điểm đầu; cần tính lại theo địa chỉ khách sạn thực tế.", travel_source, .5)))
                else:
                    travel_source = f"calculation:route+transition-buffer({buffer}min); " + leg.duration_minutes.source
                    distance = leg.distance_km
                    if leg.estimated:
                        assumptions = True
                        notes.append(Fact[str](**_f("Chặng đi là ước tính haversine, không phải thời gian đường thực tế đã xác minh.", leg.duration_minutes.source, route_conf)))
                stops.append(PlanStop(place_id=place.id, arrival=_f(_clock(arrival), source, confidence),
                    departure=_f(_clock(arrival + duration), source, confidence),
                    travel_minutes=_f(travel, travel_source, route_conf), distance_km=distance, notes=notes))
                current = arrival + duration
                previous = place
                del remaining[place.id]
            if stops or (activities and day_start >= day_finish-60):
                result.itinerary.append(DayPlan(day_number=day_index + 1,
                    date=_f(day_date, "calculation:trip.start_date+day_index; " + request.start_date.source) if day_date else None,
                    title=_f(f"Ngày {day_index + 1} - Cụm tham quan {day_index + 1}", "calculation:spatial-cluster-plan"),
                    stops=stops, activities=complete_activities(stops, activities, max(start,day_start), min(finish,day_finish)),
                    tips=[Fact[str](**_f("Khung ngày 08:00–21:00; ăn trưa 12:00–13:00, nghỉ 13:00–14:00, ăn tối 18:00–19:00 khi không vướng chuyến đi. Giờ ăn, lưu trú và chuyển tiếp là dự kiến, chưa phải đặt chỗ.", "assumption:planner-policy", 0.5))]))
            else:
                result.missing_days.append(day_index + 1)
        for place in remaining.values():
            exclude(place, "Chưa xếp được do cửa sổ mở cửa, thời gian, chặng không đi được hoặc giới hạn số điểm/ngày.", "validation:planner-capacity")
        if assumptions:
            result.warnings.append(Fact[str](**_f("Lịch có giả định giờ mở cửa/thời lượng hoặc chặng ước tính; cần kiểm duyệt trước khi sử dụng.", "assumption:provisional-itinerary", 0.3)))
        result.status = "needs_attention" if result.missing_days else "partial" if result.warnings or result.unplanned else "ready"
        return result

    async def run(self, state: State) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        if project is None or project["status"] not in {"queued", "paused", "needs_attention"}:
            raise ValueError("Planner needs an existing queued/paused/needs_attention project")
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        owned = [task for task in tasks if task["agent"] == "planner"]
        if any(by_id[dependency]["status"] != "done" for task in owned for dependency in task["depends_on"]):
            raise ValueError("Complete Researcher dependencies before planning")
        self.db.update_project(project_id, status="running", stage="planner", progress=25, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Planner gom cụm và xếp lịch theo cửa sổ thời gian")
        def on_event(payload):
            self.event(project_id, "routing_cache_hit" if payload["cache_hit"] else "routing_request", "Planner lấy ma trận di chuyển", payload)
        try:
            result = await self.plan(state["trip_request"], state["places"], on_event=on_event, project_id=project_id, on_attempt=self.model_attempt(project_id))
            run_id = uuid4().hex[:16]
            confidence = min((stop.arrival.confidence for day in result.itinerary for stop in day.stops), default=0)
            artifacts = [self.save_json(project_id, run_id, "itinerary.json", result.itinerary, kind="itinerary",
                source="calculation:cluster+opening-window+age-policy", confidence=confidence),
                self.save_json(project_id, run_id, "planner_report.json", result, kind="review",
                source="calculation:PlannerAgent report", confidence=confidence)]
            complete = not result.missing_days
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done" if complete else "blocked")
            if complete:
                for task in tasks:
                    if task["agent"] == "budget" and all(dep in {item["id"] for item in owned} or by_id[dep]["status"] == "done" for dep in task["depends_on"]):
                        self.db.set_task_status(project_id, task["id"], "ready")
            update: State = {"itinerary": result.itinerary, "planner_warnings": result.warnings,
                "stage": "budget" if complete else "needs_attention", "artifacts": artifacts,
                "fixture_mode": False, "production_approved": False, "error": None}
            self.db.update_project(project_id, status="queued" if complete else "needs_attention", stage=update["stage"],
                progress=35 if complete else 25, state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "itinerary_ready" if complete else "planner_needs_attention", "Planner đã lưu lịch trình có nguồn và thời gian",
                {"status": result.status, "day_count": len(result.itinerary), "unplanned_count": len(result.unplanned), "missing_days": result.missing_days})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            self.db.update_project(project_id, status="failed", error=f"Planner failed: {type(error).__name__}")
            self.event(project_id, "agent_failed", "Planner chưa tạo được lịch hợp lệ", {"error_type": type(error).__name__})
            raise
