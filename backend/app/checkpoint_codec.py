"""Explicit, versioned codec for domain State; never imports types from saved data."""
import base64
from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
from uuid import UUID

from langgraph.types import Send, Interrupt
from pydantic import BaseModel, AnyUrl

from . import schemas

MODELS = {value.__name__: value for value in vars(schemas).values()
    if isinstance(value, type) and issubclass(value, BaseModel) and value.__module__ == schemas.__name__
    and not value.__pydantic_generic_metadata__.get("origin")}


def encode(value):
    if value is None or isinstance(value, (str, bool, int, float)): return value
    if isinstance(value, BaseModel):
        origin = value.__class__.__pydantic_generic_metadata__.get("origin") or value.__class__
        if MODELS.get(origin.__name__) is not origin: raise TypeError("Unsupported checkpoint model")
        return {"$": "model", "name": origin.__name__, "data": encode(value.model_dump(mode="python", exclude_computed_fields=True))}
    if isinstance(value, dict): return {"$":"dict", "items":[[encode(key),encode(item)] for key,item in value.items()]}
    if isinstance(value, list): return {"$":"list", "items":[encode(item) for item in value]}
    if isinstance(value, (tuple, set, frozenset)):
        return {"$":type(value).__name__, "items":[encode(item) for item in value]}
    if isinstance(value, (datetime, date, time)): return {"$":type(value).__name__, "value":value.isoformat()}
    if isinstance(value, Decimal): return {"$":"decimal", "value":str(value)}
    if isinstance(value, AnyUrl): return {"$":"url", "value":str(value)}
    if isinstance(value, bytes): return {"$":"bytes", "value":base64.b64encode(value).decode("ascii")}
    if isinstance(value, UUID): return {"$":"uuid", "value":str(value)}
    if isinstance(value, timedelta): return {"$":"timedelta", "days":value.days,"seconds":value.seconds,"microseconds":value.microseconds}
    if isinstance(value, Send): return {"$":"send", "node":value.node,"arg":encode(value.arg)}
    if isinstance(value, Interrupt): return {"$":"interrupt", "id":value.id,"value":encode(value.value)}
    if isinstance(value, BaseException): return {"$":"error", "name":type(value).__name__, "message":str(value)}
    raise TypeError("Unsupported checkpoint value: "+type(value).__name__)


def decode(value):
    if not isinstance(value, dict): return value
    kind = value["$"]
    if kind == "model": return MODELS[value["name"]].model_validate(decode(value["data"]))
    if kind == "dict": return {decode(key):decode(item) for key,item in value["items"]}
    if kind in {"list", "tuple", "set", "frozenset"}:
        return {"list":list,"tuple":tuple,"set":set,"frozenset":frozenset}[kind](decode(item) for item in value["items"])
    if kind in {"datetime", "date", "time"}: return {"datetime":datetime,"date":date,"time":time}[kind].fromisoformat(value["value"])
    if kind == "decimal": return Decimal(value["value"])
    if kind == "url": return AnyUrl(value["value"])
    if kind == "bytes": return base64.b64decode(value["value"],validate=True)
    if kind == "uuid": return UUID(value["value"])
    if kind == "timedelta": return timedelta(days=value["days"],seconds=value["seconds"],microseconds=value["microseconds"])
    if kind == "send": return Send(value["node"],decode(value["arg"]))
    if kind == "interrupt": return Interrupt(value=decode(value["value"]),id=value["id"])
    if kind == "error": return RuntimeError(value["name"]+": "+value["message"])
    raise ValueError("Unsupported checkpoint tag")


class StateSerializer:
    def dumps(self, value): return json.dumps(encode(value),ensure_ascii=False,allow_nan=False,separators=(",",":")).encode("utf-8")
    def loads(self, value): return decode(json.loads(value))
    def dumps_typed(self, value): return "travelbook-json-v1",self.dumps(value)
    def loads_typed(self, value):
        if value[0] != "travelbook-json-v1": raise ValueError("Unsupported checkpoint encoding")
        return self.loads(value[1])
