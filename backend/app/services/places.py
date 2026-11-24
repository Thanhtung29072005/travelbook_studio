"""Nominatim geocoding, bounded Overpass POIs, linked Wikidata descriptions."""

from decimal import Decimal
import asyncio
import math
import re
import inspect
import unicodedata
from urllib.parse import urlsplit

from pydantic import Field, ValidationError

from ..schemas import Fact, Place, Schema
from .public_api import DataServiceError
from .wikidata_places import WikidataPlaces
from .place_popularity import PopularityService, diverse_selection, rank_candidates, tourism_candidate


class PlaceSearchResult(Schema):
    destination: Place | None = None
    places: list[Place] = Field(default_factory=list)
    warnings: list[Fact[str]] = Field(default_factory=list)
    notes: list[Fact[str]] = Field(default_factory=list)
    normalized_destination: Fact[str] | None = None


def destination_key(value):
    value = unicodedata.normalize('NFD', value.casefold().replace('đ', 'd'))
    return ' '.join(''.join(char for char in value if not unicodedata.combining(char)).split())


def telex_search_variant(query):
    """One conservative retry for a dangling Vietnamese tone key, e.g. Quocs.

    This is only a search query, never evidence of a place or its coordinates.
    Require Vietnamese diacritics to avoid rewriting foreign names such as Francs.
    """
    if destination_key(query) == query.casefold().strip():
        return None
    repaired = re.sub(r'([cpt])[sfrxj]\b', r'\1', query.strip(), flags=re.I)
    return repaired if repaired != query.strip() else None


def fact(value, source, confidence):
    return {"value": value, "source": source, "confidence": confidence}


class PlacesService:
    def __init__(self, settings, http):
        self.settings, self.http = settings, http

    async def geocode(self, query, *, on_event=None, expected_name=None):
        if not isinstance(query, str) or not query.strip() or len(query) > 300:
            raise ValueError("Destination query must contain 1..300 characters")
        response = await self.http.request("nominatim", self.settings.nominatim_base_url + "/search",
            params={"q": query, "format": "jsonv2", "addressdetails": 1, "namedetails": 1,
                    "extratags": 1, "limit": 3, "accept-language": "vi,en"},
            interval=self.settings.nominatim_min_interval_seconds, on_event=on_event)
        if not isinstance(response.data, list):
            raise DataServiceError("nominatim: expected search result array")
        for row in response.data:
            if not isinstance(row, dict):
                continue
            try:
                osm_type, osm_id = row["osm_type"], int(row["osm_id"])
                if osm_type not in {"node", "way", "relation"} or osm_id <= 0:
                    continue
                namedetails = row.get("namedetails") if isinstance(row.get("namedetails"), dict) else {}
                name = namedetails.get("name:vi") or row.get("name") or row.get("display_name")
                if expected_name is not None:
                    matching = [value for value in (name, row.get('name'), namedetails.get('name:vi'), namedetails.get('name:en'))
                                if isinstance(value, str) and destination_key(value) == destination_key(expected_name)]
                    if not matching:
                        continue
                    name = matching[0]
                source = response.source + "; retrieved=" + response.retrieved_at
                return Place(id=f"osm:{osm_type}:{osm_id}", name=fact(name, source, 0.75),
                             latitude=fact(float(row["lat"]), source, 0.75),
                             longitude=fact(float(row["lon"]), source, 0.75),
                             address=fact(row["display_name"], source, 0.75) if row.get("display_name") else None)
            except (KeyError, TypeError, ValueError, ValidationError):
                continue
        return None

    async def nearby(self, destination, *, on_event=None):
        latitude, longitude = destination.latitude.value, destination.longitude.value
        if not math.isfinite(latitude) or not math.isfinite(longitude):
            raise ValueError("Invalid coordinates")
        radius = self.settings.research_radius_m
        around = f"(around:{radius},{latitude:.6f},{longitude:.6f})"
        query_timeout=min(20,max(1,int(self.settings.data_timeout_seconds)-3))
        query = (f"[out:json][timeout:{query_timeout}];("
                 f'nwr{around}["tourism"~"^(attraction|museum|viewpoint|gallery)$"];'
                 f'nwr{around}["natural"="beach"];nwr{around}["leisure"="park"];'
                 ");out body center 60;")
        endpoints=list(dict.fromkeys([self.settings.overpass_url,*[
            item.strip() for item in getattr(self.settings,'overpass_fallback_urls','').split(',') if item.strip()]]))
        errors=[]
        for index,endpoint in enumerate(endpoints):
            try:
                response=await self.http.request('overpass',endpoint,form={'data':query},interval=1,on_event=on_event)
                if not isinstance(response.data,dict) or not isinstance(response.data.get('elements'),list):
                    raise DataServiceError('overpass: expected elements array')
                if response.data.get('remark'): raise DataServiceError('overpass: incomplete query result')
                break
            except DataServiceError as error:
                # Do not rotate servers to evade rate limits or hide invalid queries.
                if error.status_code is not None and error.status_code not in {500,502,503,504}:
                    raise
                errors.append(str(error))
                if index+1==len(endpoints):
                    raise DataServiceError('overpass: all configured endpoints failed: '+'; '.join(errors),status_code=error.status_code) from None
                if on_event:
                    notified=on_event({'provider':'overpass','cache_hit':False,'status':'failover',
                        'http_status':error.status_code,'from_host':urlsplit(endpoint).hostname,
                        'to_host':urlsplit(endpoints[index+1]).hostname})
                    if inspect.isawaitable(notified): await notified
        candidates = []
        seen = set()
        for row in response.data["elements"]:
            if not isinstance(row, dict):
                continue
            try:
                tags = row.get("tags") if isinstance(row.get("tags"), dict) else {}
                name = tags.get("name:vi") or tags.get("name") or tags.get("name:en")
                osm_type, osm_id = row["type"], int(row["id"])
                if not name or osm_type not in {"node", "way", "relation"} or osm_id <= 0:
                    continue
                coordinates = row if osm_type == "node" else row.get("center", {})
                qid = tags.get("wikidata", "")
                qid = qid if re.fullmatch(r"Q[1-9][0-9]*", qid) else None
                identity = qid or f"{osm_type}:{osm_id}"
                if identity in seen:
                    continue
                source = f"https://www.openstreetmap.org/{osm_type}/{osm_id}; retrieved={response.retrieved_at}; via={urlsplit(endpoint).hostname}"
                confidence = 0.8 if osm_type == "node" else 0.7
                data = dict(id=f"osm:{osm_type}:{osm_id}", name=fact(name, source + "#name", 0.75),
                            wikidata_id=qid,
                            latitude=fact(float(coordinates["lat"]), source, confidence),
                            longitude=fact(float(coordinates["lon"]), source, confidence))
                kind=tags.get('tourism') or tags.get('natural') or tags.get('leisure')
                if kind:
                    data['tourism_kind']=fact(kind,source+'#tourism-kind',.7)
                if tags.get("opening_hours"):
                    data["opening_hours"] = fact(tags["opening_hours"], source + "#opening_hours", 0.6)
                website = tags.get("website") or tags.get("contact:website")
                if isinstance(website, str) and website.startswith(("https://", "http://")):
                    data["official_website"] = fact(website, source + "#website", 0.75)
                for tag, field in (("min_age", "minimum_age"), ("max_age", "maximum_age")):
                    if re.fullmatch(r"[0-9]{1,3}", str(tags.get(tag, ""))):
                        data[field] = fact(int(tags[tag]), source + "#" + tag, 0.6)
                if tags.get("description:vi") or tags.get("description"):
                    data["description"] = fact(tags.get("description:vi") or tags["description"], source + "#description", 0.6)
                # Parse only unambiguous explicit charge + currency, never estimate admission fees.
                charge = str(tags.get("charge") or tags.get("fee:amount") or "")
                currency = tags.get("charge:currency") or tags.get("currency")
                if re.fullmatch(r"[0-9]+(?:\.[0-9]{1,2})?", charge) and currency and re.fullmatch(r"[A-Z]{3}", currency):
                    charge += " " + currency
                match = re.fullmatch(r"([0-9]+(?:\.[0-9]{1,2})?)\s+([A-Z]{3})", charge)
                if match:
                    data["ticket_price"] = fact(Decimal(match[1]), source + "#charge", 0.6)
                    data["currency"] = fact(match[2], source + "#charge", 0.6)
                place = Place.model_validate(data)
                seen.add(identity)
                candidates.append((place, qid, tags))
            except (TypeError, KeyError, ValueError, ValidationError):
                continue
        return candidates

    async def wikidata_description(self, qid, *, on_event=None):
        if not re.fullmatch(r"Q[1-9][0-9]*", qid):
            raise ValueError("Invalid Wikidata entity ID")
        response = await self.http.request("wikidata", self.settings.wikidata_entity_base_url + f"/{qid}.json",
                                           interval=0.5, on_event=on_event)
        try:
            entity = response.data["entities"][qid]
            if entity.get("id") != qid:
                raise KeyError("Entity mismatch")
            descriptions = entity.get("descriptions", {})
            for language in ("vi", "en"):
                text = descriptions.get(language, {}).get("value")
                if isinstance(text, str) and text.strip():
                    return Fact[str](value=text, source=response.source + f"#description-{language}; retrieved={response.retrieved_at}", confidence=0.75)
        except (TypeError, KeyError, AttributeError):
            raise DataServiceError("wikidata: malformed or mismatched entity") from None
        return None

    async def search(self, destination_text, interests=(), *, on_event=None, trip_request=None):
        result = PlaceSearchResult(notes=[Fact[str](value="© OpenStreetMap contributors; dữ liệu cộng đồng cần kiểm tra lại.",
                                                   source="https://www.openstreetmap.org/copyright", confidence=1)])
        def warning(text, source):
            result.warnings.append(Fact[str](value=text, source=source, confidence=1))
        try:
            result.destination = await self.geocode(destination_text, on_event=on_event)
        except DataServiceError as error:
            warning(str(error), self.settings.nominatim_base_url)
        variant = telex_search_variant(destination_text)
        if result.destination is None and variant:
            try:
                resolved = await self.geocode(variant, on_event=on_event, expected_name=variant)
                if resolved is not None:
                    result.destination = resolved
                    result.normalized_destination = Fact(value=resolved.name.value,
                        source=f'derived:geocoder-name-normalization; original={destination_text}; {resolved.name.source}', confidence=resolved.name.confidence)
                    result.notes.append(Fact(value=f'Đã chuẩn hóa tên điểm đến “{destination_text}” thành “{resolved.name.value}” theo kết quả bản đồ.',
                        source=result.normalized_destination.source, confidence=1))
            except DataServiceError as error:
                warning(str(error), self.settings.nominatim_base_url)
        if result.destination is None:
            warning("Chưa xác định được tọa độ điểm đến; không tự tạo địa điểm dự phòng.", self.settings.nominatim_base_url)
            return result
        result.notes.append(Fact[str](value="Điểm đến được lấy từ ứng viên hợp lệ đầu tiên của geocoder; cần kiểm tra khi tên mơ hồ.",
                                      source=result.destination.name.source, confidence=1))
        try:
            candidates = await asyncio.wait_for(self.nearby(result.destination, on_event=on_event),timeout=self.settings.data_timeout_seconds)
        except asyncio.TimeoutError:
            warning('overpass: hết thời gian tìm địa điểm; tiếp tục với nguồn độc lập.',self.settings.overpass_url)
            candidates=[]
        except DataServiceError as error:
            warning(str(error), self.settings.overpass_url)
            candidates=[]
        popularity=getattr(self.settings,'research_popularity_enabled',True)
        if (not candidates or popularity) and getattr(self.settings,'research_wikidata_fallback_enabled',True):
            result.notes.append(Fact(value='Tìm thêm các điểm nổi bật từ Wikidata trong vùng lân cận.' if candidates else 'Không có địa điểm từ Overpass; thử nguồn Wikidata độc lập.',source='policy:independent-place-provider-fallback',confidence=1))
            if on_event and not candidates:
                notified=on_event({'provider':'wikidata-query','cache_hit':False,'status':'failover','from_host':'overpass','to_host':urlsplit(self.settings.wikidata_query_url).hostname})
                if inspect.isawaitable(notified): await notified
            try:
                regional=await WikidataPlaces(self.settings,self.http).nearby(result.destination,on_event=on_event,
                    radius_m=max(self.settings.research_radius_m,self.settings.research_highlight_radius_m) if popularity else None,
                    limit=self.settings.research_candidate_limit if popularity else None)
                known={place.wikidata_id or qid or place.id:place for place,qid,_ in candidates}
                for item in regional:
                    identity=item[0].wikidata_id or item[1] or item[0].id
                    if identity in known:
                        # Keep OSM coordinates/fees/hours, but do not lose the
                        # Wikidata popularity of a duplicated well-known place.
                        existing=known[identity]
                        for field in ('wikidata_sitelinks','description','official_website','tourism_kind'):
                            if getattr(existing,field) is None and getattr(item[0],field) is not None:
                                setattr(existing,field,getattr(item[0],field))
                    else:
                        candidates.append(item)
                        known[identity]=item[0]
            except DataServiceError as error:
                warning(str(error),self.settings.wikidata_query_url)
        interests_text = " ".join(interests).casefold()
        def rank(candidate):
            place, _, tags = candidate
            score = int(("biển" in interests_text or "beach" in interests_text) and tags.get("natural") == "beach")
            score += int(("bảo tàng" in interests_text or "museum" in interests_text) and tags.get("tourism") == "museum")
            return -score, place.id
        candidates=[item for item in candidates if tourism_candidate(item[0],item[2],trip_request)]
        if popularity:
            candidates=sorted(candidates,key=lambda item: -(item[0].wikidata_sitelinks.value if item[0].wikidata_sitelinks else 0))[:self.settings.research_candidate_limit]
            messages=await PopularityService(self.settings,self.http).enrich(candidates,on_event=on_event)
            for message in messages:
                warning(message,'execution:place-popularity')
            candidates=rank_candidates(candidates,result.destination,interests,trip_request)
            candidates=diverse_selection(candidates,self.settings.research_max_places)
            result.notes.append(Fact(value='Địa điểm được xếp hạng bằng sitelinks Wikidata, lượt xem Wikipedia trong 30 ngày có dữ liệu, sở thích, khoảng cách và hồ sơ nhóm. Độ nổi tiếng không chứng minh an toàn hay phù hợp trẻ nhỏ.',
                source='policy:place-popularity-ranking',confidence=1))
        else:
            candidates.sort(key=rank)
        enriched = 0
        for place, qid, _ in candidates[:self.settings.research_max_places]:
            if qid and place.description is None and enriched < self.settings.research_max_wikidata_requests:
                enriched += 1
                try:
                    description = await self.wikidata_description(qid, on_event=on_event)
                    if description is not None and place.description is None:
                        place = place.model_copy(update={"description": description})
                except DataServiceError as error:
                    warning(str(error), self.settings.wikidata_entity_base_url + f"/{qid}.json")
            result.places.append(place)
        if not result.places:
            warning("Các nguồn chưa trả địa điểm có tên và tọa độ hợp lệ trong phạm vi tìm kiếm.", 'execution:places-search')
        return result
