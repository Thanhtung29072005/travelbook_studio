"""Offline API smoke: SQLite, OpenAPI/Swagger and the complete artifact lifecycle."""

import asyncio
import argparse
from hashlib import sha256
import json
from pathlib import Path
import sys
import tempfile
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import httpx

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.db import Database
from backend.app.schemas import Artifact


async def check():
    with tempfile.TemporaryDirectory() as temporary:
        settings = Settings(_env_file=None, data_dir=Path(temporary) / "data")
        app = create_app(settings)
        async with app.router.lifespan_context(app), httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            docs = await client.get("/docs")
            spec = await client.get("/openapi.json")
            assert docs.status_code == spec.status_code == 200 and "SwaggerUIBundle" in docs.text
            response = await client.post("/api/projects", json={"title": "Demo API Đà Nẵng", "prompt": "Đà Nẵng 3 ngày, 5 triệu"})
            assert response.status_code == 201, response.text
            project_id = response.json()["id"]
            base = "/api/projects/"+project_id
            assert (await client.get(base)).status_code == 200
            assert (await client.get("/api/projects")).json()["total"] == 1
            assert (await client.get(base+"/events")).json()["items"][0]["event_type"] == "project_created"
            path = settings.artifacts_dir / project_id / "research.json"
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps({"value": "Đà Nẵng", "source": "fixture:api-smoke", "confidence": .5}, ensure_ascii=False), encoding="utf-8")
            app.state.db.add_artifact(Artifact(id="demo-artifact", project_id=project_id, agent_id="researcher", kind="research", name=path.name,
                path=str(path), mime_type="application/json", source="fixture:api-smoke", confidence=.5))
            artifact = (await client.get(base+"/artifacts")).json()["items"][0]
            assert (await client.get(base+"/artifacts/demo-artifact")).status_code == 200
            download = await client.get(artifact["download_url"])
            assert download.status_code == 200 and download.content == path.read_bytes()
            digest = sha256(download.content).hexdigest()
            deleted = await client.delete(base)
            assert deleted.status_code == 204 and not path.exists()
            assert (await client.get(base)).status_code == 404
            report = {"mode": "ASGI HTTP; isolated SQLite; no LLM", "docs_status": docs.status_code, "openapi_status": spec.status_code,
                "create_status": response.status_code, "download_status": download.status_code, "download_sha256": digest,
                "delete_status": deleted.status_code, "cascade_verified": not app.state.db.get_events(project_id),
                "project_files_deleted": not path.exists(), "routes": sorted(spec.json()["paths"])}
        output = ROOT / "reference-review/api_validation.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))


def seed():
    """Create one clearly labeled sample artifact for manual Swagger download tests."""
    settings = Settings()
    db = Database(settings.database_path)
    db.init()
    project_id = "api-demo-"+uuid4().hex[:12]
    db.create_project(project_id, "Demo kiểm thử API", "Dự án fixture để thử /docs; không chạy LLM", state={"project_id": project_id, "fixture_mode": True, "production_approved": False})
    path = settings.artifacts_dir / project_id / "research.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"value": "Đà Nẵng", "source": "fixture:api-docs-demo", "confidence": .3}, ensure_ascii=False, indent=2), encoding="utf-8")
    artifact_id = "artifact-"+uuid4().hex[:12]
    db.add_artifact(Artifact(id=artifact_id, project_id=project_id, agent_id="researcher", kind="research", name=path.name,
        path=str(path), mime_type="application/json", source="fixture:api-docs-demo", confidence=.3))
    print(json.dumps({"project_id": project_id, "artifact_id": artifact_id,
        "download_path": f"/api/projects/{project_id}/artifacts/{artifact_id}/download"}, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true", help="Create a demo project/artifact in the configured database for Swagger")
    args = parser.parse_args()
    seed() if args.seed else asyncio.run(check())
