import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from backend.app.config import Settings
from backend.app.schemas import Place
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.routing import RoutingService, haversine_km


def point(name, lat=16, lon=108):
    def fact(value):
        return {"value": value, "source": "fixture:routing-test", "confidence": 1}
    return Place(id=name, name=fact(name), latitude=fact(lat), longitude=fact(lon))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "cache.sqlite3"
        self.settings = Settings(_env_file=None, data_cache_path=self.path)
        self.calls = []
        self.mode = "ok"
        def handler(request):
            self.calls.append(request)
            if self.mode in {"down", "ors"} and request.url.host == "router.project-osrm.org":
                return httpx.Response(503)
            if self.mode == "down":
                return httpx.Response(503)
            if self.mode == "null":
                return httpx.Response(200, json={"code": "Ok", "durations": [[0, None], [None, 0]], "distances": [[0, None], [None, 0]]})
            if self.mode == "no_table":
                return httpx.Response(400, json={"code": "NoTable"})
            return httpx.Response(200, json={"code": "Ok", "durations": [[0, 61], [120, 0]], "distances": [[0, 1500], [2000, 0]]})
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.places = [point("a", 16, 108), point("b", 16.01, 108.02)]

    def service(self):
        return RoutingService(self.settings, PublicAPIClient(self.settings, client=self.http))

    async def test_osrm_lon_lat_seconds_meters_and_asymmetric_matrix(self):
        matrix = await self.service().matrix(self.places)
        self.assertIn("108.000000,16.000000;108.020000,16.010000", self.calls[0].url.path)
        self.assertEqual(matrix.leg("a", "b").duration_minutes.value, 2)
        self.assertEqual(matrix.leg("b", "a").duration_minutes.value, 2)
        self.assertEqual(matrix.leg("a", "b").distance_km.value, 1.5)
        self.assertEqual(matrix.leg("b", "a").distance_km.value, 2)
        self.assertFalse(matrix.leg("a", "b").estimated)

    async def test_osrm_failure_uses_ors_and_never_caches_api_key(self):
        self.mode = "ors"
        self.settings = Settings(_env_file=None, data_cache_path=self.path, openrouteservice_api_key="test-ors-secret")
        matrix = await self.service().matrix(self.places)
        self.assertEqual(matrix.leg("a", "b").provider, "ors")
        request = self.calls[-1]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.headers["Authorization"], "test-ors-secret")
        self.assertEqual(json.loads(request.content)["locations"][0], [108, 16])
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute("SELECT * FROM api_cache").fetchall()
        finally:
            connection.close()
        self.assertNotIn("test-ors-secret", str(rows))

    async def test_http_failure_returns_explicit_estimate(self):
        self.mode = "down"
        matrix = await self.service().matrix(self.places)
        leg = matrix.leg("a", "b")
        self.assertTrue(leg.estimated)
        self.assertEqual(leg.provider, "haversine")
        self.assertLessEqual(leg.duration_minutes.confidence, 0.25)
        self.assertIn("speed_kmh=25", leg.duration_minutes.source)
        self.assertGreater(leg.duration_minutes.value, 0)

    async def test_known_unreachable_is_not_given_a_fake_travel_time(self):
        self.mode = "null"
        matrix = await self.service().matrix(self.places)
        leg = matrix.leg("a", "b")
        self.assertFalse(leg.reachable)
        self.assertIsNone(leg.duration_minutes)
        self.assertEqual(leg.provider, "unreachable")

    async def test_walking_does_not_use_car_profile(self):
        self.settings = Settings(_env_file=None, data_cache_path=self.path, routing_mode="walking")
        matrix = await self.service().matrix(self.places)
        self.assertEqual(self.calls, [])
        self.assertIn("speed_kmh=4", matrix.leg("a", "b").duration_minutes.source)

    async def test_explicit_no_table_error_is_not_finite_haversine(self):
        self.mode = "no_table"
        matrix = await self.service().matrix(self.places)
        self.assertFalse(matrix.leg("a", "b").reachable)
        self.assertIsNone(matrix.leg("a", "b").duration_minutes)

    async def test_matrix_cache_and_duplicate_guard(self):
        first = await self.service().matrix(self.places)
        second = await self.service().matrix(self.places)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(first, second)
        with self.assertRaises(ValueError):
            await self.service().matrix([self.places[0], self.places[0]])

    def test_haversine_known_geometry_and_invalid_coordinates(self):
        self.assertEqual(haversine_km(0, 0, 0, 0), 0)
        self.assertAlmostEqual(haversine_km(0, 0, 0, 1), 111.195, places=2)
        self.assertAlmostEqual(haversine_km(0, 179, 0, -179), 222.390, places=2)
        with self.assertRaises(ValueError):
            haversine_km(91, 0, 0, 0)
