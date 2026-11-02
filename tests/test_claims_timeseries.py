from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.claims_contract import (
    SERIES_IDS,
    ClaimsError,
    buffer_start,
    twenty_year_range,
    weeks,
)
from market_data_foundation.historical.claims_source import (
    ClaimsBatch,
    ClaimsPoint,
)
from market_data_foundation.historical.claims_timeseries import (
    ClaimsObservation,
    ClaimsTimeSeriesService,
    project_range,
)

T0 = datetime(2026, 10, 1, 12, tzinfo=UTC)
T1 = T0 + timedelta(days=1)
T2 = T1 + timedelta(days=1)


def row(week, value, *, sid="ICSA", revision=1, observed=T0, available=T1):
    return ClaimsObservation(
        sid,
        week,
        Decimal(value) if value is not None else None,
        observed,
        available,
        revision,
    )


def test_calendar_years_and_real_saturdays_cross_year_leap_and_month():
    start, end = twenty_year_range("2026-11-02")
    assert (start, end) == ("2006-11-02", "2026-11-02")
    assert len(weeks(start, end)) == 1044
    assert buffer_start(start) == "2006-10-05"
    assert weeks("2023-12-30", "2024-01-06") == ("2023-12-30", "2024-01-06")
    assert weeks("2024-02-27", "2024-03-03") == ("2024-03-02",)
    assert weeks("2026-11-01", "2026-11-02") == ()


@pytest.mark.parametrize("sid", SERIES_IDS)
def test_each_series_keeps_its_own_week_and_identity(sid):
    result = project_range(
        [row("2026-09-26", "100", sid=sid)],
        "2026-09-26",
        "2026-10-03",
        series_ids=(sid,),
    )
    assert [r["week_ending"] for r in result["rows"]] == ["2026-10-03", "2026-09-26"]
    assert result["rows"][0]["value"] is None
    actual = result["rows"][1]
    assert actual["value"] == "100"
    assert actual["provider"] == "FRED"
    assert actual["original_agency"] == "DOL-ETA"
    assert actual["official_first_release"] is None
    assert actual["event_binding_status"] == "unconfirmed"
    assert actual["surprise"] is None
    assert actual["unit"] == "Number"
    assert ("local_four_week_average" in actual) == (sid not in {"IC4WSA", "CC4WSA"})


def test_same_cutoff_selects_both_numerator_and_denominator_inclusively():
    rows = [
        row("2026-09-19", "100"),
        row("2026-09-26", "120"),
        row("2026-09-19", "200", revision=2, observed=T1, available=T2),
    ]
    before = project_range(
        rows,
        "2026-09-26",
        "2026-09-26",
        series_ids=("ICSA",),
        as_of=T2 - timedelta(microseconds=1),
    )
    after = project_range(
        rows, "2026-09-26", "2026-09-26", series_ids=("ICSA",), as_of=T2
    )
    assert before["rows"][0]["weekly_change"]["value"] == "20"
    assert after["rows"][0]["weekly_change"]["value"] == "-80"
    assert before["rows"][0]["weekly_change_ratio"]["value"] == "0.2"
    assert after["rows"][0]["weekly_change_ratio"]["value"] == "-0.4"
    assert after["rows"][0]["weekly_change"]["inputs"][1]["revision"] == 2
    observed = project_range(
        rows,
        "2026-09-26",
        "2026-09-26",
        series_ids=("ICSA",),
        as_of=T1,
        as_of_basis="observed",
    )
    assert observed["rows"][0]["weekly_change"]["value"] == "-80"


def test_four_week_average_is_exact_separate_from_official_and_never_fills_gaps():
    rows = [
        row(w, v)
        for w, v in zip(
            ("2026-09-05", "2026-09-12", "2026-09-19", "2026-09-26"),
            ("100", "200", "300", "401"),
            strict=True,
        )
    ]
    rows.append(row("2026-09-26", "249", sid="IC4WSA"))
    result = project_range(
        rows, "2026-09-26", "2026-09-26", series_ids=("ICSA", "IC4WSA")
    )
    assert result["rows"][0]["local_four_week_average"]["value"] == "250.25"
    assert result["rows"][1]["value"] == "249"
    del rows[1]
    missing = project_range(rows, "2026-09-26", "2026-09-26", series_ids=("ICSA",))
    assert missing["rows"][0]["local_four_week_average"]["value"] is None
    assert missing["rows"][0]["weekly_change"]["value"] == "101"


def test_provider_missing_missing_row_and_zero_have_distinct_results():
    rows = [row("2026-09-12", "0"), row("2026-09-19", "5"), row("2026-09-26", None)]
    result = project_range(rows, "2026-09-12", "2026-10-03", series_ids=("ICSA",))
    missing, null, five, zero = result["rows"]
    assert missing["missing_reason"] == "local_not_provided"
    assert null["missing_reason"] == "provider_missing"
    assert zero["value"] == "0"
    assert five["weekly_change"]["value"] == "5"
    assert five["weekly_change_ratio"]["missing_reason"] == "zero_denominator"
    assert result["coverage"] == {
        "expected": 4,
        "available": 2,
        "provider_missing_values": 1,
        "missing_rows": 1,
    }


def test_invalid_week_value_revision_time_and_conflict_rejected():
    for kwargs in (
        {"week_ending": "2026-09-25"},
        {"value": Decimal("NaN")},
        {"available_at": T0 - timedelta(seconds=1)},
        {"revision": 0},
    ):
        with pytest.raises(ClaimsError):
            replace(row("2026-09-26", "10"), **kwargs)
    with pytest.raises(ClaimsError, match="revision_conflict"):
        project_range(
            [row("2026-09-26", "10"), row("2026-09-26", "11")],
            "2026-09-26",
            "2026-09-26",
            series_ids=("ICSA",),
        )
    with pytest.raises(ClaimsError, match="basis_unsupported"):
        project_range([], "2026-09-26", "2026-09-26", as_of_basis="official")


class ReadStore:
    def __init__(self, observations):
        self.observations = tuple(observations)
        self.calls = []

    @contextmanager
    def read_snapshot(self):
        yield

    def read_range(self, *args):
        self.calls.append(args)
        return self.observations


class Source:
    request_count = attempts = 0

    def __init__(self, *, fail=False):
        self.fail = fail

    def fetch(self, *args):
        self.request_count += 1
        self.attempts += 1
        if self.fail:
            raise ClaimsError("claims_http_failure")
        return (
            ClaimsBatch(
                (
                    ClaimsPoint("ICSA", "2026-09-19", Decimal("999"), {}),
                    ClaimsPoint("ICSA", "2026-09-26", Decimal("888"), {}),
                ),
                b"{}",
                {},
                T2,
                1,
            ),
        )


@pytest.mark.parametrize("allow", [False, True])
def test_history_is_read_only_and_never_calls_upstream(allow):
    source = Source(fail=True)
    service = ClaimsTimeSeriesService(ReadStore([row("2026-09-26", "10")]), source)
    result = service.query_range(
        "2026-09-26", "2026-09-26", series_ids=("ICSA",), as_of=T1, allow_upstream=allow
    )
    assert result["provider_requests"] == source.request_count == 0
    assert result["rows"][0]["value"] == "10"


def test_upstream_fault_preserves_local_and_never_writes_or_fabricates():
    store = ReadStore([row("2026-09-26", "10")])
    service = ClaimsTimeSeriesService(store, Source(fail=True))
    result = service.query_range(
        "2026-09-19", "2026-09-26", series_ids=("ICSA",), allow_upstream=True
    )
    assert result["source_failure"] == "claims_http_failure"
    assert result["provider_requests"] == result["provider_attempts"] == 1
    assert result["rows"][0]["value"] == "10"
    assert result["rows"][1]["value"] is None
    assert len(store.observations) == 1


def test_upstream_only_fills_absent_points_and_has_no_available_time():
    service = ClaimsTimeSeriesService(ReadStore([row("2026-09-26", None)]), Source())
    result = service.query_range(
        "2026-09-19", "2026-09-26", series_ids=("ICSA",), allow_upstream=True
    )
    null, supplement = result["rows"]
    assert null["missing_reason"] == "provider_missing"
    assert supplement["value"] == "999"
    assert supplement["provenance"]["available_at"] is None
    assert supplement["provenance"]["revision"] is None
    assert supplement["first_captured_at"] is None


@pytest.mark.parametrize(
    "start,end",
    [
        ("2027-01-01", "2027-02-01"),
        ("2026-10-03", "2026-10-03"),
        ("2026-10-01", "2026-10-02"),
    ],
)
def test_future_weeks_do_not_trigger_source(start, end):
    source = Source()
    service = ClaimsTimeSeriesService(ReadStore([]), source, clock=lambda: T0)
    result = service.query_range(start, end, series_ids=("ICSA",), allow_upstream=True)
    assert result["provider_requests"] == source.request_count == 0


def test_first_capture_value_is_independent_from_latest_and_not_official_release():
    initial = row("2026-09-26", "100")
    revised = row("2026-09-26", "120", revision=2, observed=T1, available=T2)
    result = project_range(
        [revised, initial], "2026-09-26", "2026-09-26", series_ids=("ICSA",), as_of=T2
    )
    actual = result["rows"][0]
    assert actual["value"] == "120"
    assert actual["first_captured_value"] == "100"
    assert actual["first_captured_at"] == T0.isoformat()
    assert actual["first_capture_provenance"]["available_at"] == T1.isoformat()
    assert actual["official_first_release"] is None
    before_capture = project_range(
        [initial, revised], "2026-09-26", "2026-09-26", series_ids=("ICSA",), as_of=T0
    )
    assert before_capture["rows"][0]["first_captured_value"] is None


def test_missing_explicit_fred_capability_reports_failure_without_fallback():
    service = ClaimsTimeSeriesService(ReadStore([row("2026-09-26", "10")]))
    result = service.query_range(
        "2026-09-19", "2026-09-26", series_ids=("ICSA",), allow_upstream=True
    )
    assert result["source_failure"] == "claims_fred_capability_unbound"
    assert result["rows"][0]["value"] == "10"
    assert result["rows"][1]["value"] is None


def test_storage_failure_remains_storage_failure_and_does_not_trigger_source():
    class FailedStore(ReadStore):
        def read_range(self, *args):
            raise PermissionError("synthetic-local-read-denied")

    source = Source()
    with pytest.raises(PermissionError):
        ClaimsTimeSeriesService(FailedStore([]), source).query_range(
            "2026-09-26", "2026-09-26", series_ids=("ICSA",), allow_upstream=True
        )
    assert source.request_count == 0


def test_malformed_supplement_is_discarded_as_a_whole_and_local_survives():
    class BadSource(Source):
        def fetch(self, *args):
            self.request_count += 1
            self.attempts += 1
            return (
                ClaimsBatch(
                    (
                        ClaimsPoint("ICSA", "2026-09-19", Decimal("999"), {}),
                        ClaimsPoint("ICSA", "2026-09-12", Decimal("-1"), {}),
                    ),
                    b"{}",
                    {},
                    T2,
                    1,
                ),
            )

    result = ClaimsTimeSeriesService(
        ReadStore([row("2026-09-26", "10")]), BadSource()
    ).query_range("2026-09-19", "2026-09-26", series_ids=("ICSA",), allow_upstream=True)
    assert result["source_failure"] == "claims_value_invalid"
    assert [r["value"] for r in result["rows"]] == ["10", None]


def test_failed_buffer_fetch_is_partial_even_when_target_level_is_present():
    result = ClaimsTimeSeriesService(
        ReadStore([row("2026-09-26", "10")]), Source(fail=True)
    ).query_range("2026-09-26", "2026-09-26", series_ids=("ICSA",), allow_upstream=True)
    assert result["coverage"]["expected"] == result["coverage"]["available"] == 1
    assert result["status"] == "partial"
    assert result["source_failure"] == "claims_http_failure"
    assert result["rows"][0]["weekly_change"]["value"] is None
