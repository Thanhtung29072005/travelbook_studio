"""Small on-demand OSM viewports and independently verified OSRM road geometry."""

import asyncio
from dataclasses import dataclass
from io import BytesIO
import json
import math
from pathlib import Path
import re
import tempfile
import time
from hashlib import sha256
from urllib.parse import urlsplit

import httpx
from PIL import Image

from ..schemas import DayPlan
from .maps import place_index
from .public_api import DataServiceError
from .routing import haversine_km

OSM_ATTRIBUTION = '© OpenStreetMap contributors'
OSM_COPYRIGHT = 'https://www.openstreetmap.org/copyright'


def mercator(lon, lat, zoom):
    if not math.isfinite(lon) or not math.isfinite(lat) or not -180 <= lon <= 180 or not -85.0511 <= lat <= 85.0511:
        raise ValueError('Coordinates outside Web Mercator range')
    scale = 256 * 2**zoom
    return ((lon + 180) / 360 * scale,
            (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * scale)


@dataclass
class MapViewport:
    zoom: int
    left: int
    top: int
    width: int
    height: int
    tiles: list


def viewport(coordinates, *, max_zoom=16, max_tiles=16, width=900, height=640):
    if not coordinates or max(x[0] for x in coordinates)-min(x[0] for x in coordinates)>180:
        raise ValueError('Unsupported empty/dateline-crossing viewport')
    for zoom in range(max_zoom, 1, -1):
        points = [mercator(lon, lat, zoom) for lon, lat in coordinates]
        xs, ys = zip(*points)
        if max(xs)-min(xs) > width*.76 or max(ys)-min(ys) > height*.76:
            continue
        limit = 256 * 2**zoom
        left = max(0, min(limit-width, round((min(xs)+max(xs)-width)/2)))
        top = max(0, min(limit-height, round((min(ys)+max(ys)-height)/2)))
        tiles = [(zoom, x, y) for x in range(left//256, (left+width-1)//256+1)
                 for y in range(top//256, (top+height-1)//256+1)]
        if len(tiles) <= max_tiles:
            return MapViewport(zoom, left, top, width, height, tiles)
    raise ValueError('Viewport exceeds map request budget')


@dataclass
class MapLayer:
    image: Image.Image
    view: MapViewport
    route: list
    route_type: str
    sources: list
    warnings: list


class BasemapService:
    def __init__(self, settings, public_api, *, on_event=None):
        self.settings, self.api, self.on_event = settings, public_api, on_event
        self._semaphore = asyncio.Semaphore(2)
        self._tiles = {}

    async def tile(self, z, x, y):
        key = (z, x, y)
        if key not in self._tiles:
            self._tiles[key] = asyncio.create_task(self._tile(z, x, y))
        return await self._tiles[key]

    async def _tile(self, z, x, y):
        url = self.settings.map_tile_url.format(z=z, x=x, y=y)
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
            raise DataServiceError('map-tiles: HTTPS tile provider required')
        provider = sha256(self.settings.map_tile_url.encode()).hexdigest()[:16]
        path = Path(self.settings.map_tile_cache_dir) / provider / str(z) / str(x) / f'{y}.png'
        metadata = path.with_suffix('.json')
        try:
            cache = json.loads(metadata.read_text(encoding='utf-8'))
            if cache['expires'] > time.time() and path.stat().st_size <= 500000:
                with Image.open(path) as image:
                    if image.size == (256, 256) and image.format == 'PNG':
                        return image.convert('RGB'), url
        except (OSError, ValueError, KeyError, TypeError):
            pass
        async with self._semaphore:
            await self.api._notify(self.on_event, 'osm-map-tiles', False)
            try:
                async with self.api.client.stream('GET', url, follow_redirects=False,
                        headers={'User-Agent': self.settings.data_user_agent, 'Accept': 'image/png'},
                        timeout=self.settings.map_network_timeout_seconds) as response:
                    response.raise_for_status()
                    if 'image/png' not in response.headers.get('content-type', ''):
                        raise DataServiceError('map-tiles: not a PNG response')
                    chunks, length = [], 0
                    async for chunk in response.aiter_bytes():
                        length += len(chunk)
                        if length > 500000:
                            raise DataServiceError('map-tiles: response too large')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                    match = re.search(r'max-age=(\d+)', response.headers.get('cache-control', ''))
                    ttl = int(match[1]) if match else 7*86400
            except httpx.HTTPError:
                raise DataServiceError('map-tiles: tile provider unavailable') from None
            try:
                with Image.open(BytesIO(raw)) as image:
                    if image.format != 'PNG' or image.size != (256, 256):
                        raise ValueError('Invalid tile dimensions')
                    rgb = image.convert('RGB')
            except (OSError, ValueError):
                raise DataServiceError('map-tiles: invalid tile pixels') from None
            # Atomic writes: concurrent projects never consume a half-written tile.
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, suffix='.tmp', delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(raw)
            try:
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            metadata.write_text(json.dumps({'expires': time.time()+ttl, 'source': url}), encoding='utf-8')
            return rgb, url

    async def road(self, selected):
        if len(selected) < 2:
            return [], [], []
        settings = self.settings
        profile = getattr(settings, f'osrm_{settings.routing_mode}_profile', '')
        if not profile or 'osrm' not in settings.routing_provider_order.split(',') or len(selected)>settings.routing_max_points:
            return [], [], ['Chưa có nguồn hình học đường đi đúng phương tiện; nét đứt chỉ thứ tự điểm.']
        coords = ';'.join(f'{p.longitude.value:.6f},{p.latitude.value:.6f}' for p in selected)
        url = settings.osrm_base_url + f'/route/v1/{profile}/' + coords
        try:
            response = await asyncio.wait_for(self.api.request('osrm-route-geometry', url,
                params={'overview': 'full', 'geometries': 'geojson', 'steps': 'false'}, ttl=86400,
                on_event=self.on_event), timeout=settings.map_network_timeout_seconds)
            data = response.data
            if data.get('code') != 'Ok':
                raise ValueError('OSRM route unavailable')
            geometry = data['routes'][0]['geometry']
            route = geometry['coordinates']
            if geometry['type'] != 'LineString' or not 2 <= len(route) <= 30000:
                raise ValueError('Malformed route geometry')
            for point in route:
                if not isinstance(point, list) or len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) for v in point):
                    raise ValueError('Invalid route coordinates')
                mercator(*point, 2)
            waypoints = data['waypoints']
            if len(waypoints) != len(selected):
                raise ValueError('Mismatched route waypoints')
            for place, waypoint in zip(selected, waypoints):
                lon, lat = waypoint['location']
                if haversine_km(place.latitude.value, place.longitude.value, lat, lon)>2:
                    raise ValueError('Route snapped too far from itinerary stop')
            for point, place in ((route[0], selected[0]), (route[-1], selected[-1])):
                if haversine_km(place.latitude.value, place.longitude.value, point[1], point[0])>2:
                    raise ValueError('Route endpoints outside itinerary')
            return route, [response.source + '; retrieved=' + response.retrieved_at], []
        except (DataServiceError, asyncio.TimeoutError, ValueError, KeyError, IndexError, TypeError, AttributeError):
            return [], [], ['Không lấy được tuyến đường thực tế; nét đứt chỉ thứ tự điểm, không phải đường giao thông.']

    async def prepare(self, days, places):
        indexed = place_index(places)
        layers, warnings = {}, []
        if not self.settings.map_basemap_enabled:
            return layers, ['Đã tắt bản đồ nền; sử dụng sơ đồ tọa độ dự phòng.']
        async def one(day):
            day = DayPlan.model_validate(day)
            selected = [indexed[stop.place_id] for stop in day.stops]
            if not selected:
                return
            route, route_sources, route_warnings = await self.road(selected)
            coordinates = [(p.longitude.value, p.latitude.value) for p in selected]
            try:
                view = viewport([*coordinates, *route], max_zoom=self.settings.map_tile_max_zoom,
                                max_tiles=self.settings.map_tile_max_count)
                loaded = await asyncio.gather(*(self.tile(*key) for key in view.tiles), return_exceptions=True)
                if any(isinstance(value, BaseException) for value in loaded):
                    raise DataServiceError('Incomplete map tiles')
                canvas = Image.new('RGB', (view.width, view.height))
                sources = [OSM_COPYRIGHT, *route_sources]
                for (_, x, y), (tile, source) in zip(view.tiles, loaded):
                    canvas.paste(tile, (x*256-view.left, y*256-view.top))
                    sources.append(source)
                layers[day.day_number] = MapLayer(canvas, view, route, 'road_geometry' if route else 'straight_segments' if len(selected)>1 else 'single_stop',
                    list(dict.fromkeys(sources)), route_warnings)
                warnings.extend(f'Ngày {day.day_number}: '+message for message in route_warnings)
            except (DataServiceError, ValueError, OSError):
                warnings.append(f'Ngày {day.day_number}: không tải đủ bản đồ nền; dùng sơ đồ tọa độ dự phòng.')
        try:
            # Bound the entire map stage, not only each individual HTTP request.
            await asyncio.wait_for(self._days(days, one), timeout=self.settings.map_total_timeout_seconds)
        except asyncio.TimeoutError:
            warnings.append('Hết thời gian tải bản đồ; các ngày chưa có bản đồ dùng sơ đồ tọa độ dự phòng.')
        finally:
            for task in self._tiles.values():
                if not task.done():
                    task.cancel()
            if self._tiles:
                await asyncio.gather(*self._tiles.values(), return_exceptions=True)
        return layers, warnings

    @staticmethod
    async def _days(days, callback):
        # Fetch only the requested daily viewport, not adjacent/prefetched regions.
        for day in days:
            await callback(day)
