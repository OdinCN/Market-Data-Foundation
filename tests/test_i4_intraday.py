"""Synthetic wire fixtures only; no Provider or private storage access."""

import struct
from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from market_data_foundation.candidates.tdx_intraday_v1 import (
    Material,
    Operation,
    Request,
    decode,
)
from market_data_foundation.candidates.tdx_intraday_v1.model import DecodeError, sha
from market_data_foundation.candidates.tdx_intraday_v1.qualification import (
    observe_material,
)
from market_data_foundation.candidates.tdx_intraday_v1.semantics import field_facts
from market_data_foundation.qualification.contracts import Capability, Fixture

NOW = datetime(2026, 10, 22, 10, tzinfo=UTC)


def material(req, payload, **kwargs):
    options = dict(
        raw_sha256=sha(payload),
        locator="synthetic://i4/page",
        source="synthetic.tdx",
        binding="synthetic.binding",
        capture_id="synthetic-capture",
        mode="synthetic",
        obtained_at=NOW,
        received_at=NOW,
        known_at=NOW,
    )
    options.update(kwargs)
    return Material(req, payload, **options)


def varint(value):
    negative = value < 0
    value = abs(value)
    data = bytearray([(value & 63) | (64 if negative else 0)])
    value >>= 6
    while value:
        data[-1] |= 128
        data.append(value & 127)
        value >>= 7
    return bytes(data)


def fixture(operation):
    if operation == Operation.HISTORY_MINUTE:
        req = Request(operation, 0, "sz000001", date="20261021")
        payload = (
            struct.pack("<H", 2)
            + bytes(4)
            + b"".join(varint(v) for v in (100, 0, 5, -2, 0, 7))
        )
    elif operation in (Operation.TRADE, Operation.HISTORY_TRADE):
        req = Request(operation, 0, "sz000001", start=0, count=2, date="20261021")
        prefix = struct.pack("<H", 1) + (
            bytes(4) if operation == Operation.HISTORY_TRADE else b""
        )
        values = (
            (100, 5, 0, 0)
            if operation == Operation.HISTORY_TRADE
            else (100, 5, 2, 0, 0)
        )
        payload = prefix + struct.pack("<H", 570) + b"".join(varint(v) for v in values)
    else:
        req = Request(operation, 0, "sz000001", count=1, period=7, kind="stock")
        encoded_day = (2026 - 2004) * 2048 + 10 * 100 + 21
        payload = struct.pack("<HHH", 1, encoded_day, 570)
        payload += b"".join(varint(v) for v in (100, 1, 2, -1)) + struct.pack(
            "<ff", 1000, 250
        )
    return req, payload


@pytest.mark.parametrize(
    "operation",
    [
        Operation.HISTORY_MINUTE,
        Operation.TRADE,
        Operation.HISTORY_TRADE,
        Operation.BARS,
    ],
)
def test_native_pages_row_identity_and_exact_raw(operation):
    req, raw = fixture(operation)
    page = decode(material(req, raw))
    assert page.identity == page.rows[0].page_identity
    assert page.declared_count == len(page.rows)
    assert page.material.raw == raw
    first = page.rows[0]
    assert first.raw_sha256 == sha(
        raw[first.byte_offset : first.byte_offset + first.byte_length]
    )
    assert first.byte_length > 0
    assert first.identity != replace(first, index=20).identity
    assert (
        replace(page.material, locator="synthetic://other").receipt()
        != page.material.receipt()
    )
    assert (
        decode(replace(page.material, locator="synthetic://other")).identity
        != page.identity
    )
    native = first.native
    native["Price"] = 999
    assert first.native != native


@pytest.mark.parametrize(
    "operation",
    [
        Operation.HISTORY_MINUTE,
        Operation.TRADE,
        Operation.HISTORY_TRADE,
        Operation.BARS,
    ],
)
def test_every_truncation_and_trailing_bytes_fail_closed(operation):
    req, raw = fixture(operation)
    for end in range(len(raw)):
        with pytest.raises(DecodeError):
            decode(material(req, raw[:end]))
    with pytest.raises(DecodeError, match="trailing"):
        decode(material(req, raw + b"\0"))


def test_trade_clocks_history_number_and_etf_scale_are_distinct():
    req, raw = fixture(Operation.HISTORY_TRADE)
    page = decode(material(req, raw))
    assert page.rows[0].native["Number"] is None
    assert page.rows[0].native["Price"] == 1000
    assert page.rows[0].source_utc == "2026-10-21T01:30:00+00:00"
    assert "REQUEST_DATE" in page.rows[0].clock_status
    etf = decode(material(replace(req, code="sz159001"), raw))
    assert etf.rows[0].native["Price"] == 100
    assert "NUMBER_ABSENT_IN_HISTORY" in page.findings


def test_history_minute_labels_are_synthetic_not_source_clock():
    req, raw = fixture(Operation.HISTORY_MINUTE)
    page = decode(material(req, raw))
    assert page.rows[0].native["Time"] == "09:31"
    assert page.rows[0].native["Price"] == 1000
    assert page.rows[1].native["Price"] == 980
    assert page.rows[0].source_utc is None
    assert page.rows[0].source_local is None


def test_kline_wire_time_does_not_depend_on_host_or_fix_kline_now():
    req, raw = fixture(Operation.BARS)
    row = decode(material(req, raw)).rows[0]
    assert row.source_local == "2026-10-21T09:30"
    assert row.source_utc is None
    assert row.native["Volume"] == 10
    assert row.native["Amount"] == 250000
    assert row.native["High"] == 102
    index_raw = raw + struct.pack("<HH", 5, 4)
    index = decode(material(replace(req, kind="index"), index_raw)).rows[0]
    assert index.native["Volume"] == 1000
    assert index.native["UpCount"] == 5


@pytest.mark.parametrize(
    "changes",
    [
        {"market": True},
        {"market": 2},
        {"code": "000001"},
        {"code": "sh000001"},
        {"start": True},
        {"count": 1.0},
        {"count": 1801},
        {"start": 65535, "count": 2},
        {"date": "20260230"},
        {"date": "2026011"},
        {"date": "19910101"},
        {"category": 0},
        {"period": 4},
        {"kind": "stock"},
    ],
)
def test_request_invalid_types_ranges_and_context(changes):
    req, _ = fixture(Operation.TRADE)
    with pytest.raises(DecodeError):
        replace(req, **changes)


def test_getminute_alias_cannot_masquerade_as_live_operation():
    with pytest.raises(ValueError):
        Operation("GetMinute")


def test_material_digest_mutability_naive_and_fold_chronology():
    req, raw = fixture(Operation.TRADE)
    m = material(req, raw)
    for changes in (
        {"raw": bytearray(raw)},
        {"raw_sha256": "0" * 64},
        {"obtained_at": NOW.replace(tzinfo=None)},
        {"sdk_commit": "0" * 40},
    ):
        with pytest.raises(DecodeError):
            replace(m, **changes)
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    material(req, raw, obtained_at=first, received_at=second, known_at=None)
    with pytest.raises(DecodeError, match="clock_order"):
        material(req, raw, obtained_at=second, received_at=first, known_at=None)


def test_capability_fixture_observation_contract_and_redacted_failure():
    req, raw = fixture(Operation.TRADE)
    m = material(req, raw)
    spec = Fixture(
        "i4.synthetic.trade", Capability.TRADES_INTRADAY, (req.code,), None, None, 2, {}
    )
    observation = observe_material(m, spec, observed_at=NOW)
    assert observation["success"]
    assert observation["coverage"]["complete"] is None
    assert observation["pagination"]["exhausted"] is None
    failed = observe_material(
        replace(m, raw=b"x", raw_sha256=sha(b"x")), spec, observed_at=NOW
    )
    assert not failed["success"]
    assert failed["exception"]["category"] == "validation"
    assert "message" not in failed["exception"]
    with pytest.raises(DecodeError):
        observe_material(
            m, replace(spec, capability=Capability.BARS_INTRADAY), observed_at=NOW
        )


def test_fields_carry_precise_ast_api_and_context():
    req, _ = fixture(Operation.HISTORY_TRADE)
    facts = field_facts(req)
    number = next(f for f in facts if f["source_field_name"] == "Number")
    assert number["semantic_status"] == "ABSENT_ON_WIRE_SDK_ZERO_NOT_OBSERVED"
    assert number["market"] == 0 and number["producing_method"] == "GetHistoryTrade"
    assert number["source_ref"].startswith("protocol/model_trade.go:")


def test_unterminated_varint_and_declared_over_request():
    req, raw = fixture(Operation.TRADE)
    with pytest.raises(DecodeError):
        decode(material(req, raw[:4] + b"\x80" * 8))
    with pytest.raises(DecodeError, match="declared_exceeds"):
        decode(material(replace(req, count=1), struct.pack("<H", 2) + raw[2:]))


def test_index_volume_projection_cannot_silently_overflow_sdk_int64():
    req, raw = fixture(Operation.BARS)
    req = replace(req, code="sz123001", kind="index")
    changed = bytearray(raw)
    struct.pack_into("<f", changed, len(raw) - 8, 1e18)
    with pytest.raises(DecodeError, match="projection_overflow"):
        decode(material(req, bytes(changed) + struct.pack("<HH", 1, 1)))
