# Conditional Review routing and MAX_REVISIONS

`backend/app/revisions.py` contains the deterministic routing table and `RevisionController`. The existing fixture-backed `TravelBookWorkflow` now attaches real LangGraph conditional edges after Reviewer. A review decision routes to `repair_<agent>` or Layout. Repair nodes return to Reviewer after all affected steps finish. A single-branch visual repair has its own return edge; it does not wait on the initial three-branch fan-in barrier.

## Routing table

| Error category/rule | Agent |
| --- | --- |
| Research/source errors | `researcher` (or valid `pm` ownership for requirements) |
| Itinerary, opening hours, travel distance | `planner` (or valid `researcher`/`pm` ownership for missing inputs) |
| Budget | `budget` |
| Content | `content_editor` |
| Photo / `LLM_PHOTO_LOCATION` | `image_curator` |
| Decorative style / `LLM_STYLE_CONSISTENCY` | `illustrator` |
| Map/chart | `map_agent` |
| Layout | `layout_editor` |
| `SCHEMA_TRIP_REQUEST` | `pm` |

Exact rule overrides take precedence. Otherwise `target_agent` is honored only when allowed for its category; invalid cross-category ownership falls back to the table. Model output cannot name an arbitrary graph node. All issues remain in Review; routing prioritizes blockers, then majors, preserving the original order within each severity. One repair round handles one selected owner and its downstream dependencies; Reviewer then reassesses all issues. Minor warnings do not trigger repair loops.

Upstream edits refresh downstream outputs: Planner repairs also rerun Budget, Content Editor and all three visual roles; Budget/Content repairs rerun their downstream roles. Visual repairs rerun only that visual agent. Repair steps are sequential; the initial visual fan-out remains unchanged.

## Exact limit semantics

```dotenv
MAX_REVISIONS=3
```

This is a **global number of repair rounds per project**, not per agent and not a count of Reviewer calls. A round may execute several dependent agents but increments `revision_count` only once.

For a persistent error and `MAX_REVISIONS=2`:

1. Initial Reviewer finds error; repair round 1 runs.
2. Reviewer still finds error; repair round 2 runs.
3. Reviewer still finds error; record limit warning and continue to Layout/PM finalization.

There are exactly 2 repair rounds and 3 reviews. `MAX_REVISIONS=0` permits no repairs. If the last allowed repair fixes the error, the next Review passes normally without an exhaustion warning. `recursion_limit` is sized to accommodate the configured repair count; it is not used as the business limit.

## Exhaustion behavior

- Keep the unresolved `Review` and its `REVISE` verdict unchanged.
- Set `revision_exhausted=true`, retain the final count, clear pending repair steps, and keep `production_approved=false`.
- Add a source/confidence-bearing Fact to `revision_warnings`.
- Write `revision_limit_reached` to SQLite and save a JSON warning artifact with the complete remaining Review.
- Continue to Layout, then finalize the draft. The PDF overview visibly warns that unresolved issues remain; its manifest retains the warning facts.

Workflow completion therefore means the draft was delivered, not that unresolved errors were approved. Missing required files, malformed Review and execution exceptions remain failures; they are not disguised as successful revisions.

## State, events and artifacts

New State fields: `review_route`, `revision_pending`, `revision_exhausted`, `revision_warnings`. Existing `revision_count`, `revision_target` and `feedback` carry the selected repair context.

Events include `revision_requested`, `repair_completed`, `review_ready`, and `revision_limit_reached`. Repair requests record the selected rule, agent, full repair plan and repair number.

The fixture workflow copies the latest output into `revisions/rN` before repair, excluding nested history. Artifacts use revision-specific identities; each Review report has its round in the filename. This avoids artifact reducer conflicts and retains earlier files. Layout repair output is separate from the final PDF of the same round.

## Reproduce intentional errors

From the project root:

```powershell
python scripts/check_revision_routing.py --agent image_curator --max-revisions 2
python scripts/check_revision_routing.py --agent planner --max-revisions 1
python scripts/check_revision_routing.py --agent layout_editor --max-revisions 1 --clear-after 1
```

The first two deliberately preserve the injected error until the limit; the third simulates fixing it on the last allowed attempt. Each command executes the actual fixture graph, SQLite events/tasks, PNG renderers and PDF layout, validates routing/counts, and saves `revision_validation.json` under its project. These are offline routing tests with intentionally injected Review findings, not live LLM quality evaluations.

Validation for this delivery: 11 routing tests, 7 workflow integration tests, 6 layout tests and 8 schema tests passed. The three commands above also completed successfully. They verify persistent and cleared errors, zero allowed repairs, severity priority, routing exceptions, downstream refresh, async node compatibility and final PDF warnings.

## Real-agent integration boundary

`RevisionController.attach(graph, repair_actions)` also accepts async agent entry points; use `graph.ainvoke()` for those. Supply all roles from `REPAIR_PLANS`, an existing Reviewer node and Layout node, and let the controller own the outgoing edge from Reviewer. Real agent adapters must still honor input requirements and their task lifecycle; a request for user clarification or a service failure requires its appropriate pause/failure handling.

This delivery connects the controller to the existing fixture graph and tests the reusable controller with sync/async nodes. It does not replace all fixture nodes with a fully live production pipeline. Existing real Reviewer, visual and other agents remain available for that integration.
