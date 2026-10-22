"""Pinned v1.0.0 field evidence; no Source admission or economic inference."""

import hashlib
import json
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, datetime
from enum import StrEnum

SDK_VERSION = "v1.0.0"
SDK_COMMIT = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
DECODE_REVISION = "mdf.exhq.directory.decode.v1"
SCHEMA_REVISION = "mdf.exhq.directory.snapshot.v1"
IDENTITY_NAMESPACE = "tdx.exhq.source_identity.v1"
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_DIRECTORY_BYTES = 16 * 1024 * 1024
MAX_DIRECTORY_ROWS = 100_000
MAX_DIRECTORY_PAGES = 1024


class DirectoryError(ValueError):
    """Stable classification only: never include raw bytes or exception messages."""

    def __init__(self, classification: str):
        self.classification = classification
        super().__init__(classification)


def require(condition: bool, classification: str) -> None:
    if not condition:
        raise DirectoryError(classification)


def uint(value: int, bits: int, field: str) -> None:
    require(type(value) is int and 0 <= value < 2**bits, f"invalid_{field}_uint{bits}")


def text(value: str) -> None:
    require(type(value) is str and bool(value.strip()), "nonempty_text_required")


def timestamp(value: datetime | None) -> None:
    require(
        value is None or (type(value) is datetime and value.utcoffset() is not None),
        "aware_timestamp_or_unknown_required",
    )


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def content_hash(value: object) -> str:
    """Explicit v1 canonical JSON: UTC instants, bytes as hex, ordered arrays."""

    def encode(item: object) -> object:
        if isinstance(item, datetime):
            timestamp(item)
            return item.astimezone(UTC).isoformat()
        if isinstance(item, StrEnum):
            return item.value
        if is_dataclass(item) and not isinstance(item, type):
            return {f.name: encode(getattr(item, f.name)) for f in fields(item)}
        if type(item) is bytes:
            return {"bytes_hex": item.hex()}
        if type(item) in (tuple, list):
            return [encode(part) for part in item]
        if type(item) is dict:
            require(all(type(key) is str for key in item), "string_hash_keys_required")
            return {key: encode(part) for key, part in item.items()}
        require(item is None or type(item) in (str, int, bool), "noncanonical_content")
        return item

    return digest(
        json.dumps(
            encode(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    )


class Operation(StrEnum):
    MARKETS = "ExMarkets"
    COUNT = "ExCount"
    INSTRUMENTS = "ExInstruments"


class EvidenceMode(StrEnum):
    SYNTHETIC = "synthetic"
    RETAINED = "retained"


@dataclass(frozen=True)
class FieldEvidence:
    response_struct: str
    source_field_name: str
    json_name: str
    go_type: str
    source_ref: str
    transform_ref: str
    sdk_native_unit: str = "UNKNOWN"
    decoded_unit: str = "UNKNOWN"
    semantic_status: str = "UPSTREAM_STRUCT_AND_DECODE"
    market_scope: str = "exact_response_market_category"
    clock_semantics: str = "UNKNOWN"
    nullable_unknown_rule: str = "absent_economics_remain_UNKNOWN"


# Exact AST names/types/tags/lines; ExMarket.Market is uint16 despite a u8 wire read.
FIELD_EVIDENCE = tuple(
    FieldEvidence(struct, name, tag, go_type, f"protocol/model_ex.go:{line}", transform)
    for struct, transform, members in (
        (
            "ExMarket",
            "protocol/model_ex.go:176-198",
            (
                ("Market", "market", "uint16", 31),
                ("Category", "category", "uint8", 32),
                ("Name", "name", "string", 33),
                ("ShortName", "shortName", "string", 34),
            ),
        ),
        (
            "ExInstrument",
            "protocol/model_ex.go:224-246",
            (
                ("Category", "category", "uint8", 39),
                ("Market", "market", "uint8", 40),
                ("Code", "code", "string", 41),
                ("Name", "name", "string", 42),
                ("Desc", "desc", "string", 43),
            ),
        ),
    )
    for name, tag, go_type, line in members
)


@dataclass(frozen=True)
class StaticMarketReference:
    symbol: str
    market: int
    source_ref: str
    evidence_kind: str = "UPSTREAM_STATIC_ENUM_ONLY"


# Reference hints only; never used as the ExMarkets response or a venue resolver.
STATIC_MARKET_REFERENCES = tuple(
    StaticMarketReference(symbol, market, f"protocol/types.go:{line}")
    for symbol, market, line in (
        ("ExchangeHK", 31, 186),
        ("ExchangeUS", 74, 187),
        ("ExchangeCFF", 47, 191),
        ("ExchangeCZC", 28, 192),
        ("ExchangeDCE", 29, 193),
        ("ExchangeSHF", 30, 194),
        ("ExchangeINE", 30, 195),
    )
)


@dataclass(frozen=True)
class ExtensionEntry:
    family: str
    integration_port: str
    qualification: str = "UNKNOWN"
    source_rights: str = "UNKNOWN"
    calendar_revision: None = None


EXTENSION_ENTRIES = tuple(
    ExtensionEntry(family, f"tdx.exhq.{family}.offline.v1")
    for family in ("hk", "futures", "global")
)
