"""Native BEA mapping/unit/security/retry fixtures, entirely offline."""

import json
from contextlib import contextmanager
from copy import deepcopy
from urllib.parse import parse_qs

import pytest

from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE as P,
)
from market_data_foundation.historical.consumption_source import (
    ConsumptionRangeSource,
    parse_response,
)
from market_data_foundation.historical.pce_timeseries_source import PceSourceError
from tests.consumption_fixtures import NOW, VALUES


def document(table):
    return {
        "BEAAPI": {
            "Request": {"UserID": "synthetic-key"},
            "Results": {
                "Data": [
                    dict(
                        TableName=table,
                        LineNumber=m["line_number"],
                        SeriesCode=s,
                        TimePeriod="2026M09",
                        CL_UNIT="Level",
                        UNIT_MULT=m["unit_mult"],
                        METRIC_NAME=m["metric_name"],
                        DataValue=VALUES[s],
                        NoteRef="synthetic-note",
                    )
                    for s, m in P.series_metadata.items()
                    if m["table_name"] == table
                ],
                "Notes": [
                    {"NoteText": "synthetic units; SAAR levels; chained (2017) dollars"}
                ],
            },
        }
    }


def raw(table, mutate=lambda d: None):
    d = document(table)
    mutate(d)
    return json.dumps(d).encode()


@pytest.mark.parametrize("sid", P.series_ids)
def test_exact_seven_native_mappings_and_magnitudes(sid):
    points = parse_response(
        raw("T20600"), P.series_ids, "2026-09", "2026-09", second_raw=raw("T20806")
    )
    p = next(p for p in points if p.series_id == sid)
    assert str(p.index_value) == VALUES[sid]
    assert p.provider_calculations["bea"]["SeriesCode"] == sid
    assert p.provider_calculations["retained_notes"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("SeriesCode", "UNKNOWN"),
        ("LineNumber", "999"),
        ("METRIC_NAME", "Fisher Price Index"),
        ("UNIT_MULT", "9"),
        ("CL_UNIT", "Percent"),
    ],
)
def test_selected_identity_or_unit_mismatch_is_rejected(field, value):
    d = document("T20600")
    d["BEAAPI"]["Results"]["Data"][0][field] = value
    with pytest.raises(PceSourceError):
        parse_response(json.dumps(d).encode(), P.series_ids, "2026-09", "2026-09")


@pytest.mark.parametrize("value", ["-2.3", "0", "3.8", None, "(NA)"])
def test_native_saving_rate_zero_negative_null(value):
    d = document("T20600")
    row = next(r for r in d["BEAAPI"]["Results"]["Data"] if r["SeriesCode"] == "A072RC")
    row["DataValue"] = value
    parsed = parse_response(json.dumps(d).encode(), ("A072RC",), "2026-09", "2026-09")[
        0
    ]
    assert (
        parsed.index_value is None
        if value in (None, "(NA)")
        else str(parsed.index_value) == value
    )


def test_source_missing_is_not_null_or_zero_and_unknown_rows_are_not_new_series():
    d = document("T20600")
    d["BEAAPI"]["Results"]["Data"] = []
    assert (
        parse_response(json.dumps(d).encode(), P.series_ids, "2026-09", "2026-09") == ()
    )
    d = document("T20600")
    row = deepcopy(d["BEAAPI"]["Results"]["Data"][0])
    row.update(SeriesCode="unrelated", LineNumber="99")
    d["BEAAPI"]["Results"]["Data"].append(row)
    assert (
        len(parse_response(json.dumps(d).encode(), P.series_ids, "2026-09", "2026-09"))
        == 4
    )
    del d["BEAAPI"]["Results"]["Data"][0]["DataValue"]
    with pytest.raises(PceSourceError, match="value_missing"):
        parse_response(json.dumps(d).encode(), P.series_ids, "2026-09", "2026-09")


class Control:
    provider = "BEA"

    def __init__(self):
        self.retries = []

    @contextmanager
    def attempt(self):
        yield self.retries.append


class Wire:
    def __init__(self, outcomes):
        self.outcomes, self.calls = iter(outcomes), []

    def post(self, endpoint, body, headers, **options):
        self.calls.append(parse_qs(body.decode()))
        result = next(self.outcomes)
        if isinstance(result, Exception):
            raise result
        return result


def source(outcomes, **options):
    wire, control = Wire(outcomes), Control()
    sleeps = []
    result = ConsumptionRangeSource(
        credential_loader=lambda: "synthetic-key",
        transport=wire,
        request_control=control,
        sleep=sleeps.append,
        clock=lambda: NOW,
        **options,
    )
    return result, wire, control, sleeps


def test_safe_results_projection_two_requests_and_retry_counts():
    s, wire, _, sleeps = source(
        [
            HttpResult(503, b"", {}),
            HttpResult(200, raw("T20600"), {}),
            HttpResult(200, raw("T20806"), {}),
        ]
    )
    batch = s.fetch(P.series_ids, "2026-09", "2026-09")[0]
    assert len(batch.points) == 7 and batch.attempts == 3
    assert batch.request["request_count"] == 2 and sleeps
    assert [c["TableName"][0] for c in wire.calls] == ["T20600", "T20600", "T20806"]
    assert b"synthetic-key" not in batch.raw and b"UserID" not in batch.raw
    assert b'"exact_original_retained":false' in batch.raw
    assert b'"second_table"' in batch.raw and b'"official_mom"' not in batch.raw
    assert s.plan_windows("2005-10", "2026-09") == (
        (2005, 2009),
        (2010, 2014),
        (2015, 2019),
        (2020, 2024),
        (2025, 2026),
    )


def test_results_credential_echo_rejected_without_echoing_secret():
    d = document("T20600")
    d["BEAAPI"]["Results"]["secret"] = "synthetic-key"
    s, *_ = source([HttpResult(200, json.dumps(d).encode(), {})])
    with pytest.raises(PceSourceError, match="credential_echo") as raised:
        s.fetch(P.series_ids, "2026-09", "2026-09")
    assert "synthetic-key" not in str(raised.value)


def test_204_empty_table_is_partial_not_whole_query_failure():
    s, *_ = source([HttpResult(200, raw("T20600"), {}), HttpResult(204, b"", {})])
    batch = s.fetch(P.series_ids, "2026-09", "2026-09")[0]
    assert len(batch.points) == 4 and batch.attempts == 2


def test_429_stops_bounded_attempt_and_records_shared_cooldown():
    s, wire, control, _ = source([HttpResult(429, b"", {"Retry-After": "90"})])
    with pytest.raises(PceSourceError) as raised:
        s.fetch(P.series_ids, "2026-09", "2026-09")
    assert raised.value.attempts == 1 and raised.value.http_status == 429
    assert len(wire.calls) == 1 and len(control.retries) == 1


def test_failed_second_table_counts_both_logical_requests_and_all_attempts():
    s, wire, _, _ = source(
        [HttpResult(200, raw("T20600"), {})] + [HttpResult(503, b"", {})] * 3
    )
    with pytest.raises(PceSourceError) as raised:
        s.fetch(P.series_ids, "2026-09", "2026-09")
    assert raised.value.request_count == 2 and raised.value.attempts == 4
    assert raised.value.transient and len(wire.calls) == 4


def test_nonempty_204_and_permission_errors_are_contract_failures():
    for response in (
        HttpResult(204, b"unexpected", {}),
        PermissionError("synthetic-do-not-echo"),
    ):
        s, wire, _, _ = source([response])
        with pytest.raises(PceSourceError) as raised:
            s.fetch(P.series_ids, "2026-09", "2026-09")
        assert not raised.value.transient and len(wire.calls) == 1
        assert "synthetic-do-not-echo" not in str(raised.value)


@pytest.mark.parametrize("notes", [[], [{"NoteText": "chained (2022) dollars"}]])
def test_chained_base_must_remain_explicit_and_qualified(notes):
    document_value = document("T20806")
    document_value["BEAAPI"]["Results"]["Notes"] = notes
    with pytest.raises(PceSourceError, match="chained_base"):
        parse_response(
            raw("T20600"),
            P.series_ids,
            "2026-09",
            "2026-09",
            second_raw=json.dumps(document_value).encode(),
        )
