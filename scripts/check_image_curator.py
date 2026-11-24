"""Choose one photo per JSON place, with attribution; --mock is not real photo evidence."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx
from pydantic import TypeAdapter

from backend.app.agents.image_curator import ImageCuratorAgent
from backend.app.attribution import AttributionRecord, attribution_text
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Photo, TripRequest
from backend.app.services.photos import PhotoService
from backend.app.services.public_api import PublicAPIClient
from backend.app.task_plan import build_tasks


def mock_commons(place, number=1):
    field = lambda value: {"value": value}
    return {"query": {"pages": [{"pageid": number, "title": "File:MOCK " + place["name"]["value"] + ".jpg",
        "imageinfo": [{"mime": "image/jpeg", "width": 1600, "height": 900,
            "url": f"https://upload.wikimedia.org/mock-tests/{number}.jpg",
            "descriptionurl": f"https://commons.wikimedia.org/wiki/File:MOCK_{number}.jpg",
            "extmetadata": {"Artist": field('<a href="https://commons.wikimedia.org/wiki/User:Mock">MOCK author — test only</a>'),
                "LicenseShortName": field("CC BY-SA 4.0"),
                "LicenseUrl": field("https://creativecommons.org/licenses/by-sa/4.0/"),
                "Credit": field("MOCK HTTP metadata, not a real licensed photograph; do not publish.")}}]}]}}


async def check(args):
    places = json.loads(args.research.read_text(encoding="utf-8"))
    trip = TripRequest.model_validate(json.loads(args.trip_request.read_text(encoding="utf-8"))) if args.trip_request else None
    with tempfile.TemporaryDirectory() as temp:
        settings = Settings(_env_file=None, agent_reasoning_enabled=False, data_cache_path=Path(temp) / "cache.db") if args.mock else Settings()
        if args.timeout is not None:
            settings = Settings.model_validate({**settings.model_dump(), "data_timeout_seconds": args.timeout})
        def handler(request):
            query = request.url.params.get("gsrsearch", "")
            match = next(((index, place) for index, place in enumerate(places, 1) if query.startswith(place["name"]["value"])), None)
            return httpx.Response(200, json=mock_commons(match[1], match[0]) if match else {"query": {"pages": []}})
        db = Database(settings.database_path)
        db.init()
        project_id = ("photos-mock-" if args.mock else "photos-live-") + uuid4().hex[:12]
        db.create_project(project_id, "Image Curator demo", "Find licensed photos from JSON places", trip_request=trip)
        tasks = build_tasks(project_id)
        for task in tasks[:5]:
            task["status"] = "done"
        tasks[5]["status"] = "ready"
        db.replace_tasks(project_id, tasks)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler) if args.mock else None, trust_env=False) as http:
            agent = ImageCuratorAgent(db, PhotoService(settings, PublicAPIClient(settings, client=http)), settings.artifacts_dir)
            update = await agent.run({"project_id": project_id, "places": places, "trip_request": trip})
        photos_path = Path(update["artifacts"][0].path)
        photos = TypeAdapter(list[Photo]).validate_json(photos_path.read_text(encoding="utf-8"))
        records = TypeAdapter(list[AttributionRecord]).validate_json(Path(update["artifacts"][1].path).read_text(encoding="utf-8"))
        credit_file = photos_path.with_name("attribution.txt")
        credit_file.write_text(attribution_text(records), encoding="utf-8")
        selected = {photo.place_id for photo in photos}
        missing = [place["id"] for place in places if place["id"] not in selected]
        report = {"mode": "MOCK HTTP — synthetic metadata, not real photo/license verification" if args.mock else "LIVE provider APIs; visual match requires review",
            "project_id": project_id, "schema_valid": True, "place_count": len(places), "photo_count": len(photos),
            "one_photo_per_place": len(photos) == len(places) == len(selected), "missing_place_ids": missing,
            "photos": [{"place_id": photo.place_id, "provider": photo.provider, "author": photo.author.value,
                        "license": photo.license.value} for photo in photos],
            "warnings": [fact.value for fact in update["photo_warnings"]],
            "artifacts": [artifact.path for artifact in update["artifacts"]], "attribution_text": str(credit_file)}
        (settings.artifacts_dir / project_id / "photos_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 2 if missing else 0


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--research", type=Path, default=ROOT / "backend/data/artifacts/danang_3days/research.json")
    parser.add_argument("--trip-request", type=Path)
    parser.add_argument("--timeout", type=float)
    sys.exit(asyncio.run(check(parser.parse_args())))
