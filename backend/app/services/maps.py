"""Daily map rendering, with an explicit offline coordinate fallback."""

from __future__ import annotations

import math
from pathlib import Path
import textwrap

from ..schemas import DayPlan, Place
from .plotting import COLORS, footer, plot_canvas, provenance, save_png


def place_index(places: list[Place | dict]) -> dict[str, Place]:
    parsed = [Place.model_validate(place) for place in places]
    result = {place.id: place for place in parsed}
    if len(result) != len(parsed):
        raise ValueError("Duplicate place IDs")
    return result


def render_day_map(
    day: DayPlan | dict, places: list[Place | dict], destination: str | Path,
    *, size: tuple[int, int] = (1200, 800),
) -> Path:
    day = DayPlan.model_validate(day)
    indexed = place_index(places)
    if any(stop.place_id not in indexed for stop in day.stops):
        raise ValueError("Every itinerary stop must reference a known place")
    selected = [indexed[stop.place_id] for stop in day.stops]
    xs = [place.longitude.value for place in selected]
    ys = [place.latitude.value for place in selected]
    facts = [fact for place in selected for fact in (place.name, place.longitude, place.latitude)]
    if day.title is not None:
        facts.append(day.title)
    sources, confidence = provenance(facts)
    notes = ["Straight segments between stop coordinates; not a navigable road route.",
             "Offline coordinate diagram without map tiles or a routing API."]
    if any(source.startswith("fixture:") for source in sources):
        notes.append("Input includes unverified demo coordinates.")
    with plot_canvas(size) as (fig, ax):
        if not selected:
            ax.set_axis_off()
            ax.text(.5,.5,'Ngày di chuyển / nghỉ ngơi\nKhông có điểm tham quan cần vẽ bản đồ',ha='center',va='center',transform=ax.transAxes)
            return save_png(fig,destination,size,sources=sources,confidence=confidence,
                data={'day_number':day.day_number,'place_ids':[],'coordinates':[],'basemap':'none','route_type':'none'})
        ax.plot(xs, ys, "--", color=COLORS["teal"], linewidth=2, alpha=0.65)
        for index, (place, x, y) in enumerate(zip(selected, xs, ys), 1):
            ax.scatter([x], [y], s=330, c=COLORS["teal"], zorder=4, edgecolors="white")
            ax.text(x, y, str(index), ha="center", va="center", color="white", weight="bold", zorder=5)
            label = "\n".join(textwrap.wrap(f"{index}. {place.name.value}", 34))
            ax.plot([], [], "o", color=COLORS["teal"], label=label)
        for x0, y0, x1, y1 in zip(xs, ys, xs[1:], ys[1:]):
            if (x0, y0) != (x1, y1):
                ax.annotate("", xy=(x1, y1), xytext=(x0, y0),
                            arrowprops={"arrowstyle": "->", "color": COLORS["orange"], "lw": 1.5})
        dx, dy = max(max(xs) - min(xs), 0.006), max(max(ys) - min(ys), 0.006)
        ax.set_xlim(min(xs) - dx * 0.35, max(xs) + dx * 0.35)
        ax.set_ylim(min(ys) - dy * 0.35, max(ys) + dy * 0.35)
        ax.set_aspect(1 / max(0.05, math.cos(math.radians(sum(ys) / len(ys)))))
        ax.ticklabel_format(style="plain", useOffset=False)
        ax.set_xlabel("Kinh độ (°)")
        ax.set_ylabel("Vĩ độ (°)")
        ax.grid(alpha=0.18)
        ax.legend(loc="best", fontsize=8, framealpha=0.8)
        ax.set_title(f"Ngày {day.day_number} — Sơ đồ tọa độ các điểm", fontsize=15, pad=20)
        footer(fig, "Nét nối thể hiện thứ tự tham quan. Tra cứu đường đi trước khi khởi hành.")
        return save_png(fig, destination, size, sources=sources, confidence=confidence, notes=notes,
                        data={"day_number": day.day_number, "place_ids": [stop.place_id for stop in day.stops],
                              "coordinates": list(zip(xs, ys)), "route_type": "straight_segments", "basemap": "none"})


def render_basemap(day, places, layer, destination, *, size=(1200, 800)):
    from .basemaps import mercator, OSM_ATTRIBUTION
    day = DayPlan.model_validate(day)
    indexed = place_index(places)
    selected = [indexed[stop.place_id] for stop in day.stops]
    coordinates = [(place.longitude.value, place.latitude.value) for place in selected]
    sources, confidence = provenance([fact for p in selected for fact in (p.name, p.latitude, p.longitude)])
    sources = list(dict.fromkeys([*sources, *layer.sources]))
    view = layer.view
    def xy(point):
        x, y = mercator(*point, view.zoom)
        return x-view.left, y-view.top
    with plot_canvas(size) as (fig, ax):
        ax.set_position([.035, .13, .70, .75])
        ax.imshow(layer.image, extent=(0, view.width, view.height, 0))
        path = layer.route if layer.route else coordinates
        projected = [xy(point) for point in path]
        if len(projected)>1:
            xs, ys = zip(*projected)
            if layer.route:
                ax.plot(xs, ys, color='white', linewidth=6, zorder=2)
            ax.plot(xs, ys, '-' if layer.route else '--', color=COLORS['teal'], linewidth=3, zorder=3)
            for index in ([max(1, len(projected)*2//3)] if layer.route else range(1, len(projected))):
                a, b = projected[index-1], projected[index]
                if a != b:
                    ax.annotate('', xy=b, xytext=a, arrowprops={'arrowstyle': '->', 'color': COLORS['orange'], 'lw': 2}, zorder=4)
        for number, point in enumerate(coordinates, 1):
            x, y = xy(point)
            ax.scatter([x], [y], s=360, c=COLORS['teal'], edgecolors='white', linewidth=2, zorder=5)
            ax.text(x, y, str(number), ha='center', va='center', color='white', weight='bold', fontsize=10, zorder=6)
        ax.set_xlim(0, view.width)
        ax.set_ylim(view.height, 0)
        ax.set_axis_off()
        fig.text(.04, .93, f'Ngày {day.day_number} - Bản đồ hành trình', fontsize=18, weight='bold', color=COLORS['ink'])
        fig.text(.76, .84, 'THỨ TỰ THAM QUAN', fontsize=10, weight='bold', color=COLORS['teal'])
        count = len(selected)
        for number, place in enumerate(selected, 1):
            label = '\n'.join(textwrap.wrap(f'{number}. {place.name.value}', 26)[:2])
            fig.text(.76, .78-(number-1)*min(.14, .64/max(1, count)), label,
                     fontsize=max(6, 10-max(0, count-10)*.2), va='top', color=COLORS['ink'])
        fig.text(.04, .085, OSM_ATTRIBUTION, fontsize=9, color=COLORS['ink'])
        scope = ('Tuyến đường tham khảo từ OSRM. Kiểm tra điều kiện giao thông trước khi đi.' if layer.route else
                 'Nét đứt chỉ thứ tự điểm; chưa có dữ liệu đường đi thực tế.' if count>1 else 'Vị trí điểm tham quan trên bản đồ nền.')
        footer(fig, scope)
        return save_png(fig, destination, size, sources=sources, confidence=min(.75, confidence),
            notes=[OSM_ATTRIBUTION, scope, *layer.warnings], data={
                'day_number': day.day_number, 'place_ids': [stop.place_id for stop in day.stops],
                'coordinates': coordinates, 'route_type': layer.route_type, 'basemap': 'openstreetmap',
                'tile_zoom': view.zoom, 'tile_count': len(view.tiles), 'route_coordinates': layer.route})


def render_day_maps(days, places, output_dir: str | Path, *, size=(1200, 800), layers=None) -> list[Path]:
    parsed = [DayPlan.model_validate(day) for day in days]
    indexed = place_index(places)
    if len({day.day_number for day in parsed}) != len(parsed):
        raise ValueError("Duplicate day numbers would overwrite map files")
    if any(stop.place_id not in indexed for day in parsed for stop in day.stops):
        raise ValueError("Unknown place in itinerary")
    layers = layers or {}
    return [(render_basemap(day, list(indexed.values()), layers[day.day_number], Path(output_dir) / f"day{day.day_number}.png", size=size)
             if day.day_number in layers else render_day_map(day, list(indexed.values()), Path(output_dir) / f"day{day.day_number}.png", size=size))
            for day in parsed]
