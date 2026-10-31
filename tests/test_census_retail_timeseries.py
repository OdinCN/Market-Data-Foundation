"""Synthetic range, retry and arithmetic contracts; no provider or storage IO."""

import hashlib
import json
import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical import census_retail_timeseries_source as source
from market_data_foundation.historical.census_retail import (
    CATEGORY_CODES,
    ENDPOINT,
    HEADERS,
)
from market_data_foundation.historical.census_retail_live import HttpResult
from market_data_foundation.historical.census_retail_timeseries import (
    calculate_snapshot,
)
from market_data_foundation.historical.cpi_timeseries import month_at, month_number

AT = datetime(2026, 10, 31, tzinfo=UTC)
KEY = "synthetic0123456789abcdef"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("deterministic retail test attempted network")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def payload(start, end, *, duplicates=True):
    header = list(HEADERS)
    if duplicates:
        header += ["seasonally_adj", "error_data"]
    rows = [header]
    for n in range(month_number(start), month_number(end) + 1):
        for category, kind in [(c, "SM") for c in CATEGORY_CODES] + [
            ("44X72", "MPCSM"),
            ("445", "SM"),
        ]:
            value = str(1000 + n) if category == "44X72" else "10"
            if kind == "MPCSM":
                value = "9.7"
            if category == "445":
                value = "(S)"
            row = dict(
                category_code=category,
                data_type_code=kind,
                seasonally_adj="yes",
                error_data="no",
                cell_value=value,
                time=month_at(n),
                us="1",
            )
            rows.append([row[name] for name in header])
    return rows


def encode(table):
    return json.dumps(table).encode()


def response(body):
    return HttpResult(200, body, {"content-type": "application/json; charset=utf-8"})


class Wire:
    def __init__(self, outcomes=None):
        self.calls, self.outcomes = [], outcomes

    def get(self, endpoint, parameters, *, timeout, max_response_bytes):
        self.calls.append((endpoint, parameters.copy(), timeout, max_response_bytes))
        if self.outcomes is not None:
            result = self.outcomes.pop(0)
            if isinstance(result, Exception):
                raise result
            return result
        _, start, _, end = parameters["time"].split()
        return response(encode(payload(start, end)))


def reader(wire, **kwargs):
    return source.CensusRetailRangeSource(
        enabled=True, credential=KEY, transport=wire, clock=lambda: AT, **kwargs
    )


def snapshot(table, start, end):
    raw = encode(table)
    request = source.RetailRangeRequest(start, end)
    cells = source.parse_range_response(raw, start, end)
    plan = source.plan_range(start, end, buffer_months=0)
    return source.RetailInputSnapshot(
        plan, (source.RetailBatch(request, cells, raw, AT, 1),), AT
    )


def target(table, month, category="44X72", kind="SM"):
    header = table[0]
    return next(
        row
        for row in table[1:]
        if (
            row[header.index("time")],
            row[header.index("category_code")],
            row[header.index("data_type_code")],
        )
        == (month, category, kind)
    )


def test_default_off_and_explicit_credential_before_io():
    wire = Wire()
    with pytest.raises(source.RetailSourceError, match="explicit_opt_in"):
        source.CensusRetailRangeSource(transport=wire).fetch("2026-01", "2026-09")
    for key in (None, "", "x", "secret with spaces"):
        with pytest.raises(source.RetailSourceError, match="credential_invalid"):
            source.CensusRetailRangeSource(
                enabled=True, credential=key, transport=wire
            ).fetch("2026-01", "2026-09")
    assert wire.calls == []


def test_twenty_year_target_plus_buffer_is_single_252_month_request():
    wire = Wire()
    result = reader(wire).fetch("2006-10", "2026-09")
    assert len(result.batches) == 1 and len(result.batches[0].cells) == 1512
    assert result.plan.input_start_month == "2005-10"
    assert result.plan.transport_attempt_budget == 3
    endpoint, params, timeout, maximum = wire.calls[0]
    assert endpoint == ENDPOINT and params["time"] == "from 2005-10 to 2026-09"
    assert params["for"] == "us:*" and params["seasonally_adj"] == "yes"
    assert params["error_data"] == "no" and "category_code" not in params
    assert timeout == 20.0 and maximum > 628365
    assert KEY not in repr(result) and KEY not in repr(
        result.batches[0].request.persisted()
    )
    points = calculate_snapshot(result)
    assert len(points) == 240
    assert all(p.headline_rates[12].value is not None for p in points)


def test_bounded_plan_preflights_all_requests_before_transport():
    plan = source.plan_range("2006-10", "2047-09")
    assert [(r.start_month, r.end_month) for r in plan.requests] == [
        ("2005-10", "2026-09"),
        ("2026-10", "2047-09"),
    ]
    assert plan.transport_attempt_budget == 6
    wire = Wire()
    with pytest.raises(source.RetailSourceError, match="budget_exceeded"):
        reader(wire, max_requests=1).fetch("2006-10", "2047-09")
    assert not wire.calls


@pytest.mark.parametrize(
    "start,end,kwargs",
    [
        ("2026-13", "2027-01", {}),
        ("2026-02", "2026-01", {}),
        ("1900-01", "1900-02", {}),
        ("2026-01", "2026-02", {"buffer_months": 13}),
        ("2026-01", "2026-02", {"max_requests": True}),
    ],
)
def test_invalid_ranges_and_budgets(start, end, kwargs):
    with pytest.raises(source.RetailSourceError):
        source.plan_range(start, end, **kwargs)


def test_cross_batch_all_horizons_use_merged_inputs():
    result = reader(Wire()).fetch("2006-10", "2027-01")
    points = {p.month: p for p in calculate_snapshot(result)}
    assert len(result.batches) == 2
    for horizon in (1, 3, 6, 12):
        point = points["2026-10"]
        n = month_number(point.month)
        assert (
            point.headline_rates[horizon].value
            == Decimal(1000 + n) / Decimal(1000 + n - horizon) - 1
        )
        assert (
            point.control_rates[horizon].value
            == Decimal(960 + n) / Decimal(960 + n - horizon) - 1
        )
        assert {c.month for c in point.control_rates[horizon].inputs} == {
            point.month,
            month_at(n - horizon),
        }


def test_official_percent_never_overwritten_by_derived_ratio():
    table = payload("2026-08", "2026-09")
    h = table[0].index("cell_value")
    target(table, "2026-08")[h] = "100"
    target(table, "2026-09")[h] = "103.12"
    point = calculate_snapshot(snapshot(table, "2026-08", "2026-09"))[1]
    assert point.official_headline_mpcsm.value == Decimal("9.7")
    assert point.official_headline_mpcsm.unit == "percent"
    assert point.headline_rates[1].value == Decimal("0.0312")
    assert point.headline_rates[1].unit == "ratio"
    assert point.control_level.value == Decimal("63.12")
    assert len(point.control_level.inputs) == 5
    assert "445" not in {c.category_code for c in point.control_level.inputs}
    assert point.headline_rates[3].reason == "comparison_level_unavailable"


@pytest.mark.parametrize(
    "marker,status,reason",
    [
        ("(S)", "suppressed", "source_suppression_marker"),
        ("(D)", "suppressed", "source_suppression_marker"),
        (None, "unavailable", "source_null"),
        ("(NA)", "unavailable", "source_non_numeric_marker"),
        ("unknown", "unavailable", "source_non_numeric_marker"),
    ],
)
def test_source_markers_preserved_per_operand(marker, status, reason):
    table = payload("2026-08", "2026-09")
    target(table, "2026-09", "441")[table[0].index("cell_value")] = marker
    point = calculate_snapshot(snapshot(table, "2026-08", "2026-09"))[1]
    cell = next(c for c in point.source_cells if c.category_code == "441")
    assert (cell.value, cell.status, cell.reason, cell.source_value) == (
        None,
        status,
        reason,
        marker,
    )
    assert point.control_level.value is None and point.control_rates[1].value is None
    assert point.headline_rates[1].value is not None


def test_missing_whole_month_and_missing_official_are_explicit():
    table = payload("2026-08", "2026-09")
    table = [table[0]] + [
        r for r in table[1:] if r[table[0].index("time")] == "2026-08"
    ]
    points = calculate_snapshot(snapshot(table, "2026-08", "2026-10"))
    assert len(points) == 3
    assert all(
        c.status == "missing" and c.reason == "source_row_absent"
        for c in points[1].source_cells
    )
    assert points[1].official_headline_mpcsm.value is None
    assert points[1].control_level.value is None
    assert points[2].headline_level.value is None


def test_zero_is_a_real_source_value_but_zero_denominator_is_undefined():
    table = payload("2026-08", "2026-09")
    target(table, "2026-08")[table[0].index("cell_value")] = "0"
    point = calculate_snapshot(snapshot(table, "2026-08", "2026-09"))[1]
    assert point.headline_rates[1].value is None
    assert point.headline_rates[1].reason == "comparison_level_zero"


@pytest.mark.parametrize("duplicate", [False, True])
def test_header_order_and_allowed_duplicate_predicates(duplicate):
    table = payload("2026-09", "2026-09", duplicates=duplicate)
    table = [list(reversed(row)) for row in table]
    cells = source.parse_range_response(encode(table), "2026-09", "2026-09")
    assert len(cells) == 6 and all(c.status == "available" for c in cells)
    assert all(c.raw_sha256 == hashlib.sha256(encode(table)).hexdigest() for c in cells)


@pytest.mark.parametrize(
    "mutation,classification",
    [
        ("duplicate_dimension", "duplicate_dimension_mismatch"),
        ("duplicate_header", "header_invalid"),
        ("duplicate_target", "duplicate_target"),
        ("wrong_month", "row_range_invalid"),
        ("wrong_us", "row_dimensions_invalid"),
        ("wrong_sa", "row_dimensions_invalid"),
        ("wrong_error", "row_dimensions_invalid"),
    ],
)
def test_dimensions_scope_and_duplicates_fail_closed(mutation, classification):
    table = payload("2026-09", "2026-09")
    h = table[0]
    if mutation == "duplicate_dimension":
        table[-1][-1] = "yes"  # even a non-target row is checked
    elif mutation == "duplicate_header":
        table[0].append("time")
    elif mutation == "duplicate_target":
        table.append(table[1].copy())
    else:
        name, value = {
            "wrong_month": ("time", "2026-10"),
            "wrong_us": ("us", "2"),
            "wrong_sa": ("seasonally_adj", "no"),
            "wrong_error": ("error_data", "yes"),
        }[mutation]
        for i, field in enumerate(h):
            if field == name:
                table[1][i] = value
    with pytest.raises(source.RetailSourceError, match=classification):
        source.parse_range_response(encode(table), "2026-09", "2026-09")


@pytest.mark.parametrize("escaped", [False, True])
def test_credential_echo_guard_covers_non_target_and_json_escapes(escaped):
    table = payload("2026-09", "2026-09")
    table[-1][table[0].index("cell_value")] = KEY
    raw = encode(table)
    if escaped:
        raw = raw.replace(KEY.encode(), ("\\u%04x" % ord(KEY[0]) + KEY[1:]).encode())
    wire = Wire([response(raw)])
    with pytest.raises(
        source.RetailSourceError, match="credential_echo_forbidden"
    ) as caught:
        reader(wire).fetch("2026-09", "2026-09", buffer_months=0)
    assert caught.value.attempts == 1 and len(wire.calls) == 1
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)


def test_retry_transient_transport_three_attempts_and_safe_errors():
    wire = Wire(
        [
            HttpResult(503, b"", {}),
            TimeoutError(KEY),
            response(encode(payload("2026-09", "2026-09"))),
        ]
    )
    delays = []
    result = reader(wire, sleep=delays.append).fetch(
        "2026-09", "2026-09", buffer_months=0
    )
    assert result.batches[0].attempts == 3 and delays == [1.0, 2.0]
    wire = Wire([TimeoutError(KEY)] * 3)
    with pytest.raises(source.RetailSourceError) as caught:
        reader(wire, sleep=lambda _: None).fetch("2026-09", "2026-09")
    assert caught.value.attempts == 3 and KEY not in repr(caught.value)


@pytest.mark.parametrize(
    "status,headers,seconds",
    [
        (429, {}, 300),
        (503, {"retry-after": "60"}, 60),
        (503, {"Retry-After": "Sat, 31 Oct 2026 00:01:00 GMT"}, 60),
    ],
)
def test_long_wait_returns_retry_at_without_polling(status, headers, seconds):
    wire, delays = Wire([HttpResult(status, b"", headers)]), []
    with pytest.raises(source.RetailSourceError) as caught:
        reader(wire, sleep=delays.append).fetch("2026-09", "2026-09")
    assert caught.value.retry_at == AT + timedelta(seconds=seconds)
    assert caught.value.attempts == 1 and len(wire.calls) == 1 and not delays


def test_short_retry_after_and_redirect_terminal():
    wire, delays = (
        Wire(
            [
                HttpResult(503, b"", {"retry-after": "3"}),
                response(encode(payload("2026-09", "2026-09"))),
            ]
        ),
        [],
    )
    reader(wire, sleep=delays.append).fetch("2026-09", "2026-09", buffer_months=0)
    assert delays == [3]
    wire = Wire([HttpResult(302, b"", {"location": "https://elsewhere/"})])
    with pytest.raises(source.RetailSourceError) as caught:
        reader(wire).fetch("2026-09", "2026-09")
    assert caught.value.http_status == 302 and caught.value.attempts == 1


def test_success_followed_by_parse_or_clock_failure_never_reissues_request():
    wire = Wire([response(b"bad json")])
    with pytest.raises(source.RetailSourceError) as caught:
        reader(wire).fetch("2026-09", "2026-09")
    assert len(wire.calls) == 1 and caught.value.attempts == 1
    wire = Wire()
    times = iter([AT, RuntimeError(KEY)])

    def clock():
        value = next(times)
        if isinstance(value, Exception):
            raise value
        return value

    obj = source.CensusRetailRangeSource(
        enabled=True, credential=KEY, transport=wire, clock=clock
    )
    with pytest.raises(source.RetailSourceError, match="local_failure") as caught:
        obj.fetch("2026-09", "2026-09")
    assert (
        len(wire.calls) == 1
        and caught.value.attempts == 1
        and KEY not in repr(caught.value)
    )


def test_later_batch_failure_does_not_reissue_successful_batch():
    first = response(encode(payload("2005-10", "2026-09")))
    wire = Wire([first] + [HttpResult(503, b"", {})] * 3)
    with pytest.raises(source.RetailSourceError) as caught:
        reader(wire, sleep=lambda _: None).fetch("2006-10", "2026-10")
    assert caught.value.attempts == 4
    assert [call[1]["time"] for call in wire.calls] == ["from 2005-10 to 2026-09"] + [
        "from 2026-10 to 2026-10"
    ] * 3


@pytest.mark.parametrize("category", CATEGORY_CODES)
@pytest.mark.parametrize("availability", ["absent", "suppressed"])
def test_every_control_operand_shortfall_propagates(category, availability):
    table = payload("2026-09", "2026-09")
    row = target(table, "2026-09", category)
    if availability == "absent":
        table.remove(row)
    else:
        row[table[0].index("cell_value")] = "(S)"
    point = calculate_snapshot(snapshot(table, "2026-09", "2026-09"))[0]
    assert point.control_level.value is None
    cell = next(c for c in point.control_level.inputs if c.category_code == category)
    assert cell.status == ("missing" if availability == "absent" else "suppressed")


def test_missing_official_value_does_not_destroy_valid_sm_calculations():
    table = payload("2026-08", "2026-09")
    table.remove(target(table, "2026-09", kind="MPCSM"))
    point = calculate_snapshot(snapshot(table, "2026-08", "2026-09"))[1]
    assert point.official_headline_mpcsm.status == "missing"
    assert point.headline_rates[1].value is not None
    assert point.control_rates[1].value is not None


def test_each_comparison_gap_blocks_control_rate_without_faking_zero():
    table = payload("2025-09", "2026-09")
    for month in ("2026-08", "2026-06", "2026-03", "2025-09"):
        table.remove(target(table, month, "441"))
    point = calculate_snapshot(snapshot(table, "2025-09", "2026-09"))[-1]
    assert point.control_level.value is not None
    assert all(
        r.value is None and r.reason == "comparison_level_unavailable"
        for r in point.control_rates.values()
    )
    assert all(r.value is not None for r in point.headline_rates.values())


def test_decimal_sales_preserve_precision_beyond_binary_float():
    table = payload("2026-09", "2026-09")
    target(table, "2026-09")[table[0].index("cell_value")] = (
        "999999999999999999.000000001"
    )
    point = calculate_snapshot(snapshot(table, "2026-09", "2026-09"))[0]
    assert point.headline_level.value == Decimal("999999999999999999.000000001")
    assert point.control_level.value == Decimal("999999999999999959.000000001")


@pytest.mark.parametrize("outcome", [ValueError(KEY), object()])
def test_terminal_transport_or_result_failure_reports_actual_safe_attempts(outcome):
    wire = Wire([outcome])
    with pytest.raises(source.RetailSourceError) as caught:
        reader(wire).fetch("2026-09", "2026-09")
    assert caught.value.attempts == 1 and len(wire.calls) == 1
    assert KEY not in str(caught.value) and KEY not in repr(caught.value)


def test_local_sleep_failure_is_not_a_new_transport_attempt():
    wire = Wire([HttpResult(503, b"", {})])

    def fail_sleep(seconds):
        raise OSError(KEY)

    with pytest.raises(source.RetailSourceError, match="local_failure") as caught:
        reader(wire, sleep=fail_sleep).fetch("2026-09", "2026-09")
    assert caught.value.attempts == 1 and len(wire.calls) == 1
