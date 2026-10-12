"""Author-created decoded-shape fixtures; no retained provider values or bytes."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from market_data_foundation.candidates.stock_snapshot import (
    INACTIVE_DEFINITION,
    SDK_RELEASE,
    UNITS,
    Clocks,
    StockSnapshotCandidate,
    parse_synthetic,
)
from market_data_foundation.canonical.model import ContractError

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def clocks(n, request=None):
    requested = n if request is None else request
    return Clocks(
        BASE + timedelta(seconds=requested),
        BASE + timedelta(seconds=n + 1),
        BASE + timedelta(seconds=n + 2),
        BASE + timedelta(seconds=n + 3),
    )


def fixture(price=12340):
    """Numbers invented for unit arithmetic, not actual stock quotations."""
    rows = []
    for exchange, code in ((1, "600519"), (0, "000001")):
        rows.append(
            {
                "Exchange": exchange,
                "Code": code,
                "Kline": {
                    "Last": price,
                    "Open": price,
                    "High": price + 10,
                    "Low": price - 10,
                    "Close": price,
                    "Amount": 234567,
                    "Volume": 17,
                    "Time": BASE.isoformat(),
                },
                "BuyLevel": [
                    {"Buy": True, "Price": price - i * 10, "Number": i}
                    for i in range(1, 6)
                ],
                "SellLevel": [
                    {"Buy": False, "Price": price + i * 10, "Number": i}
                    for i in range(1, 6)
                ],
                "ServerTime": "opaque-synthetic",
                "Active1": 1,
                "Active2": 2,
            }
        )
    return rows


def test_units_identity_and_inactive_boundary():
    rows = fixture()
    quotes = parse_synthetic(rows[::-1], units=UNITS)
    assert [q.security for q in quotes] == ["sh600519", "sz000001"]
    assert quotes[0].close_cny == Decimal("12.34")
    assert quotes[0].amount_cny == Decimal("234.567")
    assert quotes[0].volume_sdk == 17
    assert not INACTIVE_DEFINITION.source_qualified
    assert INACTIVE_DEFINITION.revision == "inactive-r1"
    assert "51c1ed51020fa43727300339dd92cbecf06a7d2e" in SDK_RELEASE
    rows[0]["Kline"]["Close"] = 999
    assert quotes[0].close_cny == Decimal("12.34")


# Independent bounds from the pinned SDK's decoded fields. Go int Number is
# limited to the common 32-bit subset used by its int32-backed CutInt decoder.
INTEGER_FIELDS = (
    [
        (("Kline", name), (1 << 63) - 1)
        for name in ("Last", "Open", "High", "Low", "Close", "Amount", "Volume")
    ]
    + [
        ((side, level, name), maximum)
        for side in ("BuyLevel", "SellLevel")
        for level in range(5)
        for name, maximum in (("Price", (1 << 63) - 1), ("Number", (1 << 31) - 1))
    ]
    + [((name,), (1 << 16) - 1) for name in ("Active1", "Active2")]
)


def set_field(row, path, value):
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value


@pytest.mark.parametrize("path,maximum", INTEGER_FIELDS)
@pytest.mark.parametrize("boundary", ["zero", "maximum"])
def test_decoded_integer_boundaries(path, maximum, boundary):
    rows = fixture()
    value = 0 if boundary == "zero" else maximum
    set_field(rows[1], path, value)
    record = StockSnapshotCandidate().observe(rows, clocks=clocks(0))
    assert record.outcome == "sample"
    assert len(record.quotes) == 2 and record.classification is None
    quote = record.quotes[1]
    if path[0] == "Kline":
        retained = dict(
            zip(("Last", "Open", "High", "Low", "Close"), quote.prices_li, strict=True),
            Amount=quote.amount_li,
            Volume=quote.volume_sdk,
        )[path[1]]
    elif path[0] in ("BuyLevel", "SellLevel"):
        levels = quote.bids if path[0] == "BuyLevel" else quote.asks
        retained = levels[path[1]][0 if path[2] == "Price" else 1]
    else:
        retained = quote.active_raw[0 if path[0] == "Active1" else 1]
    assert retained == value


@pytest.mark.parametrize("path,maximum", INTEGER_FIELDS)
@pytest.mark.parametrize(
    "invalid", ["negative", "above_max", "huge", "bool", "float", "string"]
)
def test_decoded_integer_rejection_preserves_both_old_values(path, maximum, invalid):
    candidate = StockSnapshotCandidate()
    first = candidate.observe(fixture(), clocks=clocks(0))
    rows = fixture(23450)  # Valid first member must not be partially installed.
    value = {
        "negative": -1,
        "above_max": maximum + 1,
        "huge": 10**100,
        "bool": True,
        "float": 1.0,
        "string": "1",
    }[invalid]
    set_field(rows[1], path, value)
    with pytest.raises(ContractError, match="^invalid_integer_field$"):
        parse_synthetic(rows, units=UNITS)
    failed = candidate.observe(rows, clocks=clocks(10))
    assert failed.outcome == "failed"
    assert failed.classification == "invalid_integer_field" and failed.quotes == ()
    for index, security in enumerate(("sh600519", "sz000001")):
        view = candidate.read(security, at=failed.clocks.known)
        assert view.status == "unavailable"
        assert view.state.quote == first.quotes[index]
        assert view.state.acquisition == first.acquisition
    assert candidate.as_of(first.clocks.known) is first


@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0.000"),
        (1, "0.001"),
        (999, "0.999"),
        (1000, "1.000"),
        (12345, "12.345"),
        (234567, "234.567"),
        ((1 << 63) - 1, "9223372036854775.807"),
    ],
)
@pytest.mark.parametrize("precision,trap", [(28, False), (4, False), (1, True)])
def test_exact_yuan_projection_ignores_decimal_context(
    value, expected, precision, trap
):
    rows = fixture()
    rows[0]["Kline"]["Close"] = value
    rows[0]["Kline"]["Amount"] = value
    expected_decimal = Decimal(expected)
    with localcontext() as context:
        context.prec = precision
        context.Emax = 0
        context.Emin = 0
        context.traps[Inexact] = trap
        context.traps[Rounded] = trap
        context.clear_flags()
        quote = parse_synthetic(rows, units=UNITS)[0]
        assert quote.close_cny == expected_decimal
        assert quote.amount_cny == expected_decimal
        assert not any(context.flags.values())


class MutableRevision(str):
    """A str subtype can carry externally mutable identity metadata."""


@pytest.mark.parametrize(
    "revision",
    [
        None,
        True,
        1,
        1.0,
        ["r2"],
        {"revision": "r2"},
        ("r2",),
        b"r2",
        bytearray(b"r2"),
        MutableRevision("r2"),
        "",
        "x" * 65,
    ],
)
@pytest.mark.parametrize("mode", ["sample", "failure", "correction"])
def test_invalid_parser_revision_has_no_history_or_state_effect(revision, mode):
    candidate = StockSnapshotCandidate()
    first = candidate.observe(fixture(), clocks=clocks(0))
    kwargs = {}
    if mode == "failure":
        kwargs["failure"] = "timeout"
    elif mode == "correction":
        kwargs["supersedes"] = first.acquisition
    next_clocks = clocks(10, request=0 if mode == "correction" else 10)
    with pytest.raises(ContractError, match="^parser_revision$"):
        candidate.observe(
            fixture(23450), clocks=next_clocks, parser_revision=revision, **kwargs
        )
    assert candidate.history == (first,)
    assert candidate.exact(first.acquisition) is first
    assert candidate.as_of(next_clocks.known) is first
    for index, security in enumerate(("sh600519", "sz000001")):
        view = candidate.read(security, at=next_clocks.known)
        assert view.status == "observed" and view.state == first.states[index]
    accepted = candidate.observe(
        fixture(23450), clocks=next_clocks, parser_revision="r2", **kwargs
    )
    assert accepted.acquisition == "sample-2"
    assert accepted.outcome == (
        "failed"
        if mode == "failure"
        else "corrected"
        if mode == "correction"
        else "sample"
    )


def test_external_revision_mutation_cannot_rewrite_known_identity():
    candidate = StockSnapshotCandidate()
    revision = ["synthetic-parser-r1"]
    first = candidate.observe(fixture(), clocks=clocks(0), parser_revision=revision[0])
    with pytest.raises(ContractError, match="^parser_revision$"):
        candidate.observe(fixture(), clocks=clocks(10), parser_revision=revision)
    revision[0] = "rewritten-after-known"
    assert candidate.history == (first,)
    assert candidate.as_of(first.clocks.known).parser_revision == "synthetic-parser-r1"
    corrected = candidate.observe(
        fixture(23450),
        clocks=clocks(10, request=0),
        parser_revision=revision[0],
        supersedes=first.acquisition,
    )
    revision[0] = "rewritten-again"
    assert corrected.parser_revision == "rewritten-after-known"
    assert candidate.as_of(first.clocks.known) is first
    assert candidate.as_of(corrected.clocks.known) is corrected


@pytest.mark.parametrize("revision", ["r", "x" * 64])
def test_parser_revision_string_length_boundaries(revision):
    record = StockSnapshotCandidate().observe(
        fixture(), clocks=clocks(0), parser_revision=revision
    )
    assert type(record.parser_revision) is str and record.parser_revision == revision


@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "missing_member",
        "duplicate",
        "wrong_exchange",
        "etf",
        "missing_field",
        "truncated",
        "wrong_side",
        "float_price",
        "bool_volume",
        "negative",
        "naive",
        "bad_time",
        "null_kline",
        "oversize_time",
    ],
)
def test_atomic_rejection(mutation):
    rows = fixture()
    if mutation == "empty":
        rows = []
    elif mutation == "missing_member":
        rows.pop()
    elif mutation == "duplicate":
        rows[1] = deepcopy(rows[0])
    elif mutation == "wrong_exchange":
        rows[0]["Exchange"] = 0
    elif mutation == "etf":
        rows[0]["Code"] = "510300"
    elif mutation == "missing_field":
        del rows[0]["Kline"]["Amount"]
    elif mutation == "truncated":
        rows[0]["BuyLevel"].pop()
    elif mutation == "wrong_side":
        rows[0]["BuyLevel"][0]["Buy"] = False
    elif mutation == "float_price":
        rows[0]["Kline"]["Close"] = 12.34
    elif mutation == "bool_volume":
        rows[0]["Kline"]["Volume"] = True
    elif mutation == "negative":
        rows[0]["SellLevel"][0]["Number"] = -1
    elif mutation == "naive":
        rows[0]["Kline"]["Time"] = "2026-01-01T00:00:00"
    elif mutation == "bad_time":
        rows[0]["Kline"]["Time"] = "bad"
    elif mutation == "null_kline":
        rows[0]["Kline"] = None
    elif mutation == "oversize_time":
        rows[0]["ServerTime"] = "x" * 65
    candidate = StockSnapshotCandidate()
    first = candidate.observe(fixture(), clocks=clocks(0))
    failed = candidate.observe(rows, clocks=clocks(10))
    assert failed.outcome == "failed"
    assert failed.quotes == ()
    view = candidate.read("sh600519", at=clocks(10).known)
    assert view.status == "unavailable"
    assert view.state.quote == first.quotes[0]
    assert view.state.acquisition == first.acquisition


def test_wrong_scale_declared_fails_closed():
    with pytest.raises(ContractError, match="unit_declaration_mismatch"):
        parse_synthetic(fixture(), units=("CNY", "shares", "lots"))


def test_time_gates():
    with pytest.raises(ContractError, match="naive_time"):
        Clocks(*(datetime(2026, 1, 1) for _ in range(4)))
    with pytest.raises(ContractError, match="clock_order_invalid"):
        Clocks(BASE, BASE, BASE, BASE - timedelta(seconds=1))
    candidate = StockSnapshotCandidate()
    candidate.observe(fixture(), clocks=clocks(0))
    with pytest.raises(ContractError, match="known_time_not_forward"):
        candidate.observe(fixture(), clocks=clocks(0))


def test_duplicate_late_uncertain_failure_age_and_recovery():
    c = StockSnapshotCandidate(ttl=timedelta(seconds=10))
    first = c.observe(fixture(), clocks=clocks(0))
    duplicate_rows = fixture()
    duplicate_rows[0]["Kline"]["Time"] = clocks(5).received.isoformat()
    duplicate_rows[0]["Active1"] = 50
    duplicate = c.observe(duplicate_rows, clocks=clocks(5))
    assert duplicate.outcome == "duplicate"
    assert duplicate.acquisition != first.acquisition
    assert c.observe(fixture(9990), clocks=clocks(10, request=0)).outcome == "late"
    assert (
        c.observe(fixture(9990), clocks=clocks(15, request=5)).outcome
        == "order_uncertain"
    )
    view = c.read("sh600519", at=clocks(15).known)
    assert view.state.acquisition == duplicate.acquisition
    assert view.status == "stale"
    assert view.source_event_at is None
    assert view.source_freshness == "UNKNOWN"
    c.observe(None, clocks=clocks(20), failure="timeout")
    failed = c.read("sh600519", at=clocks(20).known)
    assert failed.status == "unavailable" and failed.stale
    assert failed.state.quote is not None
    c.observe(fixture(8880), clocks=clocks(25))
    assert c.read("sz000001", at=clocks(25).known).status == "observed"


def test_unknown_no_qualified_value_and_ttl_boundary():
    c = StockSnapshotCandidate(ttl=timedelta(seconds=10))
    c.observe(None, clocks=clocks(0), failure="timeout")
    view = c.read("sz000001", at=clocks(0).known)
    assert view.state.quote is None and view.age is None
    assert view.status == "unavailable"
    sample = c.observe(fixture(), clocks=clocks(5))
    assert (
        c.read("sz000001", at=sample.clocks.received + timedelta(seconds=10)).status
        == "observed"
    )
    assert (
        c.read("sz000001", at=sample.clocks.received + timedelta(seconds=11)).status
        == "stale"
    )


def test_exact_asof_bounded_no_backdating_and_correction():
    c = StockSnapshotCandidate(capacity=2)
    a = c.observe(fixture(), clocks=clocks(0))
    with pytest.raises(ContractError, match="asof_not_retained"):
        c.as_of(a.clocks.frozen)
    b = c.observe(
        fixture(7770),
        clocks=clocks(10, request=0),
        parser_revision="synthetic-parser-r2",
        supersedes=a.acquisition,
    )
    assert b.outcome == "corrected" and b.supersedes == a.acquisition
    assert c.as_of(b.clocks.known - timedelta(microseconds=1)) == a
    assert c.exact(a.acquisition).quotes[0].close_cny == Decimal("12.34")
    assert c.as_of(b.clocks.known) == b
    with pytest.raises(ContractError, match="not_yet_known"):
        c.read("sh600519", at=a.clocks.known, acquisition=b.acquisition)
    c.observe(fixture(), clocks=clocks(20))
    assert len(c.history) == 2
    with pytest.raises(ContractError, match="exact_not_retained"):
        c.exact(a.acquisition)
    with pytest.raises(ContractError, match="asof_not_retained"):
        c.as_of(a.clocks.known)


def test_reconnect_rejects_old_segment_and_requires_new_baseline():
    c = StockSnapshotCandidate()
    c.observe(fixture(), clocks=clocks(0))
    gap = c.reconnect(clocks=clocks(10))
    assert c.read("sh600519", at=gap.clocks.known).status == "unavailable"
    old = c.observe(fixture(7770), clocks=clocks(20, request=5))
    assert old.outcome == "late" and old.segment == 1
    assert c.read("sh600519", at=old.clocks.known).status == "unavailable"
    new = c.observe(fixture(8880), clocks=clocks(25))
    assert new.segment == 1
    assert c.read("sh600519", at=new.clocks.known).status == "observed"


@pytest.mark.parametrize("capacity", [0, 1, 257, True])
def test_history_bounds(capacity):
    with pytest.raises(ContractError, match="history_bound_invalid"):
        StockSnapshotCandidate(capacity=capacity)


def test_initial_unknown_and_repeated_correction_keeps_observation_age():
    c = StockSnapshotCandidate(ttl=timedelta(seconds=10))
    assert c.read("sh600519", at=BASE).status == "unknown"
    a = c.observe(fixture(), clocks=clocks(0))
    b = c.observe(
        fixture(7770),
        clocks=clocks(10, request=0),
        parser_revision="synthetic-parser-r2",
        supersedes=a.acquisition,
    )
    d = c.observe(
        fixture(8880),
        clocks=clocks(20, request=0),
        parser_revision="synthetic-parser-r3",
        supersedes=b.acquisition,
    )
    view = c.read("sh600519", at=d.clocks.known)
    assert view.state.observed_at == a.clocks.received
    assert view.age == timedelta(seconds=22)
    assert view.status == "stale"
    assert c.as_of(b.clocks.known) == b
    with pytest.raises(ContractError, match="correction_not_current"):
        c.observe(
            fixture(),
            clocks=clocks(30, request=0),
            parser_revision="synthetic-parser-r4",
            supersedes=a.acquisition,
        )


def test_failures_are_classifications_and_bound_configuration():
    c = StockSnapshotCandidate()
    with pytest.raises(ContractError, match="failure_classification"):
        c.observe(None, clocks=clocks(0), failure="raw exception credential text")
    for ttl in (timedelta(0), timedelta(minutes=6)):
        with pytest.raises(ContractError, match="ttl_bound_invalid"):
            StockSnapshotCandidate(ttl=ttl)
    with pytest.raises(ContractError, match="security_out_of_scope"):
        c.read("sh510300", at=BASE)


def test_late_failure_and_reconnect_cannot_change_current_state():
    c = StockSnapshotCandidate()
    a = c.observe(fixture(), clocks=clocks(10))
    late = c.observe(None, clocks=clocks(20, request=0), failure="timeout")
    assert late.outcome == "late"
    assert c.read("sh600519", at=late.clocks.known).state.acquisition == a.acquisition
    assert c.read("sh600519", at=late.clocks.known).status == "observed"
    with pytest.raises(ContractError, match="reconnect_request_not_forward"):
        c.reconnect(clocks=clocks(30, request=0))
    assert c.history[-1] == late
