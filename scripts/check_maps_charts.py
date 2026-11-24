"""Render six PNGs from the Da Nang JSON fixtures and verify saved dimensions."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from backend.app.services.charts import render_budget_chart, render_timeline_chart, render_weather_chart  # noqa: E402
from backend.app.services.maps import render_day_maps  # noqa: E402


def main():
    folder = ROOT / "backend/data/artifacts/danang_3days"
    read = lambda name: json.loads((folder / name).read_text(encoding="utf-8"))
    places, days, budget = read("research.json"), read("itinerary.json"), read("budget.json")
    paths = render_day_maps(days, places, folder / "maps")
    paths += [render_budget_chart(budget, folder / "charts/budget.png"),
              render_weather_chart(days, folder / "charts/weather.png"),
              render_timeline_chart(days, places, folder / "charts/timeline.png")]
    records = []
    for path in paths:
        expected = (1400, 900) if path.name == "timeline.png" else (1200, 800)
        with Image.open(path) as image:
            image.load()
            if image.size != expected:
                raise RuntimeError(f"Wrong PNG dimensions: {path.name}")
            records.append({"file": path.relative_to(folder).as_posix(), "size": list(image.size),
                            "source": json.loads(image.info["Source"]),
                            "confidence": float(image.info["Confidence"]),
                            "notes": json.loads(image.info["Notes"]),
                            "data": json.loads(image.info["Data"])})
    manifest = {"png_count": len(records), "all_dimensions_valid": True, "outputs": records}
    (folder / "maps_charts_validation.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps({"png_count": len(records), "all_dimensions_valid": True,
                      "files": [record["file"] for record in records]}, indent=2))


if __name__ == "__main__":
    main()
