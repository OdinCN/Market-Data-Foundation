"""Reader transactions and source boundaries, without PostgreSQL or providers."""

from contextlib import contextmanager
from types import SimpleNamespace

import psycopg
import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE,
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    InMemoryTimeSeriesStore,
    PostgresTimeSeriesStore,
)
from market_data_foundation.historical.gdp_store import (
    GdpStorageError,
    PostgresGdpStore,
)
from market_data_foundation.historical.retail_timeseries_store import (
    PostgresRetailStore,
    RetailStorageError,
)

PROFILES = (
    CPI_PROFILE,
    PPI_PROFILE,
    PCE_PROFILE,
    EMPLOYMENT_PROFILE,
    CONSUMPTION_PROFILE,
)


class Connection:
    autocommit = True

    def __init__(self):
        self.info = SimpleNamespace(
            transaction_status=psycopg.pq.TransactionStatus.IDLE
        )
        self.statements = []
        self.exits = []

    @contextmanager
    def transaction(self):
        self.info.transaction_status = psycopg.pq.TransactionStatus.INTRANS
        try:
            yield
        except BaseException:
            self.exits.append("rollback")
            raise
        else:
            self.exits.append("commit")
        finally:
            self.info.transaction_status = psycopg.pq.TransactionStatus.IDLE

    def execute(self, sql, params=()):
        self.statements.append(sql)
        return SimpleNamespace(fetchone=lambda: (True,))


@pytest.mark.parametrize(
    "factory,error",
    [
        (PostgresTimeSeriesStore, CpiStorageError),
        (PostgresGdpStore, GdpStorageError),
        (PostgresRetailStore, RetailStorageError),
    ],
)
def test_snapshot_is_top_level_readonly_and_releases_after_exception(factory, error):
    conn = Connection()
    store = factory(conn, None)
    for failure in (False, True):
        try:
            with store.read_snapshot():
                assert conn.statements[-1] == (
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                with pytest.raises(error, match="top_level_transaction_required"):
                    with store.read_snapshot():
                        pytest.fail("nested transaction admitted")
                if failure:
                    raise LookupError("synthetic reader failure")
        except LookupError:
            assert failure
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE
    assert conn.exits == ["commit", "rollback"]
    assert not any("advisory" in sql for sql in conn.statements)


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
@pytest.mark.parametrize("upstream", [False, True])
def test_query_reads_one_snapshot_then_fetches_without_transaction(profile, upstream):
    class Store(InMemoryTimeSeriesStore):
        depth = 0
        reads = 0
        snapshots = 0

        @contextmanager
        def operation(self):
            pytest.fail("a query requested the writer lock")
            yield

        @contextmanager
        def read_snapshot(self):
            self.snapshots += 1
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

        def read_range(self, *args, **kwargs):
            assert self.depth == 1
            self.reads += 1
            return super().read_range(*args, **kwargs)

        def save_batches(self, *args, **kwargs):
            pytest.fail("a query attempted persistence")

    store = Store(profile=profile)
    calls = []

    def fetch(*args):
        assert store.depth == 0
        calls.append(args)
        return ()

    service = CpiTimeSeriesService(store, SimpleNamespace(fetch=fetch), profile=profile)
    service.query_range("2026-09", "2026-09", allow_upstream=upstream)
    assert store.snapshots == 1 and store.reads == 2
    assert bool(calls) is upstream
    assert not store._history and not store._batches


def test_gdp_source_query_also_ends_snapshot_before_fetch():
    from tests.test_gdp_chain import service

    candidate, _, _, archive = service()
    depth = 0
    original = candidate.store.read_snapshot

    @contextmanager
    def snapshot():
        nonlocal depth
        with original():
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    candidate.store.read_snapshot = snapshot
    original_fetch = candidate.source.fetch

    def fetch(*args):
        assert depth == 0
        return original_fetch(*args)

    candidate.source.fetch = fetch
    result = candidate.query_range("2026-Q3", "2026-Q3", allow_upstream=True)
    assert result["provider_requests"] > 0 and not archive.saved
    assert candidate.status()["batch_count"] == 0


def test_gdp_release_reads_do_not_request_the_writer_lock():
    from market_data_foundation.historical.gdp_release_records import GdpReleaseRecords
    from tests.test_gdp_chain import service

    candidate, _, _, _ = service()
    records = GdpReleaseRecords(candidate.store)
    records.prepare()

    def fail():
        pytest.fail("release read requested the writer lock")

    candidate.store.operation = fail
    assert records.read()["events"] == []


@pytest.mark.parametrize("profile", PROFILES, ids=lambda p: p.key)
def test_release_evidence_and_annotations_share_one_snapshot(profile):
    from market_data_foundation.historical.macro_release_records import ReleaseRecords

    store = InMemoryTimeSeriesStore(profile=profile)
    records = ReleaseRecords(store)
    depth = 0
    snapshots = 0

    @contextmanager
    def snapshot():
        nonlocal depth, snapshots
        assert depth == 0
        depth += 1
        snapshots += 1
        try:
            yield
        finally:
            depth -= 1

    store.read_snapshot = snapshot
    original_rows, original_annotations = records._rows, records._annotations

    def rows(*args):
        assert depth == 1
        return original_rows(*args)

    def annotations(*args):
        assert depth == 1
        return original_annotations(*args)

    records._rows, records._annotations = rows, annotations
    assert records.read()["events"] == []
    assert records.link_result({"points": []})["points"] == []
    assert snapshots == 2 and depth == 0
