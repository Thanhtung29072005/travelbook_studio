"""Validated domain data and the shared LangGraph state.

Domain facts use Fact[T]: value, source and confidence are always supplied
explicitly. Unknown facts are None, never invented values or implicit confidence.
IDs, file paths and workflow routing fields are application metadata.
"""

from __future__ import annotations

from datetime import date as Date
from datetime import datetime, time, timezone, timedelta
from decimal import Decimal
from typing import Annotated, Generic, Literal, TypeVar, TypedDict

from pydantic import AnyHttpUrl, BaseModel, ConfigDict, Field, computed_field, model_validator

T = TypeVar("T")
Text = Annotated[str, Field(min_length=1)]
Confidence = Annotated[float, Field(ge=0, le=1, allow_inf_nan=False)]
Money = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
SignedMoney = Annotated[Decimal, Field(allow_inf_nan=False)]
PositiveInt = Annotated[int, Field(gt=0, strict=True)]
NonNegativeInt = Annotated[int, Field(ge=0, strict=True)]
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$")]

AgentId = Literal[
    "pm", "researcher", "planner", "budget", "content_editor",
    "image_curator", "illustrator", "map_agent", "reviewer", "layout_editor",
]


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Evidence(Schema):
    """Source can be a URL, user message ID, or a reproducible calculation ID.

    confidence is an explicit assessment in [0, 1], not a verified probability.
    An LLM estimate should identify its model/run in source.
    """

    source: Text
    confidence: Confidence


class Fact(Evidence, Generic[T]):
    value: T


class TripRequest(Schema):
    destination: Fact[Text]
    days: Fact[PositiveInt]
    budget: Fact[Money]
    currency: Fact[Currency]
    travelers: Fact[PositiveInt]
    start_date: Fact[Date] | None = None
    end_date: Fact[Date] | None = None
    departure: Fact[Text] | None = None
    outbound_departure_at: Fact[datetime] | None = None
    destination_arrival_at: Fact[datetime] | None = None
    destination_departure_at: Fact[datetime] | None = None
    return_arrival_at: Fact[datetime] | None = None
    transport_mode: Fact[Text] | None = None
    transport_times_flexible: Fact[bool] | None = None
    hotel_name: Fact[Text] | None = None
    interests: list[Fact[Text]] = Field(default_factory=list)
    traveler_ages: list[Fact[NonNegativeInt]] = Field(default_factory=list)
    requirements: list[Fact[Text]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_trip(self) -> TripRequest:
        if self.traveler_ages and len(self.traveler_ages) != self.travelers.value:
            raise ValueError("traveler_ages must contain one age per traveler when supplied")
        if self.end_date is not None:
            if self.start_date is None:
                raise ValueError("end_date requires start_date")
            length = (self.end_date.value - self.start_date.value).days + 1
            if length != self.days.value:
                raise ValueError("inclusive date range must match days")
        stamps=[item.value if item.value.tzinfo else item.value.replace(tzinfo=timezone(timedelta(hours=7)))
                for item in (self.outbound_departure_at,self.destination_arrival_at,self.destination_departure_at,self.return_arrival_at) if item]
        if any(right<=left for left,right in zip(stamps,stamps[1:])):
            raise ValueError('Journey timestamps must be chronological')
        if self.start_date:
            final=self.start_date.value+timedelta(days=self.days.value-1)
            for item in (self.destination_arrival_at,self.destination_departure_at):
                local_date = (item.value.astimezone(timezone(timedelta(hours=7))) if item.value.tzinfo else item.value).date() if item else None
                if item and not self.start_date.value<=local_date<=final:
                    raise ValueError('Destination arrival/departure dates must be within the trip dates')
        return self


class AdmissionQuote(Schema):
    """Published admission price, backed by a verbatim official-page excerpt."""
    amount: Money
    currency: Currency
    audience: Literal["adult", "all"]
    source_url: AnyHttpUrl
    retrieved_at: datetime
    excerpt: Annotated[str, Field(min_length=1, max_length=2000)]
    place_excerpt: Annotated[str, Field(max_length=1000)] = ''
    validity_excerpt: Annotated[str, Field(max_length=1000)] = ''
    valid_from: Date | None = None
    valid_until: Date | None = None
    conditions: Text

    @model_validator(mode="after")
    def check_dates(self):
        if self.valid_from and self.valid_until and self.valid_until < self.valid_from:
            raise ValueError("Invalid price validity range")
        return self


class Place(Schema):
    id: Text
    wikidata_id: Annotated[str, Field(pattern=r'^Q[1-9][0-9]*$')] | None = None
    wikidata_sitelinks: Fact[NonNegativeInt] | None = None
    wikipedia_title: Fact[Text] | None = None
    wikipedia_language: Literal['vi', 'en'] | None = None
    wikipedia_pageviews: Fact[NonNegativeInt] | None = None
    popularity_score: Fact[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = None
    tourism_kind: Fact[Text] | None = None
    distance_to_destination_km: Fact[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = None
    name: Fact[Text]
    latitude: Fact[Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]]
    longitude: Fact[Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]]
    address: Fact[Text] | None = None
    description: Fact[Text] | None = None
    opening_hours: Fact[Text] | None = None
    ticket_price: Fact[Money] | None = None
    currency: Fact[Currency] | None = None
    official_website: Fact[AnyHttpUrl] | None = None
    ticket_quote: AdmissionQuote | None = None
    visit_minutes: Fact[PositiveInt] | None = None
    age_notes: list[Fact[Text]] = Field(default_factory=list)
    minimum_age: Fact[NonNegativeInt] | None = None
    maximum_age: Fact[NonNegativeInt] | None = None

    @model_validator(mode="after")
    def check_price(self) -> Place:
        if self.ticket_price is not None and self.currency is None:
            raise ValueError("ticket_price requires currency")
        if self.minimum_age is not None and self.maximum_age is not None and self.minimum_age.value > self.maximum_age.value:
            raise ValueError("minimum_age must not exceed maximum_age")
        return self


class PlanStop(Schema):
    place_id: Text
    arrival: Fact[time]
    departure: Fact[time]
    travel_minutes: Fact[NonNegativeInt] | None = None
    distance_km: Fact[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = None
    notes: list[Fact[Text]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_times(self) -> PlanStop:
        if self.arrival.value.tzinfo is not None or self.departure.value.tzinfo is not None:
            raise ValueError("stop times must be local times without timezone offsets")
        if self.departure.value <= self.arrival.value:
            raise ValueError("departure must be after arrival within the same day")
        return self


class WeatherDay(Schema):
    """Daily forecast units: degrees Celsius, percent probability, millimetres."""
    day_number: PositiveInt
    date: Fact[Date] | None = None
    temperature_min: Fact[Annotated[float, Field(allow_inf_nan=False)]] | None = None
    temperature_max: Fact[Annotated[float, Field(allow_inf_nan=False)]] | None = None
    rain_probability: Fact[Annotated[float, Field(ge=0, le=100, allow_inf_nan=False)]] | None = None
    precipitation_mm: Fact[Annotated[float, Field(ge=0, allow_inf_nan=False)]] | None = None

    @model_validator(mode="after")
    def check_range(self) -> WeatherDay:
        if self.temperature_min is not None and self.temperature_max is not None and self.temperature_min.value > self.temperature_max.value:
            raise ValueError("temperature_min must not exceed temperature_max")
        return self


class WeatherReport(Schema):
    status: Literal["available", "partial", "unavailable", "not_requested", "outside_forecast_window"]
    daily: list[WeatherDay] = Field(default_factory=list)
    timezone: Fact[Text] | None = None
    grid_latitude: Fact[Annotated[float, Field(ge=-90, le=90, allow_inf_nan=False)]] | None = None
    grid_longitude: Fact[Annotated[float, Field(ge=-180, le=180, allow_inf_nan=False)]] | None = None
    missing_dates: list[Fact[Date]] = Field(default_factory=list)
    notes: list[Fact[Text]] = Field(default_factory=list)


class VenueRecommendation(Schema):
    id: Text
    kind: Literal['restaurant','hotel']
    name: Fact[Text]
    latitude: Fact[Annotated[float,Field(ge=-90,le=90,allow_inf_nan=False)]]
    longitude: Fact[Annotated[float,Field(ge=-180,le=180,allow_inf_nan=False)]]
    source_url: Fact[AnyHttpUrl]
    address: Fact[Text] | None = None
    opening_hours: Fact[Text] | None = None
    accommodation_type: Literal['hotel','guest_house','hostel'] | None = None
    stars: int | None = Field(default=None,ge=0,le=5)


class ActivitySlot(Schema):
    kind: Literal['breakfast', 'lunch', 'dinner', 'rest', 'hotel', 'transfer', 'terminal', 'free_time']
    title: Fact[Text]
    start: Fact[time]
    end: Fact[time]
    within_place_id: Text | None = None
    venue: VenueRecommendation | None = None
    venue_transfer_minutes: Fact[NonNegativeInt] | None = None

    @model_validator(mode='after')
    def check_times(self):
        if self.start.value.tzinfo or self.end.value.tzinfo or self.end.value <= self.start.value:
            raise ValueError('Activity must have increasing local times within a day')
        return self


class DayPlan(Schema):
    day_number: PositiveInt
    date: Fact[Date] | None = None
    title: Fact[Text] | None = None
    stops: list[PlanStop] = Field(default_factory=list)
    activities: list[ActivitySlot] = Field(default_factory=list)
    weather: list[Fact[Text]] = Field(default_factory=list)
    tips: list[Fact[Text]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_schedule(self) -> DayPlan:
        if not self.stops and not self.activities:
            raise ValueError('Day requires visits or scheduled activities')
        def minutes(clock):
            return clock.hour * 60 + clock.minute + clock.second / 60
        blocks = []
        for slot in self.activities:
            a, b = minutes(slot.start.value), minutes(slot.end.value)
            if slot.within_place_id:
                parent = next((stop for stop in self.stops if stop.place_id == slot.within_place_id), None)
                if slot.kind not in {'lunch', 'rest', 'dinner'} or parent is None or not (minutes(parent.arrival.value) <= a < b <= minutes(parent.departure.value)):
                    raise ValueError('On-site activity must be contained in its visit')
            for stop in self.stops:
                start, end = minutes(stop.arrival.value), minutes(stop.departure.value)
                travel = stop.travel_minutes.value if stop.travel_minutes else 0
                if a < end and b > start - travel and stop.place_id != slot.within_place_id:
                    raise ValueError('Activity overlaps a visit or travel')
            if any(a < y and b > x for x, y in blocks):
                raise ValueError('Activities must not overlap')
            blocks.append((a, b))
        for previous, current in zip(self.stops, self.stops[1:]):
            gap = (
                datetime.combine(Date.min, current.arrival.value)
                - datetime.combine(Date.min, previous.departure.value)
            ).total_seconds() / 60
            if gap < 0:
                raise ValueError("stops must be chronological and must not overlap")
            if current.travel_minutes is not None and gap < current.travel_minutes.value:
                raise ValueError("insufficient travel time before the next stop")
        return self


class BudgetItem(Schema):
    id: Text
    category: Literal["food", "accommodation", "transport", "tickets", "contingency", "other"]
    description: Fact[Text]
    amount: Fact[Money]  # Full line total, in Budget.currency, not a unit price.
    price_kind: Literal["sourced", "estimated"]
    day_number: PositiveInt | None = None
    price_evidence: AdmissionQuote | None = None


class Budget(Schema):
    currency: Fact[Currency]
    limit: Fact[Money]
    items: list[BudgetItem] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def validate_calculated_fields(cls, data):
        """Accept our exported totals only when they exactly match recalculation.

        Older inputs contain only currency/limit/items and remain valid. Supplied
        totals are assertions, never authoritative values, including their evidence.
        """
        if isinstance(data, dict):
            names = {"total", "sourced_total", "estimated_total", "variance", "remaining", "within_budget"}
            supplied = names.intersection(data)
            if supplied:
                clean = {key: value for key, value in data.items() if key not in names}
                recalculated = cls.model_validate(clean)
                for name in supplied:
                    expected = getattr(recalculated, name)
                    actual = type(expected).model_validate(data[name])
                    if actual != expected:
                        raise ValueError(f"Supplied budget.{name} disagrees with recalculated value/evidence")
                return clean
        return data

    @model_validator(mode="after")
    def check_ids(self) -> Budget:
        if len({item.id for item in self.items}) != len(self.items):
            raise ValueError("budget item IDs must be unique")
        return self

    @computed_field
    @property
    def total(self) -> Fact[Money]:
        """Compute with Decimal; never accept an LLM's supplied sum."""
        return Fact[Money](
            value=sum((item.amount.value for item in self.items), Decimal("0")),
            source="calculation:sum(budget.items.amount); inputs="
            + "; ".join(item.amount.source for item in self.items),
            confidence=min((item.amount.confidence for item in self.items), default=1.0),
        )

    @computed_field
    @property
    def within_budget(self) -> Fact[bool]:
        total = self.total
        return Fact[bool](
            value=total.value <= self.limit.value,
            source=f"calculation:budget.total<=budget.limit; {total.source}; {self.limit.source}",
            confidence=min(total.confidence, self.limit.confidence),
        )

    def _subtotal(self, price_kind: str) -> Fact[Money]:
        items = [item for item in self.items if item.price_kind == price_kind]
        return Fact[Money](value=sum((item.amount.value for item in items), Decimal("0")),
            source=f"calculation:sum(budget.items.amount where price_kind={price_kind}); inputs="
                + "; ".join(item.amount.source for item in items),
            confidence=min((item.amount.confidence for item in items), default=1))

    @computed_field
    @property
    def sourced_total(self) -> Fact[Money]:
        return self._subtotal("sourced")

    @computed_field
    @property
    def estimated_total(self) -> Fact[Money]:
        return self._subtotal("estimated")

    @computed_field
    @property
    def variance(self) -> Fact[SignedMoney]:
        total = self.total
        return Fact[SignedMoney](value=total.value - self.limit.value,
            source=f"calculation:budget.total-budget.limit; {total.source}; {self.limit.source}",
            confidence=min(total.confidence, self.limit.confidence))

    @computed_field
    @property
    def remaining(self) -> Fact[SignedMoney]:
        variance = self.variance
        return Fact[SignedMoney](value=-variance.value,
            source="calculation:-budget.variance; " + variance.source, confidence=variance.confidence)


class BudgetSavingOption(Schema):
    title: Text
    original_amount: Money
    target_amount: Money
    assumption: Text

    @property
    def saving(self) -> Decimal:
        return max(Decimal(0), self.original_amount-self.target_amount)


class BudgetSavingsPlan(Schema):
    currency: Currency
    initial_total: Money
    current_total: Money
    limit: Money
    proposed_total: Money
    options: list[BudgetSavingOption] = Field(default_factory=list)
    notes: list[Text] = Field(default_factory=list)
    requires_confirmation: Literal[True] = True


class DayContent(Schema):
    day_number: PositiveInt
    introduction: Fact[Text]


class ContentDocument(Schema):
    """Structured editorial content; as_facts preserves the existing PDF contract."""
    introduction: Fact[Text]
    days: list[DayContent] = Field(min_length=1)
    tips: list[Fact[Text]] = Field(min_length=1)
    food: list[Fact[Text]] = Field(min_length=1)
    culture: list[Fact[Text]] = Field(min_length=1)
    warnings: list[Fact[Text]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_days(self):
        numbers = [day.day_number for day in self.days]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ValueError("Content days must be ordered, unique and contiguous from 1")
        return self

    def as_facts(self) -> list[Fact[Text]]:
        def labeled(label, fact):
            return Fact[Text](value=label + fact.value, source=fact.source, confidence=fact.confidence)
        return [self.introduction,
                *[labeled(f"Ngày {day.day_number}: ", day.introduction) for day in self.days],
                *[labeled("Mẹo: ", fact) for fact in self.tips],
                *[labeled("Món ăn: ", fact) for fact in self.food],
                *[labeled("Văn hóa: ", fact) for fact in self.culture],
                *[labeled("Lưu ý: ", fact) for fact in self.warnings]]


class Photo(Schema):
    id: Text
    place_id: Text
    url: Fact[AnyHttpUrl]
    author: Fact[Text]
    license: Fact[Text]
    source_url: Fact[AnyHttpUrl]
    caption: Fact[Text] | None = None
    license_url: Fact[AnyHttpUrl] | None = None
    local_path: Text | None = None
    provider: Literal["wikimedia", "unsplash", "pexels"] | None = None
    author_url: Fact[AnyHttpUrl] | None = None
    credit: Fact[Text] | None = None
    attribution: Fact[Text] | None = None
    restrictions: list[Fact[Text]] = Field(default_factory=list)
    selection_note: Fact[Text] | None = None


class ReviewIssue(Schema):
    category: Literal["research", "itinerary", "budget", "content", "photo", "style", "map", "layout"]
    severity: Literal["minor", "major", "blocker"]
    message: Fact[Text]
    target_agent: AgentId
    artifact_ids: list[Text] = Field(default_factory=list)
    rule_code: Text | None = None
    day_number: PositiveInt | None = None
    place_id: Text | None = None
    photo_id: Text | None = None
    classification: Literal["critical", "warning"] | None = None
    evidence_ids: list[Text] = Field(default_factory=list)

    @model_validator(mode="after")
    def classify(self):
        expected = "warning" if self.severity == "minor" else "critical"
        if self.classification is not None and self.classification != expected:
            raise ValueError("classification must agree with severity")
        self.classification = expected
        return self


class Review(Evidence):
    verdict: Literal["PASS", "REVISE"]
    summary: Fact[Text]
    issues: list[ReviewIssue] = Field(default_factory=list)
    revision: NonNegativeInt = 0

    @model_validator(mode="after")
    def check_verdict(self) -> Review:
        if self.verdict == "PASS" and any(
            issue.severity in {"major", "blocker"} for issue in self.issues
        ):
            raise ValueError("major or blocker issues cannot pass review")
        if self.verdict == "REVISE" and not self.issues:
            raise ValueError("REVISE requires at least one actionable issue")
        return self


class Artifact(Evidence):
    id: Text
    project_id: Text
    agent_id: AgentId
    kind: Literal[
        "plan", "research", "itinerary", "budget", "content", "photo",
        "illustration", "map", "chart", "review", "page", "pdf",
    ]
    name: Text
    path: Text
    mime_type: Text
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def check_timestamp(self) -> Artifact:
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        return self


def merge_artifacts(left: list[Artifact], right: list[Artifact]) -> list[Artifact]:
    """Merge fan-out updates by ID; identical replay is idempotent.

    Branches must use distinct IDs. Conflicting updates fail instead of silently
    choosing whichever parallel branch finishes last.
    """
    merged = {item.id: item for item in left}
    for item in right:
        if item.id in merged and merged[item.id] != item:
            raise ValueError(f"conflicting artifact updates: {item.id}")
        merged[item.id] = item
    return list(merged.values())


class State(TypedDict, total=False):
    """Partial LangGraph updates; nodes return only the keys they change.

    Validate external input with the Pydantic models before writing to State.
    TypedDict itself does not validate untrusted runtime dictionaries.
    The three visual branches own photos, illustrations and maps respectively;
    their shared artifacts channel uses a reducer for fan-in.
    """

    project_id: str
    user_prompt: str
    trip_request: TripRequest | None
    clarification_questions: list[Fact[str]]
    assumptions: list[Fact[str]]
    model_usage: list[dict[str, object]]
    plan: list[Fact[str]]
    research: list[Fact[str]]
    places: list[Place]
    destination_place: Place | None
    weather: WeatherReport
    research_warnings: list[Fact[str]]
    itinerary: list[DayPlan]
    planner_warnings: list[Fact[str]]
    budget: Budget
    budget_baseline: Budget | None
    budget_savings: BudgetSavingsPlan | None
    cost_items: list[BudgetItem]
    budget_warnings: list[Fact[str]]
    content: list[Fact[str]]
    content_document: ContentDocument
    photos: list[Photo]
    photo_warnings: list[Fact[str]]
    illustrations: list[Artifact]
    illustration_warnings: list[Fact[str]]
    maps: list[Artifact]
    charts: list[Artifact]
    map_input_paths: dict[str, str]
    map_warnings: list[Fact[str]]
    fixture_mode: bool
    production_approved: bool
    review: Review
    artifacts: Annotated[list[Artifact], merge_artifacts]
    stage: Literal[
        "pm", "researcher", "planner", "budget", "content_editor", "visuals",
        "reviewer", "layout_editor", "completed", "needs_attention",
    ]
    revision_count: int
    revision_target: AgentId | None
    review_route: str
    revision_pending: list[AgentId]
    revision_exhausted: bool
    revision_warnings: list[Fact[str]]
    resume_node: str
    execution_count: int
    restart_requested: bool
    approval_route: str
    approval_resolution: dict[str, object]
    supervisor_frontier: list[str]
    supervisor_selected: list[str]
    supervisor_remaining: list[str]
    supervisor_trace: list[dict[str, object]]
    feedback: list[Fact[str]]
    final_pdf: Artifact | None
    completed: bool
    error: str | None
