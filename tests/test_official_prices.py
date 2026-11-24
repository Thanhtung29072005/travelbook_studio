import json
from datetime import date
from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend.app.config import Settings
from backend.app.schemas import Place, TripRequest, Fact, AdmissionQuote
from backend.app.services.public_api import PublicAPIClient, DataServiceError
from backend.app.services.official_prices import (OfficialPageReader, OfficialPriceService,
    OfficialPage, PriceSelection, verify_selection, parse_vnd, validate_public_url)


def f(value):
    return Fact(value=value, source='user:test', confidence=1)


class PriceEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.trip = TripRequest(destination=f('Huế'), days=f(3), budget=f(10000000), currency=f('VND'), travelers=f(2), start_date=f(date(2026, 12, 23)))
        self.place = Place(id='wikidata:Q123', name=f('Bảo tàng Hoàng gia'), latitude=f(16), longitude=f(107))
        self.text = 'Bảo tàng Hoàng gia\nVé vào cửa người lớn: 150.000 đồng/người\nTrẻ em: 50.000 đồng/người'
        self.pages = [OfficialPage('https://museum.example/gia-ve', '2026-11-23T12:00:00+00:00', self.text, [])]
        self.selection = PriceSelection(page_index=0, excerpt='Vé vào cửa người lớn: 150.000 đồng/người',
            place_excerpt='Bảo tàng Hoàng gia', amount_text='150.000', currency='VND', audience='adult',
            per_person=True, admission_only=True, unconditional=True, rationale='Exact adult admission row')

    def test_vietnamese_price_and_evidence_round_trip(self):
        quote = verify_selection(self.selection, self.pages, self.place, self.trip)
        self.assertEqual(quote.amount, Decimal(150000))
        self.assertEqual(quote.source_url.host, 'museum.example')
        self.assertEqual(AdmissionQuote.model_validate(quote.model_dump(mode='json')), quote)
        self.assertEqual(parse_vnd('150,000'), 150000)
        self.assertEqual(parse_vnd('150 000'), 150000)
        with self.assertRaises(ValueError):
            parse_vnd('150.00')

    def test_hallucinated_amount_excerpt_or_place_is_rejected(self):
        for changes in ({'amount_text': '900.000'}, {'excerpt': 'Vé người lớn: 100.000 đồng'},
                        {'place_excerpt': 'Bảo tàng khác'}, {'currency': 'USD'}, {'per_person': False},
                        {'page_index': 5}, {'audience': 'child'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                verify_selection(self.selection.model_copy(update=changes), self.pages, self.place, self.trip)

    def test_combo_and_ambiguous_prices_rejected(self):
        for text in ('Vé combo người lớn: 150.000 đồng/người',
                     'Vé người lớn: 150.000 đồng; trẻ em: 50.000 đồng',
                     'Vé người lớn quốc tế: 150.000 đồng/người'):
            pages = [OfficialPage(self.pages[0].url, self.pages[0].retrieved_at, 'Bảo tàng Hoàng gia\n'+text, [])]
            with self.subTest(text=text), self.assertRaises(ValueError):
                verify_selection(self.selection.model_copy(update={'excerpt': text}), pages, self.place, self.trip)

    def test_expired_or_unsupported_validity_is_rejected(self):
        with self.assertRaises(ValueError):
            verify_selection(self.selection.model_copy(update={'valid_until': date(2026, 12, 1)}), self.pages, self.place, self.trip)
        with self.assertRaises(ValueError):
            verify_selection(self.selection.model_copy(update={'valid_until': date(2027, 1, 1)}), self.pages, self.place, self.trip)

    def test_valid_date_range_requires_evidence(self):
        text = 'Áp dụng 01/01/2026 - 31/12/2026'
        self.pages[0].text += '\n'+text
        quote = verify_selection(self.selection.model_copy(update={'valid_from': date(2026, 1, 1),
            'valid_until': date(2026, 12, 31), 'validity_excerpt': text}), self.pages, self.place, self.trip)
        self.assertEqual(quote.valid_until, date(2026, 12, 31))

    def test_model_cannot_ignore_explicit_expired_notice(self):
        self.pages[0].text += '\nÁp dụng đến hết 31/12/2025'
        with self.assertRaises(ValueError):
            verify_selection(self.selection, self.pages, self.place, self.trip)


class WebsiteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.settings = Settings(_env_file=None, data_cache_path=Path(temp.name)/'api.sqlite', agent_reasoning_enabled=False)
        self.calls = []
        self.mode = 'ok'
        def handler(request):
            self.calls.append(str(request.url))
            if request.url.host == 'www.wikidata.org':
                return httpx.Response(200, json={'entities': {'Q123': {'id': 'Q123', 'claims': {'P856': [
                    {'mainsnak': {'datavalue': {'value': 'https://museum.example'}}}]}}}})
            if self.mode == 'redirect':
                return httpx.Response(302, headers={'location': 'http://127.0.0.1/admin'})
            if self.mode == 'large':
                return httpx.Response(200, text='a'*2000001, headers={'content-type': 'text/html'})
            if request.url.path == '/gia-ve':
                return httpx.Response(200, text='<h1>Bảo tàng Hoàng gia</h1><p>Vé người lớn: 150.000 đồng/người</p>', headers={'content-type': 'text/html'})
            return httpx.Response(200, text='<script>fake price 9 đồng</script><h1>Bảo tàng Hoàng gia</h1>'
                '<a href="/gia-ve">Giá vé</a><a href="http://127.0.0.1/admin">Giá vé</a>'
                '<a href="https://reseller.example/tickets">Tickets</a>', headers={'content-type': 'text/html'})
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True)
        self.addAsyncCleanup(client.aclose)
        self.api = PublicAPIClient(self.settings, client=client)
        self.reader = OfficialPageReader(self.settings, self.api)
        async def public(url):
            if '127.0.0.1' in url:
                raise DataServiceError('private')
            return url
        self.validation = patch('backend.app.services.official_prices.validate_public_url', side_effect=public)
        self.validation.start()
        self.addCleanup(self.validation.stop)
        self.place = Place(id='wikidata:Q123', name=f('Bảo tàng Hoàng gia'), latitude=f(16), longitude=f(107))
        self.trip = TripRequest(destination=f('Huế'), days=f(3), budget=f(10000000), currency=f('VND'), travelers=f(2))

    async def test_html_is_bounded_cached_and_scripts_excluded(self):
        first = await self.reader.read('https://museum.example/')
        count = len(self.calls)
        second = await self.reader.read('https://museum.example/')
        self.assertNotIn('fake price', first.text)
        self.assertEqual(first.retrieved_at, second.retrieved_at)
        self.assertEqual(len(self.calls), count)

    async def test_redirect_to_private_network_and_oversized_pages_rejected(self):
        self.mode = 'redirect'
        with self.assertRaises(DataServiceError):
            await self.reader.read('https://museum.example/redirect')
        self.assertFalse(any('127.0.0.1' in url for url in self.calls))
        self.mode = 'large'
        with self.assertRaises(DataServiceError):
            await self.reader.read('https://museum.example/large')

    async def test_discovery_crawl_and_verified_price_replace_unknown(self):
        service = OfficialPriceService(self.settings, self.api, reader=self.reader)
        selection = PriceSelection(page_index=1, excerpt='Vé người lớn: 150.000 đồng/người', place_excerpt='Bảo tàng Hoàng gia',
            amount_text='150.000', per_person=True, admission_only=True, unconditional=True, rationale='Exact price')
        async def decide(*args, **kwargs):
            kwargs['validate'](selection)
            return selection
        service.engine.decide = AsyncMock(side_effect=decide)
        warnings = await service.enrich([self.place], self.trip)
        self.assertEqual(warnings, [])
        self.assertEqual(self.place.ticket_price.value, 150000)
        self.assertIn('official-admission', self.place.ticket_price.source)
        self.assertEqual(str(self.place.ticket_quote.source_url), 'https://museum.example/gia-ve')
        self.assertFalse(any('reseller.example' in url or '127.0.0.1' in url for url in self.calls))

    async def test_no_model_output_does_not_invent_zero_or_price(self):
        service = OfficialPriceService(self.settings, self.api, reader=self.reader)
        service.engine.decide = AsyncMock(return_value=None)
        warnings = await service.enrich([self.place], self.trip)
        self.assertIsNone(self.place.ticket_price)
        self.assertIsNone(self.place.ticket_quote)
        self.assertTrue(warnings)

    async def test_dns_blocks_private_ipv4_and_ipv6(self):
        for ip in ('127.0.0.1', '10.0.0.1', '169.254.169.254', '::1', 'fc00::1'):
            with self.subTest(ip=ip), patch('socket.getaddrinfo', return_value=[(None, None, None, None, (ip, 80))]):
                with self.assertRaises(DataServiceError):
                    await validate_public_url('http://museum.example/')
