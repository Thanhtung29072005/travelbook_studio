# Frontend workspace extension

The updated project workspace has six URL-addressable tabs: Overview, List, Schedule, Messages, Agents and Artifacts. The existing global 9Router settings page and project run/pause/resume/stop controls are retained.

## Recheck of the five fixes

The user's absolute download URL handling, queued-project delete button and explicit blocked-state handling were retained. Project creation can start the workflow. That workflow is still the fixture-backed graph, which is now explicitly identified in the UI.

The previous increased limits (500 projects / 1000 artifacts) were replaced with actual pagination. Events are accumulated using their cursor, deduplicated and sorted; selecting another project resets the cursor, and stale responses cannot overwrite the selected project's state. Helpers cover route restoration, task status and pagination in frontend tests.

## New features

| Area | Behavior |
| --- | --- |
| Schedule | Dates supplied by the backend render as a timeline; undated tasks remain explicit, without invented dates. Fixture nodes record their execution dates when no dates exist. |
| List | Subtasks are indented by parent_id; blocked, testing and awaiting_approval are retained as distinct states. |
| Agents | Add profiles for any of the ten TravelBook roles, edit names/colors/instructions/models/fallbacks/temperature, activate a replacement profile and delete non-core profiles. |
| Messages | Read human-readable content/evidence, inspect event artifacts, edit permitted JSON with schema validation, or send a task back to a chosen role with feedback. |
| Approvals | Select plan/itinerary/visuals/final gates. Approve to continue or request a correction with feedback and target role. |
| Overview | Review findings, responsible agents and revision exhaustion warnings are visible. |
| Navigation | Hash routes restore project/tab on reload and support browser Back/Forward. |

The create dialog can enable approval gates before the workflow starts, or disable auto-start so the user can configure the crew first. Editing role settings/content requires the project to be idle or paused.

## Agent profiles and effective configuration

The graph retains exactly ten domain roles. Added agents are replacement profiles for a role, not arbitrary new graph nodes. Exactly one profile is active per role; activating a replacement disables the previous profile. Removing an active custom profile restores its core profile. Required core roles cannot be removed or switched off without a replacement.

Profile model/instructions are applied by the actual structured LLM router using the project/role context of the model-attempt callback. This includes PM, Content Editor and the DecisionEngine-based agents. Vision requests can use the profile image_model; Illustrator image generation also supports its image model/fallbacks and added design instructions. System schema/evidence constraints and style-lock checks remain in place.

Budget, Map and Layout use code, so irrelevant model/instruction fields are disabled in the editor. The fixture graph does not call LLMs regardless of those settings; it remains visibly labeled as a demo.

## Durable workspace API

Project crew, settings, approvals and content overrides persist in a project_workspace SQLite table with a cascading project foreign key. Existing core tables and user data are retained.

New routes:

- GET/POST `/api/projects/{id}/agents`
- PATCH/DELETE `/api/projects/{id}/agents/{agent_id}`
- GET/PATCH `/api/projects/{id}/settings`
- GET `/api/projects/{id}/approvals`
- POST `/api/projects/{id}/approvals/{approval_id}`
- GET/PUT `/api/projects/{id}/events/{event_id}/content`
- POST `/api/projects/{id}/interventions`

Project detail includes crew/approval settings and history. Event content is explicitly the agent's **current** content; attached artifact files are the historical event outputs. Editable fields are schema restricted, and Reviewer code findings are read-only. Human edits persist as overrides and are applied when that role runs again. Editing does not silently resume a workflow.

## Pause, approval and resume

The manager now uses a durable SQLite LangGraph checkpointer; normal resume uses the graph's pending tasks rather than a manual routing hint. A single visual-role intervention returns through its dedicated repair path rather than waiting for the original three-way fan-in. Upstream interventions regenerate downstream steps. See PAUSE_RESUME.md.

Normal checkpoint resume keeps the same files and artifact identities. Deliberate content rewinds fork a new checkpoint thread and execution directory. Stop waits cooperatively for the running graph step instead of cancelling an asyncio wrapper while its thread keeps writing. An interrupted server's running projects become paused on restart. Run one API worker for the in-process workflow manager.

Approval gates now call LangGraph interrupt and persist with the SQLite checkpoint. Both decisions resume the same thread using Command(resume=...). Revise requires feedback, routes to the responsible role, writes a new output generation, and asks for approval again. See MANUAL_APPROVALS.md. A resolved approval cannot be submitted twice. These human gates are separate from the existing automatic Reviewer revision limit.

## Run and test

```powershell
python -m pip install -e .
python -m backend.app.main
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Frontend helper tests require Node 22.18 or newer (validated with Node 24):

```powershell
npm test
npm run build
```

Backend tests:

```powershell
python -m unittest discover -s tests -p test_workspace.py -v
```

Validation includes a real fixture graph pausing for human approval, continuing without rerunning PM, safe cooperative stop/retry, targeted visual-role resume, crew persistence, schema edits, immutable Reviewer findings and mocked HTTP verification that configured model/instructions reach the router. API, router, workflow, revision, Illustrator, PM and Content Editor regression suites are checked separately.

No live Uvicorn/browser click test was performed here: the earlier direct server attempt was blocked by automatic review for unexpected telemetry. Verification uses isolated ASGI, graph integration tests, frontend helper tests and a production build. Fully replacing fixture workflow nodes with live agents remains a separate integration task.
