"""Render Map Agent outputs from explicitly supplied project JSON paths."""

import argparse
import asyncio
import json
from pathlib import Path
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PIL import Image

from backend.app.agents.map_agent import MapAgent, MapInputFiles, MapResult
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import TripRequest
from backend.app.task_plan import build_tasks


async def check(args):
    settings = Settings()
    trip = TripRequest.model_validate_json(args.trip_request.read_text(encoding="utf-8-sig")) if args.trip_request else None
    db = Database(settings.database_path)
    db.init()
    project_id = "map-files-" + uuid4().hex[:12]
    db.create_project(project_id, "Map Agent from project files", "Render provided JSON without fixture defaults", trip_request=trip)
    tasks = build_tasks(project_id)
    for task in tasks[:5]:
        task["status"] = "done"
    tasks[7]["status"] = "ready"
    db.replace_tasks(project_id, tasks)
    files = MapInputFiles(itinerary=args.itinerary, places=args.places, budget=args.budget, weather=args.weather)
    agent = MapAgent(db, settings.artifacts_dir)
    update = await agent.run({"project_id": project_id, "trip_request": trip}, inputs=files)
    manifest = MapResult.model_validate_json(Path(update["artifacts"][-1].path).read_text(encoding="utf-8"))
    records = []
    for artifact in [*update["maps"], *update["charts"]]:
        with Image.open(artifact.path) as image:
            image.load()
            records.append({"file": artifact.path, "size": list(image.size), "data": json.loads(image.info["Data"]),
                            "inputs": json.loads(image.info["Inputs"]), "confidence": float(image.info["Confidence"])})
    report = {"project_id": project_id, "day_count": manifest.day_count, "map_count": len(update["maps"]),
        "chart_count": len(update["charts"]), "contains_demo_input": manifest.contains_demo_input.value,
        "has_numeric_weather": manifest.has_numeric_weather.value,
        "inputs": [snapshot.model_dump(mode="json") for snapshot in manifest.inputs],
        "warnings": [fact.value for fact in manifest.warnings], "outputs": records,
        "manifest": update["artifacts"][-1].path}
    output = settings.artifacts_dir / project_id / "map_agent_validation.json"
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({key: value for key, value in report.items() if key not in {"outputs", "inputs"}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--itinerary", type=Path, required=True, help="Planner's actual itinerary.json")
    parser.add_argument("--places", "--research", dest="places", type=Path, required=True, help="Researcher list[Place] JSON")
    parser.add_argument("--budget", type=Path, required=True)
    parser.add_argument("--weather", type=Path, help="Researcher WeatherReport JSON, optional")
    parser.add_argument("--trip-request", type=Path)
    asyncio.run(check(parser.parse_args()))
