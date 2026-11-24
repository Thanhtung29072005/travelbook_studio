"""Isolated provider tests: fake JSON and MockTransport, no server or live API."""
import asyncio
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock

import httpx

from backend.app.config import Settings
from backend.app.schemas import Fact, Place
from backend.app.services.places import PlacesService, PlaceSearchResult
from backend.app.services.wikidata_places import WikidataPlaces
from backend.app.services.public_api import PublicAPIClient, DataServiceError
from backend.app.agents.researcher import ResearcherAgent
from backend.app.schemas import TripRequest


def f(value): return Fact(value=value,source='test:input',confidence=1)
def binding(qid='Q600269',name='Tháp Po Nagar',point='Point(109.196 12.265)',kind='Q44539'):
    return {'item':{'value':'http://www.wikidata.org/entity/'+qid},'itemLabel':{'value':name},
        'location':{'value':point},'kind':{'value':'http://www.wikidata.org/entity/'+kind}}


class IndependentPlacesTests(unittest.TestCase):
    def setUp(self):
        self.settings=Settings(_env_file=None,research_max_places=5,research_popularity_enabled=False)
        self.center=Place(id='destination',name=f('Nha Trang'),latitude=f(12.24),longitude=f(109.19))

    def test_overpass_down_uses_real_structured_wikidata_rows_without_inventing_prices(self):
        calls=[]
        async def request(provider,url,**kwargs):
            calls.append(provider)
            if provider=='nominatim':
                body=[{'osm_type':'relation','osm_id':1,'name':'Nha Trang','lat':'12.24','lon':'109.19'}]
            elif provider=='overpass': raise DataServiceError('overpass: HTTP 502',status_code=502)
            elif provider=='wikidata-query': body={'results':{'bindings':[binding(),binding()]}}
            else: raise AssertionError(provider)
            return SimpleNamespace(data=body,source=url,retrieved_at='2026-11-22T15:00:00Z')
        result=asyncio.run(PlacesService(self.settings,SimpleNamespace(request=request)).search('Nha Trang'))
        self.assertEqual([place.id for place in result.places],['wikidata:Q600269'])
        self.assertEqual(calls.count('overpass'),2)
        self.assertEqual(calls.count('wikidata-query'),1)
        place=result.places[0]
        self.assertIn('wikidata.org/wiki/Q600269',place.latitude.source)
        self.assertIsNone(place.ticket_price);self.assertIsNone(place.opening_hours)

    def test_bad_ids_types_far_coordinates_and_unlabelled_entities_are_rejected(self):
        rows=[binding(qid='Q0'),binding(kind='Q5'),binding(point='Point(-77 38)'),binding(name='Q600269'),binding()]
        async def request(*args,**kwargs): return SimpleNamespace(data={'results':{'bindings':rows}},retrieved_at='now')
        result=asyncio.run(WikidataPlaces(self.settings,SimpleNamespace(request=request)).nearby(self.center))
        self.assertEqual(len(result),1)
        self.assertEqual(result[0][0].name.value,'Tháp Po Nagar')

    def test_wikidata_large_pool_is_paged_without_truncated_json_or_duplicates(self):
        queries=[]
        async def request(*args,**kwargs):
            query=kwargs['params']['query'];queries.append(query)
            offset=16 if 'OFFSET 16' in query else 0
            rows=[binding(qid='Q'+str(i+1),name='Tourist point '+str(i+1)) for i in range(offset,min(offset+16,20))]
            return SimpleNamespace(data={'results':{'bindings':rows}},retrieved_at='2026-11-24T00:00:00Z')
        result=asyncio.run(WikidataPlaces(self.settings,SimpleNamespace(request=request)).nearby(self.center,limit=20))
        self.assertEqual(len(result),20)
        self.assertEqual(len(queries),2)
        self.assertIn('LIMIT 16 OFFSET 16',queries[1])

    def test_all_sources_fail_preserves_errors_and_does_not_fabricate_places(self):
        async def request(provider,url,**kwargs):
            if provider=='nominatim': return SimpleNamespace(data=[{'osm_type':'node','osm_id':1,'name':'Nha Trang','lat':'12.24','lon':'109.19'}],source=url,retrieved_at='now')
            raise DataServiceError(provider+': connection error (ReadTimeout)')
        result=asyncio.run(PlacesService(self.settings,SimpleNamespace(request=request)).search('Nha Trang'))
        self.assertEqual(result.places,[])
        self.assertTrue(any('wikidata-query:' in note.value for note in result.warnings))

    def test_overpass_success_does_not_query_wikidata_search(self):
        calls=[]
        async def request(provider,url,**kwargs):
            calls.append(provider)
            if provider=='nominatim': body=[{'osm_type':'node','osm_id':1,'name':'Nha Trang','lat':'12.24','lon':'109.19'}]
            elif provider=='overpass': body={'elements':[{'type':'node','id':2,'lat':12.25,'lon':109.2,'tags':{'name':'Named place'}}]}
            else: raise AssertionError(provider)
            return SimpleNamespace(data=body,source=url,retrieved_at='now')
        result=asyncio.run(PlacesService(self.settings,SimpleNamespace(request=request)).search('Nha Trang'))
        self.assertEqual(len(result.places),1);self.assertNotIn('wikidata-query',calls)

    def test_model_cannot_repeat_failed_provider_chain_despite_refine_action(self):
        found=PlaceSearchResult(destination=self.center,warnings=[f('overpass: HTTP 502')])
        places=SimpleNamespace(settings=self.settings,search=AsyncMock(return_value=found))
        agent=ResearcherAgent(None,places,None,Path('.'))
        agent.reasoning.decide=AsyncMock(side_effect=[SimpleNamespace(search_interests=[],include_weather=False),
            SimpleNamespace(action='refine_search',search_interests=[],priority_place_ids=[],missing_information=[])])
        trip=TripRequest(destination=f('Nha Trang'),days=f(3),budget=f(10000000),currency=f('VND'),travelers=f(2))
        asyncio.run(agent.research(trip))
        self.assertEqual(places.search.await_count,1)
        self.assertEqual(agent.reasoning.decide.await_args_list[1].args[3]['remaining_search_calls'],0)

    def test_transport_errors_are_distinguished_without_leaking_exception_text(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                settings=Settings(_env_file=None,data_cache_path=Path(folder)/'cache.db')
                def handler(request): raise httpx.ReadTimeout('sensitive-url-token')
                async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                    with self.assertRaises(DataServiceError) as caught:
                        await PublicAPIClient(settings,client=client).request('provider','https://example.test/data')
                self.assertIn('ReadTimeout',str(caught.exception))
                self.assertNotIn('sensitive-url-token',str(caught.exception))
        asyncio.run(check())

    def test_html_success_is_reported_as_invalid_json(self):
        async def check():
            with tempfile.TemporaryDirectory() as folder:
                settings=Settings(_env_file=None,data_cache_path=Path(folder)/'cache.db')
                async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,text='<html>Error</html>'))) as client:
                    with self.assertRaisesRegex(DataServiceError,'invalid JSON'):
                        await PublicAPIClient(settings,client=client).request('provider','https://example.test/data')
        asyncio.run(check())
