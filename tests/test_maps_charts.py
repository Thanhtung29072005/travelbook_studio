import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from PIL import Image
from pydantic import ValidationError

from backend.app.services.charts import WeatherPoint, render_budget_chart, render_timeline_chart, render_weather_chart
from backend.app.services.maps import render_day_map, render_day_maps

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


def fact(value):
    return {"value": value, "source": "fixture:weather-test", "confidence": 0.6}


class MapsChartsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.output = Path(self.directory.name)
        read = lambda name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        self.places = read("research.json")
        self.days = read("itinerary.json")
        self.budget = read("budget.json")

    def data(self, path):
        with Image.open(path) as image:
            image.load()
            self.assertEqual(image.format, "PNG")
            self.assertEqual(image.size, (960, 640))
            self.assertTrue(any(low != high for low, high in image.getextrema()))
            self.assertIn("Source", image.info)
            self.assertTrue(0 <= float(image.info["Confidence"]) <= 1)
            return json.loads(image.info["Data"])

    def test_three_maps_preserve_stop_order_and_coordinates(self):
        paths = render_day_maps(self.days, self.places, self.output, size=(960, 640))
        self.assertEqual([path.name for path in paths], ["day1.png", "day2.png", "day3.png"])
        indexed = {place["id"]: place for place in self.places}
        for day, path in zip(self.days, paths):
            data = self.data(path)
            self.assertEqual(data["place_ids"], [stop["place_id"] for stop in day["stops"]])
            self.assertEqual(data["route_type"], "straight_segments")
            self.assertEqual(data["coordinates"], [
                [indexed[stop["place_id"]]["longitude"]["value"], indexed[stop["place_id"]]["latitude"]["value"]]
                for stop in day["stops"]
            ])

    def test_travel_day_and_activities_render_without_fake_map_points(self):
        from backend.app.services.day_schedule import slot
        day={'day_number':1,'stops':[],'activities':[slot('terminal','Chờ chuyến về',480,540).model_dump(mode='json')]}
        map_data=self.data(render_day_map(day,[],self.output/'travel.png',size=(960,640)))
        self.assertEqual(map_data['place_ids'],[])
        timeline=self.data(render_timeline_chart([day],[],self.output/'activities.png',size=(960,640)))
        self.assertEqual(timeline['stops'],[])
        self.assertEqual(timeline['activities'][0]['kind'],'terminal')

    def test_unknown_place_and_duplicate_day_reject_before_output(self):
        with self.assertRaises(ValueError):
            render_day_maps(self.days, self.places[:1], self.output)
        with self.assertRaises(ValueError):
            render_day_maps([self.days[0], self.days[0]], self.places, self.output)
        self.assertEqual(list(self.output.iterdir()), [])

    def test_budget_categories_sum_to_exact_decimal_total(self):
        data = self.data(render_budget_chart(self.budget, self.output / "budget.png", size=(960, 640)))
        self.assertEqual(Decimal(data["total"]), Decimal("3700000"))
        self.assertEqual(Decimal(data["limit"]), Decimal("5000000"))
        self.assertTrue(data["within_budget"])
        calculated = sum(Decimal(part["sourced"]) + Decimal(part["estimated"])
                         for part in data["categories"].values())
        self.assertEqual(calculated, Decimal(data["total"]))
        self.assertTrue(all(Decimal(part["sourced"]) == 0 for part in data["categories"].values()))

    def test_empty_weather_does_not_invent_forecast(self):
        path = render_weather_chart(self.days, self.output / "weather.png", size=(960, 640))
        data = self.data(path)
        self.assertFalse(data["has_numeric_forecast"])
        self.assertEqual(data["forecasts"], [])
        with Image.open(path) as image:
            self.assertEqual(float(image.info["Confidence"]), 0)
            self.assertIn("No numeric forecast", image.info["Notes"])

    def test_numeric_weather_preserves_fact_values_and_missing_days(self):
        forecasts = [{"day_number": 1, "temperature_min": fact(24), "temperature_max": fact(30),
                      "rain_probability": fact(45)}]
        data = self.data(render_weather_chart(self.days, self.output / "forecast.png",
                                              forecasts=forecasts, size=(960, 640)))
        self.assertTrue(data["has_numeric_forecast"])
        self.assertEqual(len(data["forecasts"]), 1)
        self.assertEqual(data["forecasts"][0]["temperature_max"]["value"], 30)
        self.assertEqual([point["day_number"] for point in data["forecasts"]], [1])

    def test_weather_rejects_invalid_range_and_unknown_day(self):
        with self.assertRaises(ValidationError):
            WeatherPoint(day_number=1, temperature_min=fact(30), temperature_max=fact(20))
        with self.assertRaises(ValidationError):
            WeatherPoint(day_number=1, rain_probability=fact(101))
        with self.assertRaises(ValueError):
            render_weather_chart(self.days, self.output / "invalid.png", forecasts=[{"day_number": 4}])

    def test_timeline_preserves_all_stops_and_time_ranges(self):
        data = self.data(render_timeline_chart(self.days, self.places, self.output / "timeline.png", size=(960, 640)))
        self.assertEqual(len(data["stops"]), sum(len(day["stops"]) for day in self.days))
        self.assertEqual(data["stops"][0]["start_hour"], 7)
        self.assertEqual(data["stops"][0]["end_hour"], 8.5)
        self.assertEqual(data["stops"][1]["travel_hours"], 0.5)

    def test_invalid_size_does_not_create_png(self):
        with self.assertRaises(ValueError):
            render_day_map(self.days[0], self.places, self.output / "map.png", size=(0, 800))
        self.assertFalse((self.output / "map.png").exists())


if __name__ == "__main__":
    unittest.main()
