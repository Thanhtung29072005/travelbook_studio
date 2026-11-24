"""Bounded popularity evidence, tourism filtering and profile-aware ranking."""
import asyncio
from datetime import datetime, timedelta, timezone
import math
import re
import unicodedata
from urllib.parse import quote

from ..schemas import Fact, TripRequest
from .public_api import DataServiceError
from .wikidata_places import distance_km


def folded(value):
    return ''.join(char for char in unicodedata.normalize('NFD',value.casefold().replace('đ','d')) if not unicodedata.combining(char))


def has_children(trip):
    if trip is None:
        return False
    trip=TripRequest.model_validate(trip)
    needs=folded(' '.join(item.value for item in trip.requirements))
    ages=[item.value for item in trip.traveler_ages]+[int(age) for age in re.findall(r'\b(\d{1,2})\s*tuoi\b',needs)]
    return any(age<18 for age in ages) or any(word in needs for word in ('tre em','tre nho','children','child'))


def tourism_candidate(place, tags=None, trip=None):
    tags=tags or {}
    name=folded(place.name.value)
    description=folded(place.description.value) if place.description else ''
    description=re.sub(r'^(?:a|an|the|mot)\s+','',description)
    excluded=('khu cong nghe cao','khu cong nghiep','khu che xuat','industrial park','industrial zone',
        'high-tech park','high tech park','technology park','business park','science park','cong vien phan mem','software park')
    if any(word in name or description.startswith(word) for word in excluded):
        return False
    if tags.get('landuse') in {'industrial','commercial'} and tags.get('tourism') not in {'museum','gallery'}:
        return False
    if has_children(trip):
        if tags.get('amenity') in {'nightclub','casino','stripclub'} or any(word in name for word in ('casino','nightclub','strip club','song bai','vu truong')):
            return False
        known_ages=[age.value for age in TripRequest.model_validate(trip).traveler_ages]
        needs=folded(' '.join(item.value for item in TripRequest.model_validate(trip).requirements))
        known_ages += [int(age) for age in re.findall(r'\b(\d{1,2})\s*tuoi\b',needs)]
        if place.minimum_age and any(age<place.minimum_age.value for age in known_ages):
            return False
        if any('khong phu hop tre' in folded(note.value) or 'adults only' in folded(note.value) for note in place.age_notes):
            return False
    return True


def rank_candidates(candidates, destination, interests=(), trip=None):
    needs=folded(' '.join(interests))
    family=has_children(trip)
    result=[]
    for place,qid,tags in candidates:
        if not tourism_candidate(place,tags,trip):
            continue
        distance=distance_km(destination.latitude.value,destination.longitude.value,place.latitude.value,place.longitude.value)
        place.distance_to_destination_km=Fact(value=round(distance,3),source='calculation:haversine-to-destination; '+destination.latitude.source,confidence=1)
        links=place.wikidata_sitelinks.value if place.wikidata_sitelinks is not None else 0
        views=place.wikipedia_pageviews.value if place.wikipedia_pageviews is not None else 0
        kind=place.tourism_kind.value if place.tourism_kind else tags.get('tourism') or tags.get('natural') or tags.get('leisure') or ''
        interest_bonus=0
        if ('bien' in needs or 'beach' in needs) and (tags.get('natural')=='beach' or kind=='beach'):
            interest_bonus+=15
        if ('bao tang' in needs or 'museum' in needs) and (tags.get('tourism')=='museum' or kind=='museum'):
            interest_bonus+=15
        family_bonus=6 if family and kind in {'park','beach','museum','theme_park'} else 0
        score=max(0,12*math.log1p(links)+4*math.log1p(views)+interest_bonus+family_bonus+10-min(10,distance/5))
        sources=[fact.source for fact in (place.wikidata_sitelinks,place.wikipedia_pageviews) if fact]
        place.popularity_score=Fact(value=round(score,6),
            source='calculation:12*log1p(sitelinks)+4*log1p(pageviews30d)+interest+family+distance; missing metrics are unknown; '+'; '.join(sources),confidence=.7 if sources else .3)
        result.append((place,qid,tags))
    return sorted(result,key=lambda item:(-item[0].popularity_score.value,item[0].distance_to_destination_km.value,item[0].id))


def diverse_selection(ranked, limit):
    """Keep the fame order, without filling a short itinerary with only bridges."""
    selected=[]
    deferred=[]
    counts={}
    for item in ranked:
        place,_,tags=item
        kind=place.tourism_kind.value if place.tourism_kind else tags.get('tourism') or tags.get('natural') or tags.get('leisure') or 'unknown'
        if kind=='heritage_site' and any(other.tourism_kind and other.tourism_kind.value=='heritage_site'
                and (folded(place.name.value) in folded(other.name.value) or folded(other.name.value) in folded(place.name.value))
                and distance_km(place.latitude.value,place.longitude.value,other.latitude.value,other.longitude.value)<1
                for other,_,_ in selected):
            continue
        cap=2 if kind=='bridge' else 3
        if kind!='unknown' and counts.get(kind,0)>=cap:
            deferred.append(item)
            continue
        selected.append(item)
        counts[kind]=counts.get(kind,0)+1
        if len(selected)==limit:
            break
    return (selected+deferred)[:limit]


class PopularityService:
    def __init__(self,settings,public_api):
        self.settings,self.api=settings,public_api

    async def enrich(self,candidates,*,on_event=None,today=None):
        warnings=[]
        qids={place.wikidata_id or qid for place,qid,_ in candidates if place.wikidata_id or qid}
        qids={qid for qid in qids if re.fullmatch(r'Q[1-9][0-9]*',qid)}
        async def metadata():
            if not qids:
                return
            ids=sorted(qids)
            for start in range(0,len(ids),50):
                response=await self.api.request('wikidata-popularity','https://www.wikidata.org/w/api.php',
                    params={'action':'wbgetentities','ids':'|'.join(ids[start:start+50]),'props':'sitelinks|descriptions','languages':'vi|en','format':'json'},
                    ttl=86400,on_event=on_event)
                entities=response.data.get('entities',{})
                for place,qid,_ in candidates:
                    qid=place.wikidata_id or qid
                    entity=entities.get(qid,{})
                    if entity.get('id')!=qid:
                        continue
                    links=entity.get('sitelinks')
                    source=f'https://www.wikidata.org/wiki/{qid}; retrieved={response.retrieved_at}'
                    if isinstance(links,dict):
                        place.wikidata_sitelinks=Fact(value=len(links),source=source+'; field=sitelinks',confidence=.9)
                        for lang in ('vi','en'):
                            title=links.get(lang+'wiki',{}).get('title')
                            if isinstance(title,str) and title.strip():
                                place.wikipedia_title=Fact(value=title,source=source+'; sitelink='+lang+'wiki',confidence=.9)
                                place.wikipedia_language=lang
                                break
                    if place.description is None:
                        for lang in ('vi','en'):
                            text=entity.get('descriptions',{}).get(lang,{}).get('value')
                            if isinstance(text,str) and text.strip():
                                place.description=Fact(value=text,source=source+'; description='+lang,confidence=.75)
                                break
        async def views():
            reference=today or datetime.now(timezone.utc).date()
            end=reference-timedelta(days=2)
            start=end-timedelta(days=29)
            selected=sorted((place for place,_,_ in candidates if place.wikipedia_title),
                key=lambda place: -(place.wikidata_sitelinks.value if place.wikidata_sitelinks else 0))[:self.settings.research_pageview_limit]
            semaphore=asyncio.Semaphore(3)
            async def one(place):
                async with semaphore:
                    lang=place.wikipedia_language
                    title=quote(place.wikipedia_title.value.replace(' ','_'),safe='')
                    url=f'https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/{lang}.wikipedia.org/all-access/user/{title}/daily/{start:%Y%m%d}00/{end:%Y%m%d}00'
                    try:
                        response=await self.api.request('wikipedia-pageviews',url,ttl=86400,on_event=on_event)
                        rows=response.data.get('items',[])
                        expected={(start+timedelta(days=i)).strftime('%Y%m%d') for i in range(30)}
                        dates=[row.get('timestamp','')[:8] for row in rows]
                        if set(dates)!=expected or len(dates)!=30 or any(type(row.get('views')) is not int or row['views']<0 for row in rows):
                            raise ValueError('Incomplete pageview window')
                        place.wikipedia_pageviews=Fact(value=sum(row['views'] for row in rows),
                            source=response.source+f'; retrieved={response.retrieved_at}; window={start}/{end}; language={lang}',confidence=.9)
                    except (DataServiceError,ValueError,TypeError,KeyError,AttributeError):
                        warnings.append('Chưa lấy đủ 30 ngày lượt xem Wikipedia: '+place.name.value)
            await asyncio.gather(*(one(place) for place in selected))
        async def run():
            try:
                await metadata()
            except (DataServiceError,ValueError,TypeError,KeyError,AttributeError):
                warnings.append('Chưa lấy được đầy đủ số sitelinks Wikidata; giữ thông tin độ nổi tiếng chưa rõ.')
            await views()
        try:
            await asyncio.wait_for(run(),timeout=self.settings.research_popularity_timeout_seconds)
        except asyncio.TimeoutError:
            warnings.append('Hết thời gian tra độ nổi tiếng; chỉ dùng các chỉ số đã lấy được, không tạo số thay thế.')
        return warnings
