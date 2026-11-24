# Layout Editor agent

`backend/app/agents/layout_editor.py` consumes the actual agent State. It uses deterministic ReportLab layout; no LLM is needed to position text or calculate page numbers.

The complete book includes:

- Cover with an optional Illustrator cover image and a dynamically generated destination/title.
- A table of contents with final page numbers, calculated with a multi-pass build.
- Overview, structured travel tips, food and cultural notes when ContentDocument is supplied.
- Each day: a local eligible place photo, or a clearly labeled decorative image/fallback; introduction; schedule; map; travel notes and tips. Long content flows onto continuation pages.
- Budget chart, category totals, line items, sourced/estimated totals and signed budget variance calculated by the Budget schema.
- Weather chart and optional WeatherReport values/notes. Missing forecasts remain explicitly unavailable.
- Timeline chart, photo author/license/source pages, decorative illustration provenance, and source/data limitations.
- Page numbers on every page and annotations for facts below the configured confidence threshold.

## Inputs and entry points

```python
agent = LayoutEditorAgent(db, settings, settings.artifacts_dir)
update = await agent.run(state)
pdf = update["final_pdf"]
```

State requires `project_id`, `trip_request`, complete ordered `itinerary`, `places`, `budget`, `content` or `content_document`, `photos`, `maps`, `charts` and `review`. `illustrations` and `weather` are optional.

Map PNGs must be uniquely named `day1.png`, `day2.png`, etc. Charts must be `budget.png`, `weather.png`, `timeline.png`. Assets are the Artifact objects produced by Map Agent. Illustrator `cover.png` and `day_1.png` etc. are recognized; illustration credits retain the supplied Artifact provenance. Every Artifact must belong to the same project and resolve under the artifact root.

Local photos must resolve under the artifact root. Supported eligible local photos are embedded with their author/license caption. Unsplash hotlink-only images and unverified placeholder metadata remain in the source pages without being embedded. Remote image URLs are not automatically downloaded by Layout Editor. Decorative AI images are labeled and never presented as verified photographs.

`run()` enforces the Reviewer task dependency, writes events and PDF/manifest artifacts, completes Layout, and makes PM finalization ready. It does not complete PM acceptance or grant production approval. `render(state)` is a lower-level API for an already validated integration.

An unresolved `REVISE` cannot enter Layout unless `revision_exhausted=true` and explicit `revision_warnings` are supplied by the revision controller. Those warnings remain visible in the PDF. Normal service/input failures block the Layout task and are recorded in DB.

## Confidence notes

```dotenv
LAYOUT_LOW_CONFIDENCE_THRESHOLD=0.7
```

A confidence strictly below the threshold adds `Nên kiểm tra lại` beside visible editorial/name/schedule/price/weather facts where applicable. A separate source appendix covers all low-confidence facts collected from the input, including values not printed inline. Long values are abbreviated in this appendix for readability; the manifest retains their full value, source, confidence and input path. `source_index` resolves the appendix's Sxxx reference codes. Confidence values are not raised or presented as probabilities.

## Non-overlapping layout and audit

Paragraphs, tables and images use ReportLab flow layout, rather than coordinates drawn on top of images. Image aspect ratios are preserved. Tables repeat headers and can split long rows; paragraphs paginate. The body uses an embedded Unicode TrueType font for Vietnamese.

The final pass records the page and bounding box of each text/image flowable. Before publishing, intersecting text/image rectangles cause a layout error and the temporary PDF is discarded. The sibling `.manifest.json` contains `layout_boxes`, `text_image_overlap_count`, section page locations, TOC entries, embedded/omitted photo IDs, low-confidence evidence, source references and warnings. This audit covers document text and image flowables; text already rasterized inside maps/charts belongs to those upstream images.

## Run and verify

```powershell
python scripts/check_layout_editor.py
python scripts/check_layout_editor.py --illustration-dir path/to/illustrator/output
python scripts/check_layout_editor.py --state path/to/state.json
```

The default is a clearly labeled offline Da Nang fixture with placeholder photo metadata; it does not verify travel facts. `--illustration-dir` reuses existing cover/day PNGs as decorative images. `--state` uses the supplied project identity and assets; it does not relabel them as a new project. For an existing project, its Reviewer dependency must be complete. The CLI saves `layout_validation.json` next to the PDF.

```powershell
python -m unittest discover -s tests -p test_layout_editor.py -v
```

Delivery validation: Vietnamese text/folios, final TOC page locations, local photo embedding, long-content pagination, confidence notes, weather fields, dependency and revision gates, invalid assets and deliberate overlap rejection. The fixture workflow and existing layout tests are also retained. The supplied 18-page demo reuses prior generated decorative artwork; its unverified place-photo placeholders are attributed but not embedded. All demo pages were rasterized and visually inspected.

The earlier fixture workflow retains its original compact layout for compatibility. This task provides the complete real-agent Layout Editor entry point; replacing all fixture nodes with a live pipeline remains a separate integration step.
