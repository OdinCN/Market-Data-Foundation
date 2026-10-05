"""Explicit, versioned reference serialization. No executable/pickle payloads."""

import json
from dataclasses import fields, is_dataclass
from datetime import datetime
from enum import Enum

from . import model
from .model import require

_TYPES = {
    name: value
    for name, value in vars(model).items()
    if isinstance(value, type)
    and value.__module__ == model.__name__
    and (is_dataclass(value) or issubclass(value, Enum))
}


def encode(value: object) -> object:
    if isinstance(value, Enum):
        return {"type": type(value).__name__, "value": value.value}
    if isinstance(value, datetime):
        return {"type": "datetime", "value": value.isoformat()}
    if isinstance(value, bytes):
        return {"type": "bytes", "value": value.hex()}
    if isinstance(value, (tuple, frozenset)):
        values = [encode(item) for item in value]
        if isinstance(value, frozenset):
            values.sort(key=lambda item: json.dumps(item, sort_keys=True))
        return {"type": type(value).__name__, "value": values}
    if is_dataclass(value):
        return {
            "type": type(value).__name__,
            "value": {
                field.name: encode(getattr(value, field.name))
                for field in fields(value)
            },
        }
    require(
        value is None or type(value) in {str, int, bool, float}, "unsupported_value"
    )
    return value


def decode(value: object) -> object:
    if not isinstance(value, dict):
        require(
            value is None or type(value) in {str, int, bool, float}, "invalid_encoding"
        )
        return value
    require(set(value) == {"type", "value"}, "invalid_encoding")
    name, body = value["type"], value["value"]
    if name == "datetime":
        return datetime.fromisoformat(body)
    if name == "bytes":
        return bytes.fromhex(body)
    if name in {"tuple", "frozenset"}:
        return (tuple if name == "tuple" else frozenset)(decode(item) for item in body)
    require(name in _TYPES, "unknown_contract_type")
    target = _TYPES[name]
    if issubclass(target, Enum):
        return target(body)
    return target(**{key: decode(item) for key, item in body.items()})


def dumps(value: object) -> str:
    return json.dumps(
        encode(value), sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def loads(value: str) -> object:
    return decode(json.loads(value))
