"""Synthetic M3 persistence and query semantics; no external services."""

from dataclasses import replace
from datetime import timedelta

import pytest

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.m3_capital_goods import parse_response
from market_data_foundation.historical.m3_capital_goods_store import (
    InMemoryM3Store,
    M3Batch,
    migration_sql,
    raw_locator,
)
from market_data_foundation.historical.m3_capital_goods_timeseries import (
    M3TimeSeriesService,
)
from tests.test_m3_capital_goods import (  # noqa: F401
    STAMP,
    encoded,
    no_network,
    request,
    table,
)


class Raw:
    def __init__(self):
        self.saved = {}

    def put_verified(self, raw):
        import hashlib

        sha = hashlib.sha256(raw).hexdigest()
        self.saved[sha] = raw
        return raw_locator(sha)


def batch(at=STAMP, dataset="m3", delta=0):
    snapshots = []
    for category, kind in (("MDM", "NO"), ("NXA", "NO"), ("NXA", "VS")):
        t = table(category, kind)
        if category == "NXA" and kind == "VS":
            t[1][4] = str(40 + delta)
        snapshots.append(
            parse_response(encoded(t), request(dataset, category, kind), obtained_at=at)
        )
    return M3Batch(tuple(snapshots))


def service():
    clock = [STAMP + timedelta(seconds=1)]
    store = InMemoryM3Store(Raw(), clock=lambda: clock[0])

    class Source:
        calls = []

        def fetch(self, req):
            self.calls.append(req)
            return next(
                s
                for s in batch(clock[0] - timedelta(seconds=1)).snapshots
                if s.request == req
            )

    return M3TimeSeriesService(store, Source()), clock


def test_collect_readback_replay_and_current_query_are_explicit():
    s, clock = service()
    with pytest.raises(ContractError, match="write_opt_in"):
        s.collect_range("2025-01", "2025-02")
    assert not s.source.calls
    receipt = s.collect_range("2025-01", "2025-02", confirm_write=True)
    assert receipt["saved_points"] == receipt["readback_points"] == 6
    assert receipt["provider_requests"] == 3 and receipt["saved_revisions"] == 0
    before = s.query_range("2025-01", "2025-02")
    assert before["coverage"] == {"expected": 6, "available": 6}
    again = s.import_batch(batch(), confirm_write=True)
    assert again["saved_points"] == 0 and again["saved_revisions"] == 0
    assert s.query_range("2025-01", "2025-02") == before
    assert (
        len(s.store.raw_store.saved) == 4
    )  # three fragments plus independent replay material


def test_unchanged_recapture_preserves_first_times_and_economic_revisions():
    s, c = service()
    s.import_batch(batch(), confirm_write=True)
    before = s.query_range("2025-01", "2025-02")
    c[0] += timedelta(days=1)
    assert (
        s.import_batch(batch(c[0] - timedelta(seconds=1)), confirm_write=True)[
            "saved_points"
        ]
        == 0
    )
    assert s.query_range("2025-01", "2025-02") == before
    assert len(s.store.batches) == 2


def test_changed_value_cutoffs_and_stage_separation():
    s, c = service()
    s.import_batch(batch(), confirm_write=True)
    old = s.query_range("2025-01", "2025-02")
    c[0] += timedelta(days=1)
    new = batch(c[0] - timedelta(seconds=1), delta=1)
    assert s.import_batch(new, confirm_write=True)["saved_revisions"] == 1
    assert (
        s.query_range("2025-01", "2025-02", as_of=STAMP + timedelta(hours=1))["points"]
        == old["points"]
    )
    at = new.snapshots[0].obtained_at
    obtained = s.query_range("2025-01", "2025-02", as_of=at, basis="observed")
    available = s.query_range("2025-01", "2025-02", as_of=at, basis="available")
    assert obtained["points"] != available["points"]
    assert (
        s.query_range("2025-01", "2025-02", dataset="advm3")["coverage"]["available"]
        == 0
    )
    s.import_batch(batch(c[0], dataset="advm3"), confirm_write=True)
    assert (
        s.query_range("2025-01", "2025-02", dataset="advm3")["coverage"]["available"]
        == 6
    )
    assert len(s.store.rows) == 13


def test_current_supplement_never_saves_and_history_never_fetches():
    s, c = service()
    query = s.query_range("2025-01", "2025-02", allow_upstream=True)
    assert query["coverage"]["available"] == 6 and not query["persisted"]
    assert len(s.source.calls) == 3 and not s.store.rows and not s.store.batches
    assert all(p["origin"] == "upstream" for p in query["points"])
    for cutoff in (STAMP, "2026-11-03T14:00:00Z"):
        with pytest.raises(ContractError, match="history_must_be_local"):
            s.query_range("2025-01", "2025-02", as_of=cutoff, allow_upstream=True)
    assert len(s.source.calls) == 3
    assert (
        s.query_range("2025-01", "2025-02", as_of=STAMP)["coverage"]["available"] == 0
    )


def test_batch_rejects_mixed_dataset_scope_duplicate_series_and_tampering():
    good = batch()
    snapshots = list(good.snapshots)
    for bad in (
        (snapshots[0], snapshots[0], snapshots[2]),
        (batch(dataset="advm3").snapshots[0], snapshots[1], snapshots[2]),
        (replace(snapshots[0], raw_sha256="0" * 64), snapshots[1], snapshots[2]),
    ):
        with pytest.raises(ContractError):
            M3Batch(tuple(bad))


def test_unconfirmed_newer_value_is_not_replaced_by_stale_available_value():
    s, c = service()
    s.import_batch(batch(), confirm_write=True)
    c[0] += timedelta(days=1)
    s.import_batch(batch(c[0], delta=1), confirm_write=True)
    s.store.rows[-1]["available_at"] = None
    result = s.query_range("2025-01", "2025-02")
    assert result["coverage"]["available"] == 5
    assert (
        s.query_range("2025-01", "2025-02", basis="observed")["coverage"]["available"]
        == 6
    )


def test_bad_raw_locator_and_capture_order_never_publish_new_rows():
    s, c = service()
    s.store.raw_store.put_verified = lambda _: "wrong"
    with pytest.raises(ContractError, match="raw_locator_mismatch"):
        s.import_batch(batch(), confirm_write=True)
    assert not s.store.rows
    s.store.raw_store = Raw()
    s.import_batch(batch(), confirm_write=True)
    with pytest.raises(ContractError, match="capture_order_conflict"):
        s.import_batch(batch(STAMP - timedelta(seconds=1), delta=1), confirm_write=True)
    assert len(s.store.rows) == 6


def test_migration_has_no_grants_or_existing_family_schema():
    sql = "\n".join(migration_sql())
    assert "GRANT " not in sql and "DROP " not in sql
    assert "mdf_m3_capital_goods_dev_r1" in sql and "available_at" in sql
    assert "NaN" in sql and "PRIMARY KEY(dataset,series_id,month,revision)" in sql


def test_failed_confirmation_replay_recovers_without_backdating_or_revision():
    s, c = service()
    saved = batch()

    def fail():
        raise RuntimeError("synthetic confirmation failure")

    s.store.clock = fail
    with pytest.raises(RuntimeError):
        s.import_batch(saved, confirm_write=True)
    assert len(s.store.rows) == 6 and all(
        r["available_at"] is None for r in s.store.rows
    )
    assert s.query_range("2025-01", "2025-02")["coverage"]["available"] == 0
    assert (
        s.query_range("2025-01", "2025-02", basis="observed")["coverage"]["available"]
        == 6
    )
    recovery = STAMP + timedelta(hours=1)
    s.store.clock = lambda: recovery
    result = s.import_batch(saved, confirm_write=True)
    assert result["saved_points"] == result["saved_revisions"] == 0
    assert result["readback_points"] == 6 and len(s.store.rows) == 6
    assert len(s.store.raw_store.saved) == 1
    assert (
        s.query_range("2025-01", "2025-02", as_of=recovery - timedelta(seconds=1))[
            "coverage"
        ]["available"]
        == 0
    )
    assert (
        s.query_range("2025-01", "2025-02", as_of=recovery)["coverage"]["available"]
        == 6
    )
    assert all(r["available_at"] == recovery.isoformat() for r in s.store.rows)


def test_postgres_existing_batch_path_confirms_after_transaction_and_skips_raw():
    from contextlib import contextmanager
    from types import SimpleNamespace

    import psycopg

    from market_data_foundation.historical.m3_capital_goods_store import PostgresM3Store

    class Result:
        def __init__(self, one=None, rows=()):
            self.one, self.rows = one, rows

        def fetchone(self):
            return self.one

        def fetchall(self):
            return self.rows

    class Connection:
        autocommit = True
        info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)

        def __init__(self):
            self.depth = 0
            self.confirmed = False

        @contextmanager
        def transaction(self):
            self.depth += 1
            assert self.depth == 1
            try:
                yield
            finally:
                self.depth -= 1

        def execute(self, sql, params=()):
            if "SELECT 1 FROM" in sql:
                return Result((1,))
            if "SELECT observed_at" in sql:
                return Result(rows=[(STAMP,)])
            if "UPDATE " in sql:
                self.confirmed = True
                assert params[0] > STAMP
            return Result()

    raw = Raw()
    connection = Connection()
    store = PostgresM3Store(connection, raw, clock=lambda: STAMP + timedelta(hours=1))
    assert store.save_batch(batch()) == {"saved_points": 0, "saved_revisions": 0}
    assert connection.confirmed and not raw.saved


def test_local_history_range_is_not_limited_to_live_probe_window():
    s, c = service()
    result = s.query_range("2024-01", "2025-12", as_of=STAMP)
    assert result["coverage"]["expected"] == 72 and result["provider_requests"] == 0
    current = s.query_range("2024-01", "2025-12", allow_upstream=True)
    assert current["budget_exhausted"] and current["provider_requests"] <= 6
    assert current["coverage"]["expected"] == 72


def test_retained_batch_roundtrip_preserves_times_and_recovers_after_restart():
    s, c = service()
    saved = batch()
    restored = M3Batch.from_raw(saved.raw)
    assert restored == saved and restored.identity == saved.identity
    assert [p.obtained_at for p in restored.snapshots] == [STAMP] * 3
    with pytest.raises(ContractError, match="material_invalid"):
        M3Batch.from_raw(saved.raw.replace(b'"schema":', b'"unknown":'))
