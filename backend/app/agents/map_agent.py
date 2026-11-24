"""Maps/charts from explicit upstream JSON files, never from an implicit demo folder."""

from hashlib import sha256
import json
from pathlib import Path
from uuid import uuid4

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from pydantic import Field, TypeAdapter

from ..schemas import Artifact, Budget, DayPlan, Fact, Place, Schema, State, Text, TripRequest, WeatherReport
from ..services.charts import render_budget_chart, render_timeline_chart, render_weather_chart
from ..services.maps import render_day_maps
from .base import BaseAgent

MAX_JSON_BYTES = 5_000_000


class MapInputFiles(Schema):
    itinerary: Path
    places: Path
    budget: Path
    weather: Path | None = None


class InputSnapshot(Schema):
    role: Text
    path: Text
    snapshot_path: Text
    sha256: Fact[Text]
    contains_demo_input: Fact[bool]


class MapResult(Schema):
    day_count: int
    inputs: list[InputSnapshot]
    maps: list[Artifact]
    charts: list[Artifact]
    contains_demo_input: Fact[bool]
    has_numeric_weather: Fact[bool]
    warnings: list[Fact[Text]] = Field(default_factory=list)


def _fact(value, source, confidence=1):
    return {"value": value, "source": source, "confidence": confidence}


def _contains_demo(data):
    if isinstance(data, dict):
        source = str(data.get("source", "")).lower()
        return any(marker in source for marker in ("fixture:", "mock:", "mock-http", "synthetic:")) or any(
            _contains_demo(value) for value in data.values())
    return isinstance(data, list) and any(_contains_demo(value) for value in data)


def _read_json(path):
    path = Path(path).resolve(strict=True)
    if not path.is_file() or path.suffix.lower() != ".json":
        raise ValueError("Map input must be a JSON file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_JSON_BYTES + 1)
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("Map input JSON exceeds size limit")
    return path, raw, json.loads(raw.decode("utf-8-sig"))


class MapAgent(BaseAgent):
    agent_id = "map_agent"
    display_name = "Map Agent"

    def __init__(self, db, artifacts_root):
        super().__init__(db, None, artifacts_root)

    def _resolve_inputs(self, state, inputs):
        if inputs is not None:
            return MapInputFiles.model_validate(inputs)
        if state.get("map_input_paths"):
            return MapInputFiles.model_validate(state["map_input_paths"])
        # Use only matching upstream artifacts of THIS project, in reducer order.
        selected = {}
        definitions = {("planner", "itinerary.json"): "itinerary", ("researcher", "research.json"): "places",
                       ("budget", "budget.json"): "budget", ("researcher", "weather.json"): "weather"}
        for value in state.get("artifacts", []):
            artifact = Artifact.model_validate(value)
            role = definitions.get((artifact.agent_id, artifact.name))
            if role and artifact.project_id == state["project_id"] and artifact.mime_type == "application/json":
                selected[role] = artifact.path
        return MapInputFiles.model_validate(selected)

    @staticmethod
    def _load(inputs, trip_request=None):
        loaded = {role: _read_json(path) for role, path in inputs.model_dump().items() if path is not None}
        days = TypeAdapter(list[DayPlan]).validate_python(loaded["itinerary"][2])
        places = TypeAdapter(list[Place]).validate_python(loaded["places"][2])
        budget = Budget.model_validate(loaded["budget"][2])
        if not 1 <= len(days) <= 31 or [day.day_number for day in days] != list(range(1, len(days) + 1)):
            raise ValueError("Map Agent requires 1..31 complete, ordered, consecutive days")
        ids = {place.id for place in places}
        if len(ids) != len(places) or any(stop.place_id not in ids for day in days for stop in day.stops):
            raise ValueError("All itinerary stops need unique matching places")
        dates = [day.date.value for day in days if day.date is not None]
        if len(set(dates)) != len(dates) or any(current <= previous for previous, current in zip(dates, dates[1:])):
            raise ValueError("Itinerary dates must be unique and chronological")
        if any(item.day_number is not None and item.day_number > len(days) for item in budget.items):
            raise ValueError("Budget references a day outside the itinerary")
        if trip_request is not None:
            trip = TripRequest.model_validate(trip_request)
            if trip.days.value != len(days) or trip.currency.value != budget.currency.value or trip.budget.value != budget.limit.value:
                raise ValueError("TripRequest and itinerary/budget are inconsistent")
            if trip.start_date is not None:
                from datetime import timedelta
                if any(day.date is None or day.date.value != trip.start_date.value + timedelta(days=index) for index, day in enumerate(days)):
                    raise ValueError("TripRequest dates and itinerary dates are inconsistent")
        weather = WeatherReport.model_validate(loaded["weather"][2]) if "weather" in loaded else None
        warnings, forecasts = [], []
        if weather is None:
            warnings.append(Fact[Text](**_fact("Không được cung cấp weather.json; biểu đồ ghi thiếu dự báo, không sinh số liệu.", "input:weather-missing")))
        else:
            if len({point.day_number for point in weather.daily}) != len(weather.daily) or any(point.day_number > len(days) for point in weather.daily):
                raise ValueError("Weather days must be unique and belong to itinerary")
            warnings.extend(weather.notes)
            if weather.status not in {"available", "partial"} and weather.daily:
                raise ValueError("Unavailable weather cannot contain forecast days")
            for point in weather.daily:
                day = days[point.day_number - 1]
                if point.date is not None and day.date is not None and point.date.value != day.date.value:
                    raise ValueError("Weather date does not match itinerary date")
                if point.date is None or day.date is None:
                    warnings.append(Fact[Text](**_fact(f"Ngày {point.day_number}: thiếu ngày cụ thể để ghép dự báo; không dùng số liệu này.", "validation:weather-date-required")))
                    continue
                forecasts.append(point)
            if weather.status not in {"available", "partial"}:
                warnings.append(Fact[Text](**_fact("Nguồn thời tiết chưa có dự báo khả dụng.", "input:weather-status:" + weather.status)))
        numeric = any(any(getattr(point, key) is not None for key in ("temperature_min", "temperature_max", "rain_probability")) for point in forecasts)
        if any(_contains_demo(data) for _, _, data in loaded.values()):
            warnings.append(Fact[Text](**_fact("Đầu vào có dữ liệu fixture/mô phỏng; đọc file dự án không làm dữ kiện trở thành đã xác minh.", "validation:demo-source-detected")))
        if any(item.price_kind == "estimated" for item in budget.items):
            warnings.append(Fact[Text](**_fact("Ngân sách có khoản ước tính, được vẽ tách với giá có nguồn.", "validation:budget-estimated-items")))
        return loaded, days, places, budget, forecasts, numeric, warnings

    def render(self, project_id, inputs, *, trip_request=None):
        self.validate_project_id(project_id)
        inputs = MapInputFiles.model_validate(inputs)
        # Read once and validate the ENTIRE bundle before producing any PNG.
        bundle = self._load(inputs, trip_request)
        return self._render_bundle(project_id, bundle)

    async def render_live(self, project_id, inputs, *, settings, public_api, trip_request=None, on_event=None):
        from ..services.basemaps import BasemapService
        self.validate_project_id(project_id)
        # Keep the validated bytes used before networking as the render snapshot.
        bundle = self._load(MapInputFiles.model_validate(inputs), trip_request)
        layers, messages = await BasemapService(settings, public_api, on_event=on_event).prepare(bundle[1], bundle[2])
        bundle[-1].extend(Fact[Text](**_fact(message, 'execution:basemap-fallback')) for message in messages)
        return self._render_bundle(project_id, bundle, layers=layers)

    def _render_bundle(self, project_id, bundle, *, layers=None):
        loaded, days, places, budget, forecasts, numeric, warnings = bundle
        if any(day.day_number not in (layers or {}) for day in days):
            warnings.append(Fact[Text](**_fact('Một số ngày dùng sơ đồ tọa độ dự phòng, không phải đường đi thực tế.', 'scope:coordinate-map-not-road-route')))
        run_id = uuid4().hex[:16]
        folder = self.artifacts_root / project_id / self.agent_id / run_id
        snapshots = []
        for role, (path, raw, data) in loaded.items():
            snapshot = folder / "inputs" / (role + ".json")
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            snapshot.write_bytes(raw)
            snapshots.append(InputSnapshot(role=role, path=str(path), snapshot_path=str(snapshot),
                sha256=_fact(sha256(raw).hexdigest(), "calculation:sha256(input-file-bytes)"),
                contains_demo_input=_fact(_contains_demo(data), "validation:input-source-markers")))
        paths = [(path, "map", ("itinerary", "places"), (1200, 800)) for path in render_day_maps(days, places, folder / "maps", layers=layers)]
        paths.extend([(render_budget_chart(budget, folder / "charts/budget.png"), "chart", ("budget",), (1200, 800)),
                      (render_weather_chart(days, folder / "charts/weather.png", forecasts=forecasts), "chart", ("itinerary", "weather"), (1200, 800))])
        timeline_size = (1400, min(6000, max(900, sum(len(day.stops)+len(day.activities) for day in days)*60 + 240)))
        paths.append((render_timeline_chart(days, places, folder / "charts/timeline.png", size=timeline_size), "chart", ("itinerary", "places"), timeline_size))
        maps, charts = [], []
        for path, kind, roles, size in paths:
            relevant = [snapshot for snapshot in snapshots if snapshot.role in roles]
            evidence = [{"role": snapshot.role, "sha256": snapshot.sha256.value} for snapshot in relevant]
            # Keep renderer's facts/data, adding the exact file identity used by this run.
            with Image.open(path) as image:
                image.load()
                if image.format != "PNG" or image.size != size:
                    raise ValueError("Map renderer returned wrong PNG dimensions")
                info, rgb = dict(image.info), image.convert("RGB")
            sources = json.loads(info["Source"])
            sources.extend("file-sha256:" + value["role"] + ":" + value["sha256"] for value in evidence)
            info["Source"], info["Inputs"] = json.dumps(sources, ensure_ascii=False), json.dumps(evidence)
            if any(snapshot.contains_demo_input.value for snapshot in relevant):
                info["Notes"] = json.dumps([*json.loads(info["Notes"]), "Input contains unverified demo/fixture data."], ensure_ascii=False)
            png_info = PngInfo()
            for key, value in info.items():
                if isinstance(value, str):
                    png_info.add_text(key, value)
            rgb.save(path, format="PNG", pnginfo=png_info)
            artifact = Artifact(id=str(uuid4()), project_id=project_id, agent_id=self.agent_id, kind=kind, name=path.name,
                path=str(path), mime_type="image/png", source="derived:MapAgent; " + "; ".join(sources), confidence=float(info["Confidence"]))
            self.db.add_artifact(artifact)
            (maps if kind == "map" else charts).append(artifact)
            self.event(project_id, kind + "_rendered", "Đã lưu PNG từ JSON đầu vào", {"artifact_id": artifact.id, "name": path.name, "inputs": evidence})
        result = MapResult(day_count=len(days), inputs=snapshots, maps=maps, charts=charts,
            contains_demo_input=_fact(any(snapshot.contains_demo_input.value for snapshot in snapshots), "validation:input-source-markers"),
            has_numeric_weather=_fact(numeric, "validation:dated-numeric-weather-input"), warnings=warnings)
        manifest = self.save_json(project_id, run_id, "maps_charts.json", result, kind="review",
            source="derived:MapAgent file-provenance manifest", confidence=min(artifact.confidence for artifact in maps + charts))
        return result, manifest

    async def run(self, state: State, *, inputs=None, managed=False) -> State:
        project_id = state["project_id"]
        self.validate_project_id(project_id)
        project = self.db.get_project(project_id)
        if project is None or project["status"] not in {"queued", "running", "paused", "needs_attention"}:
            raise ValueError("Map Agent needs a runnable project")
        tasks = self.db.get_tasks(project_id)
        by_id = {task["id"]: task for task in tasks}
        owned = [task for task in tasks if task["agent"] == self.agent_id]
        if not owned or any(dep not in by_id or by_id[dep]["status"] != "done" for task in owned for dep in task["depends_on"]):
            raise ValueError("Complete Content Editor dependencies before Map Agent")
        if not managed:
            self.db.update_project(project_id, status="running", stage="visuals", progress=50, error=None)
        for task in owned:
            self.db.set_task_status(project_id, task["id"], "in_progress")
        self.event(project_id, "agent_started", "Map Agent đọc JSON dự án và vẽ bản đồ/biểu đồ")
        try:
            files = self._resolve_inputs(state, inputs)
            result, manifest = self.render(project_id, files, trip_request=state.get("trip_request"))
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "done")
            fresh = self.db.get_tasks(project_id)
            completed = {task["id"] for task in fresh if task["status"] == "done"}
            reviewer_ready = False
            for task in fresh:
                if not managed and task["agent"] == "reviewer" and all(dep in completed for dep in task["depends_on"]):
                    self.db.set_task_status(project_id, task["id"], "ready")
                    reviewer_ready = True
            blocked = not managed and any(task["agent"] in {"image_curator", "illustrator"} and task["status"] == "blocked" for task in fresh)
            stage = "needs_attention" if blocked else "reviewer" if reviewer_ready else "visuals"
            artifacts = [*result.maps, *result.charts, manifest]
            update: State = {"maps": result.maps, "charts": result.charts, "map_warnings": result.warnings,
                "map_input_paths": {snapshot.role: snapshot.path for snapshot in result.inputs}, "artifacts": artifacts,
                "stage": stage, "fixture_mode": False, "production_approved": False, "error": None}
            if not managed:
                self.db.update_project(project_id, status="needs_attention" if blocked else "queued", stage=stage, progress=60,
                    state={**state, **update, "artifacts": [*state.get("artifacts", []), *artifacts]})
            self.event(project_id, "maps_charts_ready", "Đã xuất bản đồ và biểu đồ từ file dự án",
                {"map_count": len(result.maps), "chart_count": len(result.charts), "contains_demo_input": result.contains_demo_input.value,
                 "has_numeric_weather": result.has_numeric_weather.value, "reviewer_ready": reviewer_ready})
            return update
        except Exception as error:
            for task in owned:
                self.db.set_task_status(project_id, task["id"], "blocked")
            if not managed:
                self.db.update_project(project_id, status="failed", error=f"Map Agent failed: {type(error).__name__}")
            self.event(project_id, "agent_failed", "Map Agent chưa tạo được bản đồ hợp lệ", {"error_type": type(error).__name__})
            raise
