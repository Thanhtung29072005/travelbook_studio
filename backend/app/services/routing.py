"""OSRM/ORS matrix routing with explicitly estimated haversine fallback."""

import math
from typing import Annotated, Literal

from pydantic import Field, model_validator

from ..schemas import Fact, NonNegativeInt, Place, Schema, Text
from .public_api import DataServiceError


def haversine_km(lat1, lon1, lat2, lon2):
    values = (lat1, lon1, lat2, lon2)
    if any(type(value) not in (int, float) or not math.isfinite(value) for value in values):
        raise ValueError("Coordinates must be finite numbers")
    if not (-90 <= lat1 <= 90 and -90 <= lat2 <= 90 and -180 <= lon1 <= 180 and -180 <= lon2 <= 180):
        raise ValueError("Coordinates out of range")
    dlat, dlon = math.radians(lat2-lat1), math.radians(lon2-lon1)
    a = math.sin(dlat/2)**2 + math.cos(math.radians(lat1))*math.cos(math.radians(lat2))*math.sin(dlon/2)**2
    return 6371.0088 * 2 * math.asin(math.sqrt(min(1, max(0, a))))


class RouteLeg(Schema):
    origin_id: Text
    destination_id: Text
    distance_km: Fact[Annotated[float, Field(ge=0, allow_inf_nan=False)]]
    duration_minutes: Fact[NonNegativeInt] | None
    provider: Literal["osrm", "ors", "haversine", "identity", "unreachable"]
    estimated: bool
    reachable: bool = True

    @model_validator(mode="after")
    def check_duration(self):
        if self.reachable and self.duration_minutes is None:
            raise ValueError("Reachable leg needs a duration")
        if not self.reachable and self.duration_minutes is not None:
            raise ValueError("Unreachable leg cannot have a travel time")
        return self


class RouteMatrix(Schema):
    place_ids: list[Text]
    mode: Literal["driving", "walking", "cycling"]
    legs: list[list[RouteLeg]]
    warnings: list[Fact[str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_shape(self):
        n = len(self.place_ids)
        if len(set(self.place_ids)) != n or len(self.legs) != n or any(len(row) != n for row in self.legs):
            raise ValueError("Invalid route matrix dimensions or duplicate IDs")
        for i, row in enumerate(self.legs):
            for j, leg in enumerate(row):
                if leg.origin_id != self.place_ids[i] or leg.destination_id != self.place_ids[j]:
                    raise ValueError("Matrix leg IDs do not match row/column IDs")
        return self

    def leg(self, origin_id, destination_id):
        return self.legs[self.place_ids.index(origin_id)][self.place_ids.index(destination_id)]


class RoutingService:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def _fallback(self, a, b, *, unreachable=False):
        same = a.id == b.id
        straight = haversine_km(a.latitude.value, a.longitude.value, b.latitude.value, b.longitude.value)
        speed = self.settings.routing_fallback_speed_kmh or {"driving": 25, "walking": 4, "cycling": 12}[self.settings.routing_mode]
        distance = 0 if same else straight if unreachable else straight * self.settings.routing_haversine_factor
        source = ("calculation:identity" if same else
            f"estimate:haversine; straight_km={straight:.6f}; factor={1 if unreachable else self.settings.routing_haversine_factor}; speed_kmh={speed}; "
            f"coordinates={a.latitude.source}|{a.longitude.source}|{b.latitude.source}|{b.longitude.source}")
        confidence = 1 if same else min(0.25, a.latitude.confidence, a.longitude.confidence, b.latitude.confidence, b.longitude.confidence)
        return RouteLeg(origin_id=a.id, destination_id=b.id,
            distance_km={"value": distance, "source": source, "confidence": confidence},
            duration_minutes=None if unreachable else {"value": math.ceil(distance/speed*60), "source": source, "confidence": confidence},
            provider="identity" if same else "unreachable" if unreachable else "haversine",
            estimated=not same, reachable=not unreachable)

    async def matrix(self, places, *, on_event=None):
        places = [Place.model_validate(place) for place in places]
        n = len(places)
        if not 1 <= n <= self.settings.routing_max_points or len({place.id for place in places}) != n:
            raise ValueError("Routing needs unique places within routing_max_points")
        legs = [[self._fallback(a, b) if i == j else None for j, b in enumerate(places)] for i, a in enumerate(places)]
        warnings, blocked = [], set()
        def warn(text, source):
            warnings.append(Fact[str](value=text, source=source, confidence=1))
        for provider in self.settings.routing_provider_order.split(","):
            if all(leg is not None for row in legs for leg in row) or provider == "haversine":
                break
            try:
                if provider == "osrm":
                    profile = getattr(self.settings, f"osrm_{self.settings.routing_mode}_profile")
                    if not profile:
                        warn("OSRM chưa cấu hình profile đúng phương tiện; bỏ qua, không dùng driving cho walking/cycling.", self.settings.osrm_base_url)
                        continue
                    coordinates = ";".join(f"{place.longitude.value:.6f},{place.latitude.value:.6f}" for place in places)
                    response = await self.http.request("osrm", self.settings.osrm_base_url + f"/table/v1/{profile}/{coordinates}",
                        params={"annotations": "duration,distance"}, ttl=86400, on_event=on_event)
                    if response.data.get("code") != "Ok":
                        raise DataServiceError("osrm: matrix request was not successful", provider_code=response.data.get("code"))
                else:
                    key = self.settings.openrouteservice_api_key.get_secret_value().strip()
                    if not key:
                        warn("ORS chưa có API key; dùng provider tiếp theo.", self.settings.ors_base_url)
                        continue
                    profile = {"driving": "driving-car", "walking": "foot-walking", "cycling": "cycling-regular"}[self.settings.routing_mode]
                    response = await self.http.request("ors", self.settings.ors_base_url + "/" + profile,
                        json_body={"locations": [[place.longitude.value, place.latitude.value] for place in places],
                                   "metrics": ["distance", "duration"], "units": "m"},
                        headers={"Authorization": key}, ttl=86400, on_event=on_event)
                durations, distances = response.data["durations"], response.data["distances"]
                if len(durations) != n or len(distances) != n or any(len(row) != n for row in [*durations, *distances]):
                    raise ValueError("Malformed matrix dimensions")
                for i in range(n):
                    for j in range(n):
                        if legs[i][j] is not None:
                            continue
                        seconds, meters = durations[i][j], distances[i][j]
                        if seconds is None:
                            blocked.add((i, j))
                            continue
                        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0 for value in (seconds, meters)):
                            continue
                        source = response.source + "; retrieved=" + response.retrieved_at + f"; cell={i},{j}"
                        confidence = min(0.75, places[i].latitude.confidence, places[i].longitude.confidence,
                                         places[j].latitude.confidence, places[j].longitude.confidence)
                        legs[i][j] = RouteLeg(origin_id=places[i].id, destination_id=places[j].id, provider=provider,
                            estimated=False, distance_km={"value": meters/1000, "source": source, "confidence": confidence},
                            duration_minutes={"value": math.ceil(seconds/60), "source": source, "confidence": confidence})
            except DataServiceError as error:
                if provider == "osrm" and error.provider_code in {"NoRoute", "NoTable", "NoSegment"}:
                    blocked.update((i, j) for i in range(n) for j in range(n) if legs[i][j] is None)
                warn(f"{provider}: không lấy được ma trận hợp lệ; thử provider tiếp theo.",
                     self.settings.osrm_base_url if provider == "osrm" else self.settings.ors_base_url)
            except (TypeError, KeyError, ValueError, AttributeError):
                warn(f"{provider}: không lấy được ma trận hợp lệ; thử provider tiếp theo.",
                     self.settings.osrm_base_url if provider == "osrm" else self.settings.ors_base_url)
        for i in range(n):
            for j in range(n):
                if legs[i][j] is None:
                    legs[i][j] = self._fallback(places[i], places[j], unreachable=(i, j) in blocked)
        if any(leg.provider == "haversine" for row in legs for leg in row):
            warn("Haversine là khoảng cách cầu nhân hệ số và tốc độ giả định; chưa xác minh đường đi, giao thông hoặc chướng ngại.", "assumption:routing-haversine")
        if any(leg.provider == "unreachable" for row in legs for leg in row):
            warn("Provider báo không có đường ở một số cặp; không tự thay các cặp không đi được bằng thời gian hữu hạn.", "routing:unreachable-cells")
        return RouteMatrix(place_ids=[place.id for place in places], mode=self.settings.routing_mode, legs=legs, warnings=warnings)
