"""Persisted model chain and explicit, failure-safe 9Router catalog sync."""
import json

import httpx
from fastapi import HTTPException
from pydantic import Field, field_validator

from .db import utcnow
from .schemas import Schema


class ModelSettings(Schema):
    models: list[str] = Field(min_length=1,max_length=20)
    image_model: str = Field(default="",max_length=160)
    image_fallback_models: list[str] = Field(default_factory=list,max_length=10)

    @field_validator("models","image_fallback_models")
    @classmethod
    def normalize(cls, values):
        if any(not value.strip() or len(value.strip())>160 for value in values):
            raise ValueError("Supply nonempty model IDs of at most 160 characters")
        return list(dict.fromkeys(value.strip() for value in values))


def mount_models(app, db, settings):
    def catalog():
        with db.connect() as connection:
            row=connection.execute("SELECT value FROM app_settings WHERE key='router_catalog'").fetchone()
        return json.loads(row[0]) if row else {"ids":[],"synced_at":None}

    def response(error=None):
        saved=catalog()
        ids=list(dict.fromkeys(item for item in [*saved["ids"],*settings.models,settings.nine_router_image_model,
            *settings.nine_router_image_fallback_models.split(',')] if item))
        return {"router":settings.nine_router_base_url,"api_key_configured":bool(settings.nine_router_api_key.get_secret_value()),
            "models":settings.models,"image_model":settings.nine_router_image_model,
            "image_fallback_models":[item.strip() for item in settings.nine_router_image_fallback_models.split(',') if item.strip()],
            "available_models":ids,"synced_at":saved.get("synced_at"),"catalog_error":error}

    @app.get('/api/router/models',tags=['Settings'])
    async def get_models():
        return response()

    @app.post('/api/router/models/sync',tags=['Settings'])
    async def sync_models():
        key=settings.nine_router_api_key.get_secret_value()
        if not key: raise HTTPException(422,"NINE_ROUTER_API_KEY is not configured")
        try:
            async with httpx.AsyncClient(timeout=8) as client:
                result=await client.get(settings.nine_router_base_url+'/models',headers={"Authorization":"Bearer "+key})
            result.raise_for_status()
            data=result.json()
            if not isinstance(data,dict) or not isinstance(data.get('data'),list): raise ValueError('Invalid catalog')
            ids=[]
            for item in data['data']:
                if not isinstance(item,dict) or not isinstance(item.get('id'),str) or not item['id'].strip() or len(item['id'].strip())>160:
                    raise ValueError('Invalid catalog model')
                ids.append(item['id'].strip())
            if not ids: raise ValueError('Empty catalog')
        except httpx.HTTPStatusError as error:
            raise HTTPException(502,f"9Router returned HTTP {error.response.status_code}; saved catalog kept") from None
        except (httpx.HTTPError,ValueError):
            raise HTTPException(502,"Cannot sync 9Router catalog; saved catalog kept") from None
        saved={"ids":list(dict.fromkeys(ids)),"synced_at":utcnow()}
        with db.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_settings VALUES('router_catalog',?)",(json.dumps(saved),))
        return response()

    @app.put('/api/router/models',tags=['Settings'])
    async def save_models(body: ModelSettings):
        body.image_model=body.image_model.strip()
        clean=body.model_dump()
        with db.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO app_settings VALUES('router_models',?)",(json.dumps(clean),))
        settings.nine_router_models=','.join(body.models)
        settings.nine_router_image_model=body.image_model
        settings.nine_router_image_fallback_models=','.join(body.image_fallback_models)
        return response()
