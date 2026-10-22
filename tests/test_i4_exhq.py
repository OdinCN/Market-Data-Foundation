import struct
from dataclasses import replace

import pytest

from market_data_foundation.candidates.tdx_exhq_v1 import decode_ex
from market_data_foundation.candidates.tdx_intraday_v1 import Operation, Request
from market_data_foundation.candidates.tdx_intraday_v1.model import DecodeError
from market_data_foundation.candidates.tdx_intraday_v1.semantics import field_facts
from tests.test_i4_intraday import material

EX_OPERATIONS = [
    Operation.EX_QUOTE,
    Operation.EX_BARS,
    Operation.EX_MINUTE,
    Operation.EX_HISTORY_MINUTE,
    Operation.EX_TRADE,
    Operation.EX_HISTORY_TRADE,
]


def fixture(operation, market=31, code="00700"):
    options = {}
    if operation in (Operation.EX_BARS, Operation.EX_TRADE, Operation.EX_HISTORY_TRADE):
        options.update(count=2)
    if operation in (Operation.EX_HISTORY_MINUTE, Operation.EX_HISTORY_TRADE):
        options.update(date="20261021")
    if operation == Operation.EX_BARS:
        options.update(period=7)
    req = Request(operation, market, code, category=2 if market == 31 else 3, **options)
    if operation == Operation.EX_QUOTE:
        raw = bytearray(150)
        raw[0] = market
        raw[1 : 1 + len(code)] = code.encode()
        struct.pack_into("<5f", raw, 14, 100, 101, 102, 99, 101.5)
        struct.pack_into("<I", raw, 42, 100)
        raw = bytes(raw)
    else:
        header, width = {
            Operation.EX_BARS: (20, 32),
            Operation.EX_MINUTE: (12, 18),
            Operation.EX_HISTORY_MINUTE: (20, 18),
            Operation.EX_TRADE: (16, 16),
            Operation.EX_HISTORY_TRADE: (16, 16),
        }[operation]
        raw = bytes(header - 2) + struct.pack("<H", 1)
        if operation == Operation.EX_BARS:
            day = (2026 - 2004) * 2048 + 1021
            raw += struct.pack("<HH4fIIf", day, 570, 100, 102, 99, 101, 1000, 5, 100)
        elif width == 18:
            raw += struct.pack("<HffII", 570, 101.5, 100.5, 123, 456)
        else:
            raw += struct.pack(
                "<HIIiH", 570, 101500, 12, -3, 256 if market == 31 else 10023
            )
    return req, raw


@pytest.mark.parametrize("operation", EX_OPERATIONS)
@pytest.mark.parametrize("identity", [(31, "00700"), (30, "rb2610")])
def test_ex_independent_hk_and_futures_native_families(operation, identity):
    req, raw = fixture(operation, *identity)
    page = decode_ex(material(req, raw))
    assert len(page.rows) == 1
    assert page.rows[0].source_utc is None
    assert "CURRENCY_LOT_MULTIPLIER_UNKNOWN" in page.findings
    assert "DIRECTORY_ASSOCIATION_UNKNOWN" in page.findings
    if operation == Operation.EX_QUOTE:
        assert page.rows[0].native["code"] == identity[1]
        assert page.rows[0].native["price"] == 101.5
        assert page.rows[0].source_local is None
    if operation in (Operation.EX_TRADE, Operation.EX_HISTORY_TRADE):
        assert page.rows[0].native["price"] == 101500
        assert page.rows[0].native["observed_second"] is None
        assert "TRADE_PRICE_UINT32_NO_QUOTE_FLOAT_SCALING" in page.findings


@pytest.mark.parametrize("operation", EX_OPERATIONS)
def test_ex_every_truncation_extra_and_declared_overflow_rejected(operation):
    req, raw = fixture(operation)
    for end in range(len(raw)):
        with pytest.raises(DecodeError):
            decode_ex(material(req, raw[:end]))
    with pytest.raises(DecodeError):
        decode_ex(material(req, raw + b"\0"))
    if req.count:
        header = 20 if operation == Operation.EX_BARS else 16
        changed = bytearray(raw)
        struct.pack_into("<H", changed, header - 2, 3)
        with pytest.raises(DecodeError, match="declared_exceeds"):
            decode_ex(material(req, bytes(changed)))


def test_ex_position_and_amount_are_same_bytes_and_can_be_nonfinite():
    req, raw = fixture(Operation.EX_BARS)
    changed = bytearray(raw)
    struct.pack_into("<I", changed, 20 + 20, 0x7FC00000)
    row = decode_ex(material(req, bytes(changed))).rows[0]
    assert row.native["position"] == 0x7FC00000
    assert row.native["amount"] is None
    assert row.native["position_amount_shared_bytes"] == "0000c07f"
    fact = next(f for f in field_facts(req) if f["source_field_name"] == "Amount")
    assert "NOT_INDEPENDENT_AMOUNT" in fact["semantic_status"]


def test_ex_quote_identity_and_nonfinite_price_rejected():
    req, raw = fixture(Operation.EX_QUOTE)
    with pytest.raises(DecodeError, match="identity_mismatch"):
        decode_ex(material(replace(req, market=48), raw))
    changed = bytearray(raw)
    struct.pack_into("<f", changed, 14, float("inf"))
    with pytest.raises(DecodeError, match="nonfinite"):
        decode_ex(material(req, bytes(changed)))


@pytest.mark.parametrize(
    "changes",
    [
        {"market": 256},
        {"category": None},
        {"category": True},
        {"category": 256},
        {"code": "0012345678"},
        {"code": "00700\0"},
        {"code": " 00700"},
        {"kind": "stock"},
        {"date": "20261022"},
        {"period": 5},
    ],
)
def test_ex_request_identity_is_not_standard_or_merged(changes):
    req, _ = fixture(Operation.EX_QUOTE)
    with pytest.raises(DecodeError):
        replace(req, **changes)


def test_ex_original_category_not_period_and_31_48_not_merged():
    req, raw = fixture(Operation.EX_TRADE)
    other = replace(req, market=48)
    first, second = decode_ex(material(req, raw)), decode_ex(material(other, raw))
    assert first.identity != second.identity
    assert first.rows[0].native == second.rows[0].native
    assert req.category == 2 and req.period == 0


def test_sample_catalog_association_checks_exact_tuple_source_and_capture():
    from market_data_foundation.candidates.tdx_exhq_directory import Operation as DirOp
    from market_data_foundation.candidates.tdx_exhq_directory import PageRequest
    from market_data_foundation.candidates.tdx_exhq_v1.catalog import (
        associate_sample,
        attach_association,
    )
    from tests.test_r5_i6_a_ex_dir import (
        instrument_row,
        market_row,
        markets_payload,
        page_payload,
        receipt,
    )

    req, raw = fixture(Operation.EX_QUOTE)
    markets = receipt(
        DirOp.MARKETS, markets_payload(market_row()), "synthetic://markets"
    )
    page = receipt(
        DirOp.INSTRUMENTS,
        page_payload(instrument_row()),
        "synthetic://instruments",
        PageRequest(0, 1),
    )
    association = associate_sample(markets, page, req)
    assert association.code == "00700" and association.economics == "UNKNOWN"
    m = material(req, raw, source=association.source, binding=association.binding)
    attached = attach_association(m, association)
    assert attached.directory_identity == association.identity
    assert "DIRECTORY_ASSOCIATION_UNKNOWN" not in decode_ex(attached).findings
    for changes in ({"market": 48}, {"category": 3}, {"code": "700"}):
        with pytest.raises(DecodeError):
            associate_sample(markets, page, replace(req, **changes))
    with pytest.raises(DecodeError):
        attach_association(replace(m, source="other"), association)
    with pytest.raises(DecodeError):
        associate_sample(
            markets,
            replace(page, context=replace(page.context, capture_id="different")),
            req,
        )
