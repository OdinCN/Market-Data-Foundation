"""Strict decoding of the frozen D1 Plan stored in a D2 durable intent."""

import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import StrEnum
from types import UnionType
from typing import get_args, get_origin, get_type_hints

from market_data_foundation.domain_contracts.market_planning import Plan
from market_data_foundation.domain_contracts.market_universe import canonical_bytes
from market_data_foundation.historical.market_daily_source import DailyError, Failure


def decode_plan(raw: bytes) -> Plan:
    # Types come solely from the compiled D1 contract, never from stored names,
    # pickle, dynamic imports or eval of journal input.
    def decode(value: object, expected: type) -> object:
        origin, args = get_origin(expected), get_args(expected)
        if origin is UnionType:
            if value is None and type(None) in args:
                return None
            choices = tuple(item for item in args if item is not type(None))
            if len(choices) != 1:
                raise ValueError()
            return decode(value, choices[0])
        if origin is tuple:
            if type(value) is not list:
                raise ValueError()
            if len(args) == 2 and args[1] is Ellipsis:
                return tuple(decode(item, args[0]) for item in value)
            if len(value) != len(args):
                raise ValueError()
            return tuple(
                decode(item, kind) for item, kind in zip(value, args, strict=True)
            )
        if expected is datetime:
            return datetime.fromisoformat(value)
        if expected is date:
            return date.fromisoformat(value)
        if isinstance(expected, type) and issubclass(expected, StrEnum):
            return expected(value)
        if is_dataclass(expected):
            if type(value) is not dict or set(value) != {
                field.name for field in fields(expected)
            }:
                raise ValueError()
            hints = get_type_hints(expected)
            return expected(
                **{name: decode(item, hints[name]) for name, item in value.items()}
            )
        if expected in (str, int, bool) and type(value) is expected:
            return value
        raise ValueError()

    try:
        plan = decode(json.loads(raw), Plan)
        if canonical_bytes(plan) != raw:
            raise ValueError()
        return plan
    except ValueError, TypeError, KeyError, RecursionError:
        raise DailyError(Failure.CORRUPT) from None
