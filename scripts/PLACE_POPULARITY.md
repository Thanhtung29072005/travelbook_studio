# Grounded place popularity and suitability

Researcher now gathers a candidate pool before truncating the final shortlist.
When popularity ranking is enabled, Overpass candidates are supplemented by
Wikidata regional discovery even if Overpass succeeds. Default highlight radius
is 50 km, to cover plausible out-of-centre day trips. Wikidata includes bridges,
theme parks, natural areas, hill stations and UNESCO-designated heritage sites.
Industrial/high-tech/business parks are excluded; a broad Wikidata "park" class
does not establish that a place is a tourist park.

Discovery orders by `wikibase:sitelinks` rather than nearest coordinates. Small
16-binding pages avoid truncated SPARQL JSON from upstream gateways. Every
candidate keeps its actual source coordinates and entity ID. Regional discovery
does not assert transport feasibility, entry price, opening hours or child safety.

Popularity evidence:

- Actual number of Wikidata sitelinks (all linked Wikimedia sites).
- Sum of a complete 30-day Wikipedia pageview window, ending two UTC days before
  retrieval to allow publication lag. Vietnamese article preferred, English used
  when Vietnamese is absent. Language/window/URL/time are kept in Fact sources.
- Missing/incomplete pageviews remain None. A real measured zero remains zero.

Score: `12*log1p(sitelinks) + 4*log1p(pageviews30d) + interest + family + proximity`.
Names, coordinates, sitelink counts and view counts are never invented by the LLM.
The shortlist limits repeated categories (particularly bridges) and overlapping
heritage-area names before it is capped. LLM follow-up priorities cannot override
the numeric popularity order. Planner respects fame when capacity is limited,
while opening windows, routes and existing child/senior pacing limits still apply.

Family profile uses both structured ages and age requirements, so a known child
is not missed merely because ages of every adult have not been supplied. Adults-
only venues/known incompatible minimum ages are filtered. This is not a blanket
claim that an unassessed famous venue is child-safe. Planner repeats the tourism
filter so old research cannot reinsert industrial parks into a new itinerary.

Default bounds: 40 candidates, 24 pageview requests, 3 concurrent pageview calls,
45 seconds for popularity enrichment. API responses are cached for one day.
Settings are under `RESEARCH_*` in `.env.example`.

Changes affect new/researched itineraries; existing PDFs are not silently
relabeled with places they did not actually visit. A changed itinerary needs
fresh timing, price checks, maps, content and illustrations before delivery.
