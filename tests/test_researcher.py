from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

import httpx

from backend.app.agents.researcher import ResearcherAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import DayPlan, Place, TripRequest
from backend.app.services.places import PlacesService
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.weather import WeatherService
from backend.app.task_plan import build_tasks
from scripts.check_researcher import mock_handler


def fact(value):
    return {"value": value, "source": "fixture:researcher-test", "confidence": 1}


class ResearcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings(_env_file=None, data_cache_path=self.root / "api.sqlite3", research_max_places=5, overpass_fallback_urls="", research_wikidata_fallback_enabled=False, destination_guide_enabled=False,research_popularity_enabled=False)
        self.requests = []
        self.mode = "ok"
        def handler(request):
            self.requests.append(request)
            if self.mode in {'typo', 'typo_unrelated'} and request.url.host == 'nominatim.openstreetmap.org':
                if request.url.params['q'] == 'Phú Quocs':
                    return httpx.Response(200, json=[])
                return httpx.Response(200, json=[{'osm_type': 'relation', 'osm_id': 99,
                    'name': 'Phú Quốc' if self.mode == 'typo' else 'Đà Nẵng',
                    'namedetails': {'name:vi': 'Thành phố Phú Quốc' if self.mode == 'typo' else 'Thành phố Đà Nẵng'},
                    'lat': '10.2', 'lon': '103.95', 'display_name': 'Địa danh bản đồ kiểm thử'}])
            if self.mode == "overpass_down" and request.url.host == "overpass-api.de":
                return httpx.Response(503)
            if self.mode == "geocode_empty" and request.url.host == "nominatim.openstreetmap.org":
                return httpx.Response(200, json=[])
            if self.mode == "wikidata_down" and request.url.host == "www.wikidata.org":
                return httpx.Response(429)
            response = mock_handler(request)
            if self.mode == "weather_bad_units" and request.url.host == "api.open-meteo.com":
                body = response.json()
                body["daily_units"]["temperature_2m_min"] = "°F"
                return httpx.Response(200, json=body)
            if self.mode == "weather_null" and request.url.host == "api.open-meteo.com":
                body = response.json()
                body["daily"]["temperature_2m_min"][0] = None
                return httpx.Response(200, json=body)
            if self.mode == "ambiguous_fee" and request.url.host == "overpass-api.de":
                body = response.json()
                body["elements"][1]["tags"]["charge"] = "40.000 VND"
                return httpx.Response(200, json=body)
            return response
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.api = PublicAPIClient(self.settings, client=self.http)
        self.places = PlacesService(self.settings, self.api)
        self.weather = WeatherService(self.settings, self.api)
        self.db = Database(self.root / "travelbook.db")
        self.db.init()
        self.agent = ResearcherAgent(self.db, self.places, self.weather, self.root / "artifacts")
        self.trip = TripRequest(destination=fact("Đà Nẵng"), days=fact(3), budget=fact("5000000"),
                                currency=fact("VND"), travelers=fact(2), start_date=fact("2026-11-23"), interests=[fact("biển")])

    async def test_places_are_sourced_and_wikidata_is_linked_by_qid(self):
        result = await self.places.search("Đà Nẵng", ["biển"])
        self.assertEqual(len(result.places), 2)
        beach, museum = result.places
        self.assertTrue(beach.name.source.startswith("https://www.openstreetmap.org/node/10"))
        self.assertIn("Special:EntityData/Q10.json", beach.description.source)
        self.assertIsNone(beach.ticket_price)
        self.assertIsNone(beach.opening_hours)
        self.assertEqual(museum.ticket_price.value, 40000)
        self.assertEqual(museum.currency.value, "VND")
        self.assertEqual(museum.opening_hours.value, "Tu-Su 08:00-17:00")
        for place in result.places:
            Place.model_validate(place.model_dump(mode="json"))
        query = next(request for request in self.requests if request.url.host == "overpass-api.de")
        from urllib.parse import parse_qs
        self.assertIn("out body center", parse_qs(query.content.decode())["data"][0])
        self.assertTrue(all("TravelBookStudio" in request.headers["User-Agent"] for request in self.requests))

    async def test_dangling_telex_key_retried_and_backed_by_geocoder(self):
        self.mode = 'typo'
        result = await self.places.search('Phú Quocs')
        self.assertEqual(result.destination.name.value, 'Phú Quốc')
        self.assertEqual(result.normalized_destination.value, 'Phú Quốc')
        self.assertIn('original=Phú Quocs', result.normalized_destination.source)
        queries = [request.url.params['q'] for request in self.requests if request.url.host == 'nominatim.openstreetmap.org']
        self.assertEqual(queries, ['Phú Quocs', 'Phú Quoc'])
        self.assertTrue(any('chuẩn hóa' in note.value for note in result.notes))

    async def test_spelling_retry_does_not_accept_unrelated_destination(self):
        self.mode = 'typo_unrelated'
        result = await self.places.search('Phú Quocs')
        self.assertIsNone(result.destination)
        self.assertIsNone(result.normalized_destination)
        self.assertEqual(result.places, [])

    async def test_repeat_uses_persistent_cache_and_keeps_provenance(self):
        first = await self.places.search("Đà Nẵng")
        count = len(self.requests)
        second_client = PublicAPIClient(self.settings, client=self.http)
        second = await PlacesService(self.settings, second_client).search("Đà Nẵng")
        self.assertEqual(len(self.requests), count)
        self.assertEqual(first.places, second.places)
        connection = sqlite3.connect(self.settings.data_cache_path)
        try:
            self.assertEqual(connection.execute("SELECT count(*) FROM api_rate WHERE provider LIKE 'nominatim:%'").fetchone()[0], 1)
        finally:
            connection.close()

    async def test_ambiguous_fee_does_not_become_a_price(self):
        self.mode = "ambiguous_fee"
        result = await self.places.search("Đà Nẵng")
        self.assertTrue(all(place.ticket_price is None for place in result.places))

    async def test_weather_dates_units_and_zero_rain_are_preserved(self):
        destination = await self.places.geocode("Đà Nẵng")
        report = await self.weather.forecast(self.trip, destination, today=date(2026, 11, 21))
        self.assertEqual(report.status, "available")
        self.assertEqual(len(report.daily), 3)
        self.assertEqual(report.daily[0].date.value, date(2026, 11, 23))
        self.assertEqual(report.daily[0].rain_probability.value, 0)
        self.assertEqual(report.daily[0].precipitation_mm.value, 0)
        self.assertIn("api.open-meteo.com", report.daily[0].temperature_max.source)
        self.assertEqual(report.timezone.value, "Asia/Ho_Chi_Minh")

    async def test_no_date_or_outside_horizon_does_not_call_weather_api(self):
        destination = await self.places.geocode("Đà Nẵng")
        self.trip.start_date = None
        report = await self.weather.forecast(self.trip, destination, today=date(2026, 11, 21))
        self.assertEqual(report.status, "not_requested")
        self.assertEqual(len(report.daily), 0)
        self.trip = TripRequest.model_validate({**self.trip.model_dump(mode="json"), "start_date": fact("2027-01-01")})
        report = await self.weather.forecast(self.trip, destination, today=date(2026, 11, 21))
        self.assertEqual(report.status, "outside_forecast_window")
        self.assertEqual(len(report.missing_dates), 3)
        self.assertFalse(any(request.url.host == "api.open-meteo.com" for request in self.requests))

    async def test_weather_report_feeds_existing_chart_directly(self):
        from backend.app.services.charts import render_weather_chart
        from PIL import Image
        destination = await self.places.geocode("Đà Nẵng")
        report = await self.weather.forecast(self.trip, destination, today=date(2026, 11, 21))
        days = [DayPlan(day_number=day.day_number, date=day.date, stops=[{
            "place_id": "example", "arrival": fact("09:00"), "departure": fact("10:00"),
        }]) for day in report.daily]
        path = render_weather_chart(days, self.root / "weather.png", forecasts=report.daily)
        with Image.open(path) as image:
            self.assertEqual(image.size, (1200, 800))
            self.assertTrue(json.loads(image.info["Data"])["has_numeric_forecast"])

    async def test_invalid_units_and_null_values_do_not_get_invented(self):
        self.mode = "weather_bad_units"
        destination = await self.places.geocode("Đà Nẵng")
        report = await self.weather.forecast(self.trip, destination, today=date(2026, 11, 21))
        self.assertEqual(report.status, "unavailable")
        self.assertEqual(report.daily, [])

    async def test_partial_forecast_keeps_null_and_zero_separate(self):
        self.mode = "weather_null"
        destination = await self.places.geocode("Đà Nẵng")
        report = await self.weather.forecast(self.trip, destination, today=date(2026, 11, 21))
        self.assertEqual(report.status, "partial")
        self.assertIsNone(report.daily[0].temperature_min)
        self.assertEqual(report.daily[0].rain_probability.value, 0)

    async def test_wikidata_error_keeps_osm_places_and_warning(self):
        self.mode = "wikidata_down"
        result = await self.places.search("Đà Nẵng")
        self.assertEqual(len(result.places), 2)
        self.assertIsNone(result.places[0].description)
        self.assertTrue(result.warnings)

    async def test_run_persists_research_and_releases_planner(self):
        self.db.create_project("research-test", "Test", "Research test", trip_request=self.trip)
        tasks = build_tasks("research-test")
        tasks[0]["status"] = "done"
        self.db.replace_tasks("research-test", tasks)
        # An undated trip is supported without inventing a forecast.
        self.trip.start_date = None
        update = await self.agent.run({"project_id": "research-test", "trip_request": self.trip})
        self.assertEqual(len(update["places"]), 2)
        self.assertEqual(self.db.get_project("research-test")["stage"], "planner")
        saved_tasks = {task["agent"]: task for task in self.db.get_tasks("research-test") if task["agent"] != "pm"}
        self.assertEqual(saved_tasks["researcher"]["status"], "done")
        self.assertEqual(saved_tasks["planner"]["status"], "ready")
        self.assertEqual(len(self.db.get_artifacts("research-test")), 3)
        self.assertEqual(update["artifacts"][0].confidence, 0.6)
        self.assertTrue(any(event["event_type"] == "data_request" for event in self.db.get_events("research-test")))
        for artifact in update["artifacts"]:
            self.assertTrue(Path(artifact.path).is_file())
            json.loads(Path(artifact.path).read_text(encoding="utf-8"))

    async def test_no_places_blocks_planner_without_fixture_fallback(self):
        self.mode = "overpass_down"
        trip = TripRequest.model_validate({**self.trip.model_dump(mode="json"), "start_date": None})
        self.db.create_project("research-failure", "Test", "Research test", trip_request=trip)
        tasks = build_tasks("research-failure")
        tasks[0]["status"] = "done"
        self.db.replace_tasks("research-failure", tasks)
        update = await self.agent.run({"project_id": "research-failure", "trip_request": trip})
        self.assertEqual(update["places"], [])
        self.assertEqual(self.db.get_project("research-failure")["status"], "needs_attention")
        self.assertEqual(next(task for task in self.db.get_tasks("research-failure") if task["agent"] == "planner")["status"], "todo")

    async def test_unknown_destination_does_not_query_other_apis(self):
        self.mode = "geocode_empty"
        result = await self.agent.research(self.trip)
        self.assertEqual(result.status, "needs_attention")
        self.assertIsNone(result.destination)
        self.assertEqual(len(self.requests), 1)

    async def test_invalid_wikidata_id_never_becomes_a_request(self):
        with self.assertRaises(ValueError):
            await self.places.wikidata_description("../../secrets")
        self.assertEqual(self.requests, [])
