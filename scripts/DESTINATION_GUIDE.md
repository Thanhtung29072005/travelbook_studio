# Destination-specific editorial information

Researcher now gathers bounded food, culture and practical-travel sections from
Vietnamese Wikipedia and English Wikivoyage. Titles are resolved through the
MediaWiki API; disambiguation and missing articles are rejected. Wikivoyage
HTML is parsed only when TextExtracts is unavailable, with scripts excluded.
Article URLs and retrieval timestamps remain attached to each research fact.

Nested headings preserve context. Long climate/history sections cannot occupy
the whole evidence budget; transport and getting-around sections are prioritized
for practical tips. At most three short source sections per topic/article enter
the editorial context. Sources are cached by the existing public API client.

Content Editor must use the available topic evidence instead of generic filler.
If it ignores available local sources, one corrective attempt is made. The
existing evidence-ID validation still rejects invented references. Sources that
cannot be obtained are kept unknown; this feature does not invent restaurants,
food prices, opening hours, allergy guarantees, or current forecasts.

PDFs show compact clickable article references below the travel-guide section.
The full source metadata remains in the content artifacts and layout manifest.
These are community travel/encyclopedic references, not official tariffs or
real-time local-operating information.

Configuration: `DESTINATION_GUIDE_ENABLED` (true) and
`DESTINATION_GUIDE_TIMEOUT_SECONDS` (45). Existing projects can be re-edited with
new research; completed PDFs do not change merely by restarting the server.

Tests:

    python -m unittest discover -s tests -p test_destination_guide.py
    python -m unittest discover -s tests -p test_content_editor.py
