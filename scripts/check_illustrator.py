"""Build decorative assets through 9Router; explicit --mock / --offline diagnostics."""

import argparse
import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from PIL import Image, ImageDraw, ImageOps

from backend.app.agents.illustrator import IllustrationSet, IllustratorAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import TripRequest
from backend.app.services.illustrations import IllustrationService
from backend.app.task_plan import build_tasks


def mock_png():
    """Synthetic color fixture only, not a real generated illustration."""
    image = Image.new("RGB", (192, 128), "#F3EBDD")
    draw = ImageDraw.Draw(image)
    draw.ellipse((72, 22, 126, 76), fill="#C76D4D")
    draw.rectangle((28, 82, 166, 97), fill="#247C82")
    stream = BytesIO()
    image.save(stream, format="PNG")
    return stream.getvalue()


def contact_sheet(manifest, output):
    width, height, columns = 250, 205, 4
    sheet = Image.new("RGB", (columns * width, ((len(manifest.assets) + columns-1)//columns)*height), "#FFFFFF")
    draw = ImageDraw.Draw(sheet)
    for index, asset in enumerate(manifest.assets):
        x, y = (index % columns)*width, (index//columns)*height
        with Image.open(asset.artifact.path) as source:
            tile = ImageOps.contain(source.convert("RGB"), (230, 165))
        sheet.paste(tile, (x + (width-tile.width)//2, y+8))
        draw.text((x+10, y+177), asset.key + (" [FALLBACK]" if asset.is_placeholder.value else ""), fill="#263F46")
    sheet.save(output)


async def check(args):
    read = lambda name: json.loads((args.input_dir / name).read_text(encoding="utf-8"))
    days, places, budget = read("itinerary.json"), read("research.json"), read("budget.json")
    fact = lambda value: {"value": value, "source": "input:illustrator-cli-demo", "confidence": 1}
    trip = TripRequest.model_validate(json.loads(args.trip_request.read_text(encoding="utf-8"))) if args.trip_request else TripRequest(
        destination=fact("Đà Nẵng"), days=fact(len(days)), budget=budget["limit"], currency=budget["currency"], travelers=fact(1))
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(_env_file=None, nine_router_api_key="mock-only", nine_router_image_model="mock-illustrator",
            image_cache_dir=Path(temp) / "images") if args.mock else Settings()
        if args.timeout is not None:
            settings = Settings.model_validate({**settings.model_dump(), "image_timeout_seconds": args.timeout})
        calls = []
        def handler(request):
            calls.append(1)
            return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(mock_png()).decode()}]})
        db = Database(settings.database_path)
        db.init()
        project_id = ("illustrator-mock-" if args.mock else "illustrator-offline-" if args.offline else "illustrator-live-") + uuid4().hex[:12]
        db.create_project(project_id, "Illustrator demo", "Create style-locked assets", trip_request=trip)
        tasks = build_tasks(project_id)
        for task in tasks[:5]:
            task["status"] = "done"
        tasks[6]["status"] = "ready"
        db.replace_tasks(project_id, tasks)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler) if args.mock else None, trust_env=False) as http:
            service = IllustrationService(settings, client=http)
            agent = IllustratorAgent(db, service, settings.artifacts_dir)
            update = await agent.run({"project_id": project_id, "trip_request": trip, "itinerary": days, "places": places}, force_fallback=args.offline)
        manifest_path = Path(update["artifacts"][-1].path)
        manifest = IllustrationSet.model_validate_json(manifest_path.read_text(encoding="utf-8"))
        for asset in manifest.assets:
            with Image.open(asset.artifact.path) as image:
                if image.size != (asset.width.value, asset.height.value) or image.format != "PNG":
                    raise ValueError("Invalid saved PNG dimensions or format")
        signature = manifest.style_signature.value
        if any(asset.style_signature.value != signature for asset in manifest.assets):
            raise ValueError("Mixed style signatures")
        contact_sheet(manifest, manifest_path.with_name("contact_sheet.png"))
        report = {"mode": "MOCK HTTP — synthetic fixture, not real AI art" if args.mock else "OFFLINE placeholders" if args.offline else "LIVE 9Router with placeholder fallback",
            "project_id": project_id, "asset_count": len(manifest.assets),
            "placeholder_count": sum(asset.is_placeholder.value for asset in manifest.assets),
            "style_signature": signature, "one_style_signature": True, "all_png_dimensions_valid": True,
            "role_counts": {role: sum(asset.role == role for asset in manifest.assets) for role in ("cover", "day_opener", "icon", "background")},
            "warnings": [fact.value for fact in manifest.warnings], "mock_http_calls": len(calls) if args.mock else None,
            "manifest": str(manifest_path), "preview": str(manifest_path.with_name("contact_sheet.png"))}
        (settings.artifacts_dir / project_id / "illustrator_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--mock", action="store_true")
    modes.add_argument("--offline", action="store_true")
    parser.add_argument("--timeout", type=float)
    parser.add_argument("--input-dir", type=Path, default=ROOT / "backend/data/artifacts/danang_3days")
    parser.add_argument("--trip-request", type=Path)
    asyncio.run(check(parser.parse_args()))
