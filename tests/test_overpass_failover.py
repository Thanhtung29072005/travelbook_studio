from types import SimpleNamespace
import unittest

from backend.app.config import Settings
from backend.app.schemas import Place, Fact
from backend.app.services.places import PlacesService
from backend.app.services.public_api import DataServiceError


class OverpassFailoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.settings=Settings(_env_file=None)
        self.place=Place(id='destination',name=Fact(value='Nha Trang',source='test:input',confidence=1),
            latitude=Fact(value=12.24,source='test:input',confidence=1),longitude=Fact(value=109.19,source='test:input',confidence=1))

    async def test_502_uses_alternative_and_preserves_source(self):
        calls=[];events=[]
        async def request(provider,url,**kwargs):
            calls.append(url)
            if len(calls)==1: raise DataServiceError('overpass: HTTP 502',status_code=502)
            return SimpleNamespace(data={'elements':[{'type':'node','id':123,'lat':12.25,'lon':109.2,'tags':{'name':'Real provider result'}}]},retrieved_at='2026-11-22T15:00:00Z')
        service=PlacesService(self.settings,SimpleNamespace(request=request))
        result=await service.nearby(self.place,on_event=events.append)
        self.assertEqual(calls,[self.settings.overpass_url,self.settings.overpass_fallback_urls])
        self.assertEqual(len(result),1)
        self.assertIn('overpass.private.coffee',result[0][0].latitude.source)
        self.assertEqual(events[0]['status'],'failover')
        self.assertEqual(events[0]['http_status'],502)

    async def test_rate_limit_is_not_bypassed_by_switching_servers(self):
        calls=[]
        async def request(provider,url,**kwargs):
            calls.append(url);raise DataServiceError('overpass: HTTP 429',status_code=429)
        with self.assertRaises(DataServiceError):
            await PlacesService(self.settings,SimpleNamespace(request=request)).nearby(self.place)
        self.assertEqual(len(calls),1)

    async def test_all_endpoints_down_keeps_explicit_error_without_fake_places(self):
        calls=[]
        async def request(provider,url,**kwargs):
            calls.append(url);raise DataServiceError('overpass: HTTP 502',status_code=502)
        with self.assertRaisesRegex(DataServiceError,'all configured endpoints failed'):
            await PlacesService(self.settings,SimpleNamespace(request=request)).nearby(self.place)
        self.assertEqual(len(calls),2)

    async def test_empty_success_does_not_trigger_more_requests(self):
        calls=[]
        async def request(provider,url,**kwargs):
            calls.append(url);return SimpleNamespace(data={'elements':[]},retrieved_at='2026-11-22T15:00:00Z')
        result=await PlacesService(self.settings,SimpleNamespace(request=request)).nearby(self.place)
        self.assertEqual(result,[])
        self.assertEqual(len(calls),1)

    async def test_fallback_configuration_rejects_credentials(self):
        with self.assertRaises(ValueError):
            Settings(_env_file=None,overpass_fallback_urls='https://user:secret@invalid.example/api/interpreter')
