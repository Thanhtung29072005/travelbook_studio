"""Run Layout Editor with a supplied State JSON or the explicitly labeled offline fixture."""

import argparse
import asyncio
import json
from pathlib import Path
import shutil
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.agents.layout_editor import LayoutEditorAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Artifact, ContentDocument, Fact, Review, TripRequest
from backend.app.task_plan import build_tasks


def sample_state(root, project_id, *, illustration_dir=None):
    sample = ROOT / "backend/data/artifacts/danang_3days"
    read = lambda name: json.loads((sample / name).read_text(encoding="utf-8"))
    fact = lambda value, confidence=.6: {"value": value, "source": "fixture:layout-editor-demo", "confidence": confidence}
    days, places, budget, photos, content = (read(name) for name in ("itinerary.json", "research.json", "budget.json", "photos.json", "content.json"))
    trip = TripRequest(destination=fact("Đà Nẵng", 1), days=fact(3, 1), budget=budget["limit"], currency=budget["currency"], travelers=fact(1, 1), traveler_ages=[fact(30, 1)])
    document = ContentDocument(introduction=content[0], days=[{"day_number": day["day_number"], "introduction": content[index+1]} for index, day in enumerate(days)],
        tips=[fact("Dành thời gian nghỉ và cập nhật đường đi trước khi khởi hành.")], food=[fact("Gợi ý món ăn địa phương cần kiểm tra lại giá và địa chỉ trước khi dùng.")],
        culture=[fact("Giữ trang phục và cách ứng xử phù hợp tại nơi thờ tự.")], warnings=[fact("Dữ liệu minh họa, chưa phải lịch trình đã xác minh.")])
    assets = {"maps": [], "charts": [], "illustrations": []}
    for kind, names in (("map", [f"day{i}.png" for i in (1, 2, 3)]), ("chart", [f"{key}.png" for key in ("budget", "weather", "timeline")])):
        folder = "maps" if kind == "map" else "charts"
        for name in names:
            target = root / "inputs" / folder / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(sample / folder / name, target)
            assets[folder].append(Artifact(id=uuid4().hex, project_id=project_id, agent_id="map_agent", kind=kind, name=name,
                path=str(target.resolve()), mime_type="image/png", source="fixture:danang_3days", confidence=.4))
    if illustration_dir:
        for name in ("cover", "day_1", "day_2", "day_3"):
            source = illustration_dir / (name+".png")
            if not source.is_file():
                raise FileNotFoundError(source)
            target = root / "inputs" / "illustrations" / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            assets["illustrations"].append(Artifact(id=uuid4().hex, project_id=project_id, agent_id="illustrator", kind="illustration", name=source.name,
                path=str(target.resolve()), mime_type="image/png", source="generated:existing-illustrator-output; decorative-only", confidence=.6))
    return {"project_id": project_id, "trip_request": trip, "itinerary": days, "places": places, "budget": budget, "photos": photos,
        "content": content, "content_document": document, **assets, "fixture_mode": True, "production_approved": False,
        "review": Review(verdict="PASS", source="fixture:layout-only-structure-test", confidence=1,
            summary=Fact[str](value="Layout structure test only; not production approval", source="fixture:layout-only-structure-test", confidence=1))}


async def check(args):
    settings = Settings()
    project_id = "layout-editor-"+uuid4().hex[:12]
    folder = settings.artifacts_dir / project_id
    state = json.loads(args.state.read_text(encoding="utf-8-sig")) if args.state else sample_state(folder, project_id, illustration_dir=args.illustration_dir)
    # Real assets must retain the supplied project identity.
    project_id = state.get("project_id", project_id)
    state["project_id"] = project_id
    db = Database(settings.database_path)
    db.init()
    if db.get_project(project_id) is None:
        db.create_project(project_id, "Layout Editor book", "Supplied state" if args.state else "Offline labeled fixture", trip_request=state.get("trip_request"))
        tasks = build_tasks(project_id)
        for task in tasks[:9]: task["status"] = "done"
        tasks[9]["status"] = "ready"
        db.replace_tasks(project_id, tasks)
    update = await LayoutEditorAgent(db, settings).run(state)
    manifest = json.loads(Path(update["artifacts"][1].path).read_text(encoding="utf-8"))
    report = {"project_id": project_id, "pdf": update["final_pdf"].path, "page_count": manifest["page_count"],
        "toc_entries": len(manifest["toc"]), "low_confidence_count": len(manifest["low_confidence_facts"]),
        "embedded_photo_ids": manifest["embedded_photo_ids"], "text_image_overlap_count": manifest["text_image_overlap_count"], "production_approved": False}
    (Path(update["final_pdf"].path).parent / "layout_validation.json").write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path)
    parser.add_argument("--illustration-dir", type=Path, help="Existing cover/day_1/day_2/day_3 PNGs; never treated as real place photos")
    asyncio.run(check(parser.parse_args()))
