from io import BytesIO
import json
from pathlib import Path
import tempfile
import unittest

import httpx
from PIL import Image

from backend.app.config import Settings
from backend.app.schemas import Place, DayPlan
from backend.app.services.basemaps import BasemapService, mercator, viewport
from backend.app.services.maps import render_day_maps
from backend.app.services.public_api import PublicAPIClient


def fact(value):
    return {'value': value, 'source': 'user:map-test', 'confidence': .8}


class BasemapTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(_env_file=None, map_tile_cache_dir=self.root/'tiles', data_cache_path=self.root/'data.sqlite')
        self.places = [Place(id=str(i), name=fact('Điểm '+str(i)), latitude=fact(lat), longitude=fact(lon))
            for i, (lon, lat) in enumerate([(107.58, 16.44), (107.60, 16.46)])]
        self.day = DayPlan(day_number=1, stops=[{'place_id': str(i), 'arrival': fact(f'{9+i:02d}:00'), 'departure': fact(f'{10+i:02d}:00')} for i in range(2)])
        self.calls, self.mode = [], 'ok'
        buffer = BytesIO()
        # Synthetic tile pixels exercise composition only, not a real map source.
        Image.new('RGB', (256, 256), '#dce8d2').save(buffer, format='PNG')
        self.tile = buffer.getvalue()
        def handler(request):
            self.calls.append(request)
            if request.url.host == 'tile.openstreetmap.org':
                return httpx.Response(403) if self.mode == 'tile_down' else httpx.Response(200,
                    content=self.tile, headers={'content-type': 'image/png', 'cache-control': 'public, max-age=604800'})
            if self.mode == 'route_down':
                return httpx.Response(200, json={'code': 'NoRoute'})
            point = [0, 0] if self.mode == 'bad_snap' else [107.58, 16.44]
            return httpx.Response(200, json={'code': 'Ok', 'routes': [{'geometry': {'type': 'LineString',
                'coordinates': [[107.58, 16.44], [107.59, 16.44], [107.60, 16.46]]}}],
                'waypoints': [{'location': point}, {'location': [107.60, 16.46]}]})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        self.api = PublicAPIClient(self.settings, client=client)

    async def test_real_basemap_metadata_and_road_geometry_are_preserved(self):
        layers, warnings = await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(warnings, [])
        layer = layers[1]
        self.assertEqual(layer.route_type, 'road_geometry')
        self.assertEqual(layer.route[1], [107.59, 16.44])
        self.assertLessEqual(len(layer.view.tiles), self.settings.map_tile_max_count)
        paths = render_day_maps([self.day], self.places, self.root/'rendered', layers=layers)
        with Image.open(paths[0]) as image:
            self.assertEqual(image.size, (1200, 800))
            data = json.loads(image.info['Data'])
            self.assertEqual(data['basemap'], 'openstreetmap')
            self.assertEqual(data['place_ids'], ['0', '1'])
            self.assertEqual(data['route_coordinates'], layer.route)
            self.assertIn('OpenStreetMap', image.info['Notes'])
        self.assertTrue(all('TravelBookStudio' in request.headers['User-Agent'] for request in self.calls))

    async def test_persistent_tiles_and_routes_do_not_redownload(self):
        await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        count = len(self.calls)
        await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(len(self.calls), count)

    async def test_missing_tiles_trigger_complete_offline_fallback(self):
        self.mode = 'tile_down'
        layers, warnings = await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(layers, {})
        self.assertTrue(any('dự phòng' in message for message in warnings))
        paths = render_day_maps([self.day], self.places, self.root/'fallback', layers=layers)
        with Image.open(paths[0]) as image:
            self.assertEqual(json.loads(image.info['Data'])['basemap'], 'none')

    async def test_route_failure_keeps_tiles_and_discloses_straight_segments(self):
        self.mode = 'route_down'
        layers, warnings = await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(layers[1].route_type, 'straight_segments')
        self.assertEqual(layers[1].route, [])
        self.assertTrue(any('nét đứt' in message for message in warnings))

    async def test_distant_road_snap_is_not_used(self):
        self.mode = 'bad_snap'
        layers, warnings = await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(layers[1].route, [])
        self.assertTrue(warnings)

    async def test_walking_does_not_silently_use_driving_routes(self):
        self.settings.routing_mode = 'walking'
        layers, _ = await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(layers[1].route_type, 'straight_segments')
        self.assertFalse(any('/route/' in request.url.path for request in self.calls))

    async def test_disabled_basemap_makes_no_network_calls(self):
        self.settings.map_basemap_enabled = False
        layers, warnings = await BasemapService(self.settings, self.api).prepare([self.day], self.places)
        self.assertEqual(layers, {})
        self.assertEqual(self.calls, [])
        self.assertTrue(warnings)

    async def test_single_stop_needs_no_road_request(self):
        day = self.day.model_copy(update={'stops': self.day.stops[:1]})
        layers, warnings = await BasemapService(self.settings, self.api).prepare([day], self.places)
        self.assertEqual(layers[1].route_type, 'single_stop')
        self.assertEqual(warnings, [])
        self.assertFalse(any('/route/' in request.url.path for request in self.calls))

    def test_projection_bounds_tile_budget_and_dateline_fallback(self):
        view = viewport([(107.58, 16.44), (107.60, 16.46)])
        self.assertLessEqual(len(view.tiles), 16)
        for point in [(107.58, 16.44), (107.60, 16.46)]:
            x, y = mercator(*point, view.zoom)
            self.assertTrue(view.left <= x <= view.left+view.width)
            self.assertTrue(view.top <= y <= view.top+view.height)
        with self.assertRaises(ValueError):
            mercator(0, 90, 10)
        with self.assertRaises(ValueError):
            viewport([(-179, 0), (179, 0)])
