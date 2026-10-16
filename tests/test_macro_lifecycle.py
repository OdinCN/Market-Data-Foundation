"""Two-family lifecycle contract, using synthetic original bytes and no network."""

import socket
import subprocess
import sys
from dataclasses import replace

import psycopg
import pytest
from test_bls_ppi_vertical import job_candidate, job_rig
from test_cpi_local_job import Rig, spec

from market_data_foundation.canonical.model import ContractError, digest
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.macro_job_journal import MacroJobJournal
from market_data_foundation.historical.macro_lifecycle import MeteredTransport
from market_data_foundation.historical.ppi_local_job import PpiJobSpec


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("deterministic Macro lifecycle attempted external IO")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


@pytest.fixture(params=["cpi", "ppi"])
def family(request, tmp_path):
    if request.param == "cpi":
        rig = Rig(tmp_path / "cpi")
        engine = rig.engine
        candidate = rig.acquirer.acquire("original", lambda: None)
        target = spec()
    else:
        engine, *_ = job_rig(tmp_path, "ppi")
        candidate = job_candidate("original", tmp_path / "original-artifacts")
        target = PpiJobSpec()
    target = replace(
        target,
        mode="retained",
        max_provider_requests=0,
        expected_raw_sha256=digest(candidate.evidence.raw_bytes),
    )
    return engine, target, candidate


def run(engine, identity, candidate):
    return engine.run(
        identity, enabled=True, preflight=lambda _: None, retained=candidate
    )


def test_retained_success_is_idempotent_and_has_zero_provider_intent(family):
    engine, target, candidate = family
    engine.submit("original", target)
    result = run(engine, "original", candidate)
    assert result["state"] == "succeeded"
    assert result["attempt_id"] == "original-a1"
    assert result["attempts"] == 1
    assert not any("provider_request" in key for key in result["effects"])
    assert run(engine, "original", None) == result
    assert engine.reconcile("original", enabled=True) == result
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        engine.repair("original", "second", enabled=True)


def test_unknown_reconcile_repair_preserves_attempt_and_original_identity(family):
    engine, target, candidate = family

    def lost_ack():
        raise RuntimeError("private message must never reach the journal")

    engine.commit = lost_ack
    engine.submit("original", target)
    result = run(engine, "original", candidate)
    assert result["state"] == "outcome_unknown"
    with pytest.raises(ContractError, match="repair_reconciliation_required"):
        engine.repair("original", "repair", enabled=True)
    observed = engine.reconcile("original", enabled=True)
    assert observed["state"] == "outcome_unknown"
    assert observed["reconciliation"]["outcome"] == "confirmed_not_published"
    history = engine.journal.history("original")
    with pytest.raises(ContractError, match="explicit_opt_in"):
        engine.repair("original", "repair")
    replacement = engine.repair("original", "repair", enabled=True)
    assert replacement == engine.repair("original", "repair", enabled=True)
    assert replacement["repair_of"] == "original"
    assert replacement["attempts"] == 0
    assert replacement["spec"]["max_provider_requests"] == 0
    with pytest.raises(ContractError, match="job_scope_already_registered"):
        engine.repair("original", "branch", enabled=True)
    engine.commit = lambda: None
    repaired = run(engine, "repair", candidate)
    assert repaired["state"] == "succeeded"
    assert repaired["attempt_id"] == "repair-a1"
    assert repaired["plan"] == observed["plan"]
    assert repaired["raw"]["sha256"] == observed["raw"]["sha256"]
    assert not any("provider_request" in key for key in repaired["effects"])
    assert engine.journal.get("original") == observed
    assert engine.journal.history("original") == history
    assert "private message" not in repr(engine.journal.history("original"))


def test_repair_rejects_other_plan_with_same_raw(family):
    engine, target, candidate = family
    engine.submit("original", target)
    journal = engine.journal
    # Cancellation after acquiring a valid plan is a safe no-storage repair point.
    original_candidate = engine._check_repair_candidate

    def cancel_after_plan(identity, plan):
        original_candidate(identity, plan)
        journal.cancel(identity, engine.clock())

    engine._check_repair_candidate = cancel_after_plan
    assert run(engine, "original", candidate)["state"] == "cancelled"
    engine._check_repair_candidate = original_candidate
    engine.repair("original", "repair", enabled=True)
    # A different identity cannot hijack the predecessor's retained acquisition.
    alternate = replace(
        candidate,
        plan=replace(
            candidate.plan, run=replace(candidate.plan.run, identity="different:run")
        ),
    )
    result = run(engine, "repair", alternate)
    assert result["state"] == "failed"
    assert not result["effects"].get("canonical_possible")


def test_both_families_share_journal_with_legacy_filename_and_read_only_restart(
    tmp_path,
):
    legacy = FileJobJournal(tmp_path / "legacy")
    assert isinstance(legacy, MacroJobJournal)
    assert legacy.path.name == "cpi-jobs.sqlite3"
    shared = MacroJobJournal(tmp_path / "shared")
    assert shared.path.name == "macro-jobs.sqlite3"
    before = sorted(tmp_path.rglob("*"))
    FileJobJournal(tmp_path / "absent", read_only=True)
    assert sorted(tmp_path.rglob("*")) == before
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from market_data_foundation.historical."
            "cpi_job_journal import FileJobJournal; import sys; "
            "assert FileJobJournal(Path(sys.argv[1]), "
            "read_only=True).list_jobs() == ()",
            str(legacy.directory),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_metered_transport_intent_precedes_effect_and_failure_is_observed():
    events = []

    class Delegate:
        def post(self, *args, **kwargs):
            events.append("effect")
            raise OSError("private response")

    transport = MeteredTransport(
        Delegate(),
        lambda: events.append("intent"),
        lambda received: events.append(received),
    )
    with pytest.raises(OSError):
        transport.post("unused", b"", {}, timeout=1, max_response_bytes=1)
    assert events == ["intent", "effect", False]
