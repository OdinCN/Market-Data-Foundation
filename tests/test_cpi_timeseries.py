from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import psycopg
import pytest

from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesError,
    CpiTimeSeriesService,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    CpiSourceError,
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    SERIES_IDS,
    CpiStorageError,
    GarageCpiRawStore,
    InMemoryTimeSeriesStore,
    PostgresTimeSeriesStore,
    digest,
    raw_locator,
)

NOW = datetime(2026, 10, 28, tzinfo=UTC)


class Source:
    def __init__(self):
        self.calls = []
        self.observed_at = NOW
        self.increment = Decimal(0)
        self.skip = set()

    def fetch(self, series, start, end):
        self.calls.append((series, start, end))
        points = tuple(
            SourcePoint(
                s,
                month,
                Decimal(100 + int(month[-2:])) + self.increment,
                {"pct_changes": {"1": "0.1"}},
                (),
            )
            for s in series
            for month in month_range(start, end)
            if (s, month) not in self.skip
        )
        return (
            SourceBatch(
                points,
                ("raw" + str(self.increment)).encode(),
                {"seriesid": list(series)},
                self.observed_at,
                1,
            ),
        )


@pytest.fixture
def setup():
    source = Source()
    store = InMemoryTimeSeriesStore()
    return CpiTimeSeriesService(store, source), store, source


def test_init_shared_comparison_buffer_and_zero_request_repeat(setup):
    service, store, source = setup
    first = service.init_range("2000-01", "2025-12")
    assert first["complete"] and first["persisted"]
    assert len(first["points"]) == 26 * 12 * 4
    assert first["calculation_missing"] == []
    assert source.calls == [(SERIES_IDS, "1999-01", "2025-12")]
    assert store.status()["observation_count"] == 27 * 12 * 4
    first_point = first["points"][0]
    assert first_point["mom_pct"] == "-9.82"
    assert first_point["yoy_pct"] == "0.00"
    assert first_point["provider_calculations"] == {"pct_changes": {"1": "0.1"}}
    second = service.init_range("2000-01", "2025-12")
    assert second["provider_requests"] == 0 and second["saved_points"] == 0
    assert len(source.calls) == 1


def test_query_does_not_persist_or_claim_raw_locator(setup):
    service, store, source = setup
    result = service.query_range("2020-01", "2020-02")
    assert result["complete"] and not result["persisted"]
    assert all(p["raw_locator"] is None for p in result["points"])
    assert store.status()["observation_count"] == 0
    assert source.calls[0][1] == "2019-01"


def test_gap_completion_preserves_local_values(setup):
    service, store, source = setup
    source.skip = {("CUUR0000SA0", "2020-02")}
    first = service.init_range("2020-01", "2020-03")
    assert first["missing"] == [{"series_id": "CUUR0000SA0", "month": "2020-02"}]
    source.skip.clear()
    source.increment = Decimal(10)
    source.observed_at += timedelta(minutes=1)
    second = service.init_range("2020-01", "2020-03")
    assert second["complete"] and second["saved_points"] == 1
    assert second["saved_revisions"] == 0
    assert source.calls[-1] == (("CUUR0000SA0",), "2020-02", "2020-02")
    first_month = [p for p in second["points"] if p["month"] == "2020-01"]
    assert {p["index_value"] for p in first_month} == {"101"}


def test_update_retains_changes_and_repeated_identical_observation(setup):
    service, store, source = setup
    first = service.init_range("2020-01", "2020-01")
    source.increment = Decimal(1)
    source.observed_at += timedelta(minutes=2)
    second = service.update_range("2020-01", "2020-01")
    assert second["saved_revisions"] == 13 * 4
    assert second["points"][0]["revision"] == 2
    assert first["points"][0]["observed_at"] != second["points"][0]["observed_at"]
    source.observed_at += timedelta(minutes=2)
    third = service.update_range("2020-01", "2020-01")
    assert third["saved_revisions"] == 0 and third["saved_points"] == 0
    assert third["points"][0]["observed_at"] == second["points"][0]["observed_at"]
    assert store.status()["revision_count"] == 13 * 4


def test_asof_only_actual_observations_never_fetches_current(setup):
    service, store, source = setup
    service.init_range("2000-01", "2000-01")
    early = service.query_range("2000-01", "2000-01", as_of="2000-02-01T00:00:00Z")
    assert not early["complete"] and early["provider_requests"] == 0
    assert len(source.calls) == 1
    source.increment = Decimal(2)
    source.observed_at += timedelta(days=1)
    service.update_range("2000-01", "2000-01")
    strict = service.query_range("2000-01", "2000-01", as_of=NOW)
    assert strict["as_of_basis"] == "available" and not strict["points"]
    old = service.query_range("2000-01", "2000-01", as_of=NOW, vintage_mode="observed")
    latest = service.query_range("2000-01", "2000-01", allow_upstream=False)
    assert old["points"][0]["index_value"] == "101"
    assert latest["points"][0]["index_value"] == "103"
    assert "not_mdf_commit" in old["as_of_semantics"]


def test_storage_failure_not_a_local_miss(setup):
    service, store, source = setup

    def fail(*args, **kwargs):
        raise CpiStorageError("cpi_timeseries_storage_read_failed")

    store.read_range = fail
    with pytest.raises(CpiTimeSeriesError, match="storage_read_failed"):
        service.query_range("2020-01", "2020-01")
    assert source.calls == []


def test_missing_buffer_reports_null_rate_without_fabricating(setup):
    service, store, source = setup
    source.skip = {(s, "2019-01") for s in SERIES_IDS}
    result = service.query_range("2020-01", "2020-01")
    assert result["complete"]
    assert len(result["calculation_missing"]) == 8
    assert all(p["yoy_pct"] is None for p in result["points"])


@pytest.mark.parametrize(
    "kwargs",
    [
        {"start_month": "2020-13", "end_month": "2021-01"},
        {"start_month": "2021-01", "end_month": "2020-01"},
        {"start_month": "2020-01", "end_month": "2020-01", "series_ids": ["wrong"]},
        {"start_month": "2020-01", "end_month": "2020-01", "as_of": "2020-01-01"},
    ],
)
def test_invalid_request_never_calls_source(setup, kwargs):
    service, store, source = setup
    with pytest.raises(CpiTimeSeriesError):
        service.query_range(**kwargs)
    assert source.calls == []


def test_source_error_preserves_safe_attempts(setup):
    service, store, source = setup

    def fail(*args):
        raise CpiSourceError("bls_timeout", 3)

    source.fetch = fail
    with pytest.raises(CpiTimeSeriesError) as error:
        service.query_range("2020-01", "2020-01")
    assert str(error.value) == "bls_timeout"
    assert error.value.provider_attempts == 3


class Wire:
    def __init__(self):
        self.object = None
        self.methods = []
        self.read_status = None
        self.unknown_put = False
        self.drop_put = False

    def __call__(self, method, url, headers, body, maximum, timeout):
        assert "/mdf-artifacts/cpi-timeseries/dev-r1/sha256/" in url
        self.methods.append(method)
        if method == "GET":
            if self.read_status:
                return self.read_status, b""
            return (404, b"") if self.object is None else (200, self.object)
        if not self.drop_put:
            self.object = body
        if self.unknown_put:
            raise TimeoutError("secret-never-propagates")
        return 200, b""


def raw_store(wire):
    return GarageCpiRawStore(
        GarageCredentials("access", "secret"), transport=wire, clock=lambda: NOW
    )


def test_garage_exact_readback_and_dedupe():
    wire = Wire()
    store = raw_store(wire)
    raw = b"provider-original"
    assert store.put_verified(raw) == raw_locator(digest(raw))
    assert wire.methods == ["GET", "PUT", "GET"]
    store.put_verified(raw)
    assert wire.methods == ["GET", "PUT", "GET", "GET"]


def test_garage_unreadable_is_not_absent():
    wire = Wire()
    wire.read_status = 403
    with pytest.raises(CpiStorageError, match="raw_read_failed"):
        raw_store(wire).put_verified(b"raw")
    assert wire.methods == ["GET"]


def test_garage_unknown_put_reconciles_without_rewrite():
    wire = Wire()
    wire.unknown_put = True
    assert raw_store(wire).put_verified(b"raw") == raw_locator(digest(b"raw"))
    assert wire.methods == ["GET", "PUT", "GET"]


def test_garage_unconfirmed_put_stays_unknown():
    wire = Wire()
    wire.unknown_put = wire.drop_put = True
    with pytest.raises(CpiStorageError, match="raw_outcome_unknown"):
        raw_store(wire).put_verified(b"raw")
    assert wire.methods == ["GET", "PUT", "GET"]


class Cursor:
    def __init__(self, value=None):
        self.value = value

    def fetchone(self):
        return self.value

    def fetchall(self):
        return []


class Connection:
    autocommit = True

    def __init__(self):
        self.calls = []
        self.commit_unknown = False
        self.rolled_back = False

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        if "advisory" in sql:
            return Cursor((True,))
        return Cursor()

    @contextmanager
    def transaction(self):
        try:
            yield
            if self.commit_unknown:
                raise psycopg.OperationalError("private-secret")
        except Exception:
            self.rolled_back = True
            raise

    def close(self):
        pass


def test_raw_failure_prevents_any_pg_mutation():
    conn = Connection()

    class FailRaw:
        def put_verified(self, raw):
            raise CpiStorageError("cpi_timeseries_raw_outcome_unknown")

    store = PostgresTimeSeriesStore(conn, FailRaw())
    batch = Source().fetch(SERIES_IDS, "2020-01", "2020-01")
    with pytest.raises(CpiStorageError, match="raw_outcome_unknown"):
        store.save_batches(batch)
    assert conn.calls == []


def test_pg_commit_uncertainty_is_classified_and_not_retried():
    conn = Connection()
    conn.commit_unknown = True
    store = PostgresTimeSeriesStore(conn, raw_store(Wire()))
    batch = Source().fetch(SERIES_IDS[:1], "2020-01", "2020-01")
    with pytest.raises(CpiStorageError) as error:
        store.save_batches(batch)
    assert str(error.value) == "cpi_timeseries_storage_outcome_unknown"
    assert conn.rolled_back
    assert sum("INSERT INTO" in sql for sql, _ in conn.calls) == 2


def test_pg_operation_does_not_reclassify_source_failure():
    store = PostgresTimeSeriesStore(Connection(), raw_store(Wire()))
    with pytest.raises(CpiSourceError, match="bls_timeout"):
        with store.operation():
            raise CpiSourceError("bls_timeout", 3)


def test_pg_range_reads_are_bounded_in_sql():
    conn = Connection()
    store = PostgresTimeSeriesStore(conn, raw_store(Wire()))
    store.read_range(SERIES_IDS, "1999-01", "2026-09", as_of=NOW)
    sql, params = conn.calls[-1]
    assert "month BETWEEN %s AND %s" in sql
    assert "observed_at <= %s" in sql and "LIMIT" not in sql
    assert params == (list(SERIES_IDS), date(1999, 1, 1), date(2026, 9, 1), NOW)


def test_memory_transaction_rejects_out_of_order_without_partial_state(setup):
    service, store, source = setup
    service.init_range("2020-01", "2020-01")
    before = store.status()
    batch = source.fetch(SERIES_IDS, "2020-01", "2020-01")[0]
    changed = replace(
        batch, points=(replace(batch.points[0], index_value=Decimal("999")),)
    )
    with pytest.raises(CpiStorageError, match="observation_order_conflict"):
        store.save_batches((changed,))
    assert store.status() == before


def test_sparse_gap_years_do_not_refetch_complete_middle_years(setup):
    service, store, source = setup
    gaps = {(SERIES_IDS[0], "2001-02"), (SERIES_IDS[0], "2024-03")}
    source.skip = gaps
    service.init_range("2000-01", "2025-12")
    source.skip.clear()
    source.calls.clear()
    result = service.init_range("2000-01", "2025-12")
    assert result["complete"] and result["saved_points"] == 2
    assert source.calls == [
        ((SERIES_IDS[0],), "2001-02", "2001-02"),
        ((SERIES_IDS[0],), "2024-03", "2024-03"),
    ]


def test_local_numerator_upstream_denominator_exposes_mixed_calculation(setup):
    service, store, source = setup
    source.skip = {(s, "2019-01") for s in SERIES_IDS}
    service.init_range("2020-01", "2020-01")
    source.skip.clear()
    result = service.query_range("2020-01", "2020-01")
    assert all(p["origin"] == "local" for p in result["points"])
    for p in result["points"]:
        calculation = p["calculation_inputs"]["yoy_pct"]
        assert calculation["numerator_origin"] == "local"
        assert calculation["denominator_origin"] == "upstream"
        assert calculation["derived_origin"] == "mixed"


def test_refresh_completeness_does_not_reuse_old_local_coverage(setup):
    service, store, source = setup
    service.init_range("2020-01", "2020-01")
    source.skip = {(s, "2020-01") for s in SERIES_IDS}
    result = service.update_range("2020-01", "2020-01")
    assert result["complete"]
    assert result["refresh_complete"] is False
    assert result["source_missing"] == [
        {"series_id": s, "month": "2020-01"} for s in sorted(SERIES_IDS)
    ]


class UnavailableSource(Source):
    def __init__(self):
        super().__init__()
        self.unavailable_month = "2025-10"

    def fetch(self, series, start, end):
        batches = super().fetch(series, start, end)
        return tuple(
            replace(
                batch,
                points=tuple(
                    replace(
                        p,
                        index_value=None,
                        footnotes=(
                            {
                                "code": "X",
                                "text": (
                                    "Data unavailable due to the "
                                    "2025 lapse in appropriations"
                                ),
                            },
                        ),
                    )
                    if p.month == self.unavailable_month
                    else p
                    for p in batch.points
                ),
            )
            for batch in batches
        )


def test_explicit_provider_unavailable_is_preserved_not_zero_or_repeated_miss():
    source = UnavailableSource()
    store = InMemoryTimeSeriesStore()
    service = CpiTimeSeriesService(store, source)
    first = service.init_range("2025-10", "2025-11")
    assert first["coverage_complete"] and not first["complete"]
    assert len(first["source_unavailable"]) == 4
    assert len(first["points"]) == 4
    assert all(p["availability"] == "available" for p in first["points"])
    assert all(
        p["index_value"] is None
        and p["availability"] == "source_unavailable"
        and p["raw_locator"]
        and p["footnotes"][0]["code"] == "X"
        for p in first["source_unavailable"]
    )
    assert all(p["mom_pct"] is None for p in first["points"])
    assert len(first["calculation_missing"]) == 8
    assert all(m["reason"] == "source_unavailable" for m in first["missing"])
    second = service.init_range("2025-10", "2025-11")
    query = service.query_range("2025-10", "2025-11")
    assert second["provider_requests"] == query["provider_requests"] == 0
    assert len(source.calls) == 1


def test_unavailable_is_not_refreshed_target_and_update_can_restore_numeric_value():
    source = UnavailableSource()
    store = InMemoryTimeSeriesStore()
    service = CpiTimeSeriesService(store, source)
    initial = service.update_range("2025-10", "2025-10")
    assert initial["refresh_coverage_complete"]
    assert not initial["refresh_complete"]
    assert not any(p["month"] == "2025-10" for p in initial["refreshed_series_months"])
    assert all(
        month != "2025-10" for month in service.status()["latest_months"].values()
    )
    source.unavailable_month = ""
    source.observed_at += timedelta(days=1)
    updated = service.update_range("2025-10", "2025-10")
    assert updated["complete"] and updated["refresh_complete"]
    assert updated["saved_revisions"] == 4
    assert all(p["revision"] == 2 for p in updated["points"])
    before = service.query_range(
        "2025-10", "2025-10", as_of=NOW, vintage_mode="observed"
    )
    assert not before["complete"] and before["coverage_complete"]
    assert len(before["source_unavailable"]) == 4
