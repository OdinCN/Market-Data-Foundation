"""Offline GDP contracts with synthetic values; no live-provider qualification."""

import base64
import json
import socket
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from urllib.parse import parse_qs

import pytest

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.gdp_contract import (
    METRICS,
    SERIES_IDS,
    TABLES,
    GdpSourceError,
    quarter_start,
    requested_tables,
    shift_quarter,
    validate_quarter,
    validate_range,
    year_windows,
)
from market_data_foundation.historical.gdp_source import GdpRangeSource, parse_response

AT = datetime(2026, 11, 2, 7, tzinfo=UTC)
KEY = "synthetic-gdp-key-not-a-real-credential"
VALUES = {
    "A191RX": "24,000,000.123",
    "LB000003": "21,000,000.456",
    "A191RL": "3.4",
    "DPCERY": "2.50",
    "A007RY": "1.60",
    "A014RY": "-0.50",
    "A822RY": "0.80",
    "A019RY": "-1.03",
}


# Independent wire fixture identities, not generated from the product registry.
SPECS = (
    ("T10106", "1", "A191RX", "Chained Dollars", "Level", "6"),
    ("T10406", "8", "LB000003", "Chained Dollars", "Level", "6"),
    (
        "T10101",
        "1",
        "A191RL",
        "Fisher Quantity Index",
        "Percent change, annual rate",
        "0",
    ),
    ("T10102", "2", "DPCERY", "Quantity Contributions", "Level", "0"),
    ("T10102", "8", "A007RY", "Quantity Contributions", "Level", "0"),
    ("T10102", "14", "A014RY", "Quantity Contributions", "Level", "0"),
    ("T10102", "22", "A822RY", "Quantity Contributions", "Level", "0"),
    ("T10102", "15", "A019RY", "Quantity Contributions", "Level", "0"),
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline GDP test attempted network access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


def document(table, quarters=("2026Q3",)):
    return {
        "BEAAPI": {
            "Request": {
                "RequestParam": [{"ParameterName": "UserID", "ParameterValue": KEY}]
            },
            "Results": {
                "Data": [
                    {
                        "TableName": table,
                        "LineNumber": line,
                        "SeriesCode": sid,
                        "TimePeriod": q,
                        "CL_UNIT": unit,
                        "UNIT_MULT": multiplier,
                        "METRIC_NAME": metric,
                        "DataValue": VALUES[sid],
                        "LineDescription": "synthetic metric",
                        "NoteRef": "synthetic-note",
                    }
                    for t, line, sid, metric, unit, multiplier in SPECS
                    if t == table
                    for q in quarters
                ],
                "Notes": [
                    {"NoteText": "Synthetic: billions of chained (2017) dollars"}
                ],
                "UTCProductionTime": "2026-10-29T12:00:00",
            },
        }
    }


def raw(table, quarters=("2026Q3",)):
    return json.dumps(document(table, quarters)).encode()


def parse(doc, *, table="T10106", series=SERIES_IDS, start="2026-Q3", end="2026-Q3"):
    return parse_response(json.dumps(doc).encode(), series, start, end, table=table)


@pytest.mark.parametrize("quarter", ["2026-Q1", "1947-Q4", "2026-Q3"])
def test_native_quarter_contract(quarter):
    assert validate_quarter(quarter) == quarter
    assert quarter_start(quarter).isoformat() == (
        f"{quarter[:4]}-{(int(quarter[-1]) - 1) * 3 + 1:02d}-01"
    )


@pytest.mark.parametrize(
    "quarter", ["2026-09", "2026Q3", "2026-Q0", "2026-Q5", "", None, True, 2026]
)
def test_months_and_malformed_quarters_never_alias_to_gdp(quarter):
    with pytest.raises(GdpSourceError, match="quarter_invalid"):
        validate_quarter(quarter)


def test_cross_year_ranges_and_budget_count_each_table():
    assert shift_quarter("2026-Q1", -1) == "2025-Q4"
    assert shift_quarter("2025-Q4", 2) == "2026-Q2"
    assert year_windows("2007-Q4", "2026-Q3", table_count=4) == (
        (2007, 2011),
        (2012, 2016),
        (2017, 2021),
        (2022, 2026),
    )
    assert len(year_windows("1900-Q1", "1959-Q4", table_count=4)) == 12
    with pytest.raises(GdpSourceError, match="request_budget_exceeded"):
        year_windows("1900-Q1", "1960-Q1", table_count=4)
    with pytest.raises(GdpSourceError, match="range_invalid"):
        validate_range("2026-Q4", "2026-Q1")
    with pytest.raises(GdpSourceError, match="offset_invalid"):
        shift_quarter("2026-Q1", True)
    assert requested_tables(("A191RL", "A019RY")) == ("T10101", "T10102")


@pytest.mark.parametrize(
    "series", [(), [], ("UNKNOWN",), ("A191RX", "A191RX"), (True,)]
)
def test_series_selection_is_explicit_and_validated(series):
    with pytest.raises(GdpSourceError, match="series_invalid"):
        requested_tables(series)


def test_eight_native_values_do_not_scale_percent_or_derive_contributions():
    points = [p for table in TABLES for p in parse(document(table), table=table)]
    assert len(points) == 8
    for p in points:
        assert p.value == Decimal(VALUES[p.series_id].replace(",", ""))
        assert p.quarter == "2026-Q3"
        assert p.estimate_kind == "unknown"
        assert p.event_binding_status == "unconfirmed"
        assert p.event_binding_reason == "current_nipa_table_has_no_release_vintage"
    metadata = {m.series_id: m for m in METRICS}
    assert metadata["A191RX"].value_unit == "million_chained_2017_USD"
    assert metadata["A191RL"].value_unit == "percent"
    assert metadata["A019RY"].value_unit == "percentage_points"
    # A rounded contribution sum is not forced to equal the published total.
    assert sum(p.value for p in points if "RY" in p.series_id) != Decimal("3.4")


def test_full_year_response_is_filtered_to_requested_quarter_without_date_guessing():
    points = parse(document("T10106", ("2026Q1", "2026Q2", "2026Q3", "2026Q4")))
    assert len(points) == 1 and points[0].quarter == "2026-Q3"


def test_duplicate_canonical_series_in_another_table_is_legitimate_but_not_selected():
    d = document("T10406")
    duplicate = deepcopy(document("T10106")["BEAAPI"]["Results"]["Data"][0])
    duplicate["TableName"] = "T10406"
    d["BEAAPI"]["Results"]["Data"].append(duplicate)
    assert [p.series_id for p in parse(d, table="T10406")] == ["LB000003"]
    d = document("T10102")
    duplicate = deepcopy(document("T10101")["BEAAPI"]["Results"]["Data"][0])
    duplicate["TableName"] = "T10102"
    d["BEAAPI"]["Results"]["Data"].append(duplicate)
    assert len(parse(d, table="T10102")) == 5


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("TableName", "T10406", "table_invalid"),
        ("SeriesCode", "UNKNOWN", "series_mapping_invalid"),
        ("LineNumber", "99", "series_mapping_invalid"),
        ("LineNumber", 1, "series_invalid"),
        ("METRIC_NAME", "Current Dollars", "unit_invalid"),
        ("CL_UNIT", "Percent change", "unit_invalid"),
        ("UNIT_MULT", "9", "unit_invalid"),
        ("TimePeriod", "2026M09", "source_quarter_invalid"),
        ("TimePeriod", "2026", "source_quarter_invalid"),
        ("TimePeriod", "2025Q4", "source_quarter_out_of_range"),
        ("DataValue", "1,00.0", "value_invalid"),
        ("DataValue", 123.4, "value_invalid"),
        ("DataValue", "NaN", "value_invalid"),
        ("DataValue", "-1", "value_invalid"),
        ("DataValue", "0", "value_invalid"),
    ],
)
def test_identity_units_periods_and_values_fail_closed(field, value, classification):
    d = document("T10106")
    d["BEAAPI"]["Results"]["Data"][0][field] = value
    with pytest.raises(GdpSourceError, match="gdp_timeseries_" + classification):
        parse(d)


def test_duplicate_rows_missing_value_and_unverified_chain_base_rejected():
    d = document("T10106")
    d["BEAAPI"]["Results"]["Data"] *= 2
    with pytest.raises(GdpSourceError, match="duplicate_point"):
        parse(d)
    d = document("T10106")
    del d["BEAAPI"]["Results"]["Data"][0]["DataValue"]
    with pytest.raises(GdpSourceError, match="value_missing"):
        parse(d)
    d = document("T10106")
    d["BEAAPI"]["Results"]["Notes"] = [{"NoteText": "chained (2012) dollars"}]
    with pytest.raises(GdpSourceError, match="chained_base_invalid"):
        parse(d)


@pytest.mark.parametrize("value", [None, "", "(NA)", "NA", "---", "--", "..."])
def test_official_null_is_distinct_from_absent_row_and_zero(value):
    d = document("T10102")
    row = d["BEAAPI"]["Results"]["Data"][0]
    row["DataValue"] = value
    p = next(p for p in parse(d, table="T10102") if p.series_id == row["SeriesCode"])
    assert p.value is None
    assert p.provider_extra["source_missing_reason"] == "official_null"
    d["BEAAPI"]["Results"]["Data"] = []
    assert parse(d, table="T10102") == ()


@pytest.mark.parametrize("value", ["0", "-1.2"])
def test_zero_and_negative_growth_are_preserved(value):
    d = document("T10101")
    d["BEAAPI"]["Results"]["Data"][0]["DataValue"] = value
    assert parse(d, table="T10101")[0].value == Decimal(value)


class Control:
    provider = "BEA"

    def __init__(self):
        self.retries = []

    @contextmanager
    def attempt(self):
        yield self.retries.append


class Wire:
    def __init__(self, outcomes=None):
        self.outcomes = None if outcomes is None else iter(outcomes)
        self.calls = []

    def post(self, endpoint, body, headers, **options):
        params = parse_qs(body.decode())
        self.calls.append(params)
        if self.outcomes is None:
            quarters = tuple(
                y + "Q" + str(q)
                for y in params["Year"][0].split(",")
                for q in range(1, 5)
            )
            return HttpResult(200, raw(params["TableName"][0], quarters), {})
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def source(outcomes=None, **options):
    wire, control, sleeps = Wire(outcomes), Control(), []
    s = GdpRangeSource(
        credential_loader=lambda: KEY,
        transport=wire,
        request_control=control,
        clock=lambda: AT,
        sleep=sleeps.append,
        **options,
    )
    return s, wire, control, sleeps


def test_quarterly_wire_planning_safe_retention_and_individual_capture_batches():
    s, wire, _, _ = source()
    batches = s.fetch(SERIES_IDS, "2025-Q4", "2026-Q3")
    assert len(batches) == 4
    assert sum(len(b.points) for b in batches) == 32
    assert [c["TableName"][0] for c in wire.calls] == list(TABLES)
    for b, call in zip(batches, wire.calls, strict=True):
        assert call["Frequency"] == ["Q"] and call["Year"] == ["2025,2026"]
        assert b.observed_at == AT and b.attempts == 1
        assert b.request["request_count"] == 1
        assert KEY not in repr(b) and "UserID" not in repr(b.request)
        assert KEY.encode() not in b.raw and b"Request" not in b.raw
        retained = json.loads(b.raw)
        assert retained["exact_original_retained"] is False
        assert retained["retention_class"] == "credential_free_bea_results_projection"
        # The provider production time is retained, never substituted for capture.
        assert retained["Results"]["UTCProductionTime"] != b.observed_at.isoformat()
        assert all(p.event_binding_status == "unconfirmed" for p in b.points)


def test_metric_subset_only_requests_its_canonical_table_and_windows():
    s, wire, _, _ = source()
    batches = s.fetch(("A191RL",), "2020-Q4", "2026-Q1")
    assert len(batches) == 2
    assert len(wire.calls) == 2
    assert all(c["TableName"] == ["T10101"] for c in wire.calls)
    assert sum(len(b.points) for b in batches) == 22


def test_budget_and_credentials_fail_before_wire():
    s, wire, _, _ = source(max_requests=3)
    with pytest.raises(GdpSourceError, match="request_budget_exceeded"):
        s.fetch(SERIES_IDS, "2026-Q3", "2026-Q3")
    assert wire.calls == []
    with pytest.raises(GdpSourceError, match="credential_invalid"):
        GdpRangeSource().fetch(("A191RX",), "2026-Q3", "2026-Q3")


def test_bounded_retry_preserves_logical_request_and_wire_attempt_counts():
    s, wire, _, sleeps = source(
        [
            HttpResult(503, b"", {}),
            HttpResult(200, raw("T10106"), {}),
            HttpResult(200, raw("T10406"), {}),
            HttpResult(200, raw("T10101"), {}),
            HttpResult(200, raw("T10102"), {}),
        ]
    )
    batches = s.fetch(SERIES_IDS, "2026-Q3", "2026-Q3")
    assert len(wire.calls) == 5 and len(sleeps) == 1
    assert sum(b.attempts for b in batches) == 5
    assert sum(b.request["request_count"] for b in batches) == 4


def test_429_stops_without_immediate_retry_and_reports_prior_work():
    s, wire, control, sleeps = source(
        [
            HttpResult(200, raw("T10106"), {}),
            HttpResult(429, b"", {"Retry-After": "90"}),
        ]
    )
    with pytest.raises(GdpSourceError) as raised:
        s.fetch(SERIES_IDS, "2026-Q3", "2026-Q3")
    assert raised.value.classification == "gdp_timeseries_http_failure"
    assert raised.value.http_status == 429
    assert raised.value.attempts == 2 and raised.value.request_count == 2
    assert len(wire.calls) == 2 and len(control.retries) == 1
    assert sleeps == [] and raised.value.retry_at > AT


def test_204_is_a_missing_table_not_official_null_or_zero():
    s, _, _, _ = source([HttpResult(204, b"", {})])
    batch = s.fetch(("A191RX",), "2026-Q3", "2026-Q3")[0]
    assert batch.points == ()
    assert json.loads(batch.raw)["Results"]["no_content"] is True


@pytest.mark.parametrize(
    "response,classification",
    [
        (HttpResult(204, b"unexpected", {}), "nonempty_no_content"),
        (
            HttpResult(200, raw("T10106"), {"Content-Encoding": "gzip"}),
            "content_encoding_rejected",
        ),
        (HttpResult(200, b"not-json", {}), "invalid_json"),
        (
            HttpResult(
                200, b'{"BEAAPI":{"Results":{"Error":{"message":"rejected"}}}}', {}
            ),
            "provider_rejected",
        ),
    ],
)
def test_invalid_responses_are_classified_without_partial_success(
    response, classification
):
    s, _, _, _ = source([response])
    with pytest.raises(GdpSourceError, match=classification) as raised:
        s.fetch(("A191RX",), "2026-Q3", "2026-Q3")
    assert raised.value.attempts == 1 and raised.value.request_count == 1


@pytest.mark.parametrize("encoded", [KEY, base64.b64encode(KEY.encode()).decode()])
def test_credential_echo_in_results_is_never_retained_or_leaked(encoded):
    d = document("T10106")
    d["BEAAPI"]["Results"]["Notes"].append({"NoteText": encoded})
    s, _, _, _ = source([HttpResult(200, json.dumps(d).encode(), {})])
    with pytest.raises(GdpSourceError, match="credential_echo_forbidden") as raised:
        s.fetch(("A191RX",), "2026-Q3", "2026-Q3")
    assert KEY not in str(raised.value)


@pytest.mark.parametrize("attempts", [0, 4, True])
def test_invalid_retry_budget_rejected(attempts):
    with pytest.raises(GdpSourceError, match="transport_attempts_invalid"):
        source(max_transport_attempts=attempts)


def test_exhausted_transport_is_bounded_and_does_not_echo_exception():
    s, wire, _, sleeps = source([OSError(KEY), OSError(KEY)], max_transport_attempts=2)
    with pytest.raises(GdpSourceError, match="transport_failure") as raised:
        s.fetch(("A191RX",), "2026-Q3", "2026-Q3")
    assert raised.value.attempts == 2 and raised.value.request_count == 1
    assert len(wire.calls) == 2 and len(sleeps) == 1
    assert KEY not in str(raised.value)


def test_capture_times_belong_to_each_table_not_a_fabricated_common_vintage():
    from datetime import timedelta

    wire = Wire()
    times = iter(AT + timedelta(seconds=n) for n in range(4))
    s = GdpRangeSource(
        credential_loader=lambda: KEY,
        transport=wire,
        request_control=Control(),
        clock=lambda: next(times),
    )
    batches = s.fetch(SERIES_IDS, "2026-Q3", "2026-Q3")
    assert [b.observed_at for b in batches] == [
        AT + timedelta(seconds=n) for n in range(4)
    ]
    assert all(p.estimate_kind == "unknown" for b in batches for p in b.points)


def test_invalid_capture_time_is_classified_after_the_actual_request():
    s = GdpRangeSource(
        credential_loader=lambda: KEY,
        transport=Wire(),
        request_control=Control(),
        clock=lambda: datetime(2026, 11, 2),
    )
    with pytest.raises(GdpSourceError, match="observed_at_invalid") as raised:
        s.fetch(("A191RX",), "2026-Q3", "2026-Q3")
    assert raised.value.attempts == 1
