"""Bounded source recovery and partial query results; deterministic only."""

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.historical.m3_capital_goods import (
    M3Request,
    parse_response,
)
from market_data_foundation.historical.m3_capital_goods_source import M3SourceError
from market_data_foundation.historical.m3_capital_goods_store import (
    InMemoryM3Store,
    M3Batch,
)
from market_data_foundation.historical.m3_capital_goods_timeseries import (
    M3TimeSeriesService,
)
from tests.test_m3_capital_goods import TABLES, encoded, no_network  # noqa: F401
from tests.test_m3_capital_goods_timeseries import Raw

AT = datetime(2026, 11, 3, tzinfo=UTC)


class Wire:
    def __init__(self):
        self.calls = []
        self.fail_at = None
        self.at = AT
        self.overrides = {}

    def fetch(self, request):
        self.calls.append(request)
        if self.fail_at == len(self.calls):
            raise M3SourceError("census_m3_http_failure", 1, 204)
        template = TABLES[request.category_code]
        row = next(r for r in template[1:] if r[0] == request.data_type_code)
        rows = [template[0]]
        for i, month in enumerate(request.months):
            cell = list(row)
            cell[4] = str(
                self.overrides.get(
                    (request.category_code, request.data_type_code, month), 100 + i
                )
            )
            cell[10] = month
            rows.append(cell)
        return parse_response(encoded(rows), request, obtained_at=self.at)


def service():
    wire = Wire()
    store = InMemoryM3Store(Raw(), clock=lambda: wire.at + timedelta(seconds=1))
    return M3TimeSeriesService(store, wire), wire


def test_cross_year_partial_collection_resumes_only_missing_without_refetch():
    s, wire = service()
    wire.fail_at = 3
    first = s.collect_range("2024-12", "2025-03", confirm_write=True)
    assert first["status"] == "partial" and first["provider_requests"] == 3
    assert first["provider_attempts"] == 3 and first["saved_points"] == 4
    assert first["source_failure"]["http_status"] == 204 and len(first["missing"]) == 8
    completed = tuple(wire.calls[:2])
    assert all(
        x.start_month == "2024-12" and x.end_month == "2025-01" for x in completed
    )
    wire.fail_at = None
    second = s.collect_range(
        "2024-12", "2025-03", confirm_write=True, resume_id=first["collection_id"]
    )
    assert second["status"] == "complete" and second["saved_points"] == 8
    assert second["saved_revisions"] == 0 and second["readback_points"] == 12
    assert all(r not in completed for r in wire.calls[3:])
    assert len(wire.calls) == 7
    assert (
        s.collect_range(
            "2024-12", "2025-03", confirm_write=True, resume_id=first["collection_id"]
        )["provider_requests"]
        == 0
    )
    assert len(s.store.rows) == 12 and len(s.store.batches) == 6


def test_long_collection_is_bounded_and_resumable_in_subsequent_calls():
    s, wire = service()
    first = s.collect_range("2023-11", "2024-08", confirm_write=True)
    assert first["provider_requests"] == 6 and first["budget_exhausted"]
    assert first["saved_points"] == 12 and len(first["missing"]) == 18
    second = s.collect_range(
        "2023-11", "2024-08", confirm_write=True, resume_id=first["collection_id"]
    )
    assert second["provider_requests"] == 6 and second["budget_exhausted"]
    third = s.collect_range(
        "2023-11", "2024-08", confirm_write=True, resume_id=first["collection_id"]
    )
    assert third["provider_requests"] == 3 and third["status"] == "complete"
    assert len(s.store.rows) == 30
    assert all(len(r.months) <= 2 and r.dataset == "m3" for r in wire.calls)


def test_204_preserves_local_values_and_full_gaps_without_leaking_message():
    s, wire = service()
    s.collect_range("2025-01", "2025-02", confirm_write=True)
    s.store.rows.pop()
    wire.fail_at = len(wire.calls) + 1
    result = s.query_range("2025-01", "2025-02", allow_upstream=True)
    assert result["coverage"] == {"expected": 6, "available": 5}
    assert result["status"] == "partial" and len(result["missing"]) == 1
    assert result["provider_requests"] == result["provider_attempts"] == 1
    assert result["source_failure"]["http_status"] == 204
    assert all(p["origin"] == "local" for p in result["points"])
    assert not result["persisted"] and len(s.store.rows) == 5
    assert "synthetic private diagnostic" not in json.dumps(result)


def test_partial_success_is_kept_when_other_series_fails():
    s, wire = service()
    wire.fail_at = 2
    result = s.query_range("2025-01", "2025-02", allow_upstream=True)
    assert result["coverage"] == {"expected": 6, "available": 4}
    assert result["provider_requests"] == result["provider_attempts"] == 3
    assert (
        len(result["missing"]) == 2 and result["source_failure"]["http_status"] == 204
    )
    assert not s.store.rows and not s.store.batches


def test_long_current_query_is_bounded_and_returns_all_requested_gaps():
    s, wire = service()
    result = s.query_range("2024-01", "2025-12", allow_upstream=True)
    assert result["coverage"] == {"expected": 72, "available": 12}
    assert len(result["missing"]) == 60 and result["provider_requests"] == 6
    assert result["budget_exhausted"] and not result["persisted"]
    assert not s.store.rows


def test_retained_partial_fragment_roundtrips_and_cannot_mix_scope():
    wire = Wire()
    a = wire.fetch(M3Request("m3", "MDM", "2025-01", "2025-02"))
    one = M3Batch((a,))
    assert M3Batch.from_raw(one.raw) == one
    with pytest.raises(Exception):
        M3Batch((a, replace(a, request=replace(a.request, dataset="advm3"))))


def test_unknown_error_never_leaks_and_attempt_count_is_unknown():
    s, wire = service()

    def fail(req):
        raise RuntimeError("private diagnostic and credential sample")

    wire.fetch = fail
    result = s.query_range("2025-01", "2025-02", allow_upstream=True)
    assert result["status"] == "unavailable" and len(result["missing"]) == 6
    assert result["provider_requests"] == 1 and result["provider_attempts"] is None
    assert "private diagnostic" not in json.dumps(result)


def test_collection_resume_confirms_committed_fragment_without_refetch():
    s, wire = service()

    def fail_clock():
        raise RuntimeError("synthetic failure after commit")

    s.store.clock = fail_clock
    first = s.collect_range("2025-01", "2025-02", confirm_write=True)
    assert first["storage_failure"] and first["provider_requests"] == 1
    assert len(s.store.rows) == 2 and all(
        r["available_at"] is None for r in s.store.rows
    )
    original_request = wire.calls[0]
    recovery = AT + timedelta(hours=1)
    s.store.clock = lambda: recovery
    result = s.collect_range(
        "2025-01", "2025-02", confirm_write=True, resume_id=first["collection_id"]
    )
    assert result["confirmed_points"] == 2
    assert result["provider_requests"] == 2 and wire.calls.count(original_request) == 1
    assert result["status"] == "complete" and len(s.store.rows) == 6
    historical = s.query_range(
        "2025-01", "2025-02", as_of=recovery - timedelta(seconds=1)
    )
    assert historical["coverage"]["available"] == 0


def test_rate_limited_source_stops_bounded_query_without_raw_exception():
    s, wire = service()

    def fail(req):
        wire.calls.append(req)
        raise M3SourceError("census_m3_http_failure", 1, 429)

    wire.fetch = fail
    result = s.query_range("2024-01", "2025-12", allow_upstream=True)
    assert result["provider_requests"] == 1 and result["provider_attempts"] == 1
    assert result["source_failure"]["http_status"] == 429
    assert len(result["missing"]) == 72


def test_new_update_fetches_existing_scope_and_records_only_actual_revision():
    s, wire = service()
    first = s.collect_range("2025-01", "2025-02", confirm_write=True)
    assert first["status"] == "complete" and first["saved_points"] == 6
    wire.at += timedelta(days=1)
    wire.overrides[("NXA", "VS", "2025-02")] = 125
    update = s.collect_range("2025-01", "2025-02", confirm_write=True)
    assert update["collection_id"] != first["collection_id"]
    assert update["provider_requests"] == 3 and update["saved_revisions"] == 1
    assert update["saved_points"] == 1 and len(s.store.rows) == 7
    wire.at += timedelta(days=1)
    unchanged = s.collect_range("2025-01", "2025-02", confirm_write=True)
    assert unchanged["provider_requests"] == 3 and unchanged["saved_revisions"] == 0
    assert unchanged["saved_points"] == 0 and len(s.store.rows) == 7


def test_resume_survives_new_service_instance_and_checks_scope():
    s, wire = service()
    first = s.collect_range("2023-11", "2024-08", confirm_write=True)
    assert first["provider_requests"] == 6 and first["budget_exhausted"]
    restarted = M3TimeSeriesService(s.store, wire)
    second = restarted.collect_range(
        "2023-11", "2024-08", confirm_write=True, resume_id=first["collection_id"]
    )
    assert second["provider_requests"] == 6
    with pytest.raises(Exception, match="collection_scope_mismatch"):
        restarted.collect_range(
            "2023-11", "2024-07", confirm_write=True, resume_id=first["collection_id"]
        )


def test_postgres_raw_verification_does_not_hold_transaction():
    import hashlib
    from contextlib import contextmanager
    from types import SimpleNamespace

    import psycopg

    from market_data_foundation.historical.m3_capital_goods_store import (
        PostgresM3Store,
        raw_locator,
    )

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

        @contextmanager
        def transaction(self):
            self.depth += 1
            try:
                yield
            finally:
                self.depth -= 1

        def execute(self, sql, params=()):
            if "SELECT observed_at" in sql:
                return Result(rows=[(AT,), (AT,)])
            return Result()

    conn = Connection()

    class Archive:
        def put_verified(self, raw):
            assert conn.depth == 0
            return raw_locator(hashlib.sha256(raw).hexdigest())

    req = M3Request("m3", "MDM", "2025-01", "2025-02")
    record = M3Batch((Wire().fetch(req),))
    result = PostgresM3Store(
        conn, Archive(), clock=lambda: AT + timedelta(seconds=1)
    ).save_batch(record)
    assert result == {"saved_points": 2, "saved_revisions": 0} and conn.depth == 0


def test_cli_partial_receipt_uses_nonzero_exit_and_resume_id(
    monkeypatch, capsys, tmp_path
):
    from types import SimpleNamespace

    import scripts.m3_capital_goods as cli

    seen = {}

    class Service:
        store = SimpleNamespace(connection=SimpleNamespace(close=lambda: None))

        def collect_range(self, start, end, **kwargs):
            seen.update(kwargs)
            return {
                "status": "partial",
                "budget_exhausted": True,
                "storage_failure": None,
            }

    monkeypatch.setattr(cli, "build_service", lambda **_: Service())
    code = cli.main(
        [
            "collect",
            "--start",
            "2025-01",
            "--end",
            "2025-02",
            "--resume-id",
            "21a6f93d-675d-47e3-b2fd-1606d49c39cd",
            "--request-control",
            str(tmp_path / "control"),
            "--confirm-dev-write",
        ]
    )
    assert code == 2 and seen["resume_id"] == "21a6f93d-675d-47e3-b2fd-1606d49c39cd"
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


def test_missing_reason_change_retains_evidence_without_economic_revision():
    wire = Wire()
    req = M3Request("m3", "MDM", "2025-01", "2025-02")
    original = wire.fetch(req)
    doc = json.loads(original.raw)
    doc[1][4] = None
    first = parse_response(encoded(doc), req, obtained_at=AT)
    store = InMemoryM3Store(Raw(), clock=lambda: AT + timedelta(days=2))
    assert store.save_batch(M3Batch((first,)))["saved_points"] == 2
    doc[1][4] = "(S)"
    second = parse_response(encoded(doc), req, obtained_at=AT + timedelta(days=1))
    receipt = store.save_batch(M3Batch((second,)))
    assert receipt["saved_points"] == receipt["saved_revisions"] == 0
    assert len(store.rows) == 2 and len(store.batches) == 2
    assert first.cells[0].reason != second.cells[0].reason
