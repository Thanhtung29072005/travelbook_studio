"""Budget, weather and itinerary timeline PNGs from validated domain data."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import textwrap

from matplotlib.patches import Patch
from matplotlib.ticker import FuncFormatter

from ..schemas import Budget, DayPlan, WeatherDay
from .maps import place_index
from .plotting import COLORS, footer, plot_canvas, provenance, save_png

CATEGORIES = {"food": "Ăn uống", "accommodation": "Lưu trú", "transport": "Di chuyển",
              "tickets": "Vé tham quan", "contingency": "Dự phòng", "other": "Khác"}


class WeatherPoint(WeatherDay):
    """Optional numeric weather input; temperatures in °C, rain probability in %."""
    pass


def _days(days):
    parsed = [DayPlan.model_validate(day) for day in days]
    if not parsed or len({day.day_number for day in parsed}) != len(parsed):
        raise ValueError("Supply a non-empty itinerary with unique day numbers")
    return sorted(parsed, key=lambda day: day.day_number)


def _money(value: Decimal) -> str:
    text = f"{value:,.0f}" if value == value.to_integral_value() else f"{value:,f}".rstrip("0").rstrip(".")
    return text.replace(",", " ")


def _hour_label(value, position):
    hours, minutes = divmod(round(value * 60), 60)
    return f"{hours:02d}:{minutes:02d}"


def render_budget_chart(budget: Budget | dict, destination: str | Path, *, size=(1200, 800)) -> Path:
    budget = Budget.model_validate(budget)
    categories = [key for key in CATEGORIES if any(item.category == key for item in budget.items)]
    sourced = {key: sum((item.amount.value for item in budget.items if item.category == key
                         and item.price_kind == "sourced"), Decimal(0)) for key in categories}
    estimated = {key: sum((item.amount.value for item in budget.items if item.category == key
                           and item.price_kind == "estimated"), Decimal(0)) for key in categories}
    facts = [budget.currency, budget.limit, *[fact for item in budget.items for fact in (item.amount, item.description)]]
    sources, confidence = provenance(facts)
    total, limit = budget.total.value, budget.limit.value
    with plot_canvas(size) as (fig, ax):
        if categories:
            ax.barh([CATEGORIES[key] for key in categories], [float(sourced[key]) for key in categories],
                    color=COLORS["teal"], label="Giá có nguồn")
            ax.barh([CATEGORIES[key] for key in categories], [float(estimated[key]) for key in categories],
                    left=[float(sourced[key]) for key in categories], color=COLORS["orange"], label="Ước tính")
            for index, key in enumerate(categories):
                value = sourced[key] + estimated[key]
                ax.annotate(_money(value), (float(value), index), xytext=(6, 0), textcoords="offset points", va="center", fontsize=9)
            ax.margins(x=0.22)
            ax.invert_yaxis()
            ax.legend(loc="lower right")
        else:
            ax.text(0.5, 0.5, "Chưa có khoản chi phí", transform=ax.transAxes, ha="center", fontsize=15)
            ax.set_yticks([])
        ax.xaxis.set_major_formatter(FuncFormatter(lambda x, pos: f"{x:,.0f}".replace(",", " ")))
        ax.set_xlabel(budget.currency.value)
        ax.grid(axis="x", alpha=0.15)
        ax.set_axisbelow(True)
        ax.set_title("Dự toán ngân sách", fontsize=17, pad=45)
        fig.text(0.14, 0.88, f"Tổng: {_money(total)} / Giới hạn: {_money(limit)} {budget.currency.value}",
                 fontsize=11, color=COLORS["teal"] if total <= limit else "#B43B35")
        footer(fig, f"Chênh lệch còn lại: {_money(limit-total)} {budget.currency.value}. "
               "Các khoản ước tính được tô màu riêng.")
        return save_png(fig, destination, size, sources=sources, confidence=confidence,
                        notes=["Estimated and sourced prices are displayed separately."],
                        data={"currency": budget.currency.value, "total": str(total), "limit": str(limit),
                              "within_budget": total <= limit,
                              "categories": {key: {"sourced": str(sourced[key]), "estimated": str(estimated[key])}
                                             for key in categories}})


def render_weather_chart(days, destination: str | Path, *, forecasts=None, size=(1200, 800)) -> Path:
    days = _days(days)
    points = [WeatherPoint.model_validate(point.model_dump(mode="json") if isinstance(point, WeatherDay) else point)
              for point in (forecasts or [])]
    indexed = {point.day_number: point for point in points}
    if len(indexed) != len(points) or set(indexed) - {day.day_number for day in days}:
        raise ValueError("Weather day numbers must be unique and belong to the itinerary")
    facts = [fact for point in points for fact in (
        point.temperature_min, point.temperature_max, point.rain_probability,
    ) if fact is not None]
    has_numeric = bool(facts)
    dated_facts = [point.date for point in points if point.date is not None] + [
        day.date for day in days if day.day_number in indexed and day.date is not None]
    sources, confidence = provenance([*facts, *(dated_facts if has_numeric else []), *[fact for day in days for fact in day.weather]])
    numbers = [day.day_number for day in days]
    notes = [] if has_numeric else ["No numeric forecast available; no weather values were invented."]
    with plot_canvas(size) as (fig, ax):
        ax.set_title("Thời tiết theo ngày", fontsize=17, pad=25)
        if has_numeric:
            for key, label, color in (("temperature_min", "Thấp nhất", COLORS["teal"]),
                                       ("temperature_max", "Cao nhất", COLORS["orange"])):
                values = [getattr(indexed.get(number), key, None) for number in numbers]
                if any(value is not None for value in values):
                    ax.plot(numbers, [value.value if value is not None else float("nan") for value in values],
                            marker="o", color=color, label=label)
            ax.set_ylabel("Nhiệt độ (°C)")
            rain = [getattr(indexed.get(number), "rain_probability", None) for number in numbers]
            rain_axis = None
            if any(value is not None for value in rain):
                rain_axis = ax.twinx()
                rain_axis.bar(numbers, [value.value if value is not None else float("nan") for value in rain],
                              width=0.35, alpha=0.18, color=COLORS["teal"], label="Khả năng mưa")
                rain_axis.set_ylabel("Khả năng mưa (%)")
                rain_axis.set_ylim(0, 100)
                fig.subplots_adjust(right=0.88)
            handles, labels = ax.get_legend_handles_labels()
            if rain_axis is not None:
                other_handles, other_labels = rain_axis.get_legend_handles_labels()
                handles += other_handles
                labels += other_labels
            ax.legend(handles, labels, loc="best")
            ax.set_xticks(numbers, [f"Ngày {number}" for number in numbers])
            ax.grid(alpha=0.15)
        else:
            ax.set_axis_off()
            ax.text(0.5, 0.91, "Chưa có dữ liệu dự báo dạng số", ha="center", transform=ax.transAxes, fontsize=16)
            for index, day in enumerate(days):
                text = "; ".join(fact.value for fact in day.weather) or "Chưa có dự báo thời tiết"
                ax.text(0.05, 0.72 - index * 0.6 / len(days),
                        f"Ngày {day.day_number}: " + "\n".join(textwrap.wrap(text, 90)),
                        transform=ax.transAxes, fontsize=11, va="top")
        footer(fig, "Cập nhật dự báo thời tiết gần ngày khởi hành.")
        return save_png(fig, destination, size, sources=sources, confidence=confidence, notes=notes,
                        data={"has_numeric_forecast": has_numeric, "day_numbers": numbers,
                              "forecasts": [point.model_dump(mode="json") for point in points]})


def render_timeline_chart(days, places, destination: str | Path, *, size=(1400, 900)) -> Path:
    days = _days(days)
    indexed = place_index(places)
    if any(stop.place_id not in indexed for day in days for stop in day.stops):
        raise ValueError("Every timeline stop must reference a known place")
    rows = [(day, item, is_activity) for day in days for _, item, is_activity in sorted(
        [(s.arrival.value,s,False) for s in day.stops]+[(a.start.value,a,True) for a in day.activities], key=lambda row:row[0])]
    facts = []
    data = []
    activity_data = []
    with plot_canvas(size) as (fig, ax):
        fig.subplots_adjust(left=0.35)
        labels, extents = [], []
        for index, (day, stop, is_activity) in enumerate(rows):
            first,last = (stop.start,stop.end) if is_activity else (stop.arrival,stop.departure)
            name = stop.title if is_activity else indexed[stop.place_id].name
            start = first.value.hour + first.value.minute / 60 + first.value.second / 3600
            end = last.value.hour + last.value.minute / 60 + last.value.second / 3600
            travel = stop.travel_minutes.value / 60 if not is_activity and stop.travel_minutes is not None else 0
            ax.barh(index, end - start, left=start, height=0.55, color='#8E9BA8' if is_activity else COLORS["teal"])
            if travel:
                ax.barh(index, travel, left=start - travel, height=0.3, color=COLORS["orange"], alpha=0.6)
            ax.text(end + 0.08, index, f"{first.value:%H:%M}–{last.value:%H:%M}", fontsize=8, va="center")
            labels.append("\n".join(textwrap.wrap(f"N{day.day_number} · {name.value}", 35)))
            facts.extend([first,last,name])
            if not is_activity and stop.travel_minutes is not None:
                facts.append(stop.travel_minutes)
            extents.extend([start - travel, end])
            if is_activity:
                activity_data.append({'day_number':day.day_number,'kind':stop.kind,'title':name.value,
                    'start_hour':start,'end_hour':end,'within_place_id':stop.within_place_id})
            else:
                data.append({"day_number": day.day_number, "place_id": stop.place_id,
                             "start_hour": start, "end_hour": end, "travel_hours": travel})
        ax.set_yticks(range(len(rows)), labels)
        ax.invert_yaxis()
        ax.set_xlim(max(0, min(extents) - 0.5), min(24, max(extents) + 2))
        ax.xaxis.set_major_formatter(FuncFormatter(_hour_label))
        ax.set_xlabel("Giờ địa phương tại điểm đến")
        ax.grid(axis="x", alpha=0.15)
        ax.set_axisbelow(True)
        ax.set_title("Timeline lịch trình 3 ngày" if len(days) == 3 else "Timeline lịch trình", fontsize=17, pad=25)
        ax.legend(handles=[Patch(color=COLORS["teal"], label="Tham quan"),
                           Patch(color='#8E9BA8', label='Ăn, nghỉ và lưu trú'),
                           Patch(color=COLORS["orange"], alpha=0.6, label="Di chuyển dự kiến")], loc="lower right")
        sources, confidence = provenance(facts)
        footer(fig, "Khung giờ tham quan và thời gian di chuyển dự kiến của từng ngày.")
        return save_png(fig, destination, size, sources=sources, confidence=confidence, data={"stops": data,'activities':activity_data})
