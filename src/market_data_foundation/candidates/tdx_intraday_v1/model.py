"""Source-native I-4 requests and immutable evidence, without I/O or admission."""

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum

from market_data_foundation.qualification.contracts import Capability

SDK_COMMIT = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
REVISION = "mdf.tdx.i4.source-native.v1"
MAX_PAYLOAD = 65535
MAX_ROWS = 2000


class DecodeError(ValueError):
    """Stable classification; no source values or raw error messages."""


def require(condition: bool, classification: str) -> None:
    if not condition:
        raise DecodeError(classification)


def sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode()


class Operation(StrEnum):
    HISTORY_MINUTE = "GetHistoryMinute"
    BARS = "GetKline"
    TRADE = "GetTrade"
    HISTORY_TRADE = "GetHistoryTrade"
    EX_QUOTE = "ExQuote"
    EX_BARS = "ExBars"
    EX_MINUTE = "ExMinute"
    EX_HISTORY_MINUTE = "ExHistMinute"
    EX_TRADE = "ExTrade"
    EX_HISTORY_TRADE = "ExHistTrade"


# Operation, response struct, wire type, capability, primary decoder evidence.
CATALOG = {
    Operation.HISTORY_MINUTE: (
        "MinuteResp",
        0x0FB4,
        Capability.BARS_INTRADAY,
        "protocol/model_history_minute.go:31-79",
    ),
    Operation.BARS: (
        "KlineResp",
        0x052D,
        Capability.BARS_INTRADAY,
        "protocol/model_kline.go:142-237",
    ),
    Operation.TRADE: (
        "TradeResp",
        0x0FC5,
        Capability.TRADES_INTRADAY,
        "protocol/model_trade.go:92-125",
    ),
    Operation.HISTORY_TRADE: (
        "TradeResp",
        0x0FB5,
        Capability.TRADES_INTRADAY,
        "protocol/model_history_trade.go:36-78",
    ),
    Operation.EX_QUOTE: (
        "ExQuote",
        0x23FA,
        Capability.QUOTE_SNAPSHOT,
        "protocol/model_ex.go:257-297",
    ),
    Operation.EX_BARS: (
        "ExKline",
        0x23FF,
        Capability.BARS_INTRADAY,
        "protocol/model_ex.go:322-357",
    ),
    Operation.EX_MINUTE: (
        "ExMinuteTick",
        0x240B,
        Capability.BARS_INTRADAY,
        "protocol/model_ex.go:369-418",
    ),
    Operation.EX_HISTORY_MINUTE: (
        "ExMinuteTick",
        0x240C,
        Capability.BARS_INTRADAY,
        "protocol/model_ex.go:385-418",
    ),
    Operation.EX_TRADE: (
        "ExTradeTick",
        0x23FC,
        Capability.TRADES_INTRADAY,
        "protocol/model_ex.go:433-497",
    ),
    Operation.EX_HISTORY_TRADE: (
        "ExTradeTick",
        0x2406,
        Capability.TRADES_INTRADAY,
        "protocol/model_ex.go:451-497",
    ),
}


@dataclass(frozen=True)
class Request:
    operation: Operation
    market: int
    code: str
    # ExInstrument.Category, never inferred from market or used as bar period.
    category: int | None = None
    start: int = 0
    count: int = 0
    date: str = ""  # Explicit YYYYMMDD, including today's trade cache context.
    period: int = 0
    kind: str = ""  # SDK KlineCache.Kind, explicitly stock/etf/index.

    def __post_init__(self) -> None:
        require(type(self.operation) is Operation, "operation_required")
        require(type(self.market) is int, "market_integer_required")
        require(type(self.code) is str, "code_text_required")
        for name, value in (("start", self.start), ("count", self.count)):
            require(type(value) is int and 0 <= value <= 65535, f"invalid_{name}")
        require(self.start + self.count <= 65536, "page_address_overflow")
        require(type(self.period) is int, "period_integer_required")
        require(
            type(self.kind) is str and type(self.date) is str, "context_text_required"
        )
        ex = self.operation.value.startswith("Ex")
        if ex:
            require(0 <= self.market <= 255, "invalid_ex_market")
            require(
                type(self.category) is int and 0 <= self.category <= 255,
                "explicit_directory_category_required",
            )
            require(
                bool(re.fullmatch(r"[A-Za-z0-9._-]{1,9}", self.code)), "invalid_ex_code"
            )
            require(not self.kind, "ex_kind_not_standard_kind")
        else:
            require(self.market in (0, 1), "standard_market_not_supported")
            prefix = "sh" if self.market else "sz"
            require(
                bool(re.fullmatch(prefix + r"[0-9]{6}", self.code)),
                "exact_standard_code_required",
            )
            require(self.category is None, "standard_category_absent_required")
        paged = self.operation in (
            Operation.BARS,
            Operation.TRADE,
            Operation.HISTORY_TRADE,
            Operation.EX_BARS,
            Operation.EX_TRADE,
            Operation.EX_HISTORY_TRADE,
        )
        limit = 800 if self.operation in (Operation.BARS, Operation.EX_BARS) else 2000
        if self.operation == Operation.TRADE:
            limit = 1800
        require(
            1 <= self.count <= limit if paged else self.count == self.start == 0,
            "invalid_page_shape",
        )
        bars = self.operation in (Operation.BARS, Operation.EX_BARS)
        require(
            self.period in (0, 1, 2, 3, 7, 8) if bars else self.period == 0,
            "invalid_intraday_period",
        )
        if self.operation == Operation.BARS:
            require(
                self.kind in ("stock", "etf", "index"),
                "explicit_standard_kind_required",
            )
        elif not ex:
            require(not self.kind, "unused_kind")
        dated = self.operation in (
            Operation.HISTORY_MINUTE,
            Operation.TRADE,
            Operation.HISTORY_TRADE,
            Operation.EX_HISTORY_MINUTE,
            Operation.EX_HISTORY_TRADE,
        )
        if dated:
            require(bool(re.fullmatch(r"[0-9]{8}", self.date)), "date_required")
            try:
                parsed = date(
                    int(self.date[:4]), int(self.date[4:6]), int(self.date[6:])
                )
            except ValueError:
                raise DecodeError("invalid_calendar_date") from None
            require(parsed >= date(1992, 1, 1), "date_outside_supported_clock_era")
        else:
            require(not self.date, "unused_date")

    @property
    def identity(self) -> str:
        return sha(canonical(self.to_dict()))

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def capability(self) -> Capability:
        return CATALOG[self.operation][2]


@dataclass(frozen=True)
class Material:
    request: Request
    raw: bytes = field(repr=False)
    raw_sha256: str
    locator: str
    source: str
    binding: str
    capture_id: str
    mode: str
    obtained_at: datetime | None
    received_at: datetime | None
    known_at: datetime | None
    sdk_commit: str = SDK_COMMIT
    producer_sha256: str | None = None
    # Full compressed network frame identity is separate from decoded payload.
    wire_sha256: str | None = None
    directory_identity: str | None = None
    endpoint: str | None = None
    peer: str | None = None
    request_wire_sha256: str | None = None
    # Evidence objects, never a digest-shaped assertion of verification.
    directory_receipts: tuple = field(default=(), repr=False)

    def __post_init__(self) -> None:
        require(type(self.request) is Request, "request_required")
        require(
            type(self.raw) is bytes and len(self.raw) <= MAX_PAYLOAD, "payload_bound"
        )
        require(self.raw_sha256 == sha(self.raw), "payload_digest_mismatch")
        require(self.sdk_commit == SDK_COMMIT, "sdk_commit_mismatch")
        require(self.mode in ("synthetic", "retained"), "evidence_mode_required")
        for value in (self.locator, self.source, self.binding, self.capture_id):
            require(
                type(value) is str and 0 < len(value) <= 1024, "identity_text_required"
            )
        for digest in (
            self.producer_sha256,
            self.wire_sha256,
            self.directory_identity,
            self.request_wire_sha256,
        ):
            require(
                digest is None or bool(re.fullmatch(r"[a-f0-9]{64}", digest)),
                "invalid_digest",
            )
        previous = None
        for value in (self.obtained_at, self.received_at, self.known_at):
            require(
                value is None
                or (type(value) is datetime and value.utcoffset() is not None),
                "aware_clock_required",
            )
            if value is not None:
                instant = value.astimezone(UTC)
                require(previous is None or previous <= instant, "clock_order")
                previous = instant

    def receipt(self) -> dict:
        return {
            "schema": REVISION,
            "request": self.request.to_dict(),
            "request_id": self.request.identity,
            "raw_sha256": self.raw_sha256,
            "raw_bytes": len(self.raw),
            "locator": self.locator,
            "source": self.source,
            "binding": self.binding,
            "capture_id": self.capture_id,
            "mode": self.mode,
            "sdk_commit": self.sdk_commit,
            "producer_sha256": self.producer_sha256,
            "wire_sha256": self.wire_sha256,
            "directory_identity": self.directory_identity,
            "directory_receipt_identities": [
                receipt.material.identity for receipt in self.directory_receipts
            ],
            "endpoint": self.endpoint,
            "peer": self.peer,
            "request_wire_sha256": self.request_wire_sha256,
            **{
                name: value.astimezone(UTC).isoformat() if value else None
                for name, value in (
                    ("obtained_at", self.obtained_at),
                    ("received_at", self.received_at),
                    ("known_at", self.known_at),
                )
            },
        }


@dataclass(frozen=True)
class Row:
    index: int
    byte_offset: int
    byte_length: int
    raw_sha256: str
    page_identity: str
    # Canonical native JSON is immutable; access returns a fresh object.
    native_json: bytes = field(repr=False)
    source_local: str | None = None
    source_utc: str | None = None
    clock_status: str = "UNKNOWN"

    @property
    def native(self) -> dict:
        return json.loads(self.native_json)

    @property
    def identity(self) -> str:
        return sha(
            canonical(
                {
                    "page": self.page_identity,
                    "index": self.index,
                    "offset": self.byte_offset,
                    "length": self.byte_length,
                    "raw_sha256": self.raw_sha256,
                    "native": self.native,
                    "local": self.source_local,
                    "utc": self.source_utc,
                    "clock_status": self.clock_status,
                }
            )
        )


@dataclass(frozen=True)
class Page:
    material: Material
    rows: tuple[Row, ...]
    declared_count: int
    findings: tuple[str, ...]
    schema: str = REVISION

    @property
    def identity(self) -> str:
        return sha(
            canonical({"receipt": self.material.receipt(), "schema": self.schema})
        )

    @property
    def content_identity(self) -> str:
        # Compare content separately from local capture clocks, receipt and request ID.
        return sha(canonical([row.native for row in self.rows]))
