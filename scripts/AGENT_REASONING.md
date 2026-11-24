# Agent decisions with system prompts

Researcher, Planner, Image Curator and Reviewer now execute structured LLM decisions through the existing LangChain/9Router client. These are bounded decisions, not an unrestricted tool-calling loop.

| Role | LLM decision | Code/tool responsibility |
| --- | --- | --- |
| Researcher | Search interests, weather selection, one optional refinement, prioritization of returned place IDs | Same requested destination; provider facts and coordinates remain unchanged |
| Planner | Assign known eligible place IDs to days and rank their priority | Routing, opening windows, age limits, lunch, visit duration and final arrival/departure times |
| Image Curator | Provider priority and selection/rejection of returned photo metadata | License/author/source requirements, dimensions, relevance filtering and Unsplash tracking |
| Reviewer | Additional qualitative issues linked to existing facts | Original rule checks and issues remain; LLM cannot clear a code failure |

Each role has a `SYSTEM` prompt in its agent module. Shared guards are in `backend/app/services/reasoning.py`. Domain validators reject unknown IDs, duplicate/omitted Planner IDs, unsupported providers and nonexistent evidence references. Curator judges metadata only; it does not inspect image pixels. A valid evidence reference alone does not prove a qualitative observation is correct: LLM quality issues remain reviewable judgments with confidence capped at 0.5.

## Configuration

Keep the existing `NINE_ROUTER_BASE_URL`, `NINE_ROUTER_API_KEY` and `NINE_ROUTER_MODELS` configuration. Add:

```dotenv
AGENT_REASONING_ENABLED=true
AGENT_REASONING_REQUIRED=false
```

With `ENABLED=true`, decisions use the configured primary/fallback model chain and prompt-hash cache. Missing credentials, invalid structured output or failed domain validation produce an audited fallback to existing code. `REQUIRED=true` instead stops the agent when a required decision fails. `ENABLED=false` explicitly selects code-only behavior. An injected router enables decisions for tests/integration regardless of the switch; callers own injected client lifetime.

Results contain `reasoning` records with role, step, status (`llm`, `fallback`, `disabled`), selected model, cache flag, usage and the accepted structured decision. LLM judgments carry a source with the context hash and confidence 0.5; provider facts retain their original source/confidence. When a project ID is provided, `reasoning_decision` events are written to SQLite. Rationales are short decision explanations, not internal chain-of-thought. Failed provider response bodies and secrets are not logged.

## Check the upgrade

Run from the project root with dependencies installed:

```powershell
python scripts/check_agent_reasoning.py --mock
```

This runs all four roles through the actual router/parser using mocked LLM HTTP and mocked data APIs; Planner's identical second request must use cache. JSON reports are saved under `backend/data/artifacts/reasoning-mock-*/`.

```powershell
python scripts/check_agent_reasoning.py
```

Without `--mock`, the LLM uses live 9Router; **all place, weather and photo API data in this smoke test remain mocked**. This smoke test requires successful LLM decisions and stops on failure. Its Reviewer verdict is expected to be REVISE because the supplied fixture lacks production evidence and a complete budget; it never grants production approval.

Focused tests:

```powershell
python -m unittest discover -s tests -p test_agent_reasoning.py -v
```

## Integration boundary

The upgraded agent methods and their `run()` entry points invoke this decision layer. Existing Visual Team calls to Image Curator also use it when configured. PM and Content Editor retain their existing LLM prompts; Illustrator retains its image-generation prompts. Budget arithmetic, Map/Chart rendering and layout remain code responsibilities.

`workflow.py` is still the earlier fixture-backed demonstration. This change does not replace it with a complete live end-to-end production graph. To integrate a live graph, call these real agent `run()` entry points with their required State and task dependencies.

## Validation for this delivery

- 11 new decision-layer tests passed, including changed tool arguments, non-default photo choice, invented IDs, closed venues, immutable code verdicts and strict failure mode.
- 57 existing role/service tests passed: Researcher 13, Planner 10, Photos 11, Image Curator 5, Reviewer 18.
- Mock four-role smoke: 7 validated LLM decisions, 2 places, 1 day, 2 selected photos; identical Planner replay hit cache.
- Live 9Router smoke did not complete: all configured models failed at the first Researcher decision. A one-model diagnostic reported a connection/timeout error. No live success is claimed.
