from datetime import date, timedelta
from pathlib import Path
import tempfile
import unittest
import httpx

from backend.app.config import Settings
from backend.app.schemas import Fact, Place, TripRequest
from backend.app.services.place_popularity import PopularityService, diverse_selection, rank_candidates, tourism_candidate
from backend.app.services.places import PlacesService
from backend.app.services.public_api import PublicAPIClient


def f(value): return Fact(value=value,source='test:popularity',confidence=1)
def place(key,name,lat=16,lon=108,links=None,views=None):
    return Place(id='wikidata:'+key,wikidata_id=key,name=f(name),latitude=f(lat),longitude=f(lon),
        wikidata_sitelinks=f(links) if links is not None else None,wikipedia_pageviews=f(views) if views is not None else None)


class PopularityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=Settings(_env_file=None,data_cache_path=Path(temp.name)/'cache.sqlite',research_max_wikidata_requests=0)
        self.today=date(2026,11,24)
        self.calls=[];self.mode='ok'
        def handler(request):
            self.calls.append(request)
            if request.url.host=='www.wikidata.org':
                return httpx.Response(200,json={'entities':{'Q1':{'id':'Q1','sitelinks':{'viwiki':{'title':'Cầu Rồng'},'enwiki':{'title':'Dragon Bridge'},'frwiki':{'title':'Pont du dragon'}}}}})
            if self.mode=='missing': return httpx.Response(404)
            start=self.today-timedelta(days=31)
            rows=[{'timestamp':(start+timedelta(days=i)).strftime('%Y%m%d')+'00','views':0 if self.mode=='zero' else 100} for i in range(30)]
            return httpx.Response(200,json={'items':rows[:-1] if self.mode=='partial' else rows})
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler));self.addAsyncCleanup(client.aclose)
        self.api=PublicAPIClient(self.settings,client=client)
        self.center=place('Q100','Đà Nẵng')
        self.trip=TripRequest(destination=f('Đà Nẵng'),days=f(3),travelers=f(3),budget=f(10000000),currency=f('VND'),requirements=[f('Có trẻ em 6 tuổi')])

    async def test_sitelinks_and_full_30_day_views_have_sources_and_cache(self):
        point=place('Q1','Cầu Rồng')
        service=PopularityService(self.settings,self.api)
        warnings=await service.enrich([(point,None,{})],today=self.today)
        self.assertEqual(warnings,[])
        self.assertEqual(point.wikidata_sitelinks.value,3)
        self.assertEqual(point.wikipedia_pageviews.value,3000)
        self.assertEqual(point.wikipedia_language,'vi')
        self.assertIn('2026-10-24/2026-11-22',point.wikipedia_pageviews.source)
        count=len(self.calls)
        await service.enrich([(point,None,{})],today=self.today)
        self.assertEqual(len(self.calls),count)

    async def test_missing_or_incomplete_pageviews_stay_unknown(self):
        for mode in ('missing','partial'):
            self.mode=mode
            point=place('Q1','Cầu Rồng')
            # Separate provider cache per case, so the incomplete response is exercised.
            self.settings.data_cache_path=self.settings.data_cache_path.with_name(mode+'.sqlite')
            warnings=await PopularityService(self.settings,self.api).enrich([(point,None,{})],today=self.today)
            self.assertIsNone(point.wikipedia_pageviews)
            self.assertTrue(warnings)

    async def test_true_zero_views_are_retained(self):
        self.mode='zero'
        point=place('Q1','Cầu Rồng')
        await PopularityService(self.settings,self.api).enrich([(point,None,{})],today=self.today)
        self.assertEqual(point.wikipedia_pageviews.value,0)

    def test_popular_regional_landmark_beats_nearby_obscure_stop(self):
        famous=place('Q1','Phố cổ nổi bật',lat=16.2,lon=108.2,links=80,views=30000)
        nearby=place('Q2','Điểm nhỏ',lat=16.001,links=1,views=10)
        ranked=rank_candidates([(nearby,None,{}),(famous,None,{})],self.center,trip=self.trip)
        self.assertEqual(ranked[0][0].id,famous.id)
        self.assertGreater(famous.distance_to_destination_km.value,15)

    def test_industrial_park_is_excluded_even_with_huge_popularity(self):
        industrial=place('Q1','Khu công nghệ cao Đà Nẵng',links=999,views=999999)
        museum=place('Q2','Bảo tàng công nghệ',links=1,views=10)
        ranked=rank_candidates([(industrial,None,{'leisure':'park'}),(museum,None,{'tourism':'museum'})],self.center,trip=self.trip)
        self.assertEqual([item[0].id for item in ranked],[museum.id])

    def test_fame_does_not_override_child_age_restrictions(self):
        restricted=place('Q1','Điểm chỉ dành người lớn',links=200,views=900000)
        restricted.minimum_age=f(18)
        self.assertFalse(tourism_candidate(restricted,trip=self.trip))
        self.assertFalse(tourism_candidate(place('Q2','Casino nổi tiếng'),trip=self.trip))

    def test_shortlist_does_not_become_only_bridges_or_duplicate_heritage_area(self):
        bridges=[]
        for i in range(4):
            point=place('Q'+str(i+1),'Cầu '+str(i),links=40-i)
            point.tourism_kind=f('bridge')
            bridges.append((point,None,{}))
        beach=place('Q10','Bãi biển',links=5)
        beach.tourism_kind=f('beach')
        selected=diverse_selection([*bridges,(beach,None,{})],3)
        self.assertEqual([item[0].name.value for item in selected],['Cầu 0','Cầu 1','Bãi biển'])
        city=place('Q20','Hội An');city.tourism_kind=f('heritage_site')
        oldtown=place('Q21','Phố cổ Hội An',lat=16.001);oldtown.tourism_kind=f('heritage_site')
        self.assertEqual(len(diverse_selection([(city,None,{}),(oldtown,None,{})],4)),1)


class RegionalDiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_regional_sitelink_ranked_candidates_are_merged_even_when_overpass_works(self):
        from types import SimpleNamespace
        with tempfile.TemporaryDirectory() as directory:
            settings=Settings(_env_file=None,data_cache_path=Path(directory)/'cache.sqlite',research_max_places=1,research_pageview_limit=0,research_max_wikidata_requests=0)
            queries=[]
            async def request(provider,url,**kwargs):
                if provider=='nominatim': data=[{'osm_type':'node','osm_id':1,'name':'Đà Nẵng','lat':'16','lon':'108'}]
                elif provider=='overpass': data={'elements':[{'type':'node','id':2,'lat':16.001,'lon':108,'tags':{'name':'Công viên nhỏ','leisure':'park','wikidata':'Q1'}}]}
                elif provider=='wikidata-query':
                    queries.append(kwargs['params']['query'])
                    def row(qid,name,count,kind):
                        return {'item':{'value':'http://www.wikidata.org/entity/'+qid},'itemLabel':{'value':name},'location':{'value':'Point(108.2 16.2)'},
                            'kind':{'value':'http://www.wikidata.org/entity/'+kind},'sitelinks':{'value':str(count)}}
                    data={'results':{'bindings':[row('Q2','Phố cổ nổi bật',80,'Q9259'),row('Q3','Khu công nghệ cao',999,'Q22698')]}}
                elif provider=='wikidata-popularity':
                    data={'entities':{qid:{'id':qid,'sitelinks':{str(i):{'title':'test'} for i in range(count)}} for qid,count in [('Q1',1),('Q2',80)]}}
                else: raise AssertionError(provider)
                return SimpleNamespace(data=data,source=url,retrieved_at='2026-11-24T00:00:00Z')
            result=await PlacesService(settings,SimpleNamespace(request=request)).search('Đà Nẵng')
            self.assertEqual([point.name.value for point in result.places],['Phố cổ nổi bật'])
            self.assertIn('DESC(?sitelinks)',queries[0])
            self.assertIn('"50.000"',queries[0])
