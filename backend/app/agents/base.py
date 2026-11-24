"""Shared lifecycle helpers inspired by the sample project's BaseAgent."""

import json
from pathlib import Path
import re
from uuid import uuid4

from pydantic import BaseModel

from ..db import Database
from ..schemas import Artifact
from ..services.router import NineRouterClient


class BaseAgent:
    agent_id = "pm"
    display_name = "Agent"

    def __init__(self, db: Database, router: NineRouterClient, artifacts_root: str | Path):
        self.db, self.router = db, router
        self.artifacts_root = Path(artifacts_root).resolve()

    @staticmethod
    def validate_project_id(project_id):
        if not isinstance(project_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}", project_id):
            raise ValueError("project_id must be a safe directory name")

    def event(self, project_id, event_type, message, payload=None):
        return self.db.add_event(project_id, self.agent_id, event_type, message, payload)

    def model_attempt(self, project_id):
        def callback(model, attempt, fallback):
            self.event(project_id, "model_fallback" if fallback else "model_call", f"{self.display_name} gọi model",
                       {"model": model, "attempt": attempt, "fallback": fallback})
        callback.project_context = (self.db, project_id, self.agent_id)
        return callback

    def save_json(self, project_id, run_id, filename, data, *, source, confidence, kind="plan"):
        self.validate_project_id(project_id)
        if not re.fullmatch(r"[a-z0-9]+", run_id) or Path(filename).name != filename:
            raise ValueError("Invalid artifact name")
        folder = self.artifacts_root / project_id / self.agent_id / run_id
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / filename
        def encode(value):
            if isinstance(value, BaseModel):
                return value.model_dump(mode="json", exclude_computed_fields=True)
            raise TypeError(f"Not serializable: {type(value).__name__}")
        path.write_text(json.dumps(data, default=encode, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        artifact = Artifact(id=str(uuid4()), project_id=project_id, agent_id=self.agent_id,
                            kind=kind, name=filename, path=str(path), mime_type="application/json",
                            source=source, confidence=confidence)
        self.db.add_artifact(artifact)
        return artifact
