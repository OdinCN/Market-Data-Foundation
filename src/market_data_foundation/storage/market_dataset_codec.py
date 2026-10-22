"""Closed, non-executable D1/D3 physical encoding, separate from old P4 codec."""

import json
from dataclasses import fields, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum

from market_data_foundation.canonical import (
    market_material,
    market_persistence,
    market_projection,
    market_publication,
    model,
)
from market_data_foundation.domain_contracts import (
    market_coverage,
    market_planning,
    market_universe,
)
from market_data_foundation.qualification.contracts import Capability

REVISION = "market.dataset.codec.r1"
MAX_PAYLOAD = 16 * 1024 * 1024
_TYPES = {"Capability": Capability}
for _module in (
    model,
    market_material,
    market_persistence,
    market_projection,
    market_publication,
    market_coverage,
    market_planning,
    market_universe,
):
    for _name, _value in vars(_module).items():
        if (
            isinstance(_value, type)
            and _value.__module__ == _module.__name__
            and (is_dataclass(_value) or issubclass(_value, Enum))
        ):
            model.require(_name not in _TYPES, "codec_type_collision")
            _TYPES[_name] = _value


def _encode(value):
    if isinstance(value, Enum):
        return ["enum", type(value).__name__, value.value]
    if isinstance(value, datetime):
        model.instant(value)
        return ["datetime", value.astimezone(UTC).isoformat()]
    if type(value) is date:
        return ["date", value.isoformat()]
    if type(value) is bytes:
        return ["bytes", value.hex()]
    if type(value) is tuple:
        return ["tuple", [_encode(item) for item in value]]
    if is_dataclass(value):
        name = type(value).__name__
        model.require(_TYPES.get(name) is type(value), "codec_type_not_allowed")
        return [
            "record",
            name,
            {
                field.name: _encode(getattr(value, field.name))
                for field in fields(value)
            },
        ]
    model.require(value is None or type(value) in (str, int, bool), "codec_value")
    return value


def _decode(value):
    if type(value) is not list:
        model.require(value is None or type(value) in (str, int, bool), "codec_value")
        return value
    tag = value[0]
    if tag == "record":
        target = _TYPES[value[1]]
        model.require(is_dataclass(target), "codec_record")
        model.require(set(value[2]) == {f.name for f in fields(target)}, "codec_fields")
        return target(**{k: _decode(v) for k, v in value[2].items()})
    if tag == "enum":
        target = _TYPES[value[1]]
        model.require(issubclass(target, Enum), "codec_enum")
        return target(value[2])
    if tag == "tuple":
        return tuple(_decode(item) for item in value[1])
    if tag == "datetime":
        return datetime.fromisoformat(value[1])
    if tag == "date":
        return date.fromisoformat(value[1])
    if tag == "bytes":
        return bytes.fromhex(value[1])
    raise model.ContractError("codec_tag")


def dumps(value: object) -> str:
    raw = json.dumps([REVISION, _encode(value)], sort_keys=True, separators=(",", ":"))
    model.require(len(raw.encode()) <= MAX_PAYLOAD, "codec_capacity")
    return raw


def loads(raw: str):
    model.require(
        type(raw) is str and len(raw.encode()) <= MAX_PAYLOAD, "codec_capacity"
    )
    try:
        revision, body = json.loads(raw)
        model.require(revision == REVISION, "codec_revision")
        result = _decode(body)
        model.require(dumps(result) == raw, "codec_noncanonical")
        return result
    except Exception:
        raise model.ContractError("invalid_market_storage_encoding") from None
