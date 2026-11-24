import json
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app.attribution import attribution_record, attribution_text, commons_license, plain_text
from backend.app.config import Settings
from backend.app.schemas import Place
from backend.app.services.photos import PhotoService
from backend.app.services.public_api import PublicAPIClient
from scripts.check_image_curator import mock_commons


def fact(value):
    return {"value": value, "source": "fixture:photo-test", "confidence": 1}


def sample_place():
    return Place(id="my_khe", name=fact("Mỹ Khê"), latitude=fact(16), longitude=fact(108))


def unsplash_result():
    return {"results": [{"id": "u1", "description": "Mỹ Khê beach", "alt_description": "Sunrise",
        "width": 1600, "height": 900, "urls": {"regular": "https://images.unsplash.com/photo-u1?ixid=keep-me&w=1080"},
        "links": {"html": "https://unsplash.com/photos/u1", "download_location": "https://api.unsplash.com/photos/u1/download?ixid=keep-me"},
        "user": {"name": "Photo Author", "links": {"html": "https://unsplash.com/@author"}}}]}


def pexels_result():
    return {"photos": [{"id": 7, "alt": "Mỹ Khê beach", "width": 1600, "height": 900,
        "src": {"original": "https://images.pexels.com/photos/7/photo.jpg"},
        "url": "https://www.pexels.com/photo/my-khe-7/", "photographer": "Author P",
        "photographer_url": "https://www.pexels.com/@author/"}]}


class PhotoTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = Settings(_env_file=None, data_cache_path=self.root / "cache.db",
            unsplash_access_key="UNSPLASH-secret-test", pexels_api_key="PEXELS-secret-test")
        self.calls = []
        self.responses = {"commons.wikimedia.org": mock_commons(sample_place().model_dump(mode="json")),
            "api.unsplash.com": unsplash_result(), "api.pexels.com": pexels_result()}
        self.tracking_status = 200
        def handler(request):
            self.calls.append(request)
            if "/download" in request.url.path:
                return httpx.Response(self.tracking_status, json={"url": "https://images.unsplash.com/photo-u1"})
            response = self.responses[request.url.host]
            return httpx.Response(response if type(response) is int else 200, json={} if type(response) is int else response)
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(self.http.aclose)
        self.service = PhotoService(self.settings, PublicAPIClient(self.settings, client=self.http))
        self.place = sample_place()

    async def test_commons_preserves_author_credit_license_and_attribution(self):
        item = self.responses["commons.wikimedia.org"]["query"]["pages"][0]["imageinfo"][0]
        item["extmetadata"]["Artist"]["value"] = '<a href="/wiki/User:Creator">Real Creator</a><script>bad()</script>'
        item["extmetadata"]["Attribution"] = {"value": "Required custom attribution"}
        result = await self.service.select(self.place)
        photo = result.selected
        self.assertEqual(photo.author.value, "Real Creator")
        self.assertEqual(photo.license.value, "CC BY-SA 4.0")
        self.assertEqual(str(photo.author_url.value), "https://commons.wikimedia.org/wiki/User:Creator")
        credit = attribution_record(photo)
        self.assertIn("Required custom attribution", credit.credit_line.value)
        self.assertIn("creativecommons.org/licenses/by-sa/4.0", attribution_text([credit]))
        self.assertTrue(any("ShareAlike" in note.value for note in credit.notes))
        self.assertIsNone(photo.local_path)

    async def test_commons_thumbnail_host_is_accepted(self):
        item = self.responses["commons.wikimedia.org"]["query"]["pages"][0]["imageinfo"][0]
        item["thumburl"] = "https://thumb.wikimedia.org/wikipedia/commons/thumb/test.jpg/1280px-test.jpg"
        result = await self.service.select(self.place)
        self.assertIsNotNone(result.selected)
        self.assertEqual(str(result.selected.url.value), item["thumburl"])

    async def test_unknown_license_and_missing_author_are_not_accepted(self):
        item = self.responses["commons.wikimedia.org"]["query"]["pages"][0]["imageinfo"][0]
        item["extmetadata"]["Artist"]["value"] = ""
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "wikimedia"})
        self.assertIsNone((await self.service.select(self.place)).selected)
        with self.assertRaises(ValueError):
            commons_license("CC BY-SA 4.0", "https://creativecommons.org/licenses/by-nc/4.0/")
        with self.assertRaises(ValueError):
            commons_license("CC BY 4.0", "https://creativecommons.org/licenses/by-sa/4.0/")

    async def test_unsplash_keeps_hotlink_tracks_selection_and_does_not_cache_tracking(self):
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "unsplash"})
        first, second = await self.service.select(self.place), await self.service.select(self.place)
        self.assertEqual(first.selected.provider, "unsplash")
        self.assertEqual(str(first.selected.url.value), unsplash_result()["results"][0]["urls"]["regular"])
        self.assertIn("utm_source=travelbook_studio", str(first.selected.author_url.value))
        self.assertEqual(len([request for request in self.calls if "/search/" in request.url.path]), 1)
        self.assertEqual(len([request for request in self.calls if "/download" in request.url.path]), 2)
        self.assertTrue(all(request.headers["Authorization"] == "Client-ID UNSPLASH-secret-test" for request in self.calls))
        data = (self.root / "cache.db").read_bytes()
        self.assertNotIn(b"UNSPLASH-secret-test", data)

    async def test_failed_unsplash_tracking_falls_back_to_pexels(self):
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "unsplash,pexels"})
        self.tracking_status = 503
        result = await self.service.select(self.place)
        self.assertEqual(result.selected.provider, "pexels")
        self.assertEqual(result.selected.license.value, "Pexels License")
        self.assertTrue(result.warnings)
        self.assertEqual(self.calls[-1].headers["Authorization"], "PEXELS-secret-test")

    async def test_foreign_tracking_url_is_rejected_without_sending_key(self):
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "unsplash"})
        self.responses["api.unsplash.com"]["results"][0]["links"]["download_location"] = "https://evil.example/photos/u1/download"
        self.assertIsNone((await self.service.select(self.place)).selected)
        self.assertEqual(len(self.calls), 1)

    async def test_irrelevant_results_do_not_count(self):
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "wikimedia"})
        page = self.responses["commons.wikimedia.org"]["query"]["pages"][0]
        page["title"] = "File:Unrelated mountain.jpg"
        self.assertIsNone((await self.service.select(self.place)).selected)

    async def test_tiny_or_non_image_results_do_not_count(self):
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "wikimedia"})
        page = self.responses["commons.wikimedia.org"]["query"]["pages"][0]
        page["imageinfo"][0]["width"] = 300
        second = json.loads(json.dumps(page))
        second["pageid"] = 2
        second["imageinfo"][0].update(width=1600, mime="image/svg+xml")
        self.responses["commons.wikimedia.org"]["query"]["pages"].append(second)
        self.assertIsNone((await self.service.select(self.place)).selected)

    async def test_same_provider_photo_not_reused_for_two_places(self):
        used = set()
        first = await self.service.select(self.place, used=used)
        self.assertIsNotNone(first.selected)
        self.service.settings = self.settings.model_copy(update={"photo_provider_order": "wikimedia"})
        self.assertIsNone((await self.service.select(self.place, used=used)).selected)

    async def test_api_errors_and_absent_keys_return_missing_without_placeholder(self):
        self.responses["commons.wikimedia.org"] = 503
        self.service.settings = self.settings.model_copy(update={"unsplash_access_key": Settings(_env_file=None).unsplash_access_key,
            "pexels_api_key": Settings(_env_file=None).pexels_api_key})
        result = await self.service.select(self.place)
        self.assertIsNone(result.selected)
        self.assertEqual(len(self.calls), 1)
        self.assertTrue(result.warnings)

    async def test_malformed_rows_are_skipped_and_next_provider_is_used(self):
        self.responses["commons.wikimedia.org"] = {"query": {"pages": [{"imageinfo": []}]}}
        self.responses["api.unsplash.com"] = {"results": [{"id": "bad"}]}
        result = await self.service.select(self.place)
        self.assertEqual(result.selected.provider, "pexels")
        self.assertNotIn(b"PEXELS-secret-test", (self.root / "cache.db").read_bytes())

    def test_plain_text_ignores_active_html_and_keeps_entities(self):
        self.assertEqual(plain_text('<script>bad()</script><b>A &amp; B</b>'), "A & B")
