"""Independent, bounded POI lookup via Wikidata Query Service (not Overpass)."""
import math
import re
from pydantic import AnyHttpUrl

from .public_api import DataServiceError
from ..schemas import Fact, Place

KINDS={'Q570116':{'tourism':'attraction'},'Q33506':{'tourism':'museum'},
       'Q40080':{'natural':'beach'},'Q22698':{'leisure':'park'},'Q44539':{'tourism':'attraction'},
       'Q12280':{'tourism':'bridge'},'Q194195':{'tourism':'theme_park'},
       'Q34763':{'tourism':'natural_area'},'Q46169':{'tourism':'national_park'},'Q9259':{'tourism':'heritage_site'},
       'Q2393184':{'tourism':'hill_station'},'Q8502':{'tourism':'natural_area'}}


def distance_km(lat,lon,other_lat,other_lon):
    a,b=map(math.radians,(lat,other_lat))
    dlat=math.radians(other_lat-lat);dlon=math.radians(other_lon-lon)
    h=math.sin(dlat/2)**2+math.cos(a)*math.cos(b)*math.sin(dlon/2)**2
    return 6371*2*math.asin(min(1,math.sqrt(max(0,h))))


class WikidataPlaces:
    def __init__(self,settings,http):
        self.settings,self.http=settings,http

    async def nearby(self,destination,*,on_event=None,radius_m=None,limit=None):
        latitude,longitude=destination.latitude.value,destination.longitude.value
        radius=(radius_m or self.settings.research_radius_m)/1000
        limit=limit or self.settings.research_max_places
        if not 0<radius<=100 or type(limit) is not int or not 1<=limit<=80:
            raise ValueError('Invalid Wikidata discovery bounds')
        # Only validated numeric input and fixed QIDs enter SPARQL. User prose
        # is never concatenated into the query or used as factual output.
        query=f'''PREFIX wd: <http://www.wikidata.org/entity/>
PREFIX wdt: <http://www.wikidata.org/prop/direct/>
PREFIX wikibase: <http://wikiba.se/ontology#>
PREFIX bd: <http://www.bigdata.com/rdf#>
PREFIX geo: <http://www.opengis.net/ont/geosparql#>
SELECT DISTINCT ?item ?itemLabel ?itemDescription ?location ?kind ?distance ?website ?sitelinks WHERE {{
 SERVICE wikibase:around {{ ?item wdt:P625 ?location .
  bd:serviceParam wikibase:center "Point({longitude:.6f} {latitude:.6f})"^^geo:wktLiteral ;
   wikibase:radius "{radius:.3f}" ; wikibase:distance ?distance . }}
 {{ VALUES ?kind {{ {' '.join('wd:'+qid for qid in KINDS)} }}
    ?item wdt:P31/wdt:P279* ?kind . }}
 UNION {{ ?item wdt:P1435 wd:Q9259 . BIND(wd:Q9259 AS ?kind) }}
 OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks . }}
 OPTIONAL {{ ?item wdt:P856 ?website . }}
 SERVICE wikibase:label {{ bd:serviceParam wikibase:language "vi,en" . }}
}} ORDER BY DESC(?sitelinks) ?distance ?item ?kind'''
        # Keep each response small: some upstream gateways truncate larger JSON
        # responses. Never try to salvage incomplete JSON as valid place data.
        rows=[]
        page_size=min(16,limit*3)
        for offset in range(0,min(240,limit*3),page_size):
            response=await self.http.request('wikidata-query',self.settings.wikidata_query_url,
                params={'query':query+f' LIMIT {page_size} OFFSET {offset}','format':'json'},headers={'Accept':'application/sparql-results+json'},
                interval=1,on_event=on_event)
            body=response.data
            if not isinstance(body,dict) or not isinstance(body.get('results'),dict) or not isinstance(body['results'].get('bindings'),list):
                raise DataServiceError('wikidata-query: expected SPARQL bindings')
            page=body['results']['bindings']
            rows.extend((row,response.retrieved_at) for row in page)
            if len(page)<page_size or len({row.get('item',{}).get('value') for row,_ in rows})>=limit:
                break
        candidates=[];seen=set()
        for row,retrieved_at in rows:
            try:
                uri=row['item']['value'];kind_uri=row['kind']['value']
                match=re.fullmatch(r'https?://www\.wikidata\.org/entity/(Q[1-9][0-9]*)',uri)
                kind_match=re.fullmatch(r'https?://www\.wikidata\.org/entity/(Q[1-9][0-9]*)',kind_uri)
                if not match or not kind_match or kind_match[1] not in KINDS: continue
                qid=match[1];name=row['itemLabel']['value'].strip()
                if not name or name==qid or qid in seen: continue
                point=re.fullmatch(r'Point\(([-+0-9.eE]+)\s+([-+0-9.eE]+)\)',row['location']['value'])
                if not point: continue
                lon,lat=float(point[1]),float(point[2])
                if not math.isfinite(lat) or not math.isfinite(lon) or not -90<=lat<=90 or not -180<=lon<=180: continue
                if distance_km(latitude,longitude,lat,lon)>radius+.001: continue
                source=f'https://www.wikidata.org/wiki/{qid}; retrieved={retrieved_at}; via=WDQS'
                def fact(value,field): return Fact(value=value,source=source+'; property='+field,confidence=.7)
                description=row.get('itemDescription',{}).get('value')
                place=Place(id='wikidata:'+qid,wikidata_id=qid,name=fact(name,'label'),latitude=fact(lat,'P625'),longitude=fact(lon,'P625'),
                    description=fact(description,'description') if isinstance(description,str) and description.strip() else None)
                tags=KINDS[kind_match[1]]
                place.tourism_kind=fact(next(iter(tags.values())),'P31/P279* or P1435')
                count=row.get('sitelinks',{}).get('value')
                if isinstance(count,str) and count.isdecimal():
                    place.wikidata_sitelinks=fact(int(count),'wikibase:sitelinks')
                website=row.get('website',{}).get('value')
                if isinstance(website,str) and website.startswith(('https://','http://')):
                    place.official_website=Fact[AnyHttpUrl](**fact(website,'P856').model_dump())
                seen.add(qid);candidates.append((place,None,KINDS[kind_match[1]]))
                if len(candidates)>=limit: break
            except (ValueError,TypeError,KeyError,AttributeError):
                continue
        return candidates
