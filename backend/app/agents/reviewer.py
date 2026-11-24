"""Code rules plus evidence-linked language and pixel review."""

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Literal
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from PIL import Image
from pydantic import Field, TypeAdapter, ValidationError

from ..attribution import attribution_record, commons_license, https_url
from ..schemas import Artifact, Budget, DayPlan, Fact, Photo, Place, Review, ReviewIssue, Schema, State, Text, TripRequest
from ..services.opening_hours import UnsupportedHours, parse_opening_hours
from ..services.day_schedule import day_frame, minute, journey_questions
from ..services.reasoning import DecisionEngine, DecisionRecord, evidence_context
from ..services.visual_review import SYSTEM as VISUAL_SYSTEM, VisualEvidence, VisualReviewDecision
from .base import BaseAgent

MARKERS = ("fixture:", "mock:", "mock-http", "synthetic:", "placeholder:", "unverified:")


def _fact(value, source, confidence=1):
    return Fact[Text](value=str(value), source=source, confidence=confidence)


def _demo(value):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_computed_fields=True)
    if isinstance(value, dict):
        source = str(value.get("source", "")).lower()
        return any(marker in source for marker in MARKERS) or source.strip() in {"unknown", "unverified", "placeholder", "mock", "n/a"} or any(_demo(child) for child in value.values())
    return isinstance(value, list) and any(_demo(child) for child in value)


class RuleCheck(Schema):
    code: Text
    status: Literal["pass", "fail", "unknown"]
    observed: Fact[Text]
    expected: Fact[Text]
    day_number: int | None = None
    place_id: str | None = None
    photo_id: str | None = None


class ReviewerResult(Schema):
    review: Review
    checks: list[RuleCheck]
    routing_mode: Text
    max_daily_distance_km: Fact[Text]
    scope: Fact[Text]
    reasoning: list[DecisionRecord] = Field(default_factory=list)


class QualitativeIssue(Schema):
    category: Literal["itinerary", "content", "photo", "budget", "research"]
    severity: Literal["minor", "major"]
    observation: str = Field(min_length=1, max_length=1000)
    evidence_ids: list[str] = Field(min_length=1, max_length=8)


class QualitativeReview(Schema):
    issues: list[QualitativeIssue] = Field(default_factory=list, max_length=12)
    rationale: str = Field(min_length=1, max_length=1200)


class ReviewerAgent(BaseAgent):
    agent_id = "reviewer"
    display_name = "Reviewer"
    SYSTEM = """Bạn là Reviewer đánh giá chất lượng trải nghiệm và nội dung sau các luật code.
Nhận xét tính phù hợp sở thích/độ tuổi, sự mạch lạc, nội dung thiếu hoặc mâu thuẫn trong dữ liệu.
Chỉ thêm issue có evidence_ids tồn tại; không sửa/xóa verdict hoặc issue của kiểm tra code.
Không tự xác nhận giờ, giá, license, ảnh thật hoặc thời tiết ngoài đầu vào. Không có pixels thì
không kết luận ảnh đúng cảnh. Rationale và observation là nhận xét cần kiểm tra, không dữ kiện mới.
"""

    def __init__(self, db, settings, artifacts_root, *, router=None, image_client=None):
        super().__init__(db, None, artifacts_root)
        self.settings = settings
        self.reasoning = DecisionEngine(self.agent_id, settings, router=router, event_sink=self.event)
        self.visual_reasoning = DecisionEngine(self.agent_id, settings.model_copy(update={"nine_router_models": settings.nine_router_vision_models}), router=router, event_sink=self.event)
        self.visual_evidence = VisualEvidence(settings, artifacts_root, client=image_client)

    async def visual_review(self, state, result=None, *, project_id=None, on_attempt=None):
        result = result or self.evaluate(state)
        if not self.settings.reviewer_visual_enabled:
            return result
        engine = self.visual_reasoning
        engine.begin(project_id)
        remote = bool(engine.enabled and (engine.router or (engine.settings.models and engine.settings.nine_router_api_key.get_secret_value().strip())))
        context, images = await self.visual_evidence.prepare(state, remote=remote)
        tasks, evidence = context["tasks"], context["evidence"]
        indexed = {task["task_id"]: task for task in tasks}
        def validate(value):
            ids = [item.task_id for item in value.assessments]
            if len(ids) != len(set(ids)) or set(ids) != set(indexed):
                raise ValueError("Visual review must cover every task exactly once")
            for item in value.assessments:
                task = indexed[item.task_id]
                if any(ref not in evidence for ref in item.evidence_ids):
                    raise ValueError("Unknown visual evidence ID")
                if item.status != "unknown" and not task["gaps"]:
                    if not set(task["required_image_ids"]) <= set(item.evidence_ids):
                        raise ValueError("Visual claim must cite its actual image pixels")
                    if task["rule"] == "age_suitability" and not any(ref.startswith("input.trip_request.traveler_ages.") for ref in item.evidence_ids):
                        raise ValueError("Age judgment must cite supplied ages")
                    if task["rule"] == "photo_location":
                        places = context["context"].get("places", [])
                        prefix = next((f"input.places.{index}." for index, place in enumerate(places) if place["id"] == task["place_id"]), None)
                        if not prefix or not any(ref.startswith(prefix) for ref in item.evidence_ids):
                            raise ValueError("Location judgment must cite the linked place")
        context["locked_code_verdict"] = result.review.verdict
        decision = await engine.decide("visual_age_review", self.VISUAL_SYSTEM, VisualReviewDecision, context,
            validate=validate, on_attempt=on_attempt, images=images, models=engine.settings.models)
        assessed = {item.task_id: item for item in decision.assessments} if decision else {}
        for task in tasks:
            item = assessed.get(task["task_id"])
            gaps = task["gaps"]
            status = "unknown" if gaps or item is None else item.status
            classification = "critical" if status == "unknown" or (task["rule"] == "photo_location" and status == "fail") else item.classification
            observation = "; ".join(gaps) if gaps else (item.observation if item else "LLM visual review unavailable; manual review required")
            refs = list(dict.fromkeys(item.evidence_ids)) if item and not gaps else ["policy:coverage"]
            source = (engine.records[-1].note.source if item and not gaps else "execution:visual-review-incomplete") + "; " + "; ".join(evidence[ref]["source"] for ref in refs)
            confidence = min([.5, *[evidence[ref]["confidence"] for ref in refs]]) if item and not gaps else 1
            code = "LLM_" + task["rule"].upper()
            result.checks.append(RuleCheck(code=code, status=status, observed=_fact(observation, source, confidence),
                expected=_fact("Complete pixel/context assessment: " + task["task_id"], "policy:visual-review-v1"),
                photo_id=task["photo_id"], place_id=task["place_id"]))
            if status != "pass":
                owner = "pm" if task["rule"] == "age_suitability" and any("ages" in gap for gap in gaps) else task["target_agent"]
                category = {"photo_location": "photo", "style_consistency": "style", "age_suitability": "itinerary"}[task["rule"]]
                if task["rule"] == "age_suitability":
                    category = {"image_curator": "photo", "illustrator": "style", "content_editor": "content"}.get(owner, "itinerary")
                result.review.issues.append(ReviewIssue(category=category, severity="major" if classification == "critical" else "minor",
                    classification=classification, message=_fact(f"[{code}/{status}] {observation}", source, confidence),
                    target_agent=owner, rule_code=code, evidence_ids=refs, artifact_ids=task["artifact_ids"],
                    photo_id=task["photo_id"], place_id=task["place_id"]))
        result.review.verdict = "REVISE" if any(item.severity in {"major", "blocker"} for item in result.review.issues) else "PASS"
        result.review.summary = _fact(f"{len(result.checks)} checks; {len(result.review.issues)} issues (code, LLM and visual evidence).", "execution:ReviewerAgent.visual-v1")
        result.review.source = "execution:ReviewerAgent.visual-v1; " + engine.records[-1].note.source
        result.review.confidence = .5 if decision else 1
        result.review = Review.model_validate(result.review.model_dump(mode="json"))
        result.scope = _fact("Code rules plus LLM pixel/style/age judgments; unknown checks require evidence. Model judgments do not prove image identity or safety.", "execution:ReviewerAgent.visual-v1")
        result.reasoning = [*result.reasoning, *engine.records]
        return result

    VISUAL_SYSTEM = VISUAL_SYSTEM

    async def qualitative(self, state, result=None, *, project_id=None, on_attempt=None):
        result = result or self.evaluate(state)
        self.reasoning.begin(project_id)
        def jsonable(value):
            if hasattr(value, "model_dump"):
                return value.model_dump(mode="json", exclude_computed_fields=True)
            if isinstance(value, dict):
                return {key: jsonable(child) for key, child in value.items()}
            if isinstance(value, list):
                return [jsonable(child) for child in value]
            return value
        context = {key: jsonable(state[key]) for key in ("trip_request", "places", "itinerary", "budget", "photos", "content") if key in state}
        evidence = evidence_context(context)
        def valid_review(value):
            if any(ref not in evidence for issue in value.issues for ref in issue.evidence_ids):
                raise ValueError("Reviewer referenced nonexistent evidence")
        decision = await self.reasoning.decide("qualitative_review", self.SYSTEM, QualitativeReview,
            {"context": context, "evidence": evidence, "code_verdict": result.review.verdict,
             "locked_code_issues": [issue.model_dump(mode="json") for issue in result.review.issues]}, validate=valid_review, on_attempt=on_attempt)
        if decision:
            targets = {"itinerary": "planner", "content": "content_editor", "photo": "image_curator", "budget": "budget", "research": "researcher"}
            for index, issue in enumerate(decision.issues):
                refs = list(dict.fromkeys(issue.evidence_ids))
                source = self.reasoning.records[-1].note.source + "; " + "; ".join(ref + "=" + evidence[ref]["source"] for ref in refs)
                result.review.issues.append(ReviewIssue(category=issue.category, severity=issue.severity,
                    message=_fact("[LLM_QUALITY] " + issue.observation, source, min([.5, *[evidence[ref]["confidence"] for ref in refs]])),
                    target_agent=targets[issue.category], rule_code="LLM_QUALITY"))
            result.review.verdict = "REVISE" if any(issue.severity in {"major", "blocker"} for issue in result.review.issues) else "PASS"
            result.review.summary = _fact(f"Code và đánh giá chất lượng: {len(result.checks)} phép kiểm code, {len(result.review.issues)} vấn đề.", "execution:ReviewerAgent.hybrid-v1")
            result.review = Review.model_validate(result.review.model_dump(mode="json"))
        result.reasoning = self.reasoning.records
        return result

    def evaluate(self, state: State) -> ReviewerResult:
        checks, issues, parsed = [], [], {}
        artifacts = [Artifact.model_validate(value) for value in state.get("artifacts", [])]
        mode = self.settings.routing_mode
        limit = Decimal(str(self.settings.reviewer_max_daily_distance_km or {"driving": 100, "walking": 15, "cycling": 50}[mode]))

        def record(code, status, observed, expected, *, source="execution:ReviewerAgent.rules-v1", confidence=1,
                   severity="major", category="itinerary", target="planner", message=None, day=None, place=None, photo=None):
            checks.append(RuleCheck(code=code, status=status, observed=_fact(observed, source, confidence),
                expected=_fact(expected, "policy:ReviewerAgent.rules-v1"), day_number=day, place_id=place, photo_id=photo))
            if status != "pass":
                issues.append(ReviewIssue(category=category, severity=severity,
                    message=_fact(f"[{code}] " + (message or f"{observed}; yêu cầu: {expected}"), source, confidence),
                    target_agent=target, rule_code=code, day_number=day, place_id=place, photo_id=photo,
                    artifact_ids=[artifact.id for artifact in artifacts if artifact.agent_id == target]))

        definitions = {"trip_request": (TripRequest, "itinerary", "pm"), "itinerary": (list[DayPlan], "itinerary", "planner"),
            "places": (list[Place], "research", "researcher"), "budget": (Budget, "budget", "budget"), "photos": (list[Photo], "photo", "image_curator")}
        for name, (schema, category, target) in definitions.items():
            try:
                parsed[name] = TypeAdapter(schema).validate_python(state.get(name))
            except (ValidationError, ValueError, TypeError):
                record("SCHEMA_" + name.upper(), "unknown", f"{name} thiếu hoặc không hợp schema", "Dữ liệu đầu vào hợp schema",
                    category=category, target=target)
        if state.get("fixture_mode") or any(_demo(parsed.get(name)) for name in ("trip_request", "itinerary", "places", "budget")):
            record("INPUT_UNVERIFIED", "unknown", "Có dữ liệu fixture/mock/LLM hoặc nguồn chưa xác minh", "Thay dữ liệu mô phỏng bằng nguồn đầu vào có thể kiểm tra",
                category="research", target="researcher", severity="blocker")
        trip, days, places, budget, photos = (parsed.get(key) for key in definitions)
        known = {place.id: place for place in places or []}
        if places is not None and len(known) != len(places):
            record("PLACE_DUPLICATE", "fail", "Trùng place ID", "ID địa điểm duy nhất", category="research", target="researcher")
        if trip is not None and days is not None:
            record("ITINERARY_DAYS", "pass" if [day.day_number for day in days] == list(range(1, trip.days.value+1)) else "fail",
                [day.day_number for day in days], list(range(1, trip.days.value+1)))
            if journey_questions(trip):
                record('JOURNEY_TIMES_MISSING','fail','Chưa có giờ đi/về','Bổ sung giờ chuyến đi hoặc xác nhận dùng lịch tạm',target='pm')
        for day in days or []:
            date = day.date.value if day.date else None
            if trip is not None and trip.start_date is not None:
                expected_date = trip.start_date.value + timedelta(days=day.day_number-1)
                if date is not None and date != expected_date:
                    record("DAY_DATE_MISMATCH", "fail", date, expected_date, day=day.day_number)
                date = date or expected_date
            if trip is not None:
                ready, deadline, required = day_frame(trip,date,
                    self.settings.planner_day_start_hour*60,self.settings.planner_day_end_hour*60)
                for stop in day.stops:
                    begin=minute(stop.arrival.value)-(stop.travel_minutes.value if stop.travel_minutes else 0)
                    fits = ready <= begin and minute(stop.departure.value) <= deadline
                    record('JOURNEY_BOUNDARY','pass' if fits else 'fail',
                        f'{stop.arrival.value}-{stop.departure.value}',f'Tham quan và di chuyển trong {ready}-{deadline} phút địa phương',
                        day=day.day_number,place=stop.place_id)
                for activity in required:
                    if activity.kind in {'breakfast','lunch','rest','dinner','hotel'}:
                        covered=any(item.kind==activity.kind and item.start.value<=activity.start.value and item.end.value>=activity.end.value for item in day.activities)
                        record('DAILY_'+activity.kind.upper(),'pass' if covered else 'fail',
                            'Có khung giờ' if covered else 'Thiếu khung giờ',activity.title.value,day=day.day_number)
            total, distances = Decimal(0), []
            missing = False
            for index, stop in enumerate(day.stops):
                place = known.get(stop.place_id)
                if places is not None:
                    if place is None:
                        record("STOP_UNKNOWN", "fail", stop.place_id, "Địa điểm có trong research", day=day.day_number, place=stop.place_id)
                    elif place.opening_hours is None or place.opening_hours.confidence <= 0:
                        record("OPENING_HOURS_MISSING", "unknown", "Chưa có giờ mở cửa đủ nguồn", "Bổ sung giờ mở cửa",
                            category="research", target="researcher", day=day.day_number, place=place.id)
                    else:
                        hours = place.opening_hours
                        try:
                            rule = parse_opening_hours(hours.value)
                        except UnsupportedHours:
                            record("OPENING_HOURS_UNSUPPORTED", "unknown", hours.value, "Giờ mở cửa được parser hỗ trợ hoặc kiểm tra bổ sung",
                                source=hours.source, confidence=hours.confidence, category="research", target="researcher", day=day.day_number, place=place.id)
                        else:
                            if date is None and rule.varies_by_weekday:
                                record("OPENING_DATE_REQUIRED", "unknown", "Lịch mở cửa khác nhau theo thứ nhưng chưa có ngày đi", "Bổ sung ngày đi cụ thể",
                                    target="pm", day=day.day_number, place=place.id)
                            else:
                                minutes = lambda time: time.hour*60 + time.minute + time.second/60 + time.microsecond/60_000_000
                                arrival, departure = minutes(stop.arrival.value), minutes(stop.departure.value)
                                fits = any(start <= arrival and departure <= end for start, end in rule.windows(date))
                                record("OPENING_WINDOW", "pass" if fits else "fail", f"{date or 'all-week'} {stop.arrival.value}-{stop.departure.value}", hours.value,
                                    source="calculation:visit-within-opening-window; " + hours.source + "; " + stop.arrival.source + "; " + stop.departure.source,
                                    confidence=min(hours.confidence, stop.arrival.confidence, stop.departure.confidence), day=day.day_number, place=place.id)
                distance = stop.distance_km
                if distance is None:
                    if index:
                        missing = True
                else:
                    total += Decimal(str(distance.value))
                    distances.append(distance)
            source = "calculation:sum(day.stops.distance_km); " + "; ".join(distance.source for distance in distances)
            confidence = min((distance.confidence for distance in distances), default=1)
            record("DAILY_DISTANCE", "fail" if total > limit else "unknown" if missing else "pass",
                f"{total} km" + (" + chặng thiếu dữ liệu" if missing else ""), f"<= {limit} km/ngày ({mode}); phạm vi các chặng đã khai báo",
                source=source, confidence=confidence, day=day.day_number)
            if any(distance.confidence <= 0 for distance in distances):
                record("DISTANCE_NO_EVIDENCE", "unknown", "Quãng đường có confidence=0", "Bổ sung dữ liệu di chuyển", day=day.day_number)
            if any("haversine" in distance.source.lower() or "estimate:" in distance.source.lower() for distance in distances):
                record("DISTANCE_ESTIMATED", "unknown", "Có chặng ước tính", "Kiểm tra đường thực tế trước khi sử dụng",
                    severity="minor", source=source, confidence=confidence, day=day.day_number)
        if trip is not None and budget is not None:
            same_currency = trip.currency.value == budget.currency.value
            record("BUDGET_CURRENCY", "pass" if same_currency else "fail", budget.currency.value, trip.currency.value,
                severity="blocker", category="budget", target="budget")
            if same_currency:
                record("BUDGET_LIMIT", "pass" if budget.limit.value == trip.budget.value else "fail", budget.limit.value, trip.budget.value,
                    source=budget.limit.source + "; " + trip.budget.source, category="budget", target="budget")
                record("BUDGET_TOTAL", "pass" if budget.total.value <= trip.budget.value else "fail", budget.total.value, f"<= {trip.budget.value} {trip.currency.value}",
                    source=budget.total.source + "; " + trip.budget.source, confidence=min(budget.total.confidence, trip.budget.confidence), category="budget", target="budget", severity='blocker',
                    message=f'Dự toán {budget.total.value:,.0f} {trip.currency.value} vượt ngân sách {trip.budget.value:,.0f} {trip.currency.value} khoảng {max(0,budget.total.value-trip.budget.value):,.0f} {trip.currency.value}; cần sửa phương án đi-về/lưu trú trước khi xuất PDF.')
            if not budget.items:
                record("BUDGET_EMPTY", "unknown", "Không có khoản dự toán", "Cần các khoản chi để xác nhận tổng", category="budget", target="budget")
            if any(item.day_number is not None and item.day_number > trip.days.value for item in budget.items):
                record("BUDGET_DAY", "fail", "Khoản chi thuộc ngày ngoài chuyến", f"Ngày 1..{trip.days.value}", category="budget", target="budget")
            for item in budget.items:
                if item.price_kind == 'sourced' and 'official-admission' in item.amount.source:
                    quote = item.price_evidence
                    valid = quote is not None and quote.currency == budget.currency.value and str(quote.source_url) in item.amount.source
                    if valid:
                        valid = item.amount.value == 0 if quote.amount == 0 else item.amount.value % quote.amount == 0 and item.amount.value / quote.amount <= trip.travelers.value
                    record("BUDGET_PRICE_EVIDENCE", "pass" if valid else "fail", item.id,
                        "Giá website chính thức phải có đoạn trích, nguồn, thời điểm tra và đơn giá khớp tổng tiền",
                        source=item.amount.source, category="budget", target="budget")
        if photos is not None:
            grouped = {}
            for photo in photos:
                grouped.setdefault(photo.place_id, []).append(photo)
                problems = []
                facts = [photo.author, photo.license, photo.source_url, photo.url]
                evidence = " ".join(fact.source for fact in facts).lower()
                labeled = any(str(fact.value).casefold().startswith(("mock ", "mock:", "placeholder", "unverified")) for fact in (photo.author, photo.license))
                test_url = "/mock-tests/" in str(photo.url.value).lower() or "/wiki/file:mock_" in unquote(str(photo.source_url.value)).lower()
                if labeled or test_url or any(marker in evidence for marker in (*MARKERS, "generation:", "llm:")) or any(fact.source.strip().lower() in {"unknown", "n/a", "unverified", "placeholder", "mock"} for fact in facts):
                    problems.append("Ảnh/metadata mô phỏng, sinh bằng AI hoặc chưa xác minh")
                if photo.author.value.casefold() in {"unknown", "unknown author", "n/a", "none"}:
                    problems.append("Chưa xác định tác giả")
                try:
                    provider = photo.provider
                    hosts = {"wikimedia": {"upload.wikimedia.org"}, "unsplash": {"images.unsplash.com"}, "pexels": {"images.pexels.com"}}
                    pages = {"wikimedia": {"commons.wikimedia.org"}, "unsplash": {"unsplash.com", "www.unsplash.com"}, "pexels": {"pexels.com", "www.pexels.com"}}
                    if provider not in hosts:
                        raise ValueError("Unsupported provider")
                    https_url(photo.url.value, hosts=hosts[provider])
                    https_url(photo.source_url.value, hosts=pages[provider])
                    prefix = {"wikimedia": "/wiki/File:", "unsplash": "/photos/", "pexels": "/photo/"}[provider]
                    if not unquote(urlsplit(str(photo.source_url.value)).path).startswith(prefix):
                        raise ValueError("Source must identify a photo page")
                    attribution_record(photo)
                    if provider == "wikimedia":
                        commons_license(photo.license.value, str(photo.license_url.value))
                    else:
                        expected = "Unsplash License" if provider == "unsplash" else "Pexels License"
                        if photo.license.value != expected or https_url(photo.license_url.value).rstrip("/") not in {
                            "https://unsplash.com/license" if provider == "unsplash" else "https://www.pexels.com/license"}:
                            raise ValueError("License metadata mismatch")
                        if photo.author_url is None or photo.author_url.confidence <= 0:
                            raise ValueError("Photographer link missing")
                        https_url(photo.author_url.value, hosts=pages[provider])
                    if photo.author.confidence <= 0 or photo.url.confidence <= 0:
                        raise ValueError("Photo evidence missing")
                    if photo.local_path:
                        if provider == "unsplash":
                            raise ValueError("Unsplash API local-file display is unsupported")
                        with Image.open(photo.local_path) as image:
                            if image.format not in {"JPEG", "PNG", "WEBP"} or image.width*image.height > 40_000_000:
                                raise ValueError("Invalid local photo format or size")
                            image.verify()
                except (ValueError, TypeError, OSError, AttributeError, Image.DecompressionBombError):
                    problems.append("Nguồn/tác giả/giấy phép/URL hoặc file ảnh không hợp lệ theo adapter được hỗ trợ")
                record("PHOTO_METADATA", "fail" if problems else "pass", "; ".join(problems) or "Metadata ảnh hợp lệ theo provider", "Ảnh nguồn hỗ trợ, có tác giả, license và URL giấy phép khớp",
                    category="photo", target="image_curator", photo=photo.id, place=photo.place_id,
                    source="validation:photo-provenance; " + "; ".join(fact.source for fact in facts), confidence=min(fact.confidence for fact in facts))
                if places is not None and photo.place_id not in known:
                    record("PHOTO_UNKNOWN_PLACE", "fail", photo.place_id, "Ảnh phải gắn với địa điểm đã nghiên cứu", category="photo", target="image_curator", photo=photo.id, place=photo.place_id)
            if places is not None:
                for place in places:
                    record("PHOTO_COVERAGE", "pass" if len(grouped.get(place.id, [])) == 1 else "fail", len(grouped.get(place.id, [])), "Đúng một ảnh được chọn cho địa điểm",
                        category="photo", target="image_curator", place=place.id)
            if len({photo.id for photo in photos}) != len(photos):
                record("PHOTO_DUPLICATE_ID", "fail", "Trùng photo ID", "Photo ID duy nhất", category="photo", target="image_curator")
        major = any(issue.severity in {"major", "blocker"} for issue in issues)
        review = Review(verdict="REVISE" if major else "PASS", issues=issues, revision=state.get("revision_count", 0),
            summary=_fact(f"Kiểm tra bằng code: {len(checks)} phép kiểm, {len(issues)} vấn đề; " + ("cần sửa/bổ sung dữ liệu." if major else "đạt các luật đã kiểm tra."), "execution:ReviewerAgent.rules-v1"),
            source="execution:ReviewerAgent.rules-v1", confidence=1)
        return ReviewerResult(review=review, checks=checks, routing_mode=mode,
            max_daily_distance_km=_fact(limit, "configuration:reviewer-distance-policy"),
            scope=_fact("Kiểm tra dữ liệu được cung cấp; không xác thực lại trên mạng, không chứng minh ảnh đúng cảnh hoặc bảo đảm quyền sử dụng pháp lý.", "scope:deterministic-data-review"))

    async def run(self, state: State) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        if project is None or project["status"] not in {"queued", "paused", "needs_attention"}:
            raise ValueError("Reviewer needs a runnable project")
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        owned = [task for task in tasks if task["agent"] == self.agent_id]
        if not owned or any(dep not in by_id or by_id[dep]["status"] != "done" for task in owned for dep in task["depends_on"]):
            raise ValueError("Complete all three visual dependencies before Reviewer")
        self.db.update_project(project_id, status="running", stage="reviewer", progress=70, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Reviewer kiểm tra các luật bằng code")
        try:
            result = await self.qualitative(state, project_id=project_id, on_attempt=self.model_attempt(project_id))
            result = await self.visual_review(state, result, project_id=project_id, on_attempt=self.model_attempt(project_id))
            run_id = uuid4().hex[:16]
            artifacts = [self.save_json(project_id, run_id, "review.json", result.review, kind="review", source=result.review.source, confidence=1),
                self.save_json(project_id, run_id, "review_checks.json", result, kind="review", source="execution:ReviewerAgent audit", confidence=1)]
            passed = result.review.verdict == "PASS"
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done")
            for task in tasks:
                if task["agent"] == "layout_editor":
                    self.db.set_task_status(project_id, task["id"], "ready" if passed else "blocked")
            actionable = [issue for issue in result.review.issues if issue.severity in {"blocker", "major"}]
            actionable.sort(key=lambda issue: issue.severity != "blocker")
            update: State = {"review": result.review, "feedback": [issue.message for issue in actionable],
                "revision_target": actionable[0].target_agent if actionable else None,
                "stage": "layout_editor" if passed else "needs_attention", "artifacts": artifacts,
                "production_approved": False, "error": None}
            self.db.update_project(project_id, status="queued" if passed else "needs_attention", stage=update["stage"], progress=80 if passed else 70,
                state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "review_passed" if passed else "review_revise", "Đã lưu báo cáo kiểm tra bằng code",
                {"verdict": result.review.verdict, "check_count": len(result.checks), "issue_count": len(result.review.issues), "revision_target": update["revision_target"]})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            self.db.update_project(project_id, status="failed", error="Reviewer failed: " + type(error).__name__)
            self.event(project_id, "agent_failed", "Reviewer chưa kiểm tra được dữ liệu", {"error_type": type(error).__name__})
            raise
