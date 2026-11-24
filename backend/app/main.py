"""Local project and artifact API; supports automated agent workflow management."""

from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
import asyncio
import shutil
import sqlite3
import json
from typing import Annotated, Any
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Path as ApiPath, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import Field

from .config import Settings, get_settings
from .db import Database, ProjectStatus
from .manager import WorkflowManager
from .schemas import Artifact, Schema, TripRequest
from .workflow import TravelBookWorkflow
from .live_workflow import LiveTravelBookWorkflow
from .workspace_api import mount_workspace
from .workspace import WorkspaceStore

SafeId = Annotated[str, ApiPath(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,99}$")]


class ProjectCreate(Schema):
    title: str = Field(default="Dự án cẩm nang du lịch", min_length=1, max_length=200)
    prompt: str = Field(min_length=1, max_length=6000, examples=["Lên cẩm nang Phú Quốc 3 ngày cho 2 người lớn, tổng ngân sách 10 triệu đồng."])
    trip_request: TripRequest | None = None
    start_workflow: bool = False
    approval_enabled: bool = False


class ProjectSummary(Schema):
    id: str
    title: str
    prompt: str
    status: ProjectStatus
    stage: str
    progress: int
    pause_requested: bool
    final_pdf: str | None
    error: str | None
    created_at: datetime
    updated_at: datetime


class ProjectDetail(ProjectSummary):
    trip_request: TripRequest | None
    state: dict[str, Any]
    tasks: list[dict[str, Any]] = Field(default_factory=list)
    agents: list[dict[str, Any]] = Field(default_factory=list)
    approvals: list[dict[str, Any]] = Field(default_factory=list)
    approval_enabled: bool = False
    approval_stages: list[str] = Field(default_factory=list)


class ProjectPage(Schema):
    items: list[ProjectSummary]
    limit: int
    offset: int
    total: int


class EventItem(Schema):
    id: int
    project_id: str
    agent: str
    event_type: str
    message: str
    payload: dict[str, Any]
    created_at: datetime


class EventPage(Schema):
    items: list[EventItem]
    next_after_id: int


class ArtifactItem(Artifact):
    download_url: str


class ArtifactPage(Schema):
    items: list[ArtifactItem]
    limit: int
    offset: int
    total: int


class RouterSettingsRequest(Schema):
    models: list[str] = Field(min_length=1, max_length=20)
    image_model: str = Field(default="", max_length=160)
    image_fallback_models: list[str] = Field(default_factory=list, max_length=10)


def create_app(settings: Settings | None = None, *, database: Database | None = None, workflow: TravelBookWorkflow | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = database or Database(settings.database_path)
    root = settings.artifacts_dir.resolve()
    wf = workflow or LiveTravelBookWorkflow(db, artifacts_root=root, settings=settings)
    manager = WorkflowManager(db, wf)

    @asynccontextmanager
    async def lifespan(app):
        db.init()
        with db.connect() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS app_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL CHECK(json_valid(value)))")
            saved = connection.execute("SELECT value FROM app_settings WHERE key='router_models'").fetchone()
            if saved:
                stored = RouterSettingsRequest.model_validate_json(saved[0])
                settings.nine_router_models = ','.join(stored.models)
                settings.nine_router_image_model = stored.image_model
                settings.nine_router_image_fallback_models = ",".join(stored.image_fallback_models)
        if not getattr(app.state, "workspace_mounted", False):
            mount_workspace(app, db, manager, project, artifact_item)
            app.state.workspace_mounted = True
            app.openapi_schema = None
        manager.set_loop(asyncio.get_running_loop())
        manager.resume_pending()
        manager.notifications.start()
        try:
            yield
        finally:
            for project_id in list(manager.running):
                await manager.stop(project_id)
            await asyncio.to_thread(manager.notifications.close)

    app = FastAPI(title="TravelBook Studio API", version="0.1.0", lifespan=lifespan,
        description="Quản lý dự án, sự kiện và file đầu ra của TravelBook Studio.")
    app.state.db, app.state.settings, app.state.manager = db, settings, manager
    app.add_middleware(CORSMiddleware, allow_origins=[settings.frontend_origin, "http://127.0.0.1:5173", "http://localhost:5173"], allow_credentials=False,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"], allow_headers=["Content-Type"])

    def project(project_id):
        row = db.get_project(project_id)
        if row is None:
            raise HTTPException(404, "Project not found")
        return row

    def select(model, row):
        return {key: value for key, value in row.items() if key in model.model_fields}

    def detail(row):
        data = WorkspaceStore(db).read(row["id"])
        return {**select(ProjectDetail, row), "tasks": db.get_tasks(row["id"]), "agents": data["agents"],
                "approvals": data["approvals"], **data["settings"]}

    def artifact(project_id, artifact_id):
        project(project_id)
        row = db.get_artifact(project_id, artifact_id)
        if row is None:
            raise HTTPException(404, "Artifact not found in this project")
        return row

    def artifact_item(row, request):
        return {**row, "download_url": str(request.url_for("download_artifact", project_id=row["project_id"], artifact_id=row["id"]))}

    @app.get("/", include_in_schema=False)
    def index():
        return {"app": "TravelBook Studio", "docs": "/docs", "openapi": "/openapi.json"}

    @app.get("/health", tags=["Health"])
    def health():
        with db.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return {"status": "ok"}

    from .model_management import mount_models
    mount_models(app, db, settings)

    @app.post("/api/projects", response_model=ProjectDetail, status_code=201, tags=["Projects"], summary="Tạo dự án")
    async def create_project(body: ProjectCreate, response: Response):
        project_id = "project-"+uuid4().hex
        state = {"project_id": project_id, "user_prompt": body.prompt, "stage": "pm", "revision_count": 0,
            "revision_pending": [], "revision_exhausted": False, "revision_warnings": [], "artifacts": [],
              "production_approved": False, "fixture_mode": wf.fixture_mode, "completed": False, "error": None}
        if body.trip_request:
            state["trip_request"] = body.trip_request
        row = db.create_project(project_id, body.title, body.prompt, trip_request=body.trip_request, state=state)
        if body.approval_enabled:
            WorkspaceStore(db).settings(project_id, {"approval_enabled": True, "approval_stages": ["plan", "itinerary", "visuals", "final"]})
        response.headers["Location"] = "/api/projects/"+project_id
        if body.start_workflow:
            manager.start(project_id, state)
        return detail(row)

    @app.get("/api/projects", response_model=ProjectPage, tags=["Projects"], summary="Danh sách dự án")
    def list_projects(limit: int = Query(20, ge=1, le=1000), offset: int = Query(0, ge=0)):
        with db.connect() as connection:
            total = connection.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        return {"items": [select(ProjectSummary, row) for row in db.list_projects(limit=limit, offset=offset)], "limit": limit, "offset": offset, "total": total}

    @app.get("/api/projects/{project_id}", response_model=ProjectDetail, tags=["Projects"], summary="Chi tiết dự án")
    def get_project(project_id: SafeId):
        return detail(project(project_id))

    @app.get("/api/projects/{project_id}/events", response_model=EventPage, tags=["Events"], summary="Đọc sự kiện theo cursor")
    def events(project_id: SafeId, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=1000)):
        project(project_id)
        rows = db.get_events(project_id, after_id=after_id, limit=limit)
        return {"items": rows, "next_after_id": rows[-1]["id"] if rows else after_id}

    @app.get("/api/projects/{project_id}/artifacts", response_model=ArtifactPage, tags=["Artifacts"], summary="Danh sách artifact")
    def artifacts(project_id: SafeId, request: Request, limit: int = Query(20, ge=1, le=1000), offset: int = Query(0, ge=0)):
        project(project_id)
        rows = db.get_artifacts(project_id)
        return {"items": [artifact_item(row, request) for row in rows[offset:offset+limit]], "total": len(rows), "limit": limit, "offset": offset}

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}", response_model=ArtifactItem, tags=["Artifacts"], summary="Chi tiết artifact")
    def get_artifact(project_id: SafeId, artifact_id: SafeId, request: Request):
        return artifact_item(artifact(project_id, artifact_id), request)

    @app.get("/api/projects/{project_id}/artifacts/{artifact_id}/download", name="download_artifact", tags=["Artifacts"],
        summary="Tải file artifact", response_class=FileResponse,
        responses={200: {"content": {"application/octet-stream": {"schema": {"type": "string", "format": "binary"}}}},
                   403: {"description": "File ngoài thư mục dự án"}, 404: {"description": "Không có artifact hoặc file"}})
    def download_artifact(project_id: SafeId, artifact_id: SafeId, inline: bool = False):
        row = artifact(project_id, artifact_id)
        folder = root / project_id
        target = Path(row["path"]).resolve()
        if folder.resolve() != folder or not target.is_relative_to(folder):
            raise HTTPException(403, "Artifact is outside this project's directory")
        if not target.is_file():
            raise HTTPException(404, "Artifact file no longer exists")
        if inline and row["mime_type"] not in {"application/pdf", "image/jpeg", "image/png", "image/webp", "image/gif"}:
            raise HTTPException(415, "Preview is available for PDF and images")
        return FileResponse(target, filename=target.name, media_type=row["mime_type"],
                            content_disposition_type="inline" if inline else "attachment",
                            headers={"X-Content-Type-Options": "nosniff"})

    @app.post("/api/projects/{project_id}/retry", tags=["Projects"], summary="Khởi động lại/tiếp tục dự án")
    async def retry_project(project_id: SafeId):
        row = project(project_id)
        if not wf.fixture_mode and row['state'].get('fixture_mode'):
            raise HTTPException(409,'Legacy demo project: create a new project to use live agents')
        if row['status']=='awaiting_input': raise HTTPException(409,'Answer the PM clarification first')
        if row["status"] == "awaiting_approval": raise HTTPException(409, "Resolve pending approval first")
        if row["status"] == "running":
            return {"status": "already_running"}
        state = row.get("state") or {}
        state["completed"] = False
        db.add_event(project_id, "system", "workflow_retried", "Khởi động lại dự án")
        manager.start(project_id, state)
        return {"status": "running"}

    @app.post("/api/projects/{project_id}/pause", tags=["Projects"], summary="Tạm dừng dự án")
    def pause_project(project_id: SafeId):
        row = project(project_id)
        if row["status"] in {"paused", "pause_requested"}:
            return {"status": row["status"]}
        if row["status"] not in {"running", "queued"}:
            raise HTTPException(409, "Chỉ có thể tạm dừng dự án đang chạy hoặc queued")
        manager.pause(project_id)
        return {"status": db.get_project(project_id)["status"]}

    @app.get("/api/projects/{project_id}/checkpoint", tags=["Projects"], summary="Thông tin checkpoint SQLite")
    def checkpoint(project_id: SafeId):
        project(project_id)
        return manager.checkpoint_info(project_id)

    @app.post("/api/projects/{project_id}/resume", tags=["Projects"], summary="Tiếp tục dự án")
    async def resume_project(project_id: SafeId):
        row = project(project_id)
        if row["status"] != "paused":
            raise HTTPException(409, "Dự án không ở trạng thái tạm dừng")
        try:
            checkpoint = await manager.resume(project_id)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        db.add_event(project_id, "system", "workflow_resumed", "Tiếp tục từ checkpoint SQLite", checkpoint)
        return {"status": "running", "checkpoint_id": checkpoint["checkpoint_id"]}

    @app.post("/api/projects/{project_id}/stop", tags=["Projects"], summary="Dừng hẳn dự án")
    async def stop_project(project_id: SafeId):
        row = project(project_id)
        if row["status"] not in {"running", "queued", "pause_requested"}:
            return {"status": row["status"]}
        stopped = await manager.stop(project_id)
        if not stopped:
            db.update_project(project_id, status="stopped", pause_requested=False, error=None)
            db.add_event(project_id, "system", "workflow_stopped", "Đã dừng dự án")
        return {"status": "stopped"}

    @app.delete("/api/projects/{project_id}", status_code=204, tags=["Projects"], summary="Xóa dự án và dữ liệu liên quan",
        responses={409: {"description": "Dự án đang chạy hoặc không thể xóa file"}})
    def delete_project(project_id: SafeId, delete_files: bool = Query(True, description="Xóa cả thư mục artifact riêng của dự án; false chỉ xóa metadata")):
        row = project(project_id)
        # Cho phép xóa khi queued, stopped, completed, failed, paused
        if row["status"] in {"running", "pause_requested"}:
            raise HTTPException(409, "Stop the project before deleting it")
        folder = root / project_id
        if delete_files and folder.exists():
            # Verify final absolute targets before recursive cleanup on Windows.
            if folder.resolve() != folder or not folder.is_relative_to(root) or folder == root or project_id == "danang_3days":
                raise HTTPException(409, "Unsafe or reserved artifact directory; files were not deleted")
            if any(path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()) for path in folder.rglob("*")):
                raise HTTPException(409, "Linked artifact directories require manual cleanup")
        try:
            with db.connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = connection.execute("SELECT status FROM projects WHERE id=?", (project_id,)).fetchone()
                if current is None:
                    raise HTTPException(404, "Project not found")
                if current["status"] in {"running", "pause_requested"}:
                    raise HTTPException(409, "Stop the project before deleting it")
                if delete_files and folder.exists():
                    shutil.rmtree(folder)
                connection.execute("DELETE FROM projects WHERE id=?", (project_id,))
        except OSError:
            raise HTTPException(409, "Unable to remove project files; metadata retained") from None
        except sqlite3.OperationalError:
            raise HTTPException(409, "Database busy; retry later") from None
        return Response(status_code=204)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    settings = get_settings()
    uvicorn.run(app, host=settings.app_host, port=settings.app_port)
