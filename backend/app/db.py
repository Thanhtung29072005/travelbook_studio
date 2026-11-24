"""SQLite persistence for TravelBook projects, work items, events and artifacts.

Connections are short-lived, transactions roll back on error, and foreign keys
are enabled on every connection. JSON retains nested source/confidence fields.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .schemas import AgentId, Artifact, Schema, Text, TripRequest

ProjectStatus = Literal[
    "queued", "running", "pause_requested", "paused", "awaiting_input",
    "awaiting_approval", "stopped", "completed", "needs_attention", "failed",
]
TaskStatus = Literal["todo", "ready", "in_progress", "testing", "awaiting_approval", "blocked", "done"]
PROJECT_STATUSES = (
    "queued", "running", "pause_requested", "paused", "awaiting_input",
    "awaiting_approval", "stopped", "completed", "needs_attention", "failed",
)
TASK_STATUSES = ("todo", "ready", "in_progress", "testing", "awaiting_approval", "blocked", "done")


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must be a non-empty string")
    return value.strip()


def _json_default(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_computed_fields=True)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Decimal, Path)):
        return str(value)
    raise TypeError(f"Not JSON serializable: {type(value).__name__}")


def _json_object(value: Any) -> str:
    if not isinstance(value, (dict, BaseModel)):
        raise ValueError("Expected a JSON object or Pydantic model")
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=_json_default)


class _TaskInput(Schema):
    id: Text
    title: Text
    agent: AgentId
    status: TaskStatus = "todo"
    start_at: datetime | None = None
    due_at: datetime | None = None
    depends_on: list[Text] = Field(default_factory=list)
    detail: str = ""
    order_index: int = Field(default=0, ge=0, strict=True)
    parent_id: Text | None = None
    item_type: Literal["task", "subtask"] = "task"
    step_key: str = ""

    @model_validator(mode="after")
    def check_structure(self) -> _TaskInput:
        for value in (self.start_at, self.due_at):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError("task timestamps must be timezone-aware")
        if self.start_at and self.due_at and self.due_at < self.start_at:
            raise ValueError("due_at must not precede start_at")
        if (self.item_type == "subtask") != (self.parent_id is not None):
            raise ValueError("subtask requires parent_id; a task must not have parent_id")
        if len(set(self.depends_on)) != len(self.depends_on):
            raise ValueError("duplicate task dependencies")
        return self


class Database:
    def __init__(self, path: str | Path):
        if str(path) == ":memory:":
            raise ValueError("Use a file path: this database opens a connection per transaction")
        self.path = Path(path).resolve()

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=10000")
        try:
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def init(self) -> None:
        """Initialize once on startup; safe to repeat without clearing data."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise RuntimeError(f"Unsupported database version: {version}")
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                BEGIN;
                CREATE TABLE IF NOT EXISTS projects (
                    id TEXT PRIMARY KEY NOT NULL CHECK(length(trim(id)) > 0),
                    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
                    prompt TEXT NOT NULL CHECK(length(trim(prompt)) > 0),
                    status TEXT NOT NULL DEFAULT 'queued' CHECK(status IN (
                        'queued','running','pause_requested','paused','awaiting_input',
                        'awaiting_approval','stopped','completed','needs_attention','failed')),
                    stage TEXT NOT NULL DEFAULT 'pm' CHECK(length(trim(stage)) > 0),
                    progress INTEGER NOT NULL DEFAULT 0 CHECK(progress BETWEEN 0 AND 100),
                    trip_request_json TEXT CHECK(trip_request_json IS NULL OR
                        (json_valid(trip_request_json) AND json_type(trip_request_json)='object')),
                    state_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(state_json) AND json_type(state_json)='object'),
                    pause_requested INTEGER NOT NULL DEFAULT 0 CHECK(pause_requested IN (0,1)),
                    final_pdf TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    title TEXT NOT NULL CHECK(length(trim(title)) > 0),
                    agent TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'todo' CHECK(status IN (
                        'todo','ready','in_progress','testing','awaiting_approval','blocked','done')),
                    start_at TEXT,
                    due_at TEXT,
                    depends_on TEXT NOT NULL DEFAULT '[]' CHECK(json_valid(depends_on) AND json_type(depends_on)='array'),
                    detail TEXT NOT NULL DEFAULT '',
                    order_index INTEGER NOT NULL DEFAULT 0 CHECK(order_index >= 0),
                    parent_id TEXT,
                    item_type TEXT NOT NULL DEFAULT 'task' CHECK(item_type IN ('task','subtask')),
                    step_key TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(project_id,id),
                    FOREIGN KEY(project_id,parent_id) REFERENCES tasks(project_id,id)
                        ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED,
                    CHECK((item_type='task' AND parent_id IS NULL) OR
                          (item_type='subtask' AND parent_id IS NOT NULL))
                );
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    agent TEXT NOT NULL CHECK(length(trim(agent)) > 0),
                    event_type TEXT NOT NULL CHECK(length(trim(event_type)) > 0),
                    message TEXT NOT NULL CHECK(length(trim(message)) > 0),
                    payload TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(payload) AND json_type(payload)='object'),
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS artifacts (
                    id TEXT PRIMARY KEY NOT NULL,
                    project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    path TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    source TEXT NOT NULL CHECK(length(trim(source)) > 0),
                    confidence REAL NOT NULL CHECK(confidence BETWEEN 0 AND 1),
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_projects_created ON projects(created_at,id);
                CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id,order_index,id);
                CREATE INDEX IF NOT EXISTS idx_events_project ON events(project_id,id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_project ON artifacts(project_id,created_at,id);
                PRAGMA user_version=1;
                COMMIT;
            """)

    @staticmethod
    def _project(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["state"] = json.loads(item["state_json"])
        trip_json = item.pop("trip_request_json")
        item["trip_request"] = json.loads(trip_json) if trip_json is not None else None
        item["pause_requested"] = bool(item["pause_requested"])
        return item

    def create_project(
        self, project_id: str, title: str, prompt: str, *,
        trip_request: TripRequest | dict | None = None, state: dict | None = None,
    ) -> dict[str, Any]:
        project_id = _text(project_id, "project_id")
        title, prompt = _text(title, "title"), _text(prompt, "prompt")
        request = TripRequest.model_validate(trip_request) if trip_request is not None else None
        state_json = _json_object(state if state is not None else {})
        now = utcnow()
        with self.connect() as db:
            db.execute(
                "INSERT INTO projects(id,title,prompt,trip_request_json,state_json,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (project_id, title, prompt, _json_object(request) if request is not None else None,
                 state_json, now, now),
            )
            db.execute(
                "INSERT INTO events(project_id,agent,event_type,message,created_at) VALUES(?,?,?,?,?)",
                (project_id, "system", "project_created", "Đã tạo dự án", now),
            )
            return self._project(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def get_project(self, project_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            return self._project(db.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone())

    def list_projects(self, *, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        self._page(limit, offset)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM projects ORDER BY created_at DESC,id LIMIT ? OFFSET ?", (limit, offset),
            ).fetchall()
        return [self._project(row) for row in rows]

    @staticmethod
    def _page(limit: int, offset: int) -> None:
        if type(limit) is not int or not 1 <= limit <= 10000 or type(offset) is not int or offset < 0:
            raise ValueError("limit must be 1..10000; offset must be a non-negative integer")

    def update_project(self, project_id: str, **fields: Any) -> bool:
        """Update whitelisted fields. Returns False when the project does not exist."""
        allowed = {"title", "status", "stage", "progress", "state", "state_json",
                   "trip_request", "pause_requested", "final_pdf", "error"}
        if not fields or fields.keys() - allowed:
            raise ValueError("Supply only supported project update fields")
        if "state" in fields and "state_json" in fields:
            raise ValueError("Supply state or state_json, not both")
        values = dict(fields)
        for name in ("title", "stage"):
            if name in values:
                values[name] = _text(values[name], name)
        if "status" in values and values["status"] not in PROJECT_STATUSES:
            raise ValueError("Invalid project status")
        if "progress" in values and (type(values["progress"]) is not int or not 0 <= values["progress"] <= 100):
            raise ValueError("progress must be an integer between 0 and 100")
        if "state" in values:
            values["state_json"] = _json_object(values.pop("state"))
        elif "state_json" in values:
            values["state_json"] = _json_object(json.loads(values["state_json"]))
        if "trip_request" in values:
            request = values.pop("trip_request")
            values["trip_request_json"] = (
                _json_object(TripRequest.model_validate(request)) if request is not None else None
            )
        if "pause_requested" in values:
            if type(values["pause_requested"]) is not bool:
                raise ValueError("pause_requested must be a boolean")
            values["pause_requested"] = int(values["pause_requested"])
        for name in ("final_pdf", "error"):
            if name in values and values[name] is not None and not isinstance(values[name], str):
                raise ValueError(f"{name} must be a string or None")
        values["updated_at"] = utcnow()
        # Column names are exclusively taken from the whitelist/explicit mappings above.
        assignments = ",".join(f"{name}=?" for name in values)
        with self.connect() as db:
            return db.execute(
                f"UPDATE projects SET {assignments} WHERE id=?", (*values.values(), project_id),
            ).rowcount > 0

    def add_event(
        self, project_id: str, agent: str, event_type: str, message: str,
        payload: dict | None = None,
    ) -> int:
        with self.connect() as db:
            cursor = db.execute(
                "INSERT INTO events(project_id,agent,event_type,message,payload,created_at) VALUES(?,?,?,?,?,?)",
                (project_id, _text(agent, "agent"), _text(event_type, "event_type"),
                 _text(message, "message"), _json_object(payload if payload is not None else {}), utcnow()),
            )
            return cursor.lastrowid

    def get_events(self, project_id: str, *, after_id: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        self._page(limit, after_id)
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM events WHERE project_id=? AND id>? ORDER BY id LIMIT ?",
                (project_id, after_id, limit),
            ).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]

    def replace_tasks(self, project_id: str, tasks: list[dict]) -> None:
        """Atomically replace a project's task plan, validating references and cycles."""
        parsed = [_TaskInput.model_validate(task) for task in tasks]
        by_id = {task.id: task for task in parsed}
        if len(by_id) != len(parsed):
            raise ValueError("Task IDs must be unique")
        for task in parsed:
            references = [*task.depends_on, *([task.parent_id] if task.parent_id else [])]
            if any(reference not in by_id or reference == task.id for reference in references):
                raise ValueError("Tasks must reference other tasks in the same project plan")
            if task.parent_id and by_id[task.parent_id].item_type != "task":
                raise ValueError("A subtask's parent must be a top-level task")
        visited, active = set(), set()

        def visit(task_id: str) -> None:
            if task_id in active:
                raise ValueError("Task dependency cycle")
            if task_id in visited:
                return
            active.add(task_id)
            for dependency in by_id[task_id].depends_on:
                visit(dependency)
            active.remove(task_id)
            visited.add(task_id)

        for task_id in by_id:
            visit(task_id)
        now = utcnow()
        with self.connect() as db:
            if not db.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError(project_id)
            db.execute("DELETE FROM tasks WHERE project_id=?", (project_id,))
            for task in parsed:
                db.execute(
                    "INSERT INTO tasks(id,project_id,title,agent,status,start_at,due_at,depends_on,"
                    "detail,order_index,parent_id,item_type,step_key,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (task.id, project_id, task.title, task.agent, task.status,
                     task.start_at.isoformat() if task.start_at else None,
                     task.due_at.isoformat() if task.due_at else None,
                     json.dumps(task.depends_on), task.detail, task.order_index,
                     task.parent_id, task.item_type, task.step_key, now, now),
                )

    def get_tasks(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM tasks WHERE project_id=? ORDER BY order_index,id", (project_id,),
            ).fetchall()
        return [{**dict(row), "depends_on": json.loads(row["depends_on"])} for row in rows]

    def set_task_status(self, project_id: str, task_id: str, status: TaskStatus) -> bool:
        """Updates one task by ID; scheduling/transition policy belongs to workflow."""
        if status not in TASK_STATUSES:
            raise ValueError("Invalid task status")
        with self.connect() as db:
            return db.execute(
                "UPDATE tasks SET status=?,updated_at=? WHERE project_id=? AND id=?",
                (status, utcnow(), project_id, task_id),
            ).rowcount > 0

    def add_artifact(self, artifact: Artifact | dict) -> str:
        """Register once; identical retry/agent-wrapper registration is idempotent.

        An ID must never be reassigned to another artifact. Creation time is
        retained from the first registration, including after a node retry.
        """
        model = Artifact.model_validate(artifact)
        data = model.model_dump(mode="json")
        columns = tuple(data)
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT * FROM artifacts WHERE id=?',(model.id,)).fetchone()
            if existing is not None:
                previous=Artifact.model_validate(dict(existing)).model_dump(mode='json')
                if any(previous[key]!=data[key] for key in data if key!='created_at'):
                    raise ValueError('Artifact ID already registered with different metadata: '+model.id)
                return model.id
            db.execute(
                f"INSERT INTO artifacts({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                tuple(data.values()),
            )
        return model.id

    def get_artifact(self, project_id: str, artifact_id: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute(
                "SELECT * FROM artifacts WHERE project_id=? AND id=?", (project_id, artifact_id),
            ).fetchone()
        return dict(row) if row is not None else None

    def get_artifacts(self, project_id: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM artifacts WHERE project_id=? ORDER BY created_at,id", (project_id,),
            ).fetchall()
        return [dict(row) for row in rows]
