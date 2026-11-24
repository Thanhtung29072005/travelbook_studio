"""9Router Images-compatible adapter with bounded responses, cache and explicit fallback."""

import base64
import binascii
from dataclasses import dataclass
from hashlib import sha256
import inspect
import json
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from PIL import Image

from .images import prepare_image

MAX_IMAGE_BYTES = 30_000_000


@dataclass
class IllustrationResult:
    image: Image.Image
    prompt: str
    model: str | None
    is_placeholder: bool
    cache_hit: bool = False
    warning: str | None = None
    service_unavailable: bool = False


class IllustrationService:
    def __init__(self, settings, *, client=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(trust_env=False, follow_redirects=False)
        self._owns_client = client is None

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()

    async def _bytes(self, method, url, *, limit, **kwargs):
        async with self.client.stream(method, url, timeout=self.settings.image_timeout_seconds,
                                      follow_redirects=False, **kwargs) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("Redirect not allowed for image transfer")
            data = bytearray()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError("Image response too large")
            return bytes(data)

    def _download_url(self, value):
        parts, base = urlsplit(value), urlsplit(self.settings.nine_router_base_url)
        if parts.username or parts.password or parts.fragment:
            raise ValueError("Invalid image URL")
        same_origin = (parts.scheme, parts.hostname, parts.port) == (base.scheme, base.hostname, base.port)
        allowed = {host.strip().lower() for host in self.settings.image_download_hosts.split(",") if host.strip()}
        if not same_origin and not (parts.scheme == "https" and parts.hostname in allowed and parts.port in {None, 443}):
            raise ValueError("Image URL host is not allowed")
        return value

    @staticmethod
    def _cache_write(path, image):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.stem + "-" + uuid4().hex + ".tmp")
        try:
            image.save(temporary, format="PNG")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)

    async def generate(self, prompt, size, style, *, on_attempt=None, force_fallback=False):
        # Every caller goes through the same style/rules; dimensions are checked before any I/O.
        fallback = prepare_image(None, size, background=style.palette[0])
        prompt = style.apply(prompt)
        def missing(reason, unavailable=False):
            return IllustrationResult(fallback.image, prompt, None, True, warning=reason, service_unavailable=unavailable)
        if force_fallback:
            return missing("offline_requested")
        key = self.settings.nine_router_api_key.get_secret_value().strip()
        configured = self.settings.nine_router_image_model + "," + self.settings.nine_router_image_fallback_models
        models = list(dict.fromkeys(model.strip() for model in configured.split(",") if model.strip()))
        context = getattr(on_attempt, "project_context", None)
        if context:
            from ..workspace import WorkspaceStore
            db, project_id, role = context
            profile = WorkspaceStore(db).agent(project_id, role)
            if profile and profile.get("image_model"):
                models = list(dict.fromkeys(model.strip() for model in [*profile["image_model"].split(","), *profile.get("fallback_models", [])] if model.strip()))
        if not key or not models:
            return missing("image_model_or_key_missing", unavailable=True)
        width, height = size
        api_size = "1024x1536" if height > width else "1536x1024" if width > height else "1024x1024"
        account = sha256(key.encode()).hexdigest()
        unavailable = []
        for index, model in enumerate(models):
            cache_key = sha256(json.dumps(["illustrations-v1", self.settings.nine_router_base_url,
                account, model, prompt, size, style.signature], ensure_ascii=False).encode()).hexdigest()
            cached = Path(self.settings.image_cache_dir) / (cache_key + ".png")
            if cached.is_file():
                try:
                    if cached.stat().st_size <= MAX_IMAGE_BYTES:
                        prepared = prepare_image(cached, size, background=style.palette[0], fallback=False)
                        return IllustrationResult(prepared.image, prompt, model, False, cache_hit=True)
                except (OSError, ValueError, Image.DecompressionBombError):
                    pass
            if on_attempt:
                value = on_attempt(model, 1, index > 0)
                if inspect.isawaitable(value):
                    await value
            try:
                raw = await self._bytes("POST", self.settings.nine_router_base_url + "/images/generations",
                    limit=MAX_IMAGE_BYTES * 2,
                    headers={"Authorization": "Bearer " + key}, json={"model": model, "prompt": prompt,
                        "size": api_size, "n": 1, "response_format": "b64_json"})
                body = json.loads(raw)
                item = body["data"][0]
                if item.get("b64_json"):
                    encoded = item["b64_json"]
                    if not isinstance(encoded, str) or len(encoded) > MAX_IMAGE_BYTES * 1.4:
                        raise ValueError("Invalid encoded image")
                    data = base64.b64decode(encoded, validate=True)
                elif item.get("url"):
                    # Download without the 9Router Authorization header, even on the same host.
                    data = await self._bytes("GET", self._download_url(item["url"]), limit=MAX_IMAGE_BYTES)
                else:
                    raise ValueError("Image endpoint returned no image")
                if len(data) > MAX_IMAGE_BYTES:
                    raise ValueError("Image too large")
                prepared = prepare_image(data, size, background=style.palette[0], fallback=False)
            except httpx.HTTPError:
                unavailable.append(True)
                continue
            except (ValueError, TypeError, KeyError, IndexError, AttributeError, OSError,
                    binascii.Error, Image.DecompressionBombError):
                unavailable.append(False)
                continue
            warning = None
            try:
                self._cache_write(cached, prepared.image)
            except OSError:
                warning = "image_cache_write_failed"
            return IllustrationResult(prepared.image, prompt, model, False, warning=warning)
        return missing("all_image_models_failed_or_invalid", unavailable=bool(unavailable) and all(unavailable))
