# Real daily maps

The live workflow now calls `MapAgent.render_live` with the project input-file
bundle, the configured settings, and the shared public-data HTTP client.
It validates and snapshots the inputs before network work. Standalone offline
`render` remains available for deterministic fixture rendering.

`BasemapService` requests a small on-demand viewport from OpenStreetMap's
standard raster tile service and gets OSRM `/route` GeoJSON separately from
the planning matrix. An OSRM matrix contains distances/times, not road geometry.
The selected driving/walking/cycling profile is respected. There is no implicit
driving fallback for an unconfigured walking/cycling profile.

Maps preserve itinerary stop order, numbered markers, exact original stop
coordinates, route geometry, provider URLs, and copyright metadata in the PNG.
Road waypoints/endpoints must be within 2 km of the requested stops. Failed
road geometry is shown as explicitly dashed straight connections on real tiles.
Failed/incomplete tiles produce a clearly identified offline coordinate diagram;
the renderer never shows missing-tile patches as a complete basemap.

Network limits: at most 16 tiles per day by default, two concurrent tile requests,
12 seconds per request, 90 seconds for the map stage. Shared tiles and route
responses are cached. No adjacent-area prefetch or bulk/offline-region downloads
are performed. Tile HTTP cache lifetimes are honored (seven days if absent).
Keep a distinctive `DATA_USER_AGENT` and observe the tile provider's usage policy:
https://operations.osmfoundation.org/policies/tiles/

PDFs put each real map on a separate page after its daily itinerary, preserving
legibility. OpenStreetMap attribution is embedded in the PNG and linked in the
PDF. Fallback diagrams keep the compact existing layout with an explicit caption.

Settings are documented in `.env.example` under `MAP_*`. Production deployments
with substantial traffic should use a suitable tile provider/hosting arrangement
and its corresponding attribution requirements rather than scale public OSM tiles.

Tests:

    python -m unittest discover -s tests -p test_basemaps.py
    python -m unittest discover -s tests -p test_map_agent.py
