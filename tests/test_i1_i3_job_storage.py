"""Actual Job/Journal/I1 through simple SQL/wire doubles, never DEV evidence."""

import json
import sys
from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical.market_material import MemoryMaterialBytes
from market_data_foundation.canonical.market_persistence import (
    Fault,
    InjectedFailure,
    MemoryMarketLedger,
    Verification,
)
from market_data_foundation.canonical.model import ContractError
from market_data_foundation.domain_contracts.market_universe import content_id
from market_data_foundation.integration.market_vertical import (
    PhysicalIntake,
    retain_page,
    verify_original,
)
from market_data_foundation.integration.market_vertical_storage import simple_intake
from market_data_foundation.storage.market_dataset_simple import SimplePostgresDataset
from scripts import mdf_i1_i3_job_storage_dev as launcher
from scripts.mdf_i1_i3_synthetic import KNOWN, layout_for, run_job
from tests.test_i3_market_dataset import RUN, Cursor
from tests.test_i3_simple_storage import SimpleSQL, Wire, store_for


@pytest.mark.parametrize("level", ["raw", "source-projection", "status", "metadata"])
def test_intake_freezes_physical_intent_before_writer_effect(tmp_path, level):
    worker, decoder = run_job(tmp_path, RUN)
    db, wire = SimpleSQL(), Wire()
    store = store_for(wire)
    ledger = SimplePostgresDataset(db, store, (decoder,), owner_run=RUN)
    request = worker.report().shards[0].receipts[0].request
    layout = layout_for(worker, request)
    memory = MemoryMarketLedger(
        "offline", MemoryMaterialBytes(), (decoder,), lambda: KNOWN
    )
    original = retain_page(worker, request, layout, memory)
    frozen = []

    class Writer:
        def retain(self, material, raw, *, level):
            assert not wire.calls
            assert (
                replace(material, locator=original.material.locator)
                == original.material
            )
            assert material.locator == simple_intake(store, level=level).locator(
                material, level
            )
            frozen.append(ledger.material_intent(material, level=level))
            return ledger.retain(material, raw, level=level)

    physical = retain_page(
        worker, request, layout, Writer(), physical=simple_intake(store, level=level)
    )
    assert frozen == [(physical.material, level)]
    assert physical.physical_level == level
    assert physical.commit.payload_digest == content_id(frozen[0])
    assert verify_original(ledger, frozen[0]) == Verification.COMMITTED
    assert verify_original(ledger, physical.material) == Verification.CONFLICT
    assert physical.material.key == original.material.key
    assert len(wire.objects) == 1


@pytest.mark.parametrize("level", [None, "", "derived", 1, True])
def test_invalid_explicit_physical_binding_has_no_effect(level):
    calls = []
    with pytest.raises(ContractError, match="material_physical_binding_required"):
        PhysicalIntake(level, lambda *_: calls.append("locator"))
    assert calls == []


def test_actual_job_pending_original_recovery_keeps_prewrite_identity(tmp_path):
    worker, decoder = run_job(tmp_path, RUN)
    db, wire = SimpleSQL(), Wire()
    store = store_for(wire)
    ledger = SimplePostgresDataset(db, store, (decoder,), owner_run=RUN)
    request = worker.report().shards[0].receipts[0].request
    captured = []

    class FailureWriter:
        def retain(self, material, raw, *, level):
            captured.append((material, level))
            return ledger.retain(material, raw, level=level, fault=Fault.BEFORE_COMMIT)

    with pytest.raises(InjectedFailure):
        retain_page(
            worker,
            request,
            layout_for(worker, request),
            FailureWriter(),
            physical=simple_intake(store, level="raw"),
        )
    material, level = captured[0]
    assert ledger.pending_inventory()[0][3] == "pending"
    assert verify_original(ledger, captured[0]) == Verification.ABSENT
    fresh = SimplePostgresDataset(db, store_for(wire), (decoder,), owner_run=RUN)
    fresh.recover(content_id(material.key))
    assert verify_original(fresh, captured[0]) == Verification.COMMITTED
    before = len(wire.calls)
    retained = retain_page(
        worker,
        request,
        layout_for(worker, request),
        fresh,
        physical=simple_intake(fresh.store, level=level),
    )
    assert retained.material == material
    assert fresh.pending_inventory()[0][3] == "complete"
    assert len(wire.calls) == before + 1
    assert len(worker.source.calls) == 2
    assert len(wire.objects) == 1


def test_complete_launcher_rehearsal_uses_job_and_sql_readback_without_network(
    tmp_path, monkeypatch
):
    """Exercise both CLI branches with SQLite; subprocess/PG/TLS remain live gates."""
    monkeypatch.chdir(tmp_path)
    db, wire = SimpleSQL(), Wire()
    db.closed = False
    db.close = lambda: setattr(db, "closed", True)
    original_execute = db.execute

    def execute(query, args=()):
        if query == "SET default_transaction_read_only=on":
            db.db.execute("PRAGMA query_only=ON")
            return Cursor([])
        return original_execute(query, args)

    db.execute = execute

    def connect(_):
        db.closed = False
        return db

    created = []
    monkeypatch.setattr(launcher, "candidate_revision", lambda: "offline-candidate")
    monkeypatch.setattr(launcher, "credentials", lambda: ("invented", "invented"))
    monkeypatch.setattr(launcher, "connect_dev", connect)
    monkeypatch.setattr(launcher, "uuid4", lambda: SimpleNamespace(hex=RUN[3:]))
    monkeypatch.setattr(
        launcher, "simple_preflight", lambda *_: dict(namespace_exists=bool(created))
    )
    monkeypatch.setattr(launcher, "migrate_simple", lambda *_: created.append(RUN))
    monkeypatch.setattr(
        launcher, "SimpleGarageMaterials", lambda *_a, **kw: store_for(wire, **kw)
    )
    child_results = []

    def child(command, origin):
        with monkeypatch.context() as patch:
            patch.setattr(sys, "argv", command[1:])
            child_results.append(launcher.main())
        assert child_results[-1] == 0

    monkeypatch.setattr(launcher, "recovery_child", child)
    receipt = tmp_path / "artifacts" / "rehearsal" / "receipt.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "launcher",
            "--opt-in-dev-write",
            "--receipt",
            str(receipt),
            "--expected-revision",
            "offline-candidate",
        ],
    )
    assert launcher.main() == 0, receipt.read_text()
    report = json.loads(receipt.read_text())
    cold = json.loads(receipt.with_name("cold-child.json").read_text())
    assert report["result"] == "LIMITED_SYNTHETIC_JOB_DEV_PASSED"
    assert cold["result"] == "COLD_PG_READBACK_PASSED"
    assert created == [RUN]
    assert report["pg_counts"]["materials"] == 2
    assert report["pg_counts"]["versions"] == 2
    assert report["pg_counts"]["publication_events"] == 2
    assert report["total_garage_requests"] <= 128
    assert report["garage_requests"] <= 96 and cold["garage_requests"] <= 32
    assert len(wire.objects) == 2
    assert all(method in ("GET", "PUT") for method, *_ in wire.calls)
    assert len(cold["cold_readback"]) == 2
    assert sum(len(v["coverage"]) for v in cold["cold_readback"]) == 9
    assert len(report["lineage"]) == 2
    assert not any(e["outcome"] != "CONFIRMED" for e in report["steps"])
    assert all(word not in receipt.read_text() for word in ("invented", "password"))


def test_mainline_drift_stops_before_candidate_read_or_storage(monkeypatch):
    calls = []

    def output(command, **kwargs):
        calls.append(command)
        if command == ["git", "rev-parse", "HEAD"]:
            return launcher.BASELINE + "\n"
        if command == ["git", "rev-parse", "refs/heads/main"]:
            return "0" * 40 + "\n"
        raise AssertionError("drift_must_stop_candidate_scan")

    monkeypatch.setattr(launcher.subprocess, "check_output", output)
    with pytest.raises(ContractError, match="git_mainline_drift"):
        launcher.candidate_revision()
    assert len(calls) == 2
