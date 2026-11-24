"""Identified, cached public API requests with shared SQLite rate reservations."""

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import inspect
import json
import sqlite3
import time

import httpx


class DataServiceError(RuntimeError):
    def __init__(self, message, *, status_code=None, provider_code=None):
        super().__init__(message)
        self.status_code, self.provider_code = status_code, provider_code


@dataclass(frozen=True)
class JsonResponse:
    data: object
    source: str
    retrieved_at: str
    cache_hit: bool


class PublicAPIClient:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(trust_env=getattr(settings,'data_trust_env',True), follow_redirects=True)
        self._owns_client = client is None

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

    def _connect(self):
        path = self.settings.data_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(path, timeout=10)
        db.execute("CREATE TABLE IF NOT EXISTS api_cache (key TEXT PRIMARY KEY, expires REAL, body TEXT, source TEXT, retrieved TEXT)")
        db.execute("CREATE TABLE IF NOT EXISTS api_rate (provider TEXT PRIMARY KEY, next_at REAL)")
        return db

    async def request(self, provider, url, *, params=None, form=None, json_body=None, headers=None,
                      ttl=604800, interval=0, on_event=None):
        if form is not None and json_body is not None:
            raise ValueError("Supply form or JSON body, not both")
        method = "POST" if form is not None or json_body is not None else "GET"
        identity = sha256(json.dumps(headers or {}, sort_keys=True).encode()).hexdigest()
        key = sha256(json.dumps([method, url, params, form, json_body, identity], sort_keys=True).encode()).hexdigest()
        db = self._connect()
        try:
            row = db.execute("SELECT expires,body,source,retrieved FROM api_cache WHERE key=?", (key,)).fetchone()
            if row and row[0] > time.time():
                try:
                    result = JsonResponse(json.loads(row[1]), row[2], row[3], True)
                except (ValueError, TypeError):
                    result = None
                if result is not None:
                    await self._notify(on_event, provider, True)
                    return result
            delay = 0
            if interval:
                # BEGIN IMMEDIATE serializes reservations across clients/processes sharing this DB.
                db.execute("BEGIN IMMEDIATE")
                rate_key = provider + ":" + url.split("/", 3)[2]
                row = db.execute("SELECT next_at FROM api_rate WHERE provider=?", (rate_key,)).fetchone()
                now = time.time()
                slot = max(now, row[0] if row else now)
                db.execute("INSERT OR REPLACE INTO api_rate VALUES(?,?)", (rate_key, slot + interval))
                db.commit()
                delay = slot - now
        finally:
            db.close()
        if delay:
            await asyncio.sleep(delay)
        await self._notify(on_event, provider, False)
        try:
            response = await self.client.request(method, url, params=params, data=form, json=json_body,
                headers={"User-Agent": self.settings.data_user_agent, "Accept": "application/json", **(headers or {})},
                timeout=self.settings.data_timeout_seconds)
            response.raise_for_status()
            if len(response.content) > 5_000_000:
                raise DataServiceError(f"{provider}: response too large")
            body = response.json()
            if not isinstance(body, (dict, list)):
                raise ValueError("Expected JSON object/array")
            if isinstance(body, dict) and (body.get("error") or body.get("remark")):
                raise DataServiceError(f"{provider}: provider reported an error or incomplete result")
            serialized = json.dumps(body, allow_nan=False)
        except httpx.HTTPStatusError as error:
            provider_code = None
            try:
                error_body = error.response.json()
                value = error_body.get("code") if isinstance(error_body, dict) else None
                if isinstance(value, str) and len(value) <= 50:
                    provider_code = value
            except ValueError:
                pass
            raise DataServiceError(f"{provider}: HTTP {error.response.status_code}",
                                   status_code=error.response.status_code, provider_code=provider_code) from None
        except httpx.TransportError as error:
            raise DataServiceError(f"{provider}: connection error ({type(error).__name__})",provider_code=type(error).__name__) from None
        except ValueError:
            raise DataServiceError(f"{provider}: invalid JSON response (expected API JSON, not an HTML error page)") from None
        retrieved = datetime.now(timezone.utc).isoformat()
        source = str(response.url)
        db = self._connect()
        try:
            with db:
                db.execute("INSERT OR REPLACE INTO api_cache VALUES(?,?,?,?,?)",
                           (key, time.time() + ttl, serialized, source, retrieved))
        finally:
            db.close()
        return JsonResponse(body, source, retrieved, False)

    @staticmethod
    async def _notify(callback, provider, cache_hit):
        if callback:
            result = callback({"provider": provider, "cache_hit": cache_hit})
            if inspect.isawaitable(result):
                await result
