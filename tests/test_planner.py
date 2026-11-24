import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app.agents.planner import PlannerAgent, cluster_places
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import DayPlan, Fact, Place, TripRequest
from backend.app.services.opening_hours import UnsupportedHours, parse_opening_hours
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.routing import RoutingService
from backend.app.task_plan import build_tasks


def fact(value):
    return {"value": value, "source": "fixture:planner-test", "confidence": 1}


def place(name, lat=16, lon=108, hours="24/7", duration=60, minimum_age=None, age_notes=()):
    return Place(id=name, name=fact(name), latitude=fact(lat), longitude=fact(lon),
                 opening_hours=fact(hours) if hours is not None else None,
                 visit_minutes=fact(duration) if duration is not None else None,
                 minimum_age=fact(minimum_age) if minimum_age is not None else None,
                 age_notes=[fact(note) for note in age_notes])


class PlannerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.settings = Settings(_env_file=None, data_cache_path=self.root / "cache.sqlite3", routing_provider_order="haversine")
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        self.addAsyncCleanup(self.http.aclose)
        self.db = Database(self.root / "test.db")
        self.db.init()
        self.agent = PlannerAgent(self.db, RoutingService(self.settings, PublicAPIClient(self.settings, client=self.http)), self.root / "outputs")
        self.trip = TripRequest(destination=fact("Đà Nẵng"), days=fact(1), budget=fact("5000000"),
                                currency=fact("VND"), travelers=fact(2), traveler_ages=[fact(30), fact(29)], start_date=fact("2026-11-23"))

    def test_clusters_keep_two_distant_groups_together(self):
        points = [place(f"a{i}", 16+i*.001, 108) for i in range(3)] + [place(f"b{i}", 16.3+i*.001, 108.3) for i in range(3)]
        groups = cluster_places(points, 2)
        self.assertEqual({frozenset(point.id[0] for point in group) for group in groups}, {frozenset("a"), frozenset("b")})

    async def test_early_closing_venue_is_visited_before_always_open(self):
        result = await self.agent.plan(self.trip, [place("a_beach"), place("z_museum", hours="09:00-10:30")])
        stops = result.itinerary[0].stops
        self.assertEqual(stops[0].place_id, "z_museum")
        self.assertLessEqual(stops[0].departure.value.hour * 60 + stops[0].departure.value.minute, 630)
        DayPlan.model_validate_json(result.itinerary[0].model_dump_json())
        self.assertTrue(all(stop.travel_minutes is not None for stop in stops))

    async def test_lunch_break_and_disjoint_opening_windows(self):
        result = await self.agent.plan(self.trip, [place("museum", hours="11:30-16:00")])
        self.assertEqual(result.itinerary[0].stops[0].arrival.value.strftime('%H:%M'), '14:30')
        self.assertEqual(result.itinerary[0].stops[0].departure.value.strftime('%H:%M'), '15:30')
        self.assertEqual({a.kind for a in result.itinerary[0].activities} & {'lunch','rest','dinner'}, {'lunch','rest','dinner'})

    async def test_weekday_closure_is_moved_to_a_day_when_open(self):
        values = self.trip.model_dump(mode="json")
        values["days"] = fact(2)
        request = TripRequest.model_validate(values)
        result = await self.agent.plan(request, [place("closed_monday", hours="Mo off; Tu-Su 09:00-17:00"), place("open")])
        self.assertEqual([day.stops[0].place_id for day in result.itinerary], ["open", "closed_monday"])
        self.assertEqual(result.missing_days, [])

    async def test_industrial_place_is_not_scheduled_even_from_old_research(self):
        result=await self.agent.plan(self.trip,[place('Khu công nghệ cao Đà Nẵng'),place('Bảo tàng')])
        selected={stop.place_id for day in result.itinerary for stop in day.stops}
        self.assertNotIn('Khu công nghệ cao Đà Nẵng',selected)
        self.assertIn('Bảo tàng',selected)

    async def test_popular_stop_is_preferred_when_time_capacity_is_limited(self):
        points=[place('a_obscure',duration=180),place('z_famous',duration=180)]
        points[0].popularity_score=Fact[float].model_validate(fact(5))
        points[1].popularity_score=Fact[float].model_validate(fact(90))
        result=await self.agent.plan(self.trip,points)
        self.assertEqual(result.itinerary[0].stops[0].place_id,'z_famous')

    async def test_age_limits_and_child_pacing(self):
        values = self.trip.model_dump(mode="json")
        values["traveler_ages"] = [fact(35), fact(5)]
        request = TripRequest.model_validate(values)
        points = [place("restricted", minimum_age=12)] + [place(f"p{i}", duration=None) for i in range(5)]
        result = await self.agent.plan(request, points)
        self.assertNotIn("restricted", [stop.place_id for day in result.itinerary for stop in day.stops])
        self.assertLessEqual(len(result.itinerary[0].stops), 4)
        first = result.itinerary[0].stops[0]
        self.assertEqual((first.departure.value.hour*60 + first.departure.value.minute) - (first.arrival.value.hour*60 + first.arrival.value.minute), 60)

    async def test_child_advisory_is_not_ignored(self):
        values = self.trip.model_dump(mode="json")
        values["traveler_ages"] = [fact(35), fact(5)]
        result = await self.agent.plan(TripRequest.model_validate(values), [place("bad", age_notes=["Không phù hợp trẻ nhỏ"]), place("good")])
        self.assertEqual([stop.place_id for day in result.itinerary for stop in day.stops], ["good"])

    async def test_unknown_hours_are_provisional_but_complex_hours_are_not_assumed_open(self):
        result = await self.agent.plan(self.trip, [place("unknown", hours=None), place("complex", hours="PH off; Mo-Su 09:00-17:00")])
        self.assertEqual(result.itinerary[0].stops[0].place_id, "unknown")
        self.assertLessEqual(result.itinerary[0].stops[0].arrival.confidence, 0.3)
        self.assertTrue(result.warnings)
        self.assertTrue(any(item.place_id == "complex" for item in result.unplanned))

    async def test_unknown_ages_do_not_pass_explicit_adult_restriction(self):
        values = self.trip.model_dump(mode="json")
        values["traveler_ages"] = []
        result = await self.agent.plan(TripRequest.model_validate(values), [place("adult", minimum_age=18)])
        self.assertEqual(result.itinerary, [])
        self.assertEqual(result.missing_days, [1])

    async def test_incomplete_day_coverage_blocks_budget_task(self):
        values = self.trip.model_dump(mode="json")
        values["days"] = fact(3)
        request = TripRequest.model_validate(values)
        self.db.create_project("test-planner", "Test", "Planner", trip_request=request)
        tasks = build_tasks("test-planner")
        tasks[0]["status"], tasks[1]["status"] = "done", "done"
        self.db.replace_tasks("test-planner", tasks)
        update = await self.agent.run({"project_id": "test-planner", "trip_request": request, "places": [place("only")]})
        self.assertEqual(len(update["itinerary"]), 1)
        self.assertEqual(self.db.get_project("test-planner")["status"], "needs_attention")
        self.assertEqual(next(task for task in self.db.get_tasks("test-planner") if task["agent"] == "budget")["status"], "todo")

    async def test_journey_boundaries_and_full_day_family_schedule(self):
        from backend.app.services.day_schedule import minute
        data=self.trip.model_dump(mode='json')
        data.update(days=fact(3),departure=fact('Hà Nội'),traveler_ages=[fact(35),fact(6)],
            outbound_departure_at=fact('2026-11-23T08:00:00+07:00'),
            destination_arrival_at=fact('2026-11-23T10:00:00+07:00'),
            destination_departure_at=fact('2026-11-25T18:00:00+07:00'),
            return_arrival_at=fact('2026-11-25T19:30:00+07:00'))
        result=await self.agent.plan(TripRequest.model_validate(data),[place(f'p{i}',duration=90) for i in range(15)])
        self.assertEqual(len(result.itinerary),3)
        for day in result.itinerary:
            DayPlan.model_validate_json(day.model_dump_json())
            kinds={item.kind for item in day.activities}
            self.assertTrue({'lunch','rest','hotel'} <= kinds)
            self.assertNotIn('Hà Nội',[s.place_id for s in day.stops])
        first,last=result.itinerary[0],result.itinerary[-1]
        self.assertGreaterEqual(minute(first.stops[0].arrival.value)-first.stops[0].travel_minutes.value,690)
        self.assertLessEqual(minute(last.stops[-1].departure.value),900)
        middle=result.itinerary[1]
        self.assertIn('dinner',{a.kind for a in middle.activities})
        self.assertEqual(middle.activities[-1].end.value.strftime('%H:%M'),'21:00')
        self.assertGreaterEqual(minute(middle.stops[-1].departure.value),16*60)

    async def test_duration_depends_on_place_kind_and_keeps_sourced_duration(self):
        from backend.app.services.day_schedule import visit_duration
        for kind,expected in [('museum',90),('bridge',45),('beach',120),('theme_park',360),('heritage_site',150)]:
            point=place(kind,duration=None)
            point.tourism_kind=Fact[str].model_validate(fact(kind))
            self.assertEqual(visit_duration(point,60),expected)
            point.visit_minutes=Fact[int].model_validate(fact(135))
            self.assertEqual(visit_duration(point,60),135)

    async def test_long_theme_park_keeps_on_site_meals_and_rest(self):
        point=place('theme park',hours='09:00-18:00',duration=None)
        point.tourism_kind=Fact[str].model_validate(fact('theme_park'))
        result=await self.agent.plan(self.trip,[point])
        day=result.itinerary[0]
        self.assertEqual(len(day.stops),1)
        self.assertEqual({a.kind for a in day.activities if a.within_place_id==point.id},{'lunch','rest'})
        DayPlan.model_validate_json(day.model_dump_json())

    async def test_early_return_day_has_no_fake_attractions(self):
        data=self.trip.model_dump(mode='json')
        data.update(days=fact(2),destination_departure_at=fact('2026-11-24T09:00:00+07:00'))
        result=await self.agent.plan(TripRequest.model_validate(data),[place('a'),place('b')])
        last=result.itinerary[-1]
        self.assertEqual(last.day_number,2)
        self.assertEqual(last.stops,[])
        self.assertTrue(any(a.kind=='terminal' for a in last.activities))

    async def test_missing_journey_times_require_clarification_not_invented_hours(self):
        data=self.trip.model_dump(mode='json')
        data['departure']=fact('Hà Nội')
        result=await self.agent.plan(TripRequest.model_validate(data),[place('museum')])
        self.assertEqual(result.status,'needs_attention')
        self.assertEqual(result.itinerary,[])
        data['transport_times_flexible']=fact(True)
        result=await self.agent.plan(TripRequest.model_validate(data),[place('museum')])
        self.assertTrue(result.itinerary)
        self.assertTrue(any('Chưa chốt giờ' in w.value for w in result.warnings))

    def test_supported_hours_and_overrides(self):
        from datetime import date
        rules = parse_opening_hours("Mo-Fr 09:00-12:00,13:00-17:00; We off")
        self.assertEqual(rules.windows(date(2026, 11, 23)), [(540, 720), (780, 1020)])
        self.assertEqual(rules.windows(date(2026, 11, 25)), [])
        with self.assertRaises(UnsupportedHours):
            parse_opening_hours("Mo-Fr 20:00-02:00")
