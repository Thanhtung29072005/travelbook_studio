"""Persistent project crew, approval gates and schema-validated content edits."""
import json
from uuid import uuid4

from pydantic import Field, TypeAdapter, field_validator

from .db import utcnow
from .schemas import AgentId, Budget, DayPlan, Fact, Photo, Place, Review, Schema, TripRequest, ContentDocument

ROLES = tuple(AgentId.__args__)
STAGES = {"pm": "plan", "planner": "itinerary", "reviewer": "visuals", "layout_editor": "final"}
APPROVAL_TARGETS = {"plan": ("pm",), "itinerary": ("planner",),
    "visuals": ("image_curator", "illustrator", "map_agent"), "final": ("layout_editor",)}
EDITABLE = {"pm": {"trip_request": TripRequest}, "researcher": {"places": list[Place]}, "planner": {"itinerary": list[DayPlan]},
    "budget": {"budget": Budget}, "content_editor": {"content": list[Fact[str]], "content_document": ContentDocument},
    "image_curator": {"photos": list[Photo]}}


class AgentInput(Schema):
    name: str = Field(min_length=1, max_length=100)
    role_key: AgentId
    instructions: str = Field(default="", max_length=12000)
    color: str = Field(default="#426b91", pattern=r"^#[0-9A-Fa-f]{6}$")
    model: str | None = Field(default=None, max_length=160)
    fallback_models: list[str] = Field(default_factory=list, max_length=10)
    image_model: str | None = Field(default=None, max_length=160)
    temperature: float | None = Field(default=None, ge=0, le=2, allow_inf_nan=False)

    @field_validator("model", "image_model")
    @classmethod
    def model_id(cls, value):
        return value.strip() or None if value is not None else None

    @field_validator("fallback_models")
    @classmethod
    def model_chain(cls, values):
        if any(len(value.strip()) > 160 for value in values): raise ValueError("Model ID too long")
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class WorkspaceStore:
    def __init__(self, db):
        self.db = db
        with db.connect() as c:
            c.execute("CREATE TABLE IF NOT EXISTS project_workspace(project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE, document TEXT NOT NULL CHECK(json_valid(document)))")

    def default(self):
        return {"agents": [{**AgentInput(name=role.replace('_', ' ').title(), role_key=role).model_dump(),
            "id": role, "is_core": True, "enabled": True} for role in ROLES],
            "settings": {"approval_enabled": False, "approval_stages": ["plan", "itinerary", "visuals", "final"]},
            "approvals": [], "overrides": {}, "interventions": []}

    def read(self, project_id):
        with self.db.connect() as c:
            row = c.execute("SELECT document FROM project_workspace WHERE project_id=?", (project_id,)).fetchone()
        return json.loads(row[0]) if row else self.default()

    def change(self, project_id, action):
        with self.db.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            if not c.execute("SELECT 1 FROM projects WHERE id=?", (project_id,)).fetchone():
                raise KeyError("Project not found")
            row = c.execute("SELECT document FROM project_workspace WHERE project_id=?", (project_id,)).fetchone()
            data = json.loads(row[0]) if row else self.default()
            result = action(data)
            c.execute("INSERT OR REPLACE INTO project_workspace VALUES(?,?)", (project_id, json.dumps(data, ensure_ascii=False)))
        return result

    def crew(self, project_id):
        return self.read(project_id)["agents"]

    def agent(self, project_id, agent_id):
        return next((agent for agent in self.crew(project_id) if agent["role_key"] == agent_id and agent["enabled"]), None)

    def add_agent(self, project_id, body):
        if body.role_key == "pm": raise ValueError("PM is fixed; edit its existing profile")
        value = {**body.model_dump(), "id": "agent-"+uuid4().hex[:12], "is_core": False, "enabled": False}
        def action(data):
            if len(data["agents"]) >= 30: raise ValueError("At most 30 agent profiles")
            data["agents"].append(value)
            return value
        return self.change(project_id, action)

    def update_agent(self, project_id, agent_id, changes):
        def action(data):
            agent = next((item for item in data["agents"] if item["id"] == agent_id), None)
            if not agent: raise KeyError("Agent not found")
            if changes.get("remove"):
                if agent["role_key"] == "pm": raise ValueError("PM cannot be removed")
                data["agents"].remove(agent)
                if agent["enabled"]:
                    replacement = next((item for item in data["agents"] if item["is_core"] and item["role_key"] == agent["role_key"]), None)
                    if replacement: replacement["enabled"] = True
                return {"status": "removed"}
            enabled = changes.pop("enabled", agent["enabled"])
            if agent["role_key"] == "pm" and not enabled: raise ValueError("PM cannot be disabled")
            parsed = AgentInput.model_validate({**{key: agent[key] for key in AgentInput.model_fields}, **changes})
            agent.update(parsed.model_dump())
            agent["enabled"] = enabled
            if enabled:
                for item in data["agents"]:
                    if item["role_key"] == agent["role_key"]: item["enabled"] = item["id"] == agent_id
            return agent
        return self.change(project_id, action)

    def settings(self, project_id, value):
        return self.change(project_id, lambda data: data.update(settings=value) or value)

    def review_approved(self, project_id, state):
        resolution = state.get("approval_resolution") or {}
        if resolution.get("node") != "reviewer" or resolution.get("decision") != "approve":
            return False
        gate = next((item for item in self.read(project_id)["approvals"]
                     if item["id"] == resolution.get("approval_id")), None)
        suffix = f":reviewer:e{state.get('execution_count', 0)}:r{state.get('revision_count', 0)}"
        if not gate or gate["status"] != "approved" or gate["stage"] != "visuals" or not (gate.get("gate_key") or "").endswith(suffix):
            return False
        review = Review.model_validate(state["review"]).model_dump(mode="json")
        return gate["content"].get("review") == review

    def gate(self, project_id, node, state, *, key=None):
        stage = STAGES.get(node)
        if not stage: return None
        def action(data):
            existing = next((item for item in data["approvals"] if key and item.get("gate_key") == key), None)
            if existing: return existing
            if not data["settings"]["approval_enabled"] or stage not in data["settings"]["approval_stages"]: return None
            # A repair/re-execution gets a new gate; resuming past a gate does not rerun the node.
            value = {"id": uuid4().hex, "stage": stage, "node": node, "status": "pending", "created_at": utcnow(),
                "content": {key: value for key, value in state.items() if key in {
                    "plan": {"trip_request", "plan"}, "itinerary": {"trip_request", "itinerary"},
                    "visuals": {"photos", "illustrations", "maps", "charts", "review"},
                    "final": {"final_pdf", "review", "revision_warnings"}}[stage]}, "feedback": "",
                "gate_key": key, "allowed_targets": list(APPROVAL_TARGETS[stage])}
            def plain(value):
                return value.model_dump(mode="json", exclude_computed_fields=True) if hasattr(value, "model_dump") else value
            value = json.loads(json.dumps(value, default=plain))
            data["approvals"].append(value)
            return value
        return self.change(project_id, action)

    def decide(self, project_id, approval_id, decision, feedback, target, *, replay=False):
        def action(data):
            item = next((item for item in data["approvals"] if item["id"] == approval_id), None)
            if not item: raise KeyError("Approval not found")
            status = "approved" if decision == "approve" else "revision_requested"
            if replay and item["status"] == status and item["feedback"] == feedback and item.get("target_agent") == target: return item
            if item["status"] != "pending": raise ValueError("Approval already resolved")
            item.update(status="approved" if decision == "approve" else "revision_requested", feedback=feedback, target_agent=target, resolved_at=utcnow())
            return item
        return self.change(project_id, action)

    def edit(self, project_id, node, content):
        schema = EDITABLE.get(node, {})
        if not isinstance(content, dict) or not content or set(content)-set(schema):
            raise ValueError("Supply only editable fields for this agent")
        clean = {key: TypeAdapter(schema[key]).validate_python(value) for key, value in content.items()}
        clean = {key: value.model_dump(mode="json", exclude_computed_fields=True) if hasattr(value, "model_dump") else TypeAdapter(schema[key]).dump_python(value, mode="json", exclude_computed_fields=True) for key, value in clean.items()}
        return self.change(project_id, lambda data: data["overrides"].update({node: clean}) or clean)
