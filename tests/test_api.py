import json
from pathlib import Path
from unittest.mock import patch
import tempfile
import unittest

import httpx

from backend.app.config import Settings
from backend.app.main import create_app
from backend.app.schemas import Artifact
from backend.app.task_plan import build_tasks


class APITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None, data_dir=self.root / "data")
        self.app = create_app(self.settings)
        self.lifespan = self.app.router.lifespan_context(self.app)
        await self.lifespan.__aenter__()
        self.addAsyncCleanup(self.lifespan.__aexit__, None, None, None)
        self.db = self.app.state.db
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://testserver")
        self.addAsyncCleanup(self.client.aclose)

    async def create(self, **kwargs):
        response = await self.client.post("/api/projects", json={"title": "Đà Nẵng", "prompt": "Đà Nẵng 3 ngày, 5 triệu", **kwargs})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def add_artifact(self, project_id, *, path=None, artifact_id="artifact-1"):
        if path is None:
            path = self.settings.artifacts_dir / project_id / "research.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{"value":"Đà Nẵng"}', encoding="utf-8")
        artifact = Artifact(id=artifact_id, project_id=project_id, agent_id="researcher", kind="research", name="research.json",
            path=str(path), mime_type="application/json", source="test:api-artifact", confidence=.8)
        self.db.add_artifact(artifact)
        return artifact

    async def test_docs_openapi_and_health(self):
        docs = await self.client.get("/docs")
        self.assertEqual(docs.status_code, 200)
        self.assertIn("SwaggerUIBundle", docs.text)
        spec = (await self.client.get("/openapi.json")).json()
        self.assertIn("post", spec["paths"]["/api/projects"])
        self.assertIn("delete", spec["paths"]["/api/projects/{project_id}"])
        self.assertIn("get", spec["paths"]["/api/projects/{project_id}/artifacts/{artifact_id}/download"])
        self.assertEqual((await self.client.get("/health")).json(), {"status": "ok"})

    async def test_create_list_detail_and_persistence(self):
        created = await self.create()
        project_id = created["id"]
        self.assertEqual(created["status"], "queued")
        self.assertEqual(created["state"]["user_prompt"], created["prompt"])
        self.assertNotIn("state_json", created)
        self.assertEqual(created["tasks"], [])
        detail = (await self.client.get("/api/projects/"+project_id)).json()
        self.assertEqual(detail["title"], "Đà Nẵng")
        self.assertEqual(self.db.get_project(project_id)["prompt"], created["prompt"])
        second = await self.create(title="Hội An")
        page = (await self.client.get("/api/projects?limit=1&offset=1")).json()
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["items"]), 1)
        self.assertNotIn("state", page["items"][0])
        self.assertNotEqual(project_id, second["id"])

    async def test_events_cursor_no_duplicate_or_cross_project(self):
        project = await self.create()
        other = await self.create()
        self.db.add_event(project["id"], "planner", "planned", "Đã lập lịch", {"confidence": .5})
        self.db.add_event(other["id"], "system", "other", "Other event")
        url = "/api/projects/"+project["id"]+"/events"
        first = (await self.client.get(url+"?limit=1")).json()
        second = (await self.client.get(url+"?after_id="+str(first["next_after_id"]))).json()
        self.assertEqual(first["items"][0]["event_type"], "project_created")
        self.assertEqual(second["items"][0]["payload"], {"confidence": .5})
        empty = (await self.client.get(url+"?after_id="+str(second["next_after_id"]))).json()
        self.assertEqual(empty["items"], [])
        self.assertEqual(empty["next_after_id"], second["next_after_id"])

    async def test_artifact_list_detail_and_exact_file_download(self):
        project = await self.create()
        artifact = self.add_artifact(project["id"])
        url = "/api/projects/"+project["id"]+"/artifacts"
        page = (await self.client.get(url)).json()
        self.assertEqual(page["total"], 1)
        self.assertEqual(page["items"][0]["confidence"], .8)
        detail = (await self.client.get(url+"/"+artifact.id)).json()
        response = await self.client.get(detail["download_url"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, Path(artifact.path).read_bytes())
        self.assertIn("attachment", response.headers["content-disposition"])

    async def test_preview_is_inline_while_download_keeps_attachment(self):
        from PIL import Image
        from reportlab.pdfgen.canvas import Canvas
        project = await self.create()
        folder = self.settings.artifacts_dir / project['id']
        folder.mkdir(parents=True, exist_ok=True)
        pdf = folder / 'guide.pdf'
        canvas = Canvas(str(pdf))
        canvas.drawString(50, 750, 'Travel guide preview')
        canvas.save()
        image = folder / 'cover.png'
        Image.new('RGB', (40, 30), 'green').save(image)
        for index, (path, mime, kind) in enumerate(((pdf, 'application/pdf', 'pdf'), (image, 'image/png', 'illustration'))):
            item = Artifact(id=f'preview-{index}', project_id=project['id'], agent_id='layout_editor', kind=kind,
                name=path.name, path=str(path), mime_type=mime, source='test:inline-preview', confidence=1)
            self.db.add_artifact(item)
            url = f"/api/projects/{project['id']}/artifacts/{item.id}/download"
            preview = await self.client.get(url+'?inline=true')
            self.assertEqual(preview.status_code, 200)
            self.assertTrue(preview.headers['content-disposition'].startswith('inline;'))
            self.assertEqual(preview.headers['content-type'], mime)
            self.assertEqual(preview.content, path.read_bytes())
            download = await self.client.get(url)
            self.assertTrue(download.headers['content-disposition'].startswith('attachment;'))
        data = self.add_artifact(project['id'])
        url = f"/api/projects/{project['id']}/artifacts/{data.id}/download?inline=true"
        self.assertEqual((await self.client.get(url)).status_code, 415)
        self.assertEqual((await self.client.get(url.replace(project['id'], 'another-project'))).status_code, 404)

    async def test_download_cannot_read_outside_project_or_missing_file(self):
        first = await self.create()
        second = await self.create()
        secret = self.root / "not-an-artifact.txt"
        secret.write_text("must not be served")
        artifact = self.add_artifact(first["id"], path=secret)
        url = f"/api/projects/{first['id']}/artifacts/{artifact.id}/download"
        self.assertEqual((await self.client.get(url)).status_code, 403)
        self.assertEqual((await self.client.get(url+'?inline=true')).status_code, 403)
        self.assertEqual((await self.client.get(f"/api/projects/{second['id']}/artifacts/{artifact.id}")).status_code, 404)
        missing = self.settings.artifacts_dir / first["id"] / "gone.pdf"
        self.add_artifact(first["id"], path=missing, artifact_id="missing")
        self.assertEqual((await self.client.get(f"/api/projects/{first['id']}/artifacts/missing/download")).status_code, 404)

    async def test_delete_cascades_and_only_removes_own_folder(self):
        first, second = await self.create(), await self.create()
        artifact = self.add_artifact(first["id"])
        survivor = self.add_artifact(second["id"], artifact_id="survivor")
        self.db.replace_tasks(first["id"], build_tasks(first["id"]))
        response = await self.client.delete("/api/projects/"+first["id"])
        self.assertEqual(response.status_code, 204, response.text)
        self.assertEqual(response.content, b"")
        self.assertFalse(Path(artifact.path).exists())
        self.assertTrue(Path(survivor.path).exists())
        self.assertIsNone(self.db.get_project(first["id"]))
        self.assertFalse(self.db.get_events(first["id"]))
        self.assertFalse(self.db.get_tasks(first["id"]))
        self.assertFalse(self.db.get_artifacts(first["id"]))

    async def test_metadata_only_delete_and_running_conflict(self):
        first = await self.create()
        artifact = self.add_artifact(first["id"])
        self.assertEqual((await self.client.delete("/api/projects/"+first["id"]+"?delete_files=false")).status_code, 204)
        self.assertTrue(Path(artifact.path).is_file())
        second = await self.create()
        for status in ("running", "pause_requested"):
            self.db.update_project(second["id"], status=status)
            self.assertEqual((await self.client.delete("/api/projects/"+second["id"])).status_code, 409)
        self.assertIsNotNone(self.db.get_project(second["id"]))

    async def test_invalid_requests_and_missing_resources(self):
        for body in ({"prompt": " "}, {"prompt": "Valid", "title": " "}, {"prompt": "Valid", "state": {}}):
            self.assertEqual((await self.client.post("/api/projects", json=body)).status_code, 422)
        self.assertEqual((await self.client.get("/api/projects?limit=0")).status_code, 422)
        self.assertEqual((await self.client.get("/api/projects?offset=-1")).status_code, 422)
        for suffix in ("", "/events", "/artifacts", "/artifacts/nope/download"):
            self.assertEqual((await self.client.get("/api/projects/missing"+suffix)).status_code, 404)
        self.assertEqual((await self.client.delete("/api/projects/missing")).status_code, 404)

    async def test_cors_only_configured_frontend(self):
        response = await self.client.options("/api/projects", headers={"Origin": self.settings.frontend_origin, "Access-Control-Request-Method": "POST"})
        self.assertEqual(response.headers["access-control-allow-origin"], self.settings.frontend_origin)
        denied = await self.client.options("/api/projects", headers={"Origin": "https://untrusted.invalid", "Access-Control-Request-Method": "POST"})
        self.assertNotIn("access-control-allow-origin", denied.headers)

    async def test_delete_failure_retains_metadata(self):
        project = await self.create()
        artifact = self.add_artifact(project["id"])
        with patch("backend.app.main.shutil.rmtree", side_effect=PermissionError("file in use")):
            response = await self.client.delete("/api/projects/"+project["id"])
        self.assertEqual(response.status_code, 409)
        self.assertIsNotNone(self.db.get_project(project["id"]))
        self.assertTrue(Path(artifact.path).exists())

    async def test_reserved_fixture_folder_is_never_deleted(self):
        self.db.create_project("danang_3days", "Reserved test", "Fixture")
        artifact = self.add_artifact("danang_3days")
        response = await self.client.delete("/api/projects/danang_3days")
        self.assertEqual(response.status_code, 409)
        self.assertTrue(Path(artifact.path).is_file())

    async def test_trip_request_evidence_and_tasks_survive_response(self):
        fact = lambda value: {"value": value, "source": "user:api-test", "confidence": 1}
        request = {"destination": fact("Đà Nẵng"), "days": fact(3), "budget": fact("5000000"), "currency": fact("VND"), "travelers": fact(1)}
        created = await self.create(trip_request=request)
        self.assertEqual(created["trip_request"]["destination"], request["destination"])
        self.db.replace_tasks(created["id"], build_tasks(created["id"]))
        detail = (await self.client.get("/api/projects/"+created["id"])).json()
        self.assertEqual(len(detail["tasks"]), 11)
