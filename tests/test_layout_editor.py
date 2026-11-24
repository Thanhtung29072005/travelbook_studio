import asyncio
from datetime import date, timedelta
import json
from pathlib import Path
import tempfile
import unittest
from copy import deepcopy

from PIL import Image
from PIL.PngImagePlugin import PngInfo
from pypdf import PdfReader

from backend.app.agents.layout_editor import LayoutEditorAgent
from backend.app.config import Settings
from backend.app.db import Database
from backend.app.schemas import Budget, BudgetItem, Fact, Review, ReviewIssue
from backend.app.services.layout import LayoutService
from backend.app.task_plan import build_tasks
from scripts.check_layout_editor import sample_state


class LayoutEditorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = Settings(_env_file=None)
        self.db = Database(self.root / "db.sqlite3")
        self.db.init()
        self.state = sample_state(self.root / "layout-test", "layout-test")
        self.agent = LayoutEditorAgent(self.db, self.settings, self.root)
        self.db.create_project("layout-test", "Layout test", "fixture")
        tasks = build_tasks("layout-test")
        for task in tasks[:9]: task["status"] = "done"
        tasks[9]["status"] = "ready"
        self.db.replace_tasks("layout-test", tasks)

    def test_real_agent_exports_clean_book_and_retains_review_in_manifest(self):
        self.state["content_document"].tips[0] = Fact[str](
            value="Gợi ý chưa xác minh: Mang theo nước uống.",
            source="llm:test; editorial-suggestion-unverified", confidence=.3)
        self.state["content_document"].warnings.append(Fact[str](
            value="Nội dung do LLM biên tập, cần kiểm duyệt.",
            source="policy:content-editor-review-required", confidence=1))
        update = asyncio.run(self.agent.run(self.state))
        manifest = json.loads(Path(update["artifacts"][1].path).read_text(encoding="utf-8"))
        with PdfReader(update["final_pdf"].path) as reader:
            texts = [page.extract_text() for page in reader.pages]
            full = "\n".join(texts)
            for expected in ("Mục lục", "Mẹo", "Văn hóa", "Giá có nguồn", "Đà Nẵng"):
                self.assertIn(expected, full)
            for internal_text in ("[Nên kiểm tra lại", "confidence", "BẢN PHÁC THẢO", "ĐTC*", "Dữ kiện nên kiểm tra lại", "Nguồn dữ liệu và giới hạn"):
                self.assertNotIn(internal_text, full)
            self.assertIn("Mang theo nước uống.", full)
            self.assertNotIn("Gợi ý chưa xác minh:", full)
            self.assertNotIn("Nội dung do LLM", full)
            self.assertNotIn("\ufffd", full)
            for index, text in enumerate(texts, 1):
                self.assertIn(f"Trang {index} / {len(texts)}", text)
            for entry in manifest["toc"]:
                self.assertIn(entry["title"], texts[entry["page"]-1])
            toc_index = next(entry["page"]-1 for entry in manifest["sections"] if entry["id"] == "toc")
            self.assertIn("Tổng quan hành trình", texts[toc_index])
            self.assertIn("Ngân sách dự kiến", texts[toc_index])
        self.assertTrue(manifest["low_confidence_facts"])
        self.assertEqual(manifest["text_image_overlap_count"], 0)
        self.assertTrue([box for box in manifest["layout_boxes"] if box["kind"] == "image"])
        self.assertEqual(next(task for task in self.db.get_tasks("layout-test") if task["id"].endswith(":pm_finalize"))["status"], "ready")
        self.assertFalse(update["production_approved"])

    def test_overview_follows_contents_on_separate_page_with_dates_and_group_budget(self):
        self.state['trip_request']=self.state['trip_request'].model_copy(update={'start_date':Fact(value=date(2026,12,23),source='user:test-date',confidence=1)})
        for index,day in enumerate(self.state['itinerary']):
            day['date']={'value':(date(2026,12,23)+timedelta(days=index)).isoformat(),'source':'user:test-date','confidence':1}
        artifacts,manifest=self.agent.render(self.state)
        pages={item['id']:item['page'] for item in manifest['sections']}
        self.assertEqual(pages['toc'],2)
        self.assertEqual(pages['overview'],3)
        self.assertGreater(pages['day_1'],pages['overview'])
        with PdfReader(artifacts[0].path) as reader:
            toc=reader.pages[pages['toc']-1].extract_text()
            overview=reader.pages[pages['overview']-1].extract_text()
            self.assertNotIn('THÔNG TIN CHUYẾN ĐI',toc)
            for value in ('23/12/2026','25/12/2026','Ngân sách cả nhóm','Dự toán bình quân/người','5 000 000','Lịch trình rút gọn'):
                self.assertIn(value,overview)

    def test_local_photo_embedded_with_caption_without_covering_text(self):
        # Synthetic pixels and metadata exercise embedding only; not a real licensed photo.
        photo = self.state["photos"][0]
        path = self.root / "layout-test" / "test-photo.png"
        Image.new("RGB", (1400, 400), "teal").save(path)
        for key, value in (("author", "Synthetic test photographer"), ("license", "CC BY-SA 4.0"),
                           ("license_url", "https://creativecommons.org/licenses/by-sa/4.0/"), ("source_url", "https://commons.wikimedia.org/wiki/File:Test.png"),
                           ("url", "https://upload.wikimedia.org/test.png")):
            photo[key] = {"value": value, "source": "fixture:embedding-test", "confidence": 1}
        photo.update(local_path=str(path), provider="wikimedia")
        artifacts, manifest = self.agent.render(self.state)
        self.assertIn(photo["id"], manifest["embedded_photo_ids"])
        self.assertEqual(manifest["text_image_overlap_count"], 0)
        with PdfReader(artifacts[0].path) as reader:
            full = "\n".join(page.extract_text() for page in reader.pages)
            self.assertIn("Synthetic test photographer", full)
            self.assertNotIn("Nguồn ảnh và giấy phép", full)
            links = [annotation.get_object().get("/A", {}).get("/URI") for page in reader.pages for annotation in page.get("/Annots", [])]
            self.assertIn(photo["source_url"]["value"], links)
            self.assertIn(photo["license_url"]["value"], links)
        self.assertTrue(manifest["photo_credits"])

    def test_official_price_evidence_is_visible_and_clickable(self):
        # Synthetic evidence validates PDF plumbing, not a real published tariff.
        quote = {'amount': '100000', 'currency': 'VND', 'audience': 'adult',
                 'source_url': 'https://museum.example/gia-ve', 'retrieved_at': '2026-11-23T12:00:00+00:00',
                 'excerpt': 'Vé người lớn: 100.000 đồng/người (dữ liệu kiểm thử)', 'conditions': 'Giá kiểm thử bố cục'}
        item = self.state['budget']['items'][0]
        item['price_evidence'] = quote
        item['price_kind'] = 'sourced'
        item['amount']['source'] = 'https://museum.example/gia-ve; official-admission'
        artifacts, manifest = self.agent.render(self.state)
        self.assertEqual(manifest['price_evidence'][0]['excerpt'], quote['excerpt'])
        self.assertEqual(manifest['text_image_overlap_count'], 0)
        with PdfReader(artifacts[0].path) as reader:
            full = '\n'.join(page.extract_text() for page in reader.pages)
            self.assertIn('Nguồn giá đã tra', full)
            self.assertIn('Tra ngày 23/11/2026', full)
            links = [annotation.get_object().get('/A', {}).get('/URI') for page in reader.pages for annotation in page.get('/Annots', [])]
            self.assertIn(quote['source_url'], links)
    def test_ai_cover_and_day_illustrations_are_embedded(self):
        folder = self.root / "layout-test" / "illustrations"
        folder.mkdir(parents=True, exist_ok=True)
        self.state["illustrations"] = []
        for key in ("cover", "day_1", "day_2", "day_3"):
            path = folder / (key + ".png")
            Image.new("RGB", (900, 600), "teal").save(path)
            self.state["illustrations"].append({"id": key, "project_id": "layout-test", "agent_id": "illustrator",
                "kind": "illustration", "name": path.name, "path": str(path), "mime_type": "image/png",
                "source": "generation:test", "confidence": .6})
        artifacts, manifest = self.agent.render(self.state)
        self.assertEqual(len(manifest["embedded_illustration_paths"]), 4)
        self.assertFalse(any(item["id"].startswith("photo_sources") for item in manifest["sections"]))
        with PdfReader(artifacts[0].path) as reader:
            full = "\n".join(page.extract_text() for page in reader.pages)
            self.assertNotIn("Minh họa AI", full)
            self.assertNotIn("Lưu ý trong ngày", full)

    def test_destination_guide_sources_are_clickable_below_editorial_content(self):
        self.state['content_document'].food=[Fact(value='Bún bò Huế: món bún địa phương.',
            source='https://vi.wikipedia.org/wiki/Hu%E1%BA%BF; scope=destination-guide:food',confidence=.6)]
        artifacts,manifest=self.agent.render(self.state)
        with PdfReader(artifacts[0].path) as reader:
            full='\n'.join(page.extract_text() for page in reader.pages)
            self.assertIn('Bún bò Huế',full)
            self.assertIn('Thông tin tham khảo',full)
            links=[annotation.get_object().get('/A',{}).get('/URI') for page in reader.pages for annotation in page.get('/Annots',[])]
            self.assertIn('https://vi.wikipedia.org/wiki/Hu%E1%BA%BF',links)
        self.assertEqual(manifest['text_image_overlap_count'],0)

    def test_real_map_gets_a_readable_page_and_clickable_attribution(self):
        path = self.root/'layout-test'/'day1.png'
        metadata = PngInfo()
        metadata.add_text('Data', json.dumps({'basemap': 'openstreetmap', 'route_type': 'road_geometry'}))
        Image.new('RGB', (1200, 800), '#dce8d2').save(path, pnginfo=metadata)
        self.state['maps'][0] = self.state['maps'][0].model_copy(update={'path': str(path)})
        artifacts, manifest = self.agent.render(self.state)
        self.assertEqual(manifest['text_image_overlap_count'], 0)
        with PdfReader(artifacts[0].path) as reader:
            page = next(page for page in reader.pages if 'Bản đồ hành trình - Ngày 1' in page.extract_text())
            self.assertIn('Tuyến đường tham khảo từ OSRM', page.extract_text())
            links = [annotation.get_object().get('/A', {}).get('/URI') for annotation in page.get('/Annots', [])]
            self.assertIn('https://www.openstreetmap.org/copyright', links)

    def test_over_budget_export_is_blocked_until_repairs_are_exhausted(self):
        original=Budget.model_validate(self.state['budget'])
        extra=BudgetItem(id='extra',category='other',description=Fact(value='Khoản chi đã chốt',source='user:test',confidence=1),
            amount=Fact(value=6000000,source='user:test',confidence=1),price_kind='estimated')
        self.state['budget']=Budget(currency=original.currency,limit=original.limit,items=[*original.items,extra])
        with self.assertRaisesRegex(ValueError,'budget repair loop'):
            self.agent.render(self.state)
        self.state.update(revision_exhausted=True,revision_count=self.settings.max_revisions,
            revision_warnings=[Fact(value='Đã hết vòng sửa',source='policy:MAX_REVISIONS',confidence=1)])
        artifacts,manifest=self.agent.render(self.state)
        self.assertIsNotNone(manifest['budget_savings'])
        self.assertTrue(any(section['id']=='savings_plan' for section in manifest['sections']))
        with PdfReader(artifacts[0].path) as reader:
            cover=reader.pages[0].extract_text()
            full='\n'.join(page.extract_text() for page in reader.pages)
            self.assertIn('Dự toán vượt ngân sách',cover)
            self.assertIn('Phương án tiết kiệm',cover)
            self.assertIn('MỨC MỤC TIÊU',full)
            self.assertIn('chưa phải báo giá',full)
            self.assertIn('vẫn vượt',full)

    def test_long_content_wraps_and_paginates_with_toc_still_correct(self):
        self.state["content_document"].days[0].introduction.value = "Mẹo an toàn <b>giữ nguyên</b>. " * 160
        artifacts, manifest = self.agent.render(self.state)
        self.assertEqual(manifest["text_image_overlap_count"], 0)
        with PdfReader(artifacts[0].path) as reader:
            full = "\n".join(page.extract_text() for page in reader.pages)
            self.assertIn("<b>giữ nguyên</b>", full)
            for entry in manifest["toc"]:
                self.assertIn(entry["title"], reader.pages[entry["page"]-1].extract_text())

    def test_missing_map_fails_and_blocks_layout_task(self):
        self.state["maps"] = self.state["maps"][:-1]
        with self.assertRaises(ValueError): asyncio.run(self.agent.run(self.state))
        self.assertEqual(self.db.get_project("layout-test")["status"], "failed")
        self.assertEqual(next(task for task in self.db.get_tasks("layout-test") if task["agent"] == "layout_editor")["status"], "blocked")

    def test_daily_notes_are_omitted_without_overlapping_text(self):
        self.state["itinerary"][0]["stops"][0]["notes"].append({
            "value": "Ghi chú hành trình cần giữ đầy đủ. " * 260,
            "source": "user:long-route-note", "confidence": 1})
        artifacts, manifest = self.agent.render(self.state)
        self.assertEqual(manifest["text_image_overlap_count"], 0)
        with PdfReader(artifacts[0].path) as reader:
            full = "\n".join(page.extract_text() for page in reader.pages)
            self.assertNotIn("Ghi chú hành trình cần giữ đầy đủ.", full)
            self.assertNotIn("Lưu ý trong ngày", full)
            self.assertIn("Sơ đồ thứ tự tham quan", full)
            for entry in manifest["toc"]:
                self.assertIn(entry["title"], reader.pages[entry["page"]-1].extract_text())

    def test_unresolved_review_requires_exhaustion_warning(self):
        fact = Fact[str](value="Pending error", source="test:layout-gate", confidence=1)
        self.state["review"] = Review(verdict="REVISE", source="test:layout-gate", confidence=1, summary=fact,
            issues=[ReviewIssue(category="photo", severity="major", target_agent="image_curator", message=fact)])
        with self.assertRaises(ValueError): asyncio.run(self.agent.run(self.state))
        self.state.update(revision_exhausted=True, revision_warnings=[fact])
        update = asyncio.run(self.agent.run(self.state))
        with PdfReader(update["final_pdf"].path) as reader:
            self.assertNotIn("Pending error", "\n".join(page.extract_text() for page in reader.pages))
        manifest = json.loads(Path(update["artifacts"][1].path).read_text(encoding="utf-8"))
        self.assertEqual(manifest["revision_warnings"][0]["value"], "Pending error")

    def test_mismatched_content_and_outside_asset_rejected(self):
        self.state["content_document"].days = self.state["content_document"].days[:2]
        with self.assertRaises(ValueError): self.agent.render(self.state)
        self.state = sample_state(self.root / "another", "layout-test")
        self.state["maps"][0].project_id = "wrong-project"
        with self.assertRaises(ValueError): self.agent.render(self.state)

    def test_numeric_weather_and_low_confidence_forecast_preserve_sources(self):
        self.state["weather"] = {"status": "partial", "daily": [{"day_number": 1,
            "temperature_min": {"value": 22, "source": "test:weather-provider", "confidence": .5},
            "temperature_max": {"value": 28, "source": "test:weather-provider", "confidence": .8},
            "rain_probability": {"value": 45, "source": "test:weather-provider", "confidence": .5}}]}
        artifacts, manifest = self.agent.render(self.state)
        self.assertTrue(any(fact["source"] == "test:weather-provider" for fact in manifest["low_confidence_facts"]))
        with PdfReader(artifacts[0].path) as reader:
            full = "\n".join(page.extract_text() for page in reader.pages)
            self.assertIn("Xác suất mưa (%)", full)
            self.assertNotIn("Trạng thái thời tiết: partial", full)

    def test_overlap_guard_rejects_deliberately_displaced_images(self):
        class OverlappingLayout(LayoutService):
            def _image(self, *args, **kwargs):
                image = super()._image(*args, **kwargs)
                original = image.drawOn
                image.drawOn = lambda canvas, x, y, _sW=0: original(canvas, x, y+120, _sW)
                return image
        self.agent.service = OverlappingLayout()
        with self.assertRaisesRegex(ValueError, "text/image overlap"):
            self.agent.render(self.state)
