"""Open-Meteo daily forecasts; no weather invented for undated/out-of-range trips."""

from datetime import date as Date, datetime, timedelta, timezone
import math

from pydantic import ValidationError

from ..schemas import Fact, WeatherDay, WeatherReport
from .public_api import DataServiceError


class WeatherService:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    def _note(self, text, source=None):
        return Fact[str](value=text, source=source or self.settings.open_meteo_url, confidence=1)

    async def forecast(self, request, destination, *, today=None, on_event=None):
        if request.start_date is None:
            return WeatherReport(status="not_requested", notes=[self._note("Chưa có ngày đi nên chưa lấy dự báo.", "input:trip_request.start_date=null")])
        if destination is None:
            return WeatherReport(status="unavailable", notes=[self._note("Chưa có tọa độ điểm đến để lấy dự báo.")])
        if request.days.value > 366:
            raise ValueError("Weather lookup supports trips up to 366 days")
        today = today or datetime.now(timezone(timedelta(hours=7))).date()
        start = request.start_date.value
        requested = [start + timedelta(days=index) for index in range(request.days.value)]
        eligible = [day for day in requested if today <= day <= today + timedelta(days=15)]
        missing = [day for day in requested if day not in eligible]
        if not eligible:
            return WeatherReport(status="outside_forecast_window",
                missing_dates=[Fact[Date](value=day, source="calculation:outside-16-day-window", confidence=1) for day in missing],
                notes=[self._note("Ngày đi nằm ngoài cửa sổ dự báo 16 ngày; không dùng thời tiết hôm nay thay thế.")])
        params = {"latitude": destination.latitude.value, "longitude": destination.longitude.value,
                  "daily": "temperature_2m_min,temperature_2m_max,precipitation_probability_max,precipitation_sum",
                  "timezone": "auto", "temperature_unit": "celsius", "precipitation_unit": "mm",
                  "start_date": eligible[0].isoformat(), "end_date": eligible[-1].isoformat()}
        try:
            response = await self.http.request("open_meteo", self.settings.open_meteo_url, params=params,
                                               ttl=1800, on_event=on_event)
            body = response.data
            daily, units = body["daily"], body["daily_units"]
            if not isinstance(daily, dict) or not isinstance(units, dict):
                raise ValueError("Invalid daily forecast structure")
            variables = {"temperature_2m_min": ("temperature_min", "°C"),
                         "temperature_2m_max": ("temperature_max", "°C"),
                         "precipitation_probability_max": ("rain_probability", "%"),
                         "precipitation_sum": ("precipitation_mm", "mm")}
            times = daily["time"]
            if not isinstance(times, list) or len(set(times)) != len(times):
                raise ValueError("Invalid daily dates")
            for variable, (_, unit) in variables.items():
                if units.get(variable) != unit or not isinstance(daily.get(variable), list) or len(daily[variable]) != len(times):
                    raise ValueError("Unexpected forecast units or array lengths")
            source = response.source + "; retrieved=" + response.retrieved_at
            days = []
            for index, value in enumerate(times):
                day = Date.fromisoformat(value)
                if day not in eligible:
                    continue
                fields = {"day_number": (day-start).days + 1,
                          "date": {"value": day, "source": source, "confidence": 1}}
                for variable, (name, _) in variables.items():
                    number = daily[variable][index]
                    if number is not None:
                        if type(number) not in (int, float) or not math.isfinite(number):
                            raise ValueError("Invalid numeric forecast")
                        fields[name] = {"value": number, "source": source + "#" + variable, "confidence": 0.65}
                days.append(WeatherDay.model_validate(fields))
            observed = {day.date.value for day in days if any((day.temperature_min, day.temperature_max, day.rain_probability, day.precipitation_mm))}
            missing = [day for day in requested if day not in observed]
            incomplete = any(any(getattr(day, name) is None for name in (
                "temperature_min", "temperature_max", "rain_probability", "precipitation_mm")) for day in days)
            report = WeatherReport(status="available" if not missing and not incomplete else "partial", daily=days,
                timezone={"value": body["timezone"], "source": source, "confidence": 1},
                grid_latitude={"value": body["latitude"], "source": source, "confidence": 0.8},
                grid_longitude={"value": body["longitude"], "source": source, "confidence": 0.8},
                missing_dates=[Fact[Date](value=day, source="calculation:requested-date-missing; " + source, confidence=1) for day in missing],
                notes=[self._note("Dự báo từ Open-Meteo; tọa độ lưới có thể khác tọa độ yêu cầu. Confidence là đánh giá nội bộ, không phải xác suất đúng.", source)])
            if not observed:
                report.status = "unavailable"
            return report
        except (DataServiceError, TypeError, KeyError, ValueError, ValidationError) as error:
            return WeatherReport(status="unavailable", notes=[self._note(
                str(error) if isinstance(error, DataServiceError) else "Open-Meteo trả dữ liệu thiếu, sai đơn vị hoặc không hợp schema.")])
