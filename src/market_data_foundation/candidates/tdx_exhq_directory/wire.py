"""Strict offline validation of decompressed ExHq response payloads, not TCP frames."""

import struct
from dataclasses import dataclass

from .schema import MAX_PAYLOAD_BYTES, DirectoryError, require, uint


@dataclass(frozen=True)
class PageRequest:
    start: int
    count: int

    def __post_init__(self) -> None:
        uint(self.start, 32, "start")
        uint(self.count, 16, "count")
        require(self.start + self.count <= 2**32, "page_range_overflow")

    @property
    def payload(self) -> bytes:
        """FrameInstrument data only; this method performs no transport effect."""
        return struct.pack("<IH", self.start, self.count)


@dataclass(frozen=True)
class ExMarket:
    market: int
    category: int
    name: str
    shortName: str  # Exact upstream JSON tag.
    row_index: int

    @property
    def key(self) -> tuple[int, int]:
        return self.market, self.category


@dataclass(frozen=True)
class ExInstrument:
    category: int
    market: int
    code: str
    name: str
    desc: str
    row_index: int

    @property
    def key(self) -> tuple[int, int, str]:
        return self.market, self.category, self.code


@dataclass(frozen=True)
class MarketTable:
    declared_count: int
    rows: tuple[ExMarket, ...]
    skipped_slots: tuple[int, ...]


@dataclass(frozen=True)
class InstrumentPage:
    request: PageRequest
    declared_count: int
    rows: tuple[ExInstrument, ...]


def _payload(raw: bytes, minimum: int) -> None:
    require(type(raw) is bytes, "immutable_payload_bytes_required")
    require(len(raw) <= MAX_PAYLOAD_BYTES, "payload_budget_exceeded")
    require(len(raw) >= minimum, "short_frame_nil_or_zero")


def _rows_length(raw: bytes, header: int, count: int) -> None:
    expected = header + count * 64
    require(len(raw) >= expected, "truncated_declared_rows")
    require(len(raw) == expected, "unexpected_trailing_bytes")


def _gbk(raw: bytes) -> str:
    # Same first-NUL boundary as exGBK. Strict encoding failure is deliberate:
    # upstream discards decoder errors; replacement characters cannot form identity.
    try:
        return raw.split(b"\0", 1)[0].decode("gbk", errors="strict")
    except UnicodeDecodeError:
        raise DirectoryError("invalid_gbk_field") from None


def decode_markets(raw: bytes) -> MarketTable:
    _payload(raw, 2)
    declared = struct.unpack_from("<H", raw)[0]
    _rows_length(raw, 2, declared)
    rows = []
    skipped = []
    seen = set()
    for index in range(declared):
        offset = 2 + index * 64
        category, market = raw[offset], raw[offset + 33]
        if category == 0 and market == 0:
            skipped.append(index)
            continue
        row = ExMarket(
            market,
            category,
            _gbk(raw[offset + 1 : offset + 33]),
            _gbk(raw[offset + 34 : offset + 36]),
            index,
        )
        require(row.key not in seen, "duplicate_market_category")
        seen.add(row.key)
        rows.append(row)
    return MarketTable(declared, tuple(rows), tuple(skipped))


def decode_count(raw: bytes) -> int:
    _payload(raw, 23)
    # Remaining bytes, if any, are opaque upstream data retained by receipt digest.
    return struct.unpack_from("<I", raw, 19)[0]


def decode_instruments(raw: bytes, request: PageRequest) -> InstrumentPage:
    require(type(request) is PageRequest, "page_request_required")
    _payload(raw, 6)
    declared = struct.unpack_from("<H", raw, 4)[0]
    require(declared <= request.count, "declared_exceeds_requested_count")
    _rows_length(raw, 6, declared)
    rows = []
    seen = set()
    for index in range(declared):
        offset = 6 + index * 64
        row = ExInstrument(
            category=raw[offset],
            market=raw[offset + 1],
            code=_gbk(raw[offset + 5 : offset + 14]),
            name=_gbk(raw[offset + 14 : offset + 31]),
            desc=_gbk(raw[offset + 31 : offset + 40]),
            row_index=index,
        )
        require(bool(row.code.strip()), "empty_instrument_code")
        require(row.key not in seen, "duplicate_instrument_identity")
        seen.add(row.key)
        rows.append(row)
    return InstrumentPage(request, declared, tuple(rows))
