import json
from datetime import date,time
from pathlib import Path
import tempfile
import unittest

import httpx

from backend.app.config import Settings
from backend.app.schemas import TripRequest,DayPlan,PlanStop,Place,ActivitySlot
from backend.app.services.public_api import PublicAPIClient
from backend.app.services.trip_venues import TripVenueService


def f(value): return {'value':value,'source':'input:venue-test','confidence':1}


class TripVenueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.settings=Settings(_env_file=None,data_cache_path=Path(temp.name)/'cache.db',
            overpass_fallback_urls='',nominatim_min_interval_seconds=1)
        self.trip=TripRequest(destination=f('Huế'),days=f(2),travelers=f(2),budget=f('2000000'),currency=f('VND'))
        self.place=Place(id='museum',name=f('Bảo tàng'),latitude=f(16.46),longitude=f(107.59))
        self.day=DayPlan(day_number=1,date=f(date(2026,11,26)),stops=[PlanStop(place_id='museum',arrival=f(time(9)),departure=f(time(11)))],activities=[
            ActivitySlot(kind='lunch',title=f('Ăn trưa (tự chọn địa điểm)'),start=f(time(12)),end=f(time(13))),
            ActivitySlot(kind='hotel',title=f('Nghỉ tối tại nơi lưu trú dự kiến (chưa chọn khách sạn)'),start=f(time(20,30)),end=f(time(21)))])
        self.records=[
            {'type':'node','id':1,'lat':16.4605,'lon':107.5905,'tags':{'name':'Nhà hàng có nguồn','amenity':'restaurant','addr:street':'Đường A','opening_hours':'10:00-21:00'}},
            {'type':'node','id':2,'lat':16.46,'lon':107.59,'tags':{'name':'Đang đóng cửa','amenity':'restaurant','opening_hours':'18:00-22:00'}},
            {'type':'node','id':3,'lat':16.4605,'lon':107.5905,'tags':{'name':'Nhà nghỉ có nguồn','tourism':'guest_house','addr:street':'Đường B'}},
            {'type':'node','id':4,'lat':16.46,'lon':107.59,'tags':{'name':'Khách sạn 5 sao','tourism':'hotel','stars':'5'}}]

    async def service(self,handler):
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(client.aclose)
        public=PublicAPIClient(self.settings,client=client)
        return TripVenueService(self.settings,public)

    async def test_named_venues_have_map_sources_and_meal_transfer_budget(self):
        service=await self.service(lambda request:httpx.Response(200,json={'elements':self.records}))
        warnings=await service.enrich(self.trip,[self.day],[self.place])
        self.assertEqual(warnings,[])
        lunch,hotel=self.day.activities
        self.assertEqual(lunch.venue.name.value,'Nhà hàng có nguồn')
        self.assertEqual(hotel.venue.name.value,'Nhà nghỉ có nguồn')
        self.assertEqual(str(lunch.venue.source_url.value),'https://www.openstreetmap.org/node/1')
        self.assertGreater(lunch.venue_transfer_minutes.value,0)
        self.assertIn('Nhà nghỉ có nguồn',hotel.title.value)
        DayPlan.model_validate_json(self.day.model_dump_json())
        from backend.app.checkpoint_codec import StateSerializer
        codec=StateSerializer()
        restored=codec.loads(codec.dumps(self.day))
        self.assertEqual(restored.activities[0].venue.name,lunch.venue.name)

    async def test_failure_never_invents_names_and_keeps_itinerary(self):
        service=await self.service(lambda request:httpx.Response(503))
        warnings=await service.enrich(self.trip,[self.day],[self.place])
        self.assertTrue(warnings)
        self.assertTrue(all(activity.venue is None for activity in self.day.activities))
        self.assertEqual(self.day.stops[0].place_id,'museum')

    async def test_on_site_meal_is_not_replaced_by_an_outside_restaurant(self):
        self.day.stops[0].departure=f(time(16))
        self.day.activities[0].within_place_id='museum'
        service=await self.service(lambda request:httpx.Response(200,json={'elements':self.records}))
        await service.enrich(self.trip,[self.day],[self.place])
        self.assertIsNone(self.day.activities[0].venue)
        self.assertIn('trong khu Bảo tàng',self.day.activities[0].title.value)

    async def test_far_restaurants_are_not_selected_even_when_provider_returns_them(self):
        for record in self.records:
            if record['tags'].get('amenity')=='restaurant': record['lat']=17.0
        service=await self.service(lambda request:httpx.Response(200,json={'elements':self.records}))
        await service.enrich(self.trip,[self.day],[self.place])
        self.assertIsNone(self.day.activities[0].venue)

    async def test_nominatim_fallback_keeps_real_named_map_identity(self):
        def handler(request):
            if 'nominatim' in request.url.host:
                kind=request.url.params.get('amenity')
                return httpx.Response(200,json=[{'osm_type':'node','osm_id':10,'lat':'16.4601','lon':'107.5901',
                    'name':'Nhà hàng dự phòng' if kind=='restaurant' else 'Khách sạn dự phòng','type':kind,'display_name':'Địa chỉ có nguồn'}])
            return httpx.Response(503)
        service=await self.service(handler)
        await service.enrich(self.trip,[self.day],[self.place])
        self.assertEqual(self.day.activities[0].venue.id,'osm:node:10')
        self.assertEqual(self.day.activities[0].venue.address.value,'Địa chỉ có nguồn')
