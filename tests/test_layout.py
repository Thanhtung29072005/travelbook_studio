import json
import tempfile
import unittest
from pathlib import Path

from pypdf import PdfReader

from backend.app.services.layout import LayoutService

FIXTURES = Path(__file__).resolve().parents[1] / "backend/data/artifacts/danang_3days"


class LayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory()
        cls.service = LayoutService()
        cls.output = Path(cls.directory.name) / "travelbook.pdf"
        cls.service.build_from_json(FIXTURES, cls.output)
        cls.reader = PdfReader(cls.output)
        cls.texts = [page.extract_text() for page in cls.reader.pages]
        cls.manifest = json.loads(cls.output.with_suffix(".manifest.json").read_text(encoding="utf-8"))

    @classmethod
    def tearDownClass(cls):
        cls.reader.close()
        cls.directory.cleanup()

    def args(self):
        read = lambda name: json.loads((FIXTURES / name).read_text(encoding="utf-8"))
        return dict(days=read("itinerary.json"), places=read("research.json"),
                    budget=read("budget.json"), content=read("content.json"), photos=read("photos.json"),
                    maps={number: FIXTURES / f"maps/day{number}.png" for number in (1, 2, 3)},
                    charts={name: FIXTURES / f"charts/{name}.png" for name in ("budget", "weather", "timeline")})

    def test_required_sections_and_all_folios(self):
        sections = {item["id"]: item["page"] for item in self.manifest["sections"]}
        self.assertTrue({"cover", "overview", "day_1", "day_2", "day_3", "budget", "weather", "timeline"} <= sections.keys())
        self.assertEqual(len(self.reader.pages), self.manifest["page_count"])
        for index, text in enumerate(self.texts, 1):
            self.assertIn(f"Trang {index} / {len(self.texts)}", text)

    def test_vietnamese_text_and_budget_survive_pdf_extraction(self):
        full = "\n".join(self.texts)
        for text in ("Đà Nẵng", "Bãi biển Mỹ Khê", "3 700 000", "5 000 000"):
            self.assertIn(text, full)
        self.assertNotIn("\ufffd", full)

    def test_maps_and_charts_are_embedded_not_just_linked(self):
        sections = {item["id"]: item["page"] for item in self.manifest["sections"]}
        for key in ("day_1", "day_2", "day_3", "budget", "weather", "timeline"):
            self.assertGreaterEqual(len(self.reader.pages[sections[key] - 1].images), 1)

    def test_unused_photos_are_omitted_from_reader_pages(self):
        self.assertEqual(self.manifest["embedded_photo_ids"], [])
        self.assertEqual(len(self.manifest["omitted_photo_ids"]), 8)
        sections = [item for item in self.manifest["sections"] if item["id"].startswith("photo_sources_")]
        self.assertEqual(sections, [])
        full = "\n".join(self.texts)
        for internal_text in ("UNVERIFIED", "example.invalid", "confidence", "BẢN PHÁC THẢO", "Nguồn dữ liệu và giới hạn", "ĐTC*"):
            self.assertNotIn(internal_text, full)

    def test_missing_assets_and_unknown_place_do_not_replace_existing_pdf(self):
        original = self.output.read_bytes()
        args = self.args()
        args["maps"][1] = FIXTURES / "does-not-exist.png"
        with self.assertRaises(FileNotFoundError):
            self.service.build_pdf(self.output, **args)
        self.assertEqual(self.output.read_bytes(), original)
        args = self.args()
        args["days"][0]["stops"][0]["place_id"] = "missing"
        with self.assertRaises(ValueError):
            self.service.build_pdf(self.output, **args)

    def test_user_text_is_escaped_and_long_text_paginated(self):
        args = self.args()
        args["content"] = [{"value": "<b>Literal & safe</b> " + "Nội dung dài. " * 650,
                            "source": "fixture:long-content-test", "confidence": 1}]
        path = Path(self.directory.name) / "long.pdf"
        self.service.build_pdf(path, title="Đà Nẵng <demo> & hành trình", **args)
        reader = PdfReader(path)
        try:
            self.assertIn("<demo> & hành trình", reader.pages[0].extract_text())
            full = "\n".join(page.extract_text() for page in reader.pages)
            self.assertIn("<b>Literal & safe</b>", full)
            self.assertGreater(len(reader.pages), len(self.reader.pages))
            for number, page in enumerate(reader.pages, 1):
                self.assertIn(f"Trang {number} / {len(reader.pages)}", page.extract_text())
        finally:
            reader.close()

    def test_schedule_meals_hotel_and_journey_times_appear_in_pdf(self):
        from backend.app.schemas import TripRequest, DayPlan
        from backend.app.services.day_schedule import day_frame
        from datetime import date, timedelta
        f=lambda value:{'value':value,'source':'synthetic:export-verification','confidence':.5}
        trip=TripRequest(destination=f('Đà Nẵng'),days=f(3),travelers=f(2),budget=f('5000000'),currency=f('VND'),
            start_date=f('2026-11-26'),departure=f('Hà Nội'),transport_mode=f('Máy bay'),
            outbound_departure_at=f('2026-11-26T08:30:00+07:00'),destination_arrival_at=f('2026-11-26T10:00:00+07:00'),
            destination_departure_at=f('2026-11-28T18:00:00+07:00'),return_arrival_at=f('2026-11-28T19:30:00+07:00'))
        args=self.args()
        for i,day in enumerate(args['days']):
            stamp=date(2026,11,26)+timedelta(days=i)
            day['date']=f(stamp.isoformat())
            day['stops']=day['stops'][:1]
            day['stops'][0].update(arrival=f('14:30' if i==0 else '09:15'),departure=f('16:00' if i==0 else '10:45'),travel_minutes=f(30))
            day['activities']=[item.model_dump(mode='json') for item in day_frame(trip,stamp,480,1260)[2]]
            DayPlan.model_validate(day)
        path=Path(self.directory.name)/'schedule.pdf'
        self.service.build_pdf(path,trip_request=trip,with_toc=True,**args)
        reader=PdfReader(path)
        try:
            full='\n'.join(page.extract_text() for page in reader.pages)
            for phrase in ('Ăn trưa','Ăn tối','Nghỉ trưa','chưa chọn khách sạn','Rời nơi khởi hành','Đến điểm du lịch','19:30 - 28/11/2026'):
                self.assertIn(phrase,full)
        finally:
            reader.close()


if __name__ == "__main__":
    unittest.main()
