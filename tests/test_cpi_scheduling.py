"""Dynamic index fixtures exercise real CPI lineage and durable scheduling offline."""

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta, timezone

import httpx
import pytest
from test_cpi_local_job import RAW, Rig, no_external_io  # noqa: F401
from test_cpi_local_job import spec as release_spec

from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.historical import cpi_history as history
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.cpi_local_read import read_local_cpi
from market_data_foundation.historical.cpi_scheduling import (
    CpiPlanStore,
    CpiScheduledWorker,
    PlanSpec,
    history_spec,
)
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_schedules import CpiSchedulesHost
from tests.r5_jobs_fixture import jobs_host

NOW = datetime(2028, 3, 10, 12, tzinfo=UTC)


def raw_indices(start=2024, end=2025, *, missing=None):
    series = []
    for sid in history.SERIES_IDS:
        rows = []
        for year in range(start, end + 1):
            for month in range(1, 13):
                pointer = f"{year:04d}-{month:02d}"
                if pointer == missing:
                    continue
                rows.append(
                    {
                        "year": str(year).zfill(4),
                        "period": f"M{month:02d}",
                        "value": str(200 + (year - 1900) * 12 + month),
                        "footnotes": [],
                    }
                )
        series.append({"seriesID": sid, "data": rows})
    return json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "message": [],
            "responseTime": 1,
            "Results": {"series": series},
        }
    ).encode()


class DynamicTransport:
    def __init__(self):
        self.calls = []
        self.failure = False

    def post(self, endpoint, body, headers, **kwargs):
        request = json.loads(body)
        self.calls.append(request)
        if self.failure:
            # No transient transport retries or credential-bearing error text.
            return HttpResult(403, b"", {})
        return HttpResult(
            200, raw_indices(int(request["startyear"]), int(request["endyear"])), {}
        )


def setup(tmp_path, **kwargs):
    rig = Rig(tmp_path, **kwargs)
    clock = [NOW]
    rig.engine.clock = lambda: clock[0]
    rig.engine.kernel.clock = lambda: clock[0]
    transport = DynamicTransport()
    rig.acquirer = replace(
        rig.acquirer,
        transport=transport,
        clock=lambda: clock[0],
        history_qualification=history.HistoryQualification("unregistered", 10, 25),
    )
    store = CpiPlanStore(rig.journal, initialize=True)
    worker = CpiScheduledWorker(
        "synthetic-history-worker", store, rig.engine, rig.acquirer, lambda _spec: None
    )
    return rig, store, worker, transport, clock


def spec(**changes):
    return replace(
        PlanSpec(
            "two-history-months",
            "2024-12",
            "2025-01",
            "Asia/Shanghai",
            "19:00",
            "22:00",
            NOW.isoformat(),
            86400,
            1,
            "synthetic",
        ),
        **changes,
    )


def tick(worker, clock, identity="history-plan"):
    clock[0] += timedelta(seconds=2)
    return worker.tick(identity, enabled=True)


@pytest.mark.parametrize(
    "reference", ["1978-01", "2001-12", "2025-01", "2026-10", "2027-02"]
)
def test_arbitrary_reference_index_arithmetic(reference):
    year = int(reference[:4])
    data = history.project(raw_indices(year - 1, year), reference)
    assert data["availability"]["state"] == "available"
    assert data["measurements"][0]["comparison_period"] == history.month_text(
        history.month_number(reference) - 1
    )
    assert all(
        m["classification"] == "mdf_index_derived_change" for m in data["measurements"]
    )
    assert data["measurements"][0]["seasonal_adjustment"] == "Seasonally Adjusted"
    assert data["measurements"][1]["seasonal_adjustment"] == "Not Seasonally Adjusted"
    assert data["indices"][0]["unit"] == "index_1982_1984_100"


def test_missing_comparison_never_becomes_completed_candidate():
    raw = raw_indices(2024, 2025, missing="2024-01")
    assert history.project(raw, "2025-01")["availability"]["state"] == "incomplete"
    evidence = history.HistoryEvidence(raw, "2025-01", NOW, NOW, 1, "fixture://history")
    with pytest.raises(ContractError, match="cpi_history_incomplete"):
        history.build_plan(evidence, "example", finished_at=NOW, frozen_at=NOW)


@pytest.mark.parametrize("location", ["target", "comparison", "unused", "metadata"])
@pytest.mark.parametrize("escaped", [False, True])
def test_registered_history_credential_echo_rejected_before_persistence(
    tmp_path, location, escaped
):
    rig, _store, _worker, _transport, _clock = setup(tmp_path)
    key = "0123456789abcdef" * 2  # Synthetic; never read a host credential.
    rig.acquirer.secret_path.write_text(f'[bls]\napi_key="{key}"\n')
    payload = json.loads(raw_indices())
    if location == "metadata":
        payload["responseTime"] = {"nested": ["echo: " + key]}
    else:
        pointer = {"target": "2025-01", "comparison": "2024-01", "unused": "2024-06"}[
            location
        ]
        row = next(
            row
            for row in payload["Results"]["series"][1]["data"]
            if row["year"] + "-" + row["period"][1:] == pointer
        )
        row["footnotes"] = [{"code": "P", "text": "echo: " + key}]
    raw = json.dumps(payload).encode()
    if escaped:
        raw = raw.replace(
            key.encode(), "".join(f"\\u{ord(c):04x}" for c in key).encode()
        )
        assert key.encode() not in raw
        assert key in json.dumps(json.loads(raw))

    class EchoTransport:
        calls = 0

        def post(self, endpoint, body, headers, **kwargs):
            self.calls += 1
            assert json.loads(body)["registrationkey"] == key
            return HttpResult(200, raw, {})

    transport = EchoTransport()
    rig.acquirer = replace(
        rig.acquirer,
        transport=transport,
        history_qualification=history.HistoryQualification("registered", 20, 500),
    )
    rig.submit(target=history_spec("2025-01", "synthetic"))
    before = rig.ledger.snapshot()
    result = rig.run()
    assert result["state"] == "failed"
    assert result["classification"] == "bls_live_credential_persistence_forbidden"
    assert transport.calls == 1
    assert result["plan"] is None and result["raw"] is None
    assert rig.store.create_attempts == rig.commits == 0
    assert rig.ledger.snapshot() == before
    assert not rig.acquirer.artifact_root.exists()
    assert key not in json.dumps(result)
    assert key not in json.dumps(rig.journal.history("first"))
    for path in (tmp_path / "journal").rglob("*"):
        if path.is_file():
            assert key.encode() not in path.read_bytes()


def test_registered_history_safe_escaped_footnotes_publish_and_read(tmp_path):
    rig, _store, _worker, _transport, clock = setup(tmp_path)
    payload = json.loads(raw_indices())
    row = payload["Results"]["series"][1]["data"][0]
    note = {"code": "P", "text": "Preliminary \u2013 valid index"}
    row["footnotes"] = [note]
    raw = json.dumps(payload).encode()
    assert b"\\u2013" in raw

    class SafeTransport:
        def post(self, endpoint, body, headers, **kwargs):
            assert "registrationkey" in json.loads(body)
            return HttpResult(200, raw, {})

    rig.acquirer = replace(
        rig.acquirer,
        transport=SafeTransport(),
        history_qualification=history.HistoryQualification("registered", 20, 500),
    )
    rig.submit(target=history_spec("2025-01", "synthetic"))
    result = rig.run()
    assert result["state"] == "succeeded"
    view = read_local_cpi(
        rig.ledger,
        reference="2025-01",
        as_of=clock[0],
        version_identity=result["reconciliation"]["evidence"]["version_identity"],
    )
    content = json.loads(view.content)
    index = next(
        item
        for item in content["data"]["indices"]
        if item["series_id"] == history.SERIES_IDS[1]
    )
    measurement = next(
        item
        for item in content["data"]["measurements"]
        if item["series_id"] == history.SERIES_IDS[1]
    )
    assert index["comparison_footnotes"] == [note]
    assert measurement["preliminary"] is True
    assert (
        rig.acquirer.artifact_root / "first-a1" / "raw-response.bin"
    ).read_bytes() == raw


@pytest.mark.parametrize("contract", ["history", "release_acquire", "release_retained"])
@pytest.mark.parametrize("matching", [False, True])
def test_raw_digest_pin_enforced_before_materialization(tmp_path, contract, matching):
    rig, _store, _worker, _transport, clock = setup(tmp_path)
    raw = raw_indices() if contract == "history" else RAW
    if contract != "history":
        rig.acquirer = replace(rig.acquirer, transport=rig.transport)
    target = (
        history_spec("2025-01", "synthetic")
        if contract == "history"
        else release_spec()
    )
    retained = None
    if contract == "release_retained":
        retained = rig.acquirer.acquire("original", lambda: None)
        target = replace(
            target,
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=digest(raw),
        )
    target = replace(
        target, expected_raw_sha256=digest(raw if matching else b"different response")
    )
    rig.submit(target=target)
    before = rig.ledger.snapshot()
    result = (
        rig.engine.run(
            "first", enabled=True, preflight=lambda _spec: None, retained=retained
        )
        if retained
        else rig.run()
    )
    if not matching:
        assert result["state"] == "failed"
        assert result["classification"] == "candidate_rejected"
        assert result["failure_stage"] == "validating_candidate"
        assert result["plan"] is None and result["raw"] is None
        assert rig.store.create_attempts == rig.commits == 0
        assert rig.ledger.snapshot() == before
    else:
        assert result["state"] == "succeeded"
        assert result["raw"]["sha256"] == target.expected_raw_sha256 == digest(raw)
        assert rig.store.create_attempts == rig.commits == 1
        view = read_local_cpi(
            rig.ledger,
            reference=target.reference,
            as_of=clock[0],
            version_identity=result["reconciliation"]["evidence"]["version_identity"],
        )
        assert json.loads(view.content)["data"]["reference_period"] == target.reference


def test_multi_month_publish_exact_read_restart_and_idempotency(tmp_path):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    tick(worker, clock)
    snapshot = store.reader().snapshot()[0]
    assert snapshot["completed_batches"] == 1
    # Reconstruct the worker/store, as on host restart.
    worker = replace(worker, store=CpiPlanStore(rig.journal))
    tick(worker, clock)
    d = store.reader().snapshot()[0]
    assert d["state"] == "completed" and d["completed_batches"] == 2
    assert len(transport.calls) == 2
    assert {r["endyear"] for r in transport.calls} == {"2024", "2025"}
    assert all(
        not r["catalog"] and not r["calculations"] and "registrationkey" not in r
        for r in transport.calls
    )
    for batch in d["batches"]:
        job = batch["jobs"][0]
        assert all(
            job[k] is not None
            for k in (
                "attempt_identity",
                "raw_sha256",
                "version_identity",
                "publication_occurrence",
            )
        )
        view = read_local_cpi(
            rig.ledger,
            reference=batch["reference"],
            as_of=clock[0],
            version_identity=job["version_identity"],
        )
        content = json.loads(view.content)
        assert (
            content["provider_vintage"] is None
            and content["historical_as_of_verified"] is False
        )
        assert content["obtained_at"] >= NOW.isoformat()
    # Existing as-of contract retains ambiguity across multiple versions.
    with pytest.raises(ReadContractError):
        read_local_cpi(rig.ledger, reference="2025-01", as_of=clock[0])
    assert store.accept("history-plan", spec(), clock[0])["state"] == "completed"
    tick(worker, clock)
    assert len(transport.calls) == 2


def test_history_exact_reader_remains_legal_beyond_catalog_budget():
    from market_data_foundation.web.cpi_api import CatalogQuery, ReadQuery
    from tests.r5_fixture import AT, ReadOnlySnapshot, fixture_ledger

    ledger, plan, publication = fixture_ledger("history_over_budget")
    before = ledger.snapshot()
    from contextlib import contextmanager

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(ledger)

    app = create_app(scope, evidence_mode="OFFLINE_FIXTURE")

    def endpoint(operation):
        return next(
            route.endpoint
            for route in app.routes
            if getattr(route, "operation_id", None) == operation
        )

    query = {"reference": "2025-09", "as_of": AT}
    catalog = endpoint("cpi_catalog")(CatalogQuery(**query))
    assert catalog.classification == "operator_query_budget_exceeded"
    assert catalog.status == "UNKNOWN" and catalog.asset is None
    assert catalog.service is None
    exact = endpoint("cpi_read")(
        ReadQuery(**query, mode="exact_version", version_identity=plan.version.identity)
    )
    assert exact.version_identity == plan.version.identity
    assert exact.publication_identity == publication.key.identity
    assert exact.measurements[0].classification == "mdf_index_derived_change"
    assert ledger.snapshot() == before


@pytest.mark.parametrize("failed", [False, True])
def test_restart_after_final_cursor_commit_settles_only_proven_batches(
    tmp_path, monkeypatch, failed
):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    tick(worker, clock)
    transport.failure = failed

    def crash_before_completion_snapshot():
        raise KeyboardInterrupt()

    # The final cursor transaction has committed; completion has not been read
    # or written. No synthetic plan/job state is injected into the journal.
    with monkeypatch.context() as patch:
        patch.setattr(store, "reader", crash_before_completion_snapshot)
        with pytest.raises(KeyboardInterrupt):
            tick(worker, clock)
    before = store.snapshot()[0]
    assert before["state"] == "active"
    assert before["cycle"] == 1 and before["cursor"] == 0
    assert before["inflight_key"] is None
    assert before["completed_batches"] == (1 if failed else 2)
    assert before["total_batches"] == 2 and len(transport.calls) == 2
    jobs_before = [
        rig.journal.history(b["jobs"][0]["job_identity"]) for b in before["batches"]
    ]

    # Restart before next_due and outside the dispatch window: settlement is
    # observation of durable proof, so it must not wait for another dispatch.
    clock[0] += timedelta(hours=3)
    assert clock[0] < datetime.fromisoformat(before["next_due"])
    restarted_store = CpiPlanStore(rig.journal)
    worker = replace(worker, store=restarted_store)
    for _ in range(2):
        tick(worker, clock)
        after = restarted_store.snapshot()[0]
        assert after["state"] == ("active" if failed else "completed")
        assert after["completed_batches"] == before["completed_batches"]
    assert len(transport.calls) == 2
    assert [
        rig.journal.history(b["jobs"][0]["job_identity"]) for b in after["batches"]
    ] == jobs_before


def test_pause_resume_cancel_and_timezone_window(tmp_path):
    _, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    store.action("history-plan", "pause", clock[0])
    tick(worker, clock)
    assert not transport.calls
    store.action("history-plan", "resume", clock[0])
    clock[0] += timedelta(hours=3)  # 23:00 Shanghai, outside the finite window
    tick(worker, clock)
    assert not transport.calls
    clock[0] += timedelta(hours=21)
    tick(worker, clock)
    assert len(transport.calls) == 1
    store.action("history-plan", "cancel", clock[0])
    tick(worker, clock)
    assert (
        len(transport.calls) == 1 and store.get("history-plan")["state"] == "cancelled"
    )


def test_failed_window_backfill_preserves_prior_attempt(tmp_path):
    _, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    transport.failure = True
    tick(worker, clock)
    transport.failure = False
    tick(worker, clock)
    assert store.reader().snapshot()[0]["completed_batches"] == 1
    with pytest.raises(ContractError):
        store.backfill("history-plan", "2025-01", "2025-01", clock[0])
    store.backfill("history-plan", "2024-12", "2024-12", clock[0])
    tick(worker, clock)
    d = store.reader().snapshot()[0]
    assert d["state"] == "completed" and d["completed_batches"] == 2
    old, new = d["batches"][0]["jobs"]
    assert old["state"] == "failed" and new["state"] == "succeeded"
    assert old["job_identity"] != new["job_identity"] and len(transport.calls) == 3


def test_unknown_commit_reconciles_without_source_replay(tmp_path):
    def unknown():
        raise RuntimeError("DO_NOT_LOG_PRIVATE_EXCEPTION")

    _, store, worker, transport, clock = setup(tmp_path, commit=unknown)
    store.accept("history-plan", spec(), clock[0])
    tick(worker, clock)
    first = store.reader().snapshot()[0]
    assert first["completed_batches"] == 0
    assert first["batches"][0]["jobs"][0]["state"] == "outcome_unknown"
    with pytest.raises(ContractError, match="reconciliation_required"):
        store.backfill("history-plan", "2024-12", "2024-12", clock[0])
    tick(worker, clock)
    assert len(transport.calls) == 1
    assert "DO_NOT_LOG" not in json.dumps(store.reader().snapshot())


def test_interrupted_source_attempt_never_replayed(tmp_path):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])

    def interrupted(*_args, **_kwargs):
        raise KeyboardInterrupt()

    transport.post = interrupted
    with pytest.raises(KeyboardInterrupt):
        tick(worker, clock)
    job_id = next(iter(store.get("history-plan")["batches"].values()))["jobs"][0]
    assert rig.journal.get(job_id)["state"] == "running"
    transport.post = lambda *_args, **_kwargs: pytest.fail("restart reacquired BLS")
    tick(worker, clock)
    assert rig.journal.get(job_id)["state"] == "outcome_unknown"
    assert store.reader().snapshot()[0]["completed_batches"] == 0


def test_quota_and_finite_splits_opt_in(tmp_path):
    _, store, worker, transport, clock = setup(tmp_path)
    worker = replace(
        worker,
        acquirer=replace(
            worker.acquirer,
            history_qualification=history.HistoryQualification("unregistered", 10, 3),
        ),
    )
    # A >20-year interval is finite and each batch still requests only two years.
    wide = spec(start="1990-01", end="2011-01")
    store.accept("history-plan", wide, clock[0])
    assert store.reader().snapshot()[0]["total_batches"] == 253
    with pytest.raises(ContractError, match="explicit_opt_in"):
        worker.tick("history-plan")
    assert not transport.calls
    for _ in range(3):
        tick(worker, clock)
    with pytest.raises(ContractError, match="quota_exhausted"):
        tick(worker, clock)
    assert len(transport.calls) == 3
    assert store.reader().snapshot()[0]["completed_batches"] == 3
    assert all(int(r["endyear"]) - int(r["startyear"]) == 1 for r in transport.calls)


def test_recurring_cycles_and_rate_limit(tmp_path):
    _, store, worker, transport, clock = setup(tmp_path)
    store.accept(
        "history-plan", spec(start="2025-01", end="2025-01", cycles=2), clock[0]
    )
    tick(worker, clock)
    tick(worker, clock)
    assert len(transport.calls) == 1
    clock[0] += timedelta(days=1)
    tick(worker, clock)
    assert len(transport.calls) == 2
    assert store.reader().snapshot()[0]["completed_batches"] == 2


@pytest.mark.parametrize("slow_check", [1, 2, "quota"])
def test_preflight_window_expiry_defers_same_pending_child(
    tmp_path, monkeypatch, slow_check
):
    rig, store, worker, transport, clock = setup(tmp_path)
    clock[0] = NOW.replace(hour=13, minute=59, second=59)
    store.accept("history-plan", spec(start="2025-01"), clock[0])
    calls = 0

    def preflight(_spec):
        nonlocal calls
        calls += 1
        if calls == slow_check:
            clock[0] += timedelta(seconds=1)  # exactly 22:00 Shanghai

    original_check = store.check_wire

    def check_wire(*args):
        original_check(*args)
        if slow_check == "quota":
            clock[0] += timedelta(seconds=1)

    monkeypatch.setattr(store, "check_wire", check_wire)
    worker = replace(worker, preflight=preflight)
    worker.tick("history-plan", enabled=True)
    deferred = store.snapshot()[0]
    child = deferred["batches"][0]["jobs"][0]
    assert child["state"] == "pending" and child["attempt_identity"] is None
    assert rig.journal.get(child["job_identity"])["attempts"] == 0
    assert deferred["state"] == "active" and deferred["completed_batches"] == 0
    assert deferred["cycle"] == 0 and deferred["cursor"] == 0
    assert deferred["inflight_key"] == deferred["batches"][0]["key"]
    assert not transport.calls and rig.store.create_attempts == 0
    with rig.journal._connection() as connection:
        assert connection.execute("SELECT * FROM cpi_quota").fetchall() == []

    # Outside-window ticks and reconstruction retain exactly the same intent.
    worker = replace(worker, store=CpiPlanStore(rig.journal))
    worker.tick("history-plan", enabled=True)
    assert store.snapshot()[0] == deferred
    clock[0] = NOW + timedelta(days=1)
    worker.tick("history-plan", enabled=True)
    completed = store.snapshot()[0]
    assert completed["state"] == "completed"
    assert completed["batches"][0]["jobs"][0]["job_identity"] == child["job_identity"]
    assert len(completed["batches"][0]["jobs"]) == 1
    assert len(transport.calls) == 1
    worker.tick("history-plan", enabled=True)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("gate", [None, lambda: True])
def test_scheduling_admission_cannot_bypass_i4_release_slot(tmp_path, gate):
    from market_data_foundation.historical.bls_cpi_release_runner import RELEASE

    rig = Rig(tmp_path)
    rig.submit()
    rig.engine.clock = lambda: RELEASE - timedelta(seconds=1)
    with pytest.raises(ContractError, match="cpi_job_release_not_due"):
        rig.run(start_allowed=gate)
    job = rig.journal.get("first")
    assert job["state"] == "pending" and job["attempt_id"] is None
    assert job["effects"] == {} and job["attempts"] == 0
    assert rig.transport.calls == 0 and rig.store.create_attempts == 0


@pytest.mark.parametrize("delay", ["retry", "reservation"])
def test_physical_wire_cannot_cross_window_end(tmp_path, monkeypatch, delay):
    rig, store, worker, transport, clock = setup(tmp_path)
    clock[0] = NOW.replace(hour=13, minute=59, second=59)
    store.accept("history-plan", spec(start="2025-01"), clock[0])
    wires = []

    def wire(*_args, **_kwargs):
        wires.append(clock[0])
        return HttpResult(500, b"", {})

    transport.post = wire
    if delay == "retry":
        monkeypatch.setattr(
            "time.sleep",
            lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
        )
    else:
        reserve = store.reserve

        def slow_reservation(*args):
            day = reserve(*args)
            clock[0] += timedelta(seconds=1)
            return day

        monkeypatch.setattr(store, "reserve", slow_reservation)
    worker.tick("history-plan", enabled=True)
    assert len(wires) == (1 if delay == "retry" else 0)
    assert all(at.hour == 13 and at.minute == 59 for at in wires)
    assert rig.store.create_attempts == 0
    snapshot = store.snapshot()[0]
    assert snapshot["state"] == "active" and snapshot["completed_batches"] == 0
    assert snapshot["batches"][0]["jobs"][0]["state"] == "failed"
    with rig.journal._connection() as connection:
        assert connection.execute("SELECT reserved FROM cpi_quota").fetchall() == [(1,)]
        assert connection.execute("SELECT * FROM cpi_wire_pending").fetchall() == []
    worker.tick("history-plan", enabled=True)
    assert len(wires) == (1 if delay == "retry" else 0)


@pytest.mark.parametrize("slow_check", [1, 2])
def test_slow_preflight_spacing_uses_physical_wire_boundary(tmp_path, slow_check):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    calls = 0
    wires = []
    original = transport.post

    def preflight(_spec):
        nonlocal calls
        calls += 1
        if calls == slow_check:
            clock[0] += timedelta(seconds=120)

    def wire(*args, **kwargs):
        wires.append(clock[0])
        return original(*args, **kwargs)

    transport.post = wire
    worker = replace(
        worker,
        preflight=preflight,
        acquirer=replace(
            worker.acquirer,
            history_qualification=history.HistoryQualification(
                "unregistered", 10, 25, 60
            ),
        ),
    )
    worker.tick("history-plan", enabled=True)
    first = wires[0]
    # Restart forces the second tick to use the durable physical boundary.
    worker = replace(worker, store=CpiPlanStore(rig.journal))
    clock[0] += timedelta(seconds=2)
    with pytest.raises(ContractError, match="cpi_plan_rate_limited"):
        worker.tick("history-plan", enabled=True)
    assert wires == [first]
    clock[0] = first + timedelta(seconds=60)
    worker.tick("history-plan", enabled=True)
    assert wires == [first, first + timedelta(seconds=60)]
    assert store.snapshot()[0]["completed_batches"] == 2


def test_physical_retries_are_charged_to_their_actual_utc_day(tmp_path, monkeypatch):
    from collections import Counter

    rig, store, worker, transport, clock = setup(tmp_path)
    clock[0] = datetime(2028, 3, 10, 23, 59, 59, tzinfo=UTC)
    store.accept(
        "history-plan",
        spec(first_due=clock[0].isoformat(), window_start="07:00", window_end="09:00"),
        clock[0],
    )
    worker = replace(
        worker,
        acquirer=replace(
            worker.acquirer,
            history_qualification=history.HistoryQualification("unregistered", 10, 3),
        ),
    )
    wires = []
    original = transport.post

    def wire(*args, **kwargs):
        wires.append(clock[0])
        if len(wires) % 3:
            return HttpResult(500, b"", {})
        return original(*args, **kwargs)

    transport.post = wire
    monkeypatch.setattr(
        "time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
    )
    worker.tick("history-plan", enabled=True)
    worker = replace(worker, store=CpiPlanStore(rig.journal))
    clock[0] += timedelta(seconds=2)
    worker.tick("history-plan", enabled=True)
    counts = Counter(t.date().isoformat() for t in wires)
    assert counts == {"2028-03-10": 1, "2028-03-11": 3}
    with rig.journal._connection() as connection:
        assert dict(connection.execute("SELECT day,reserved FROM cpi_quota")) == counts
    snapshot = store.snapshot()[0]
    assert snapshot["completed_batches"] == 1
    assert snapshot["batches"][1]["jobs"][0]["state"] == "failed"
    worker.tick("history-plan", enabled=True)
    assert len(wires) == 4  # no blind replay of the stopped logical request


@pytest.mark.parametrize("response", ["http_failure", "timeout"])
def test_retries_cannot_bypass_physical_spacing(tmp_path, monkeypatch, response):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    worker = replace(
        worker,
        acquirer=replace(
            worker.acquirer,
            history_qualification=history.HistoryQualification(
                "unregistered", 10, 25, 60
            ),
        ),
    )
    wires = []

    def wire(*_args, **_kwargs):
        wires.append(clock[0])
        # A transport can take arbitrarily longer than its admission timestamp.
        clock[0] += timedelta(seconds=120)
        if response == "timeout":
            raise TimeoutError("DO_NOT_LOG_PRIVATE_EXCEPTION")
        return HttpResult(500, b"", {})

    transport.post = wire
    monkeypatch.setattr(
        "time.sleep",
        lambda seconds: clock.__setitem__(0, clock[0] + timedelta(seconds=seconds)),
    )
    worker.tick("history-plan", enabled=True)
    snapshot = store.snapshot()[0]
    assert len(wires) == 1
    assert snapshot["completed_batches"] == 0
    assert snapshot["batches"][0]["jobs"][0]["state"] == "failed"
    assert rig.store.create_attempts == 0
    with rig.journal._connection() as connection:
        assert connection.execute(
            "SELECT reserved,last_dispatch FROM cpi_quota"
        ).fetchone() == (1, (wires[0] + timedelta(seconds=120)).isoformat())
    worker = replace(worker, store=CpiPlanStore(rig.journal))
    with pytest.raises(ContractError, match="cpi_plan_rate_limited"):
        worker.tick("history-plan", enabled=True)
    assert len(wires) == 1
    assert "DO_NOT_LOG" not in json.dumps(store.snapshot())


@pytest.mark.parametrize("failure", ["reserve", "finish", "before_wire", "during_wire"])
def test_wire_bookkeeping_failure_and_crash_fail_closed(tmp_path, monkeypatch, failure):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    reserve = store.reserve
    original = transport.post
    wires = []

    def fail(*_args, **_kwargs):
        raise OSError("DO_NOT_LOG_PRIVATE_EXCEPTION")

    def interrupted_reserve(*args):
        reserve(*args)
        raise KeyboardInterrupt()

    def wire(*args, **kwargs):
        wires.append(clock[0])
        if failure == "during_wire":
            raise KeyboardInterrupt()
        return original(*args, **kwargs)

    transport.post = wire
    with monkeypatch.context() as patch:
        if failure == "reserve":
            patch.setattr(store, "reserve", fail)
        elif failure == "finish":
            patch.setattr(store, "finish_wire", fail)
        elif failure == "before_wire":
            patch.setattr(store, "reserve", interrupted_reserve)
        if failure in {"before_wire", "during_wire"}:
            with pytest.raises(KeyboardInterrupt):
                worker.tick("history-plan", enabled=True)
        else:
            worker.tick("history-plan", enabled=True)
    expected = 0 if failure in {"reserve", "before_wire"} else 1
    assert len(wires) == expected and rig.store.create_attempts == 0
    snapshot = store.snapshot()[0]
    assert snapshot["completed_batches"] == 0
    assert snapshot["batches"][0]["jobs"][0]["state"] == (
        "running" if failure in {"before_wire", "during_wire"} else "failed"
    )
    with rig.journal._connection() as connection:
        assert connection.execute("SELECT reserved FROM cpi_quota").fetchall() == (
            [] if failure == "reserve" else [(1,)]
        )
    if failure == "reserve":
        return  # accounting failed before the physical attempt; nothing to replay

    restarted = replace(worker, store=CpiPlanStore(rig.journal))
    clock[0] += timedelta(days=1)
    if failure in {"before_wire", "during_wire"}:
        restarted.tick("history-plan", enabled=True)  # observation only
        job = restarted.store.snapshot()[0]["batches"][0]["jobs"][0]
        assert job["state"] == "outcome_unknown"
    # Another plan/day cannot bypass an interrupted or unrecorded wire end.
    store.accept("another-plan", spec(first_due=clock[0].isoformat()), clock[0])
    for _ in range(2):
        with pytest.raises(
            ContractError, match="cpi_plan_wire_reconciliation_required"
        ):
            restarted.tick("another-plan", enabled=True)
    assert len(wires) == expected
    assert "DO_NOT_LOG" not in json.dumps(store.snapshot())


def test_reservation_commit_crossing_midnight_cannot_send_on_uncharged_day(
    tmp_path, monkeypatch
):
    rig, store, worker, transport, clock = setup(tmp_path)
    clock[0] = datetime(2028, 3, 10, 23, 59, 59, tzinfo=UTC)
    store.accept(
        "history-plan",
        spec(first_due=clock[0].isoformat(), window_start="07:00", window_end="09:00"),
        clock[0],
    )
    reserve = store.reserve

    def delayed_commit(*args):
        day = reserve(*args)
        clock[0] += timedelta(seconds=2)
        return day

    monkeypatch.setattr(store, "reserve", delayed_commit)
    worker.tick("history-plan", enabled=True)
    assert not transport.calls and rig.store.create_attempts == 0
    snapshot = store.snapshot()[0]
    assert snapshot["completed_batches"] == 0
    assert snapshot["batches"][0]["jobs"][0]["state"] == "failed"
    with rig.journal._connection() as connection:
        assert connection.execute("SELECT day,reserved FROM cpi_quota").fetchall() == [
            ("2028-03-10", 1)
        ]
        assert not connection.execute("SELECT 1 FROM cpi_wire_pending").fetchall()


@pytest.mark.parametrize("clock_zone", [UTC, timezone(timedelta(hours=8))])
def test_utc_rollover_resets_quota_but_preserves_dispatch_spacing(tmp_path, clock_zone):
    rig, store, worker, transport, clock = setup(tmp_path)
    first = datetime(2028, 3, 10, 23, 58, tzinfo=UTC)
    clock[0] = first.astimezone(clock_zone)
    worker = replace(
        worker,
        acquirer=replace(
            rig.acquirer,
            history_qualification=history.HistoryQualification(
                "unregistered", 10, 3, 3600
            ),
        ),
    )
    store.accept(
        "history-plan",
        spec(
            timezone="UTC",
            window_start="00:00",
            window_end="23:59",
            first_due=clock[0].isoformat(),
        ),
        clock[0],
    )
    worker.tick("history-plan", enabled=True)
    # Reconstruct the store so the previous day's spacing must come from disk.
    worker = replace(worker, store=CpiPlanStore(rig.journal))
    for elapsed in (120, 3599):  # next day 00:00 and 00:57:59 UTC
        clock[0] = (first + timedelta(seconds=elapsed)).astimezone(clock_zone)
        with pytest.raises(ContractError, match="cpi_plan_rate_limited"):
            worker.tick("history-plan", enabled=True)
        assert len(transport.calls) == 1
        with rig.journal._connection() as connection:
            assert connection.execute(
                "SELECT day,reserved FROM cpi_quota"
            ).fetchall() == [("2028-03-10", 1)]
        batch = list(store.get("history-plan")["batches"].values())[-1]
        pending = rig.journal.get(batch["jobs"][-1])
        assert pending["state"] == "pending" and pending["attempt_id"] is None

    clock[0] = (first + timedelta(hours=1)).astimezone(clock_zone)
    worker.tick("history-plan", enabled=True)  # exact minimum spacing, 00:58 UTC
    assert len(transport.calls) == 2
    assert store.snapshot()[0]["state"] == "completed"
    with rig.journal._connection() as connection:
        assert connection.execute(
            "SELECT day,reserved FROM cpi_quota ORDER BY day"
        ).fetchall() == [("2028-03-10", 1), ("2028-03-11", 1)]
    # Two more real attempts consume the rest of the reset daily budget.
    store.accept(
        "another-plan",
        spec(
            timezone="UTC",
            window_start="00:00",
            window_end="23:59",
            first_due=clock[0].isoformat(),
        ),
        clock[0],
    )
    for _ in range(2):
        clock[0] += timedelta(hours=1)
        worker.tick("another-plan", enabled=True)
    with pytest.raises(ContractError, match="cpi_plan_daily_quota_exhausted"):
        store.reserve(
            worker.acquirer.history_qualification, clock[0] + timedelta(hours=1)
        )
    assert len(transport.calls) == 4
    with rig.journal._connection() as connection:
        assert connection.execute(
            "SELECT day,reserved FROM cpi_quota ORDER BY day"
        ).fetchall() == [("2028-03-10", 1), ("2028-03-11", 3)]


def test_http_acceptance_auth_strict_input_and_no_execution(tmp_path):
    rig, store, _, transport, clock = setup(tmp_path)
    trusted = replace(jobs_host(rig), clock=lambda: clock[0])
    app = create_app(
        schedules_host=CpiSchedulesHost(
            trusted, store, rig.acquirer.history_qualification
        )
    )
    intent = {
        **spec().document(),
        "plan_identity": "history-plan",
        "asset_identity": trusted.spec.definition,
        "asset_revision": trusted.spec.definition_revision,
    }
    intent.pop("effect_domain")

    async def exchange():
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://fixture"
        ) as client:
            assert (
                await client.post("/api/r5/cpi/plans", json=intent)
            ).status_code == 202
            assert (
                await client.post("/api/r5/cpi/plans", json=intent)
            ).status_code == 202
            assert (
                await client.post("/api/r5/cpi/plans", json={**intent, "role": "admin"})
            ).status_code == 422
            assert (await client.get("/api/r5/cpi/plans")).json()["plans"][0][
                "completed_batches"
            ] == 0
            assert (
                await client.post(
                    "/api/r5/cpi/plans/history-plan/action", json={"action": "pause"}
                )
            ).status_code == 202
            assert (await client.get("/api/r5/cpi/plans")).json()["plans"][0][
                "state"
            ] == "paused"
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(create_app()), base_url="http://fixture"
        ) as client:
            assert (await client.get("/api/r5/cpi/plans")).json()["status"] == "UNKNOWN"
            assert (
                await client.post("/api/r5/cpi/plans", json=intent)
            ).status_code == 403

    asyncio.run(exchange())
    assert not transport.calls


def test_plan_endpoint_logic_and_history_job_projection_without_threadpool(tmp_path):
    """HTTP transport is a separate Host gate; run real endpoint/DTO logic here."""
    from pydantic import ValidationError
    from starlette.requests import Request

    from market_data_foundation.web.cpi_jobs import JobQuery, Operator
    from market_data_foundation.web.cpi_schedules import PlanAction, PlanIntent

    rig, store, worker, transport, clock = setup(tmp_path)
    trusted = replace(jobs_host(rig), clock=lambda: clock[0])
    from contextlib import contextmanager

    from market_data_foundation.web.cpi_api import ReadQuery
    from tests.r5_fixture import ReadOnlySnapshot

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(rig.ledger)

    app = create_app(
        scope,
        evidence_mode="OFFLINE_FIXTURE",
        jobs_host=trusted,
        schedules_host=CpiSchedulesHost(
            trusted, store, rig.acquirer.history_qualification
        ),
    )

    def endpoint(operation):
        return next(
            r.endpoint
            for r in app.routes
            if getattr(r, "operation_id", None) == operation
        )

    request = Request({"type": "http", "headers": []})
    body = {
        **spec().document(),
        "plan_identity": "history-plan",
        "asset_identity": trusted.spec.definition,
        "asset_revision": trusted.spec.definition_revision,
    }
    body.pop("effect_domain")
    with pytest.raises(ValidationError):
        PlanIntent.model_validate({**body, "role": "admin"})
    assert (
        endpoint("cpi_plan_accept")(request, PlanIntent.model_validate(body)).acceptance
        == "DURABLE"
    )
    assert (
        endpoint("cpi_plan_accept")(
            request, PlanIntent.model_validate(body)
        ).plan_identity
        == "history-plan"
    )
    assert endpoint("cpi_plans")(request).plans[0].completed_batches == 0
    assert not transport.calls
    endpoint("cpi_plan_action")(request, "history-plan", PlanAction(action="pause"))
    assert endpoint("cpi_plans")(request).plans[0].state == "paused"
    endpoint("cpi_plan_action")(request, "history-plan", PlanAction(action="resume"))
    tick(worker, clock)
    tick(worker, clock)
    projected = endpoint("cpi_plans")(request)
    assert projected.plans[0].completed_batches == 2
    job = projected.plans[0].batches[0].jobs[0]
    jobs = endpoint("cpi_jobs")(request, JobQuery(job_identity=job.job_identity))
    assert jobs.jobs[0].version_identity == job.version_identity
    assert jobs.jobs[0].publication_occurrence == job.publication_occurrence
    assert jobs.jobs[0].reference == "2024-12"
    read = endpoint("cpi_read")(
        ReadQuery(
            reference="2024-12",
            as_of=clock[0].isoformat(),
            mode="exact_version",
            version_identity=job.version_identity,
        )
    )
    assert read.provider_vintage == "UNKNOWN" and read.obtained_at is not None
    assert read.measurements[0].classification == "mdf_index_derived_change"

    from market_data_foundation.web.cpi_jobs import Empty

    assert (
        endpoint("cpi_job_cancel")(request, job.job_identity, Empty()).acceptance
        == "DURABLE"
    )
    assert rig.journal.get(job.job_identity)["state"] == "succeeded"

    denied_host = replace(trusted, authorize=lambda _r: Operator("reader", False))
    denied_app = create_app(
        schedules_host=CpiSchedulesHost(
            denied_host, store, rig.acquirer.history_qualification
        )
    )
    deny = next(
        r.endpoint
        for r in denied_app.routes
        if getattr(r, "operation_id", None) == "cpi_plan_accept"
    )
    assert deny(request, PlanIntent.model_validate(body)).status_code == 403


def test_plan_lock_clock_rate_and_readonly_boundary(tmp_path):
    from market_data_foundation.canonical.model import digest
    from market_data_foundation.historical.cpi_job_journal import FileJobJournal

    _, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(start="2025-01", end="2025-01"), clock[0])
    store.accept("other-plan", spec(start="2025-02", end="2025-02"), clock[0])
    with store.journal.executor("plan-" + digest(b"history-plan")[:40]):
        with pytest.raises(ContractError, match="executor_busy"):
            store.action("history-plan", "pause", clock[0])
    worker.tick("history-plan", enabled=True)
    with pytest.raises(ContractError, match="rate_limited"):
        worker.tick("other-plan", enabled=True)
    assert len(transport.calls) == 1
    clock[0] += timedelta(seconds=2)
    worker.tick("other-plan", enabled=True)
    assert len(transport.calls) == 2
    missing = tmp_path / "never-create"
    reader = CpiPlanStore(FileJobJournal(missing, read_only=True))
    with pytest.raises(Exception):
        reader.snapshot()
    assert not missing.exists()


def test_retry_restart_gap_and_cycle_specific_backfill(tmp_path, monkeypatch):
    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept(
        "history-plan", spec(start="2025-01", end="2025-01", cycles=2), clock[0]
    )
    tick(worker, clock)
    clock[0] += timedelta(days=1)
    transport.failure = True
    tick(worker, clock)
    store.backfill("history-plan", "2025-01", "2025-01", clock[0])
    original = rig.engine.run

    def interrupt_before_execution(*_args, **_kwargs):
        raise KeyboardInterrupt()

    monkeypatch.setattr(rig.engine, "run", interrupt_before_execution)
    with pytest.raises(KeyboardInterrupt):
        tick(worker, clock)
    before = store.get("history-plan")
    assert before["inflight_key"] == "1:0" and not before["retry_queue"]
    assert len(before["batches"]["1:0"]["jobs"]) == 2
    monkeypatch.setattr(rig.engine, "run", original)
    transport.failure = False
    tick(worker, clock)
    after = store.reader().snapshot()[0]
    assert after["completed_batches"] == 2 and after["state"] == "completed"
    assert len(transport.calls) == 3


def test_read_rejects_corrupt_history_calculation_and_vintage():
    evidence = history.HistoryEvidence(
        raw_indices(), "2025-01", NOW, NOW, 1, "fixture://indices"
    )
    content = json.loads(
        history.build_plan(evidence, "example", finished_at=NOW, frozen_at=NOW).content
    )
    history.validate_content(content, "2025-01")
    content["data"]["measurements"][0]["value"] = "999"
    with pytest.raises(ContractError, match="payload_invalid"):
        history.validate_content(content, "2025-01")
    content["provider_vintage"] = "2025-01"
    with pytest.raises(ContractError, match="vintage_invalid"):
        history.validate_content(content, "2025-01")


@pytest.mark.parametrize("exchange", ["endpoint", "http"])
def test_exact_read_anchor_after_child_success_before_parent_update(
    tmp_path, monkeypatch, exchange
):
    """Crash at the child/parent boundary, using real journals and read routes."""
    from contextlib import contextmanager

    from fastapi.testclient import TestClient
    from starlette.requests import Request

    from market_data_foundation.web.cpi_api import ReadQuery
    from tests.r5_fixture import ReadOnlySnapshot

    rig, store, worker, transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    run = rig.engine.run

    def crash_after_success(*args, **kwargs):
        # The persisted parent mapping is earlier than the child's acquisition,
        # Publication and independent readback. No success state is fabricated.
        clock[0] += timedelta(seconds=5)
        result = run(*args, **kwargs)
        assert result["state"] == "succeeded"
        raise KeyboardInterrupt()

    with monkeypatch.context() as patch:
        patch.setattr(rig.engine, "run", crash_after_success)
        with pytest.raises(KeyboardInterrupt):
            tick(worker, clock)
    parent = store.get("history-plan")
    assert parent["inflight_key"] == "0:0" and parent["cursor"] == 0
    durable = rig.journal.get(parent["batches"]["0:0"]["jobs"][0])
    success = durable["last_success"]
    assert parent["updated_at"] < success["evidence"]["usable_at"]
    assert success["evidence"]["version_identity"] is not None
    trusted = replace(jobs_host(rig), clock=lambda: clock[0])

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(rig.ledger)

    app = create_app(
        scope,
        evidence_mode="OFFLINE_FIXTURE",
        schedules_host=CpiSchedulesHost(
            trusted, store, rig.acquirer.history_qualification
        ),
    )
    before = (rig.ledger.snapshot(), rig.journal.history(durable["job_id"]))

    def endpoint(operation):
        return next(
            route.endpoint
            for route in app.routes
            if getattr(route, "operation_id", None) == operation
        )

    def check(plans, read):
        projected = plans["plans"][0]
        batch = projected["batches"][0]
        child = batch["jobs"][0]
        assert projected["completed_batches"] == 1
        query = dict(
            reference=batch["reference"],
            mode="exact_version",
            version_identity=child["version_identity"],
        )
        # Exact old UI query fails at the unchanged Reader time gate.
        assert read({**query, "as_of": projected["updated_at"]}) == (
            409,
            {
                "status": "NOT_AVAILABLE",
                "classification": "unpublished_or_unavailable_as_of",
            },
        )
        assert child.get("read_as_of") == success["observed_at"]
        status, result = read({**query, "as_of": child["read_as_of"]})
        assert status == 200
        assert result["version_identity"] == child["version_identity"]
        assert result["reference"] == batch["reference"]
        assert datetime.fromisoformat(
            result["publication_usable_at"]
        ) == datetime.fromisoformat(success["evidence"]["usable_at"])
        # Wrong/stale reference and unknown Version still fail closed.
        for changes, classification in (
            ({"reference": "2025-01"}, "reference_stale_or_mismatched"),
            ({"reference": "2024-11"}, "reference_stale_or_mismatched"),
            ({"version_identity": "unverified-version"}, "version_not_found"),
        ):
            assert read({**query, "as_of": child["read_as_of"], **changes}) == (
                409,
                {"status": "NOT_AVAILABLE", "classification": classification},
            )

    if exchange == "http":
        with TestClient(app) as client:

            def read(query):
                response = client.get("/api/r5/cpi/read", params=query)
                return response.status_code, response.json()

            check(client.get("/api/r5/cpi/plans").json(), read)
    else:

        def read(query):
            result = endpoint("cpi_read")(ReadQuery(**query))
            if hasattr(result, "status_code"):
                return result.status_code, json.loads(result.body)
            return 200, result.model_dump(mode="json")

        check(
            endpoint("cpi_plans")(Request({"type": "http", "headers": []})).model_dump(
                mode="json"
            ),
            read,
        )
    assert (rig.ledger.snapshot(), rig.journal.history(durable["job_id"])) == before
    assert store.get("history-plan") == parent
    assert len(transport.calls) == rig.store.create_attempts == 1


@pytest.mark.parametrize(
    "missing",
    [
        "success",
        "observed_at",
        "version",
        "raw",
        "coverage",
        "reference",
        "naive",
        "early",
    ],
)
def test_plan_read_anchor_requires_matching_success_evidence(tmp_path, missing):
    rig, store, worker, _transport, clock = setup(tmp_path)
    store.accept("history-plan", spec(), clock[0])
    tick(worker, clock)
    job_id = store.get("history-plan")["batches"]["0:0"]["jobs"][0]
    job = rig.journal.get(job_id)
    assert store.reader().snapshot()[0]["batches"][0]["jobs"][0]["read_as_of"]
    # Synthetic corrupt/legacy evidence exercises only the read-only projection.
    if missing == "success":
        job["last_success"] = None
    elif missing == "observed_at":
        job["last_success"].pop("observed_at")
    elif missing == "version":
        job["last_success"]["evidence"]["version_identity"] = "other-version"
    elif missing == "raw":
        job["last_success"]["evidence"]["raw_sha256"] = "0" * 64
    elif missing == "coverage":
        job["last_success"]["evidence"]["coverage"] = []
    elif missing == "reference":
        job["spec"]["reference"] = "2025-01"
    elif missing == "naive":
        job["last_success"]["observed_at"] = "2028-03-10T12:00:02"
    else:
        job["last_success"]["observed_at"] = NOW.isoformat()
    with rig.journal._connection() as connection, connection:
        connection.execute(
            "UPDATE jobs SET document=? WHERE identity=?", (json.dumps(job), job_id)
        )
    assert store.reader().snapshot()[0]["batches"][0]["jobs"][0]["read_as_of"] is None


def test_unverified_child_publication_does_not_get_read_anchor(tmp_path):
    from contextlib import contextmanager

    @contextmanager
    def unavailable():
        raise OSError("synthetic independent read unavailable")
        yield

    rig, store, worker, transport, clock = setup(tmp_path, readback=unavailable)
    store.accept("history-plan", spec(), clock[0])
    tick(worker, clock)
    child = store.reader().snapshot()[0]["batches"][0]["jobs"][0]
    assert child["state"] == "outcome_unknown"
    assert child["version_identity"] is not None
    assert child["publication_occurrence"] is None and child["read_as_of"] is None
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "scenario,classification",
    [
        ("unpublished", "unpublished_or_unavailable_as_of"),
        ("withdrawn", "version_withdrawn"),
        ("superseded", "version_superseded"),
        ("quality", "quality_not_admitted"),
    ],
)
def test_exact_read_api_still_enforces_publication_qualification(
    scenario, classification
):
    from contextlib import contextmanager

    from market_data_foundation.canonical.codec import dumps
    from market_data_foundation.canonical.model import Decision, Grade
    from market_data_foundation.web.cpi_api import ReadQuery
    from tests.r5_fixture import AT, BASE, ReadOnlySnapshot, fixture_ledger

    ledger, plan, publication = fixture_ledger(
        "withdrawn" if scenario == "withdrawn" else "available"
    )
    as_of = AT
    if scenario == "unpublished":
        as_of = (BASE + timedelta(seconds=3)).isoformat()
    elif scenario == "superseded":
        ledger.put(
            replace(
                publication,
                request=replace(
                    publication.request,
                    identity="terminal-supersede",
                    occurrence="terminal-supersede",
                    decision=Decision.SUPERSEDE,
                    predecessor=publication.key,
                    supersedes_version=plan.version.key,
                ),
                decision_at=BASE + timedelta(seconds=5),
                usable_at=BASE + timedelta(seconds=5),
            )
        )
    elif scenario == "quality":
        publication = replace(
            publication,
            request=replace(
                publication.request, claimed_grades=frozenset({Grade.PROJECTED})
            ),
        )
        ledger._records[(publication.key.kind.value, publication.key.identity, "")] = (
            dumps(publication)
        )
    before = ledger.snapshot()

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(ledger)

    app = create_app(scope, evidence_mode="OFFLINE_FIXTURE")
    endpoint = next(
        r.endpoint for r in app.routes if getattr(r, "operation_id", None) == "cpi_read"
    )
    result = endpoint(
        ReadQuery(
            reference="2026-08",
            as_of=as_of,
            mode="exact_version",
            version_identity=plan.version.identity,
        )
    )
    assert result.status_code == 409
    assert json.loads(result.body) == {
        "status": "NOT_AVAILABLE",
        "classification": classification,
    }
    assert ledger.snapshot() == before
