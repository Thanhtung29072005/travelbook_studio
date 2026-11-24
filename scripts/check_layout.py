"""Build the sample PDF and verify sections, Unicode text, folios and image use."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pypdf import PdfReader  # noqa: E402

from backend.app.services.layout import LayoutService  # noqa: E402


def main():
    folder = ROOT / "backend/data/artifacts/danang_3days"
    path = LayoutService().build_from_json(folder)
    manifest = json.loads(path.with_suffix(".manifest.json").read_text(encoding="utf-8"))
    reader = PdfReader(path)
    sections = {section["id"]: section["page"] for section in manifest["sections"]}
    expected = {"cover", "overview", "day_1", "day_2", "day_3", "budget", "weather", "timeline"}
    if not expected <= sections.keys() or len(reader.pages) != manifest["page_count"]:
        raise RuntimeError("Missing sections or inconsistent page count")
    texts = [page.extract_text() for page in reader.pages]
    for number, text in enumerate(texts, 1):
        if f"Trang {number} / {len(texts)}" not in text:
            raise RuntimeError(f"Missing page number on page {number}")
    if "Đà Nẵng" not in texts[0] or "3 700 000" not in "\n".join(texts):
        raise RuntimeError("Vietnamese text or budget missing")
    if "Chưa có dữ liệu dự báo" not in texts[sections["weather"] - 1]:
        # Forecast state is a raster chart, so verify its embedded PNG metadata instead.
        from PIL import Image
        with Image.open(folder / "charts/weather.png") as weather:
            if json.loads(weather.info["Data"])["has_numeric_forecast"]:
                raise RuntimeError("Sample must not invent a weather forecast")
    if manifest["embedded_photo_ids"] or len(manifest["omitted_photo_ids"]) != 8:
        raise RuntimeError("Unverified fixture photos must not be embedded")
    report = {"pdf": path.name, "page_count": len(reader.pages), "sections": sections,
              "all_folios_valid": True, "vietnamese_text_valid": True,
              "unverified_photos_embedded": 0, "page_image_counts": [len(page.images) for page in reader.pages]}
    (folder / "layout_validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
