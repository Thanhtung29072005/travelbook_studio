# LLM rules: photo location, consistent style, age suitability

`ReviewerAgent.run()` now runs code rules, existing language quality review, and a dedicated multimodal review. It exports the existing `Review` schema in `review.json` and full checks/audits in `review_checks.json`. `evaluate()` remains a pure code-only entry point; `visual_review(state, result=None)` runs the new layer directly.

| Rule | Evidence | Responsible agent |
| --- | --- | --- |
| `LLM_PHOTO_LOCATION` | Actual photo pixels and facts for its linked place | `image_curator` |
| `LLM_STYLE_CONSISTENCY` | At least two decorative illustrations and configured style lock | `illustrator` |
| `LLM_AGE_SUITABILITY` | Explicit traveler ages plus itinerary | `planner` |
| `LLM_AGE_SUITABILITY` | Explicit traveler ages plus written content | `content_editor` |
| `LLM_AGE_SUITABILITY` | Explicit traveler ages plus photo/illustration pixels | `image_curator` / `illustrator` |
| Missing traveler ages | Request ages from the user | `pm` |

The backend assigns responsibility. A model cannot redirect a photo error to Budget or clear any existing code error.

## Classification and verdict

Each `ReviewIssue` now includes `classification` (`critical` / `warning`) and `evidence_ids`, alongside `category`, `severity`, `rule_code`, `target_agent` and the evidence-bearing `message` (`value`, `source`, `confidence`). Old JSON without classification remains valid: it is filled from severity.

- `critical` corresponds to existing `major`/`blocker` severity and requires `REVISE`.
- `warning` corresponds to `minor` severity and can coexist with `PASS` if no critical issues remain.
- Each visual check reports `pass`, `fail`, or `unknown` in `review_checks.json`.
- Missing evidence, failed LLM review and model-declared `unknown` become critical requests for evidence/manual review. They are not assertions that an image or activity is actually wrong.
- Wrong-place photo failures are always critical. Small decorative differences may be warnings.
- All requested tasks must be returned exactly once. Fake references and pixel-free visual claims are rejected. Real image and linked place references are required for a non-unknown photo judgment; age judgments require supplied age references.
- Model judgments retain model/context and image SHA-256 provenance with confidence capped at 0.5. This is not a calibrated probability, proof of landmark identity, legal license verification, or a safety certification.

## Configure a vision model

```dotenv
REVIEWER_VISUAL_ENABLED=true
NINE_ROUTER_VISION_MODELS=your-vision-model-id,your-vision-fallback-id
REVIEWER_MAX_IMAGES=12
AGENT_REASONING_ENABLED=true
AGENT_REASONING_REQUIRED=false
```

Use IDs available in your 9Router instance and capable of Chat Completions `image_url` content blocks with structured JSON. Text-only models are insufficient. No vision model ID is guessed. The vision model chain is separate from `NINE_ROUTER_MODELS` used for text quality review.

If a required model is unavailable, the default produces `unknown` checks and a blocking Review. `AGENT_REASONING_REQUIRED=true` instead stops the agent on model/validation failure. `REVIEWER_VISUAL_ENABLED=false` explicitly disables the new layer; that mode only evaluates the earlier checks.

## Required State and images

Supply the existing `trip_request` (including one `traveler_ages` fact per traveler), `places`, `itinerary`, `budget`, `photos`, `content` or `content_document`, and `illustrations` (Illustrator PNG Artifact objects). All three Visual Team dependencies must be complete before the Reviewer task runs.

Local `Photo.local_path` and illustration `Artifact.path` must resolve inside the configured artifact root. Remote photo pixels may be streamed directly from HTTPS `upload.wikimedia.org`, `images.unsplash.com`, or `images.pexels.com` when LLM review is configured. Redirects, custom ports, credential-bearing URLs and other hosts are rejected. Failed local evidence does not silently switch to a different remote image.

Input files are limited to 8 MiB / 24 million pixels; EXIF orientation is applied and images are reduced to JPEG thumbnails no larger than 768 pixels per side. Thumbnails, not local file paths, are sent to the model. At most 12 images are attached per request. Uninspected assets beyond the limit produce explicit `unknown` checks, rather than a claim that the complete set passed. For larger projects, batching remains future work.

Decorative style checks compare illustration assets only; photographs are not required to resemble watercolor illustrations. Style signatures alone do not establish visual consistency. Ambiguous landmark photos must remain unknown even when their captions match.

The existing router handles model fallback, schema parsing and cache. Image IDs and image-content hashes participate in the cache key; changing image pixels invalidates the cached decision. Decision events and accepted structured output are recorded through the existing DB audit mechanism; base64 pixels are not saved in the decision log/cache.

## Run

```powershell
python scripts/check_visual_review.py --mock
```

This runs the actual Reviewer lifecycle, SQLite events/tasks, LangChain structured parser and multimodal HTTP payload with mocked model responses and synthetic pixel fixtures. It demonstrates all three error types and both classifications, saves `review.json`, blocks Layout for critical issues and confirms a cached replay. It does not measure real model accuracy.

```powershell
python scripts/check_visual_review.py --state path/to/state.json
```

Live mode uses the supplied data and configured text/vision models. It requires `--state` and a configured vision model chain. This delivery was validated offline; a successful live vision request has not been confirmed in this environment.

```powershell
python -m unittest discover -s tests -p test_visual_review.py -v
```

`scripts/check_reviewer.py` remains an explicitly code-only compatibility demo. `workflow.py` remains the earlier fixture-backed graph; this change wires the real Reviewer entry point, not a new full live production workflow.
