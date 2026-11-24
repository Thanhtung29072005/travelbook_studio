"""Domain validation tests. Run: python -m unittest discover -s tests -v."""

import json
import unittest
from decimal import Decimal

from pydantic import ValidationError

from backend.app.schemas import (
    Artifact, Budget, DayPlan, Fact, Photo, Place, Review, TripRequest, merge_artifacts,
)


def fact(value, confidence=0.8, source="user:message-1"):
    return {"value": value, "source": source, "confidence": confidence}


class SchemaTests(unittest.TestCase):
    def test_facts_require_explicit_source_and_confidence(self):
        for payload in (
            {"value": "Da Nang"},
            {"value": "Da Nang", "source": "user:1"},
            fact("Da Nang", source="   "),
            fact("Da Nang", confidence=-0.1),
            fact("Da Nang", confidence=1.1),
            fact("Da Nang", confidence=float("nan")),
        ):
            with self.subTest(payload=payload), self.assertRaises(ValidationError):
                Fact[str].model_validate(payload)

    def test_meal_cannot_overlap_travel_or_visit_without_on_site_link(self):
        day={'day_number':1,'stops':[{'place_id':'park','arrival':fact('11:00'),'departure':fact('15:00'),'travel_minutes':fact(30)}],
             'activities':[{'kind':'lunch','title':fact('Ăn trưa'),'start':fact('12:00'),'end':fact('13:00')}]}
        with self.assertRaises(ValidationError):
            DayPlan.model_validate(day)
        day['activities'][0]['within_place_id']='park'
        DayPlan.model_validate(day)
        day['activities'][0].update(start=fact('10:30'),end=fact('11:00'))
        with self.assertRaises(ValidationError):
            DayPlan.model_validate(day)

    def test_journey_timestamps_require_order_and_trip_dates(self):
        data=dict(destination=fact('Đà Nẵng'),days=fact(3),budget=fact('5000000'),currency=fact('VND'),travelers=fact(2),start_date=fact('2026-12-01'),
                  outbound_departure_at=fact('2026-12-01T08:00:00+07:00'),destination_arrival_at=fact('2026-12-01T09:30:00+07:00'),
                  destination_departure_at=fact('2026-12-03T18:00:00+07:00'),return_arrival_at=fact('2026-12-03T19:30:00+07:00'))
        TripRequest.model_validate(data)
        data['return_arrival_at']=fact('2026-12-03T17:00:00+07:00')
        with self.assertRaises(ValidationError):
            TripRequest.model_validate(data)

    def test_request_dates_and_nested_provenance(self):
        data = dict(destination=fact("Da Nang"), days=fact(3), budget=fact("5000000"),
                    currency=fact("VND"), travelers=fact(2),
                    start_date=fact("2026-12-01"), end_date=fact("2026-12-03"))
        trip = TripRequest.model_validate(data)
        self.assertEqual(trip.days.value, 3)
        self.assertEqual(trip.traveler_ages, [])
        data["end_date"] = fact("2026-12-02")
        with self.assertRaises(ValidationError):
            TripRequest.model_validate(data)
        data["end_date"] = fact("2026-12-03")
        data["destination"] = {"value": "Da Nang", "confidence": 0.9}
        with self.assertRaises(ValidationError):
            TripRequest.model_validate(data)

    def test_place_unknown_hours_and_invalid_coordinates(self):
        data = dict(id="p1", name=fact("Beach"), latitude=fact(16.05), longitude=fact(108.2))
        place = Place.model_validate(data)
        self.assertIsNone(place.opening_hours)
        data["latitude"] = fact(91)
        with self.assertRaises(ValidationError):
            Place.model_validate(data)

    def test_schedule_accounts_for_travel(self):
        data = dict(day_number=1, stops=[
            dict(place_id="p1", arrival=fact("09:00"), departure=fact("10:00")),
            dict(place_id="p2", arrival=fact("10:15"), departure=fact("11:00"),
                 travel_minutes=fact(20)),
        ])
        with self.assertRaises(ValidationError):
            DayPlan.model_validate(data)
        data["stops"][1]["arrival"] = fact("10:20")
        DayPlan.model_validate(data)

    def test_budget_uses_decimal_and_propagates_confidence(self):
        budget = Budget.model_validate(dict(currency=fact("USD"), limit=fact("0.29"), items=[
            dict(id="a", category="food", description=fact("Lunch"),
                 amount=fact("0.1", 0.7), price_kind="estimated"),
            dict(id="b", category="transport", description=fact("Bus"),
                 amount=fact("0.2", 0.9), price_kind="sourced"),
        ]))
        self.assertEqual(budget.total.value, Decimal("0.3"))
        self.assertEqual(budget.total.confidence, 0.7)
        self.assertFalse(budget.within_budget.value)
        self.assertTrue(budget.total.source.startswith("calculation:"))
        exported = json.loads(budget.model_dump_json())
        self.assertEqual(exported["total"]["value"], "0.3")
        self.assertEqual(exported["total"]["confidence"], 0.7)
        self.assertFalse(exported["within_budget"]["value"])
        self.assertIn("source", exported["within_budget"])

    def test_photo_requires_attribution_and_valid_source_url(self):
        data = dict(id="photo1", place_id="p1", url=fact("https://example.org/photo.jpg"),
                    author=fact("Photographer"), license=fact("CC BY-SA 4.0"),
                    source_url=fact("https://example.org/photo-page"))
        photo = Photo.model_validate(data)
        decoded = json.loads(photo.model_dump_json())
        self.assertEqual(decoded["license"]["value"], "CC BY-SA 4.0")
        data.pop("author")
        with self.assertRaises(ValidationError):
            Photo.model_validate(data)

    def test_review_cannot_pass_blocker(self):
        data = dict(source="review:run-1", confidence=0.9, verdict="PASS", summary=fact("Issue"),
                    issues=[dict(category="photo", severity="blocker", message=fact("No license"),
                                 target_agent="image_curator")])
        with self.assertRaises(ValidationError):
            Review.model_validate(data)
        data["verdict"] = "REVISE"
        Review.model_validate(data)

    def test_artifact_fan_in_preserves_branches_and_rejects_conflicts(self):
        artifact = Artifact(id="map1", project_id="trip1", agent_id="map_agent", kind="map",
                            name="Day 1", path="maps/day1.png", mime_type="image/png",
                            source="routing:run1", confidence=0.8)
        other = artifact.model_copy(update={"id": "map2"})
        self.assertEqual(len(merge_artifacts([artifact], [other])), 2)
        self.assertEqual(merge_artifacts([artifact], [artifact]), [artifact])
        with self.assertRaises(ValueError):
            merge_artifacts([artifact], [artifact.model_copy(update={"path": "different.png"})])


if __name__ == "__main__":
    unittest.main()
