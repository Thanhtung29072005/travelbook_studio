"""Prepare synthetic images, save PNGs and verify their dimensions after reopening."""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image  # noqa: E402

from backend.app.services.images import crop_image, placeholder_image, prepare_image, resize_image  # noqa: E402
from backend.app.services.style_lock import get_style_lock  # noqa: E402


def main():
    output = ROOT / "backend/data/artifacts/image_smoke"
    output.mkdir(parents=True, exist_ok=True)
    source = Image.new("RGB", (400, 200), "#247C82")
    source.paste("#C76D4D", (200, 0, 400, 200))
    images = [
        ("resize_640x360.png", resize_image(source, (640, 360)), (640, 360), False),
        ("crop_300x300.png", crop_image(source, (300, 300)), (300, 300), False),
        ("placeholder_800x450.png", placeholder_image((800, 450)), (800, 450), True),
    ]
    fallback = prepare_image(None, (320, 180))
    images.append(("fallback_320x180.png", fallback.image, (320, 180), fallback.is_placeholder))
    results = []
    for filename, image, expected, is_placeholder in images:
        path = output / filename
        image.save(path, format="PNG")
        with Image.open(path) as saved:
            if saved.size != expected:
                raise RuntimeError(f"Wrong dimensions: {filename}: {saved.size}")
        results.append({"file": filename, "size": list(expected), "is_placeholder": is_placeholder})
    lock = get_style_lock()
    manifest = {"input": "synthetic colored rectangles, not destination photographs",
                "style_signature": lock.signature, "all_dimensions_valid": True, "images": results}
    (output / "validation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main()
