from fastapi import HTTPException, Request
from pydantic import Field
from typing import Literal
from .schemas import AgentId, Schema
from .workspace import AgentInput, WorkspaceStore, EDITABLE

class Edit(Schema):
    content: dict

class ClarificationAnswer(Schema):
    answer: str = Field(min_length=1,max_length=4000)

class Decision(Schema):
    decision: Literal["approve", "revise"]
    feedback: str = Field(default="", max_length=12000)
    target_agent: AgentId | None = None

class Intervention(Schema):
    event_id: int = Field(ge=1)
    target_agent: AgentId
    feedback: str = Field(min_length=1, max_length=12000)

class ApprovalSettings(Schema):
    approval_enabled: bool
    approval_stages: list[Literal["plan", "itinerary", "visuals", "final"]] = Field(max_length=4)

def mount_workspace(app, db, manager, project, artifact_item):
    store = WorkspaceStore(db)
    def idle(pid):
        row = project(pid)
        task = manager.running.get(pid)
        if row["status"] in {"running", "pause_requested"} or (task and not task.done()):
            raise HTTPException(409, "Pause the project before changing it")
        return row
    def call(action):
        try: return action()
        except KeyError as error: raise HTTPException(404, str(error)) from None
        except ValueError as error: raise HTTPException(422, str(error)) from None
    def event(pid, eid):
        project(pid)
        if eid < 1: raise HTTPException(422, "Invalid event ID")
        rows = db.get_events(pid, after_id=eid-1, limit=1)
        if not rows or rows[0]["id"] != eid: raise HTTPException(404, "Event not found")
        return rows[0]
    @app.post('/api/projects/{project_id}/answer',tags=['Projects'])
    async def answer(project_id: str,body: ClarificationAnswer):
        row=idle(project_id)
        if row['status']!='awaiting_input': raise HTTPException(409,'Project is not awaiting user input')
        try:
            await manager.answer(project_id,body.answer)
        except ValueError as error:
            raise HTTPException(409,str(error)) from None
        return {'status':'running'}

    @app.get("/api/projects/{project_id}/agents", tags=["Agents"])
    def agents(project_id: str):
        project(project_id)
        return store.crew(project_id)
    @app.post("/api/projects/{project_id}/agents", status_code=201, tags=["Agents"])
    def add_agent(project_id: str, body: AgentInput):
        idle(project_id)
        value = call(lambda: store.add_agent(project_id, body))
        db.add_event(project_id,"system","agent_added","Added support agent",{"agent_id":value["id"],"role":value["role_key"]})
        return value
    @app.patch("/api/projects/{project_id}/agents/{agent_id}", tags=["Agents"])
    def update_agent(project_id: str, agent_id: str, body: dict):
        idle(project_id)
        if set(body)-set(AgentInput.model_fields)-{"enabled"} or "role_key" in body: raise HTTPException(422, "Unsupported agent fields")
        if "enabled" in body and type(body["enabled"]) is not bool: raise HTTPException(422, "enabled must be boolean")
        value = call(lambda: store.update_agent(project_id, agent_id, dict(body)))
        db.add_event(project_id, "system", "agent_updated", "Đã cập nhật agent", {"agent_id": agent_id})
        return value
    @app.delete("/api/projects/{project_id}/agents/{agent_id}", tags=["Agents"])
    def remove_agent(project_id: str, agent_id: str):
        idle(project_id)
        value = call(lambda: store.update_agent(project_id, agent_id, {"remove": True}))
        db.add_event(project_id,"system","agent_removed","Removed support agent",{"agent_id":agent_id})
        return value
    @app.get("/api/projects/{project_id}/settings", tags=["Approvals"])
    def settings(project_id: str):
        project(project_id)
        return store.read(project_id)["settings"]
    @app.patch("/api/projects/{project_id}/settings", tags=["Approvals"])
    def update_settings(project_id: str, body: ApprovalSettings):
        idle(project_id)
        return store.settings(project_id, body.model_dump())
    @app.get("/api/projects/{project_id}/approvals", tags=["Approvals"])
    def approvals(project_id: str):
        project(project_id)
        return store.read(project_id)["approvals"]
    @app.post("/api/projects/{project_id}/approvals/{approval_id}", tags=["Approvals"])
    async def decide(project_id: str, approval_id: str, body: Decision):
        row = idle(project_id)
        if row["status"] != "awaiting_approval": raise HTTPException(409, "Project is not awaiting approval")
        if body.decision == "revise" and not body.feedback.strip(): raise HTTPException(422, "Feedback required")
        try:
            return await manager.decide(project_id, approval_id, body.decision, body.feedback, body.target_agent)
        except ValueError as error:
            raise HTTPException(422, str(error)) from None
    @app.get("/api/projects/{project_id}/events/{event_id}/content", tags=["Events"])
    def content(project_id: str, event_id: int, request: Request):
        item = event(project_id, event_id)
        row = project(project_id)
        node = item["payload"].get("node", item["agent"])
        keys = {"review": None} if node == "reviewer" else {"final_pdf": None} if node == "layout_editor" else EDITABLE.get(node, {})
        selected = {key: row["state"][key] for key in keys if key in row["state"]}
        ids = item["payload"].get("artifact_ids", [])
        return {"event": item, "content": selected or item["payload"], "content_key": node,
            "artifacts": [artifact_item(raw, request) for raw in db.get_artifacts(project_id) if raw["id"] in ids],
            "editable": bool(selected) and node in EDITABLE and row["status"] not in {"running", "pause_requested"},
            "can_intervene": row["status"] not in {"running", "pause_requested"},
            "note": "Nội dung hiện tại của agent; artifact là đầu ra tại sự kiện."}
    @app.put("/api/projects/{project_id}/events/{event_id}/content", tags=["Events"])
    def edit(project_id: str, event_id: int, body: Edit):
        row = idle(project_id)
        item = event(project_id, event_id)
        node = item["payload"].get("node", item["agent"])
        value = call(lambda: store.edit(project_id, node, body.content))
        db.update_project(project_id, state={**row["state"], **value, "resume_node": node, "restart_requested": True})
        if "trip_request" in value:
            db.update_project(project_id, trip_request=value["trip_request"])
        db.add_event(project_id, "user", "content_edited", "Đã sửa nội dung hợp schema", {"node": node, "fields": list(value)})
        return {"status": "saved", "content": value}
    @app.post("/api/projects/{project_id}/interventions", tags=["Approvals"])
    async def intervene(project_id: str, body: Intervention):
        row = idle(project_id)
        event(project_id, body.event_id)
        if any(item["status"] == "pending" for item in store.read(project_id)["approvals"]): raise HTTPException(409, "Resolve pending approval first")
        state = {**row["state"], "resume_node": body.target_agent, "revision_pending": [], "restart_requested": True,
            "feedback": [{"value": body.feedback, "source": "user:intervention", "confidence": 1}]}
        db.add_event(project_id, "user", "intervention_requested", body.feedback, {"target_agent": body.target_agent, "event_id": body.event_id})
        manager.start(project_id, state)
        return {"status": "queued"}
