"""LangChain structured-output pipeline over 9Router's Chat Completions API.

Uses LangChain core directly so the transport remains provider-neutral and
testable. Only schema-valid responses are cached; no prompts or API keys are
stored in SQLite. Cache rows contain generated output and usage metadata.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import math
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Callable, Generic, TypeVar

import httpx
from langchain_core.exceptions import OutputParserException
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import RunnableLambda
from pydantic import BaseModel, ValidationError

from ..config import Settings

T = TypeVar("T", bound=BaseModel)


class RouterError(RuntimeError):
    """Safe error: never includes provider response bodies or credentials."""


class RouterExhaustedError(RouterError):
    pass


class StructuredOutputError(RouterError):
    pass


@dataclass(frozen=True)
class StructuredResult(Generic[T]):
    data: T
    model: str
    cache_hit: bool
    cache_key: str
    usage: dict[str, Any]


class NineRouterClient:
    def __init__(self, settings: Settings, *, client: httpx.AsyncClient | None = None):
        self.settings = settings
        self._client = client or httpx.AsyncClient(timeout=settings.llm_timeout_seconds)
        self._owns_client = client is None
        self._inflight: dict[str, tuple[asyncio.Lock, int]] = {}

    async def __aenter__(self) -> NineRouterClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    def _cache_connection(self) -> sqlite3.Connection:
        path = self.settings.llm_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(path, timeout=10)
        connection.execute(
            "CREATE TABLE IF NOT EXISTS llm_cache ("
            "cache_key TEXT PRIMARY KEY, created_at REAL NOT NULL, payload TEXT NOT NULL)"
        )
        return connection

    def _cache_get(self, key: str, schema: type[T]) -> StructuredResult[T] | None:
        if not self.settings.llm_cache_enabled:
            return None
        connection = self._cache_connection()
        try:
            row = connection.execute(
                "SELECT created_at, payload FROM llm_cache WHERE cache_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            try:
                if time.time() - row[0] >= self.settings.llm_cache_ttl_seconds:
                    raise ValueError("expired")
                cached = json.loads(row[1])
                data = schema.model_validate(cached["data"])
                model = cached["model"]
                if not isinstance(model, str) or not isinstance(cached["usage"], dict):
                    raise ValueError("invalid cache metadata")
                return StructuredResult(
                    data=data, model=model, cache_hit=True, cache_key=key,
                    usage={"prompt_tokens": 0, "completion_tokens": 0,
                           "original_usage": cached["usage"]},
                )
            except (ValueError, KeyError, TypeError, ValidationError):
                connection.execute("DELETE FROM llm_cache WHERE cache_key=?", (key,))
                connection.commit()
                return None
        finally:
            connection.close()

    def _cache_put(self, result: StructuredResult) -> None:
        if not self.settings.llm_cache_enabled:
            return
        # Computed fields (e.g. Budget.total) cannot be supplied as validation input.
        data = result.data.model_dump(mode="json", exclude_computed_fields=True)
        payload = json.dumps({"data": data, "model": result.model, "usage": result.usage})
        connection = self._cache_connection()
        try:
            with connection:
                connection.execute(
                    "INSERT OR REPLACE INTO llm_cache VALUES (?, ?, ?)",
                    (result.cache_key, time.time(), payload),
                )
        finally:
            connection.close()

    def cache_key(
        self, *, system: str, prompt: str, schema: type[BaseModel],
        models: list[str], temperature: float, images: list[dict[str, str]] | None = None,
    ) -> str:
        context = {
            "version": 1, "base_url": self.settings.nine_router_base_url,
            "account": hashlib.sha256(
                self.settings.nine_router_api_key.get_secret_value().encode()
            ).hexdigest(),
            "system": system, "prompt": prompt,
            "schema": schema.model_json_schema(mode="validation"),
            "models": models, "temperature": temperature,
            "mode": self.settings.llm_structured_output_mode,
        }
        if images:
            context["images"] = [{"id": item["id"], "sha256": hashlib.sha256(item["data_url"].encode()).hexdigest()} for item in images]
        serialized = json.dumps(context, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    async def chat_structured(
        self, *, system: str, prompt: str, schema: type[T], temperature: float = 0.3,
        models: list[str] | None = None,
        on_attempt: Callable[[str, int, bool], Any] | None = None,
        images: list[dict[str, str]] | None = None,
    ) -> StructuredResult[T]:
        context = getattr(on_attempt, "project_context", None)
        if context:
            from ..workspace import WorkspaceStore
            db, project_id, role = context
            profile = WorkspaceStore(db).agent(project_id, role)
            if profile is None:
                raise RouterError("Agent role is disabled or removed")
            if profile:
                chosen = profile.get("image_model") if images else profile.get("model")
                if chosen:
                    models = [chosen, *profile.get("fallback_models", [])]
                elif profile.get("fallback_models"):
                    models = [*self.settings.models[:1], *profile["fallback_models"]]
                if profile.get("temperature") is not None:
                    temperature = profile["temperature"]
                if profile.get("instructions"):
                    system += "\nProject role instructions (preserve original schema/evidence constraints):\n" + profile["instructions"]
        if not self.settings.nine_router_api_key.get_secret_value().strip():
            raise RouterError("NINE_ROUTER_API_KEY is not configured")
        model_chain = list(dict.fromkeys(m.strip() for m in (
            self.settings.models if models is None else models
        ) if m.strip()))
        if not model_chain:
            raise RouterError("Configure NINE_ROUTER_MODELS: primary, fallback1, fallback2")
        if not prompt.strip() or not system.strip():
            raise ValueError("system and prompt must not be blank")
        if not math.isfinite(temperature) or not 0 <= temperature <= 2:
            raise ValueError("temperature must be between 0 and 2")
        if images is not None:
            if len(images) > 12 or len({item["id"] for item in images}) != len(images):
                raise ValueError("At most 12 uniquely identified images")
            if any(not item["data_url"].startswith("data:image/jpeg;base64,") or len(item["data_url"]) > 1500000 for item in images):
                raise ValueError("Expected bounded JPEG data URLs")
        key = self.cache_key(
            system=system, prompt=prompt, schema=schema, models=model_chain, temperature=temperature,
            images=images,
        )
        lock, users = self._inflight.get(key, (asyncio.Lock(), 0))
        self._inflight[key] = (lock, users + 1)
        try:
            async with lock:
                cached = self._cache_get(key, schema)
                if cached is not None:
                    return cached
                result = await self._generate(
                    key=key, system=system, prompt=prompt, schema=schema,
                    models=model_chain, temperature=temperature, on_attempt=on_attempt,
                    images=images,
                )
                self._cache_put(result)
                return result
        finally:
            _, users = self._inflight[key]
            if users == 1:
                del self._inflight[key]
            else:
                self._inflight[key] = (lock, users - 1)

    async def chat_json(self, **kwargs: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        """Agent-facing convenience API; schema is a required Pydantic model class."""
        result = await self.chat_structured(**kwargs)
        return result.data.model_dump(mode="json"), {
            **result.usage, "model": result.model,
            "cache_hit": result.cache_hit, "cache_key": result.cache_key,
        }

    async def _generate(
        self, *, key: str, system: str, prompt: str, schema: type[T],
        models: list[str], temperature: float, on_attempt: Callable | None,
        images: list[dict[str, str]] | None = None,
    ) -> StructuredResult[T]:
        parser = PydanticOutputParser(pydantic_object=schema)
        template = ChatPromptTemplate.from_messages([
            ("system", "{system}"),
            ("human", "{prompt}\n\nReturn only a complete JSON object.\n{instructions}"),
        ])
        attempts: list[str] = []
        for model_index, model in enumerate(models):
            for attempt in range(1, self.settings.llm_max_attempts_per_model + 1):
                if on_attempt is not None:
                    notified = on_attempt(model, attempt, model_index > 0)
                    if inspect.isawaitable(notified):
                        await notified
                usage: dict[str, int] = {}

                async def send(value):
                    content, tokens = await self._request(model, value, schema, temperature, **({"images": images} if images else {}))
                    usage.update(tokens)
                    # LangChain parsers may repair partial JSON. Reject truncated JSON first.
                    text = content.strip()
                    if text.startswith("```json\n") and text.endswith("```"):
                        text = text[8:-3].strip()
                    elif text.startswith("```\n") and text.endswith("```"):
                        text = text[4:-3].strip()
                    decoded = json.loads(text)
                    if not isinstance(decoded, dict):
                        raise StructuredOutputError("Expected a JSON object")
                    # Validate raw JSON too: no bypass of schema extra-field/type rules.
                    schema.model_validate_json(text)
                    return text

                pipeline = template | RunnableLambda(send) | parser
                try:
                    data = await pipeline.ainvoke({
                        "system": system, "prompt": prompt,
                        "instructions": parser.get_format_instructions(),
                    })
                    return StructuredResult(data, model, False, key, usage)
                except httpx.HTTPStatusError as error:
                    code = error.response.status_code
                    if code in {401, 403}:
                        raise RouterError(f"9Router authentication/authorization failed (HTTP {code})") from None
                    attempts.append(f"{model}:HTTP {code}")
                    if code in {402, 404, 429}:
                        break  # Move directly to the next model.
                    if code not in {408, 425} and code < 500:
                        raise RouterError(f"9Router rejected the request (HTTP {code})") from None
                except httpx.TransportError:
                    attempts.append(f"{model}:connection/timeout error")
                except (ValueError, OutputParserException, StructuredOutputError):
                    attempts.append(f"{model}:invalid structured output")
                if attempt < self.settings.llm_max_attempts_per_model:
                    await asyncio.sleep(self.settings.llm_retry_delay_seconds)
        raise RouterExhaustedError("All configured models failed: " + "; ".join(attempts)) from None

    async def _request(
        self, model: str, prompt_value: Any, schema: type[BaseModel], temperature: float,
        images: list[dict[str, str]] | None = None,
    ) -> tuple[str, dict[str, int]]:
        payload: dict[str, Any] = {
            "model": model, "temperature": temperature,
            "messages": [
                {"role": "system" if message.type == "system" else "user", "content": message.content}
                for message in prompt_value.to_messages()
            ],
        }
        if images:
            human = next(message for message in payload["messages"] if message["role"] == "user")
            human["content"] = [{"type": "text", "text": human["content"]}]
            for item in images:
                human["content"].extend([
                    {"type": "text", "text": "Evidence image ID: " + item["id"]},
                    {"type": "image_url", "image_url": {"url": item["data_url"]}},
                ])
        mode = self.settings.llm_structured_output_mode
        if mode == "json_object":
            payload["response_format"] = {"type": "json_object"}
        elif mode == "json_schema":
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": schema.__name__, "strict": False,
                                "schema": schema.model_json_schema(mode="validation")},
            }
        response = await self._client.post(
            self.settings.nine_router_base_url + "/chat/completions",
            headers={"Authorization": "Bearer " + self.settings.nine_router_api_key.get_secret_value()},
            json=payload, timeout=self.settings.llm_timeout_seconds,
        )
        response.raise_for_status()
        try:
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") in {"length", "content_filter"}:
                raise StructuredOutputError("Incomplete or filtered response")
            content = choice["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise StructuredOutputError("Empty response content")
            original_usage = body.get("usage") or {}
            usage = {
                name: original_usage.get(name, 0) for name in ("prompt_tokens", "completion_tokens")
            }
            if any(type(value) is not int or value < 0 for value in usage.values()):
                raise StructuredOutputError("Invalid usage metadata")
            return content, usage
        except (KeyError, IndexError, TypeError, ValueError):
            raise StructuredOutputError("Malformed Chat Completions response") from None
