# Pause/resume with a durable SQLite checkpointer

The manager now resumes the actual LangGraph checkpoint, including its pending tasks, channel versions, parent checkpoint and per-task writes. Normal resume no longer guesses the next node from a manually persisted State field or re-enters START.

## Safe pause sequence

1. `POST /api/projects/{id}/pause` sets `pause_requested=true` and status `pause_requested`. The UI displays **Yêu cầu tạm dừng**.
2. The current node continues. For the visual `Send` group, all tasks in that superstep finish before the pause is acknowledged.
3. LangGraph runs with `interrupt_after="*"` and `durability="sync"`. The manager fully exhausts that one-superstep stream before inspecting the pause flag.
4. After the checkpoint and pending writes are committed, State is mirrored to the project, status becomes `paused`, and a `workflow_paused` event identifies the checkpoint and next nodes.
5. `POST /api/projects/{id}/resume` continues with `graph.stream(None, config, ...)` from the same persisted thread. Completed nodes are not replayed during a normal safe resume.

A pause requested immediately after start is preserved; it can pause before PM with an initial checkpoint. A project created but never started can also be paused: no completed work is claimed, and its first start initializes the checkpoint. Resume is rejected while status is still pause_requested; the user must wait for paused.

If a pause request arrives at the final boundary, resuming may only need to mark an already completed checkpoint as completed. Pause/stop are cooperative, so a slow tool call must finish or time out before acknowledgement; they do not kill a thread halfway through file writes.

## SQLite storage

`backend/app/checkpointer.py` implements LangGraph's public synchronous `BaseCheckpointSaver` protocol using Python sqlite3. It is used by the manager's compiled graph; the original standalone fixture runner can still compile without persistence.

Tables live in the configured project database (`settings.database_path`, normally backend/data/travelbook.db):

| Table | Contents |
| --- | --- |
| graph_checkpoints | Complete typed checkpoint, metadata, parent ID, thread/namespace and owner project |
| graph_writes | Pending writes indexed by checkpoint/task/write index; ordinary duplicate writes are idempotent |
| graph_threads | Active checkpoint thread for a project when a deliberate rewind branches into another execution |

Connections are short-lived and use the existing SQLite timeout/foreign-key configuration. New manager/process instances can read the same checkpoint. Project deletion cascades to all generations of checkpoints and their writes. There is no new external database dependency.

`checkpoint_codec.py` uses a versioned JSON codec for the known domain models, Decimal, dates/times, URLs and LangGraph Send/Interrupt values. It does not deserialize pickle or import class names supplied by checkpoint data. Full URLs inside Photo models are supported. Future custom State model classes must be explicitly added to the codec's approved model registry.

The checkpoint database includes project inputs and generated State, so keep it local with the rest of application data. It is not included in the downloadable code package.

## Resume, approvals and deliberate rewinds

- Normal pause/resume and cooperative stop/retry preserve the checkpoint thread, output directory and artifact identities.
- Human approval gates remain independent: a pending approval blocks further execution after resume as well.
- An explicit content edit/intervention/revision sets restart_requested and forks a new checkpoint thread plus execution directory. Prior checkpoint history and files remain available. A rewind is deliberately different from continuing a paused step.
- The old resume_node field remains a display/legacy migration hint; a normal resume uses the checkpoint's pending tasks even if that hint is stale.
- On server startup, orphaned running/pause_requested projects become paused and restore the last persisted checkpoint. They do not automatically start writing files before the user resumes.

Use one API worker for the in-process manager. This implementation verifies safe pauses and restarts after a confirmed checkpoint. An abrupt process kill inside a node can still require retrying that unfinished node; filesystem writes, external APIs and a SQLite checkpoint are not one atomic transaction. Exactly-once external effects during arbitrary crashes require node-level idempotency.

## API and UI

Existing pause/resume/stop endpoints remain. `GET /api/projects/{id}/checkpoint` reports existence, checkpoint ID, pending nodes, creation time and graph step. It does not return raw serialized State bytes.

The frontend displays the request state separately from the confirmed safe pause, keeps Continue disabled while the pause is pending, and explains that a confirmed checkpoint survives closing/reopening the backend.

## Verify

Single-command demonstration, with actual fixture graph, SQLite, maps/charts and PDF:

```powershell
python scripts/check_pause_resume.py
```

To prove persistence across separate Python processes:

```powershell
python scripts/check_pause_resume.py --pause-only
# Copy the project_id printed above, then run in another terminal/process:
python scripts/check_pause_resume.py --resume PROJECT_ID
```

The first process deliberately waits inside Planner, requests a pause, lets that step finish, verifies status paused and next node approve_planner (the optional itinerary approval gate), then Budget, then exits. The second constructs a fresh graph/checkpointer/manager, reads the SQLite checkpoint and continues to PDF. The saved pause_resume_validation.json verifies that every graph node completed once and earlier files retain the same SHA-256.

Tests:

```powershell
python -m unittest discover -s tests -p test_pause_resume.py -v
```

Coverage includes an in-flight node, the parallel Send group, an immediate pause request, new-manager recovery, stale routing hints, pending-write persistence, deletion cascades and codec round trips. Workspace approval/stop/rewind, API, workflow and revision tests are also run. Frontend helper tests and production build remain passing.

The demonstration uses the existing JSON fixture graph; it does not validate live LLM/provider accuracy. No live Uvicorn/browser test is required for these isolated checks.
