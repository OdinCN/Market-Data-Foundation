"""Offline Claims contracts; isolated PostgreSQL requires a separate opt-in."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.claims_collection import ClaimsCollectionService
from market_data_foundation.historical.claims_contract import ClaimsError
from market_data_foundation.historical.claims_source import (
    ClaimsBatch,
    parse_observations,
)
from market_data_foundation.historical.claims_store import (
    PostgresClaimsStore,
    migration_sql,
    schema_name,
    validate_batch,
)
from market_data_foundation.historical.claims_timeseries import project_range
from tests.test_timeseries_read_snapshot import Connection

AT = datetime(2026, 10, 1, tzinfo=UTC)


def batch(*, values=("100", "120"), observed=AT, realtime="2026-10-01", sid="ICSA"):
    start, end = "2026-09-19", "2026-09-26"
    doc = dict(
        count=len(values),
        offset=0,
        units="lin",
        output_type=1,
        observation_start=start,
        observation_end=end,
        sort_order="asc",
        observations=[
            dict(date=week, value=value, realtime_start=realtime, realtime_end=realtime)
            for week, value in zip((start, end), values)
        ],
    )
    raw = json.dumps(doc).encode()
    parameters = dict(
        series_id=sid,
        file_type="json",
        units="lin",
        observation_start=start,
        observation_end=end,
        output_type="1",
        sort_order="asc",
        limit="100000",
    )
    return ClaimsBatch(
        parse_observations(raw, sid, start, end),
        raw,
        dict(
            url="https://api.stlouisfed.org/fred/series/observations",
            parameters=parameters,
            request_count=1,
        ),
        observed,
        1,
    )


def test_batch_reparsed_before_admission_and_safe_parameter_boundary():
    valid = batch()
    validate_batch(valid)
    wrong = replace(valid, points=(replace(valid.points[0], value=Decimal("999")),))
    with pytest.raises(ClaimsError, match="claims_batch_invalid"):
        validate_batch(wrong)
    request = dict(
        valid.request, parameters=dict(valid.request["parameters"], api_key="synthetic")
    )
    with pytest.raises(ClaimsError, match="claims_batch_invalid"):
        validate_batch(replace(valid, request=request))
    with pytest.raises(ClaimsError, match="claims_batch_invalid"):
        validate_batch(replace(valid, attempts=4))


def test_schema_bounds_and_week_view_have_no_monthly_key():
    with pytest.raises(ClaimsError, match="claims_schema_invalid"):
        schema_name("mdf_gdp_timeseries_dev_r1")
    sql = " ".join(migration_sql())
    assert "extract(isodow FROM week_ending)=6" in sql
    assert "p.week_ending=c.week_ending-7" in sql
    assert "month" not in sql


def test_claims_snapshot_is_readonly_top_level_and_never_acquires_writer_lock():
    conn = Connection()
    store = PostgresClaimsStore(conn, None)
    with store.read_snapshot():
        with pytest.raises(ClaimsError, match="top_level_transaction_required"):
            with store.read_snapshot():
                pytest.fail("nested snapshot accepted")
    assert conn.statements == [
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    ]
    assert conn.exits == ["commit"]


def test_global_week_order_and_stable_requested_series():
    from tests.test_claims_timeseries import row

    result = project_range(
        [row("2026-09-19", "100"), row("2026-09-26", "120")],
        "2026-09-19",
        "2026-09-26",
        series_ids=("CCSA", "ICSA"),
    )
    assert [(r["week_ending"], r["series_id"]) for r in result["rows"]] == [
        ("2026-09-26", "CCSA"),
        ("2026-09-26", "ICSA"),
        ("2026-09-19", "CCSA"),
        ("2026-09-19", "ICSA"),
    ]


def test_collection_validates_and_releases_snapshot_before_upstream():
    from contextlib import contextmanager

    depth = []

    class Store:
        @contextmanager
        def read_snapshot(self):
            depth.append(1)
            try:
                yield
            finally:
                depth.pop()

        def read_range(self, *args):
            assert depth
            return []

        def save_batches(self, batches):
            assert not depth
            return dict(saved_points=0, saved_revisions=0)

    calls = []

    def fetch(*args):
        assert not depth
        calls.append(args)
        return ()

    source = SimpleNamespace(fetch=fetch, request_count=0, attempts=0)
    service = ClaimsCollectionService(Store(), source)
    result = service.init_range("2026-09-19", "2026-09-26", series_ids=("ICSA",))
    assert calls == [(("ICSA",), "2026-08-22", "2026-09-26")]
    assert result["series_coverage"]["ICSA"]["trailing_missing_weeks"] == [
        "2026-09-26",
        "2026-09-19",
    ]


def test_database_failure_exposes_classification_without_raw_exception():
    import psycopg

    class BrokenConnection(Connection):
        def execute(self, *args):
            raise psycopg.OperationalError("synthetic private diagnostic")

    store = PostgresClaimsStore(BrokenConnection(), None)
    with pytest.raises(ClaimsError) as error:
        with store.read_snapshot():
            pytest.fail("broken connection admitted")
    assert str(error.value) == "claims_snapshot_failed"
    assert "private" not in str(error.value)


def test_reinitialization_only_requests_remaining_tail_and_is_local_when_complete():
    from contextlib import nullcontext

    from market_data_foundation.historical.claims_contract import weeks

    calls = []
    known = [
        SimpleNamespace(series_id="ICSA", week_ending=w)
        for w in weeks("2026-08-22", "2026-09-19")
    ]
    store = SimpleNamespace(read_snapshot=nullcontext, read_range=lambda *args: known)
    source = SimpleNamespace(
        request_count=0, attempts=0, fetch=lambda *args: calls.append(args) or ()
    )
    service = ClaimsCollectionService(store, source)
    service.query_range = lambda *args, **kwargs: dict(
        rows=[], status="partial", coverage={}
    )
    service.init_range("2026-09-19", "2026-09-26", series_ids=("ICSA",))
    assert calls == [(("ICSA",), "2026-09-26", "2026-09-26")]
    known.append(SimpleNamespace(series_id="ICSA", week_ending="2026-09-26"))
    service.init_range("2026-09-19", "2026-09-26", series_ids=("ICSA",))
    assert len(calls) == 1
