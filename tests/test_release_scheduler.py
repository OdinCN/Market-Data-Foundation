"""T09 offline lifecycle evidence: concrete handlers, no private facilities."""

import socket
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
import test_fomc_statement_local as fomc
import test_fred_alfred_vintage_local as fred

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.fomc_statement_local_read import (
    read_local_statement,
)
from market_data_foundation.historical.release_handlers import (
    FOMC,
    VINTAGE,
    StatementReleaseHandler,
    VintageReleaseHandler,
)
from market_data_foundation.historical.release_scheduler import (
    ReleaseJournal,
    ReleaseOperator,
    ReleaseSchedule,
)
from market_data_foundation.historical.vintage_local_read import read_local_vintage

AT = datetime(2026, 10, 17, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_external_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("T09 deterministic suite attempted external IO")

    import psycopg

    from market_data_foundation.historical.bls_cpi_material_store import (
        GarageMaterialStore,
    )

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(GarageMaterialStore, "create_if_absent", forbidden)
    monkeypatch.setattr(GarageMaterialStore, "read_exact", forbidden)


def setup(tmp_path, family=FOMC, failure=None, **spec_changes):
    module = fomc if family == FOMC else fred
    job, _, ledger, store, protection, clock = module.job_bindings(
        tmp_path / "unused-job", publication_failure=failure
    )
    kind = StatementReleaseHandler if family == FOMC else VintageReleaseHandler
    handler = kind(
        tmp_path / family,
        clock,
        job.kernel,
        store,
        protection,
        job.readback,
        job.commit,
    )
    journal = ReleaseJournal(tmp_path / "operator")
    operator = ReleaseOperator(journal, {family: handler}, clock)
    candidate = module.candidate(module.Clock(AT + timedelta(seconds=1)))
    spec = ReleaseSchedule(
        "schedule",
        "owner",
        family,
        handler.reference,
        "owner-fixture-window-r1",
        AT,
        AT + timedelta(hours=1),
        candidate.document.official_published_at if family == FOMC else None,
    )
    spec = replace(spec, **spec_changes)
    clock.at = AT + timedelta(seconds=10)
    operator.register(spec, actor="owner", enabled=True)
    return operator, handler, candidate, ledger, store, protection, clock, spec


def trigger(operator, candidate, identity="slot", action="manual", schedule="schedule"):
    return operator.trigger(
        schedule, identity, candidate, actor="owner", action=action, enabled=True
    )


def run(operator, candidate, identity="slot"):
    return operator.run(identity, candidate, actor="owner", enabled=True)


def restart(operator, handler, clock):
    kind = type(handler)
    fresh = kind(
        handler.root,
        clock,
        handler.kernel,
        handler.store,
        handler.protection,
        handler.readback,
        handler.commit,
    )
    return ReleaseOperator(
        ReleaseJournal(operator.journal.directory), {handler.family: fresh}, clock
    )


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_two_real_handlers_run_and_exact_asof_read_without_effects(tmp_path, family):
    op, handler, candidate, ledger, store, protection, clock, spec = setup(
        tmp_path, family
    )
    pending = trigger(op, candidate)
    assert pending["state"] == "pending" and not store.creates
    assert pending["budget"]["provider_requests"] == 0
    done = run(op, candidate)
    assert done["state"] == "succeeded", done
    success = done["last_success"]
    assert success["coverage"]
    assert success["known_at"] <= success["usable_at"]
    assert success["acquired_at"] < success["known_at"]
    if family == FOMC:
        assert (
            success["source_release_at"]
            == candidate.document.official_published_at.isoformat()
        )
        reader = read_local_statement
        assert success["time_semantics"] == "official_document_release"
    else:
        assert success["source_release_at"] is None
        assert success["selected_vintages"]["GDP"] == list(fred.VINTAGES)
        assert success["unavailable_members"]
        assert "PAYEMS|2026-08-26|2026-09-01" not in success["coverage"]
        reader = read_local_vintage
    counts = (len(store.creates), len(store.reads), ledger.publication_calls)
    history = op.journal.audit()
    exact = reader(ledger, as_of=clock(), version_identity=success["version_identity"])
    assert exact == reader(ledger, as_of=clock())
    ro = ReleaseJournal(op.journal.directory, read_only=True)
    read_operator = ReleaseOperator(ro, {}, clock)
    report = read_operator.status("schedule", at=clock())
    assert report["freshness"] == "fresh"
    assert report["unknown_slots"] == []
    assert report["last_success"] == success
    assert counts == (len(store.creates), len(store.reads), ledger.publication_calls)
    assert history == op.journal.audit()
    assert trigger(op, candidate) == done
    assert run(op, candidate) == done
    assert counts == (len(store.creates), len(store.reads), ledger.publication_calls)
    restarted = restart(op, handler, clock)
    assert run(restarted, candidate) == done
    assert counts == (len(store.creates), len(store.reads), ledger.publication_calls)
    assert protection.all_released()
    engine = handler.engine("slot")
    job = engine.journal.get(done["attempt_jobs"][0])
    assert job["attempts"] == 1 and job["spec"]["max_provider_requests"] == 0
    assert not job["effects"].get("provider_requests_possible")


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_unknown_is_never_reexecuted_restart_reconcile_then_bounded_repair(
    tmp_path, family
):
    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family, "before"
    )
    trigger(op, candidate)
    unknown = run(op, candidate)
    assert unknown["state"] == "outcome_unknown"
    counts = (len(store.creates), len(store.reads), ledger.publication_calls)
    with pytest.raises(ContractError, match="reconciliation"):
        op.repair("slot", actor="owner", enabled=True)
    assert run(op, candidate) == unknown
    assert trigger(op, candidate) == unknown
    assert counts == (len(store.creates), len(store.reads), ledger.publication_calls)
    newer = (fomc if family == FOMC else fred).candidate(
        fred.Clock(AT + timedelta(seconds=20))
    )
    clock.at += timedelta(seconds=30)
    with pytest.raises(ContractError, match="unknown"):
        trigger(op, newer, "bypass")
    restarted = restart(op, handler, clock)
    unresolved = restarted.reconcile("slot", actor="owner", enabled=True)
    assert unresolved["reconciliation"]["outcome"] == "unresolved"
    assert len(store.creates) == counts[0] and ledger.publication_calls == counts[2]
    # Original in-process engine can establish synthetic ticket-release proof.
    observed = op.reconcile("slot", actor="owner", enabled=True)
    assert observed["state"] == "outcome_unknown"
    assert observed["reconciliation"]["outcome"] == "confirmed_not_published"
    assert protection.all_released()
    assert not op.status("schedule", at=clock())["unknown_slots"]
    assert len(store.creates) == counts[0] and ledger.publication_calls == counts[2]
    repaired = restarted.repair("slot", actor="owner", enabled=True)
    assert repaired["state"] == "pending" and len(repaired["attempt_jobs"]) == 2
    ledger.publication_failure = None
    done = run(restarted, candidate)
    assert done["state"] == "succeeded", done
    assert ledger.publication_calls == 2
    with pytest.raises(ContractError, match="budget_or_state"):
        restarted.repair("slot", actor="owner", enabled=True)
    engine = restarted.handlers[family].engine("slot")
    old, new = [engine.journal.get(i) for i in done["attempt_jobs"]]
    assert old["state"] == "outcome_unknown" and new["state"] == "succeeded"
    assert old["plan"] == new["plan"] and old["raw"] == new["raw"]


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_confirmed_published_unknown_is_success_evidence_without_retry(
    tmp_path, family
):
    op, handler, candidate, ledger, store, _, clock, _ = setup(
        tmp_path, family, "after"
    )
    trigger(op, candidate)
    assert run(op, candidate)["state"] == "outcome_unknown"
    creates, publications = len(store.creates), ledger.publication_calls
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["state"] == "outcome_unknown" and result["last_success"]
    report = op.status("schedule", at=clock())
    assert report["last_success"] and report["unknown_slots"] == []
    with pytest.raises(ContractError):
        op.repair("slot", actor="owner", enabled=True)
    assert run(restart(op, handler, clock), candidate) == result
    assert len(store.creates) == creates and ledger.publication_calls == publications


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_pause_resume_cancel_pending_and_bound_owner(tmp_path, family):
    op, _, candidate, ledger, store, _, clock, _ = setup(tmp_path, family)
    with pytest.raises(ContractError, match="owner"):
        op.pause("schedule", actor="stranger", enabled=True)
    with pytest.raises(ContractError, match="opt_in"):
        op.pause("schedule", actor="owner")
    op.pause("schedule", actor="owner", enabled=True)
    with pytest.raises(ContractError, match="paused"):
        trigger(op, candidate)
    op.resume("schedule", actor="owner", enabled=True)
    trigger(op, candidate)
    op.pause("schedule", actor="owner", enabled=True)
    with pytest.raises(ContractError, match="paused"):
        run(op, candidate)
    op.cancel("slot", actor="owner", enabled=True)
    op.resume("schedule", actor="owner", enabled=True)
    assert run(op, candidate)["state"] == "cancelled"
    assert not store.creates and ledger.publication_calls == 0
    assert any(e["action"] == "cancel" for e in op.journal.audit())
    assert op.status("schedule", at=clock())["freshness"] == "unknown"


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_refresh_backfill_and_freshness_are_explicit_and_finite(tmp_path, family):
    op, handler, candidate, ledger, store, _, clock, spec = setup(tmp_path, family)
    trigger(op, candidate)
    assert run(op, candidate)["state"] == "succeeded"
    with pytest.raises(ContractError, match="already_claimed"):
        trigger(op, candidate, "duplicate", action="refresh")
    newer = (fomc if family == FOMC else fred).candidate(
        fred.Clock(AT + timedelta(seconds=20))
    )
    clock.at = AT + timedelta(seconds=30)
    trigger(op, newer, "refresh", action="refresh")
    assert run(op, newer, "refresh")["state"] == "succeeded"
    third = (fomc if family == FOMC else fred).candidate(
        fred.Clock(AT + timedelta(seconds=40))
    )
    clock.at = AT + timedelta(seconds=50)
    with pytest.raises(ContractError, match="slot_budget"):
        trigger(op, third, "exhausted")
    old_schedule = replace(spec, identity="backfill-window", max_slots=1)
    op.register(old_schedule, actor="owner", enabled=True)
    clock.at = AT + timedelta(days=2)
    with pytest.raises(ContractError, match="outside_window"):
        trigger(op, third, "old", schedule="backfill-window")
    trigger(op, third, "old", action="backfill", schedule="backfill-window")
    backfilled = run(op, third, "old")
    assert backfilled["state"] == "succeeded"
    assert datetime.fromisoformat(
        backfilled["last_success"]["known_at"]
    ) > AT + timedelta(days=2)
    report = op.status("backfill-window", at=clock())
    assert report["freshness"] == "stale" and "stale_acquisition" in report["alerts"]
    counts = (len(store.creates), len(store.reads), ledger.publication_calls)
    op.resume("backfill-window", actor="owner", enabled=True)
    assert counts == (len(store.creates), len(store.reads), ledger.publication_calls)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_unknown_schedule_window_never_executes(tmp_path, family):
    op, _, candidate, ledger, store, _, clock, _ = setup(
        tmp_path, family, not_before=None, deadline=None, source_release_at=None
    )
    with pytest.raises(ContractError, match="time_unknown"):
        trigger(op, candidate)
    report = op.status("schedule", at=clock())
    assert report["schedule_time_unknown"]
    assert "schedule_time_unknown" in report["alerts"]
    assert not report["slots"] and not store.creates and ledger.publication_calls == 0


def test_release_evidence_cannot_be_invented_or_vintage_promoted(tmp_path):
    op, _, candidate, ledger, store, _, clock, spec = setup(
        tmp_path, source_release_at=datetime(2026, 9, 17, tzinfo=UTC)
    )
    with pytest.raises(ContractError, match="release_evidence_mismatch"):
        trigger(op, candidate)
    with pytest.raises(ContractError, match="vintage_is_not_official_release"):
        replace(spec, family=VINTAGE, reference=VintageReleaseHandler.reference)
    assert not store.creates and not ledger.publication_calls
    no_release = replace(spec, identity="unknown-release", source_release_at=None)
    op.register(no_release, actor="owner", enabled=True)
    assert op.status("unknown-release", at=clock())["schedule_time_unknown"]
    with pytest.raises(ContractError):
        trigger(op, candidate, schedule="unknown-release")


@pytest.mark.parametrize("phase", ["before_job", "after_publication"])
def test_crash_claim_never_replays_even_if_engine_pending_or_succeeded(
    tmp_path, monkeypatch, phase
):
    op, handler, candidate, ledger, store, _, clock, _ = setup(tmp_path)
    trigger(op, candidate)
    if phase == "before_job":
        original = handler.engine

        def crash(*args, **kwargs):
            raise SystemExit("process stopped after durable claim")

        monkeypatch.setattr(handler, "engine", crash)
    else:

        def crash(*args, **kwargs):
            raise SystemExit("process stopped after canonical success")

        monkeypatch.setattr(op, "_finish", crash)
    with pytest.raises(SystemExit):
        run(op, candidate)
    assert op.journal.slot("slot")["state"] == "claimed"
    counts = (len(store.creates), ledger.publication_calls)
    if phase == "before_job":
        monkeypatch.setattr(handler, "engine", original)
    fresh = restart(op, handler, clock)
    assert run(fresh, candidate)["state"] == "claimed"
    result = fresh.reconcile("slot", actor="owner", enabled=True)
    assert result["state"] == (
        "outcome_unknown" if phase == "before_job" else "succeeded"
    )
    assert counts == (len(store.creates), ledger.publication_calls)
    if phase == "after_publication":
        assert result["last_success"]
    else:
        assert result["classification"] == "reconciliation_unresolved"


def test_read_only_missing_journal_creates_nothing(tmp_path):
    directory = tmp_path / "does-not-exist"
    journal = ReleaseJournal(directory, read_only=True)
    assert not directory.exists()
    with pytest.raises(Exception):
        journal.schedules()
    assert not directory.exists()


def test_capability_inventory_and_binding_inputs_fail_closed(tmp_path):
    op, handler, _, _, _, _, _, spec = setup(tmp_path)
    caps = op.capabilities()
    assert caps[FOMC]["supported"] and not caps[VINTAGE]["supported"]
    assert all(
        not caps[f]["supported"] for f in ["bea-pce", "bls-employment", "ecb-sdmx"]
    )
    with pytest.raises(ContractError, match="family_unsupported"):
        replace(spec, family="ecb-sdmx")
    with pytest.raises(ContractError, match="reference_unsupported"):
        replace(spec, reference="2027-01")
    with pytest.raises(ContractError, match="concrete_handler"):
        ReleaseOperator(op.journal, {VINTAGE: handler}, op.clock)
    with pytest.raises(ContractError, match="identity_reused"):
        op.register(replace(spec, evidence_id="changed"), actor="owner", enabled=True)


def test_candidate_identity_change_rejected_before_execution(tmp_path):
    op, _, candidate, ledger, store, _, _, _ = setup(tmp_path)
    trigger(op, candidate)
    newer = fomc.candidate(fomc.Clock(AT + timedelta(seconds=2)))
    with pytest.raises(ContractError, match="candidate_mismatch"):
        run(op, newer)
    with pytest.raises(ContractError, match="identity_reused"):
        trigger(op, newer)
    assert not store.creates and not ledger.publication_calls


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_crash_after_underlying_submit_never_requeues_pending_job(
    tmp_path, monkeypatch, family
):
    op, handler, candidate, ledger, store, _, clock, _ = setup(tmp_path, family)
    trigger(op, candidate)
    engine = handler.engine("slot", create=True)

    def crash(*args, **kwargs):
        raise SystemExit("pending source job; scheduler claim already consumed")

    monkeypatch.setattr(engine, "run", crash)
    with pytest.raises(SystemExit):
        run(op, candidate)
    fresh = restart(op, handler, clock)
    result = fresh.reconcile("slot", actor="owner", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert run(fresh, candidate) == result
    assert not store.creates and ledger.publication_calls == 0
    source_job = (
        fresh.handlers[family].engine("slot").journal.get(result["attempt_jobs"][0])
    )
    assert source_job["state"] == "pending" and source_job["attempts"] == 0


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_pre_registered_slot_blocked_after_other_slot_becomes_unknown(tmp_path, family):
    op, _, candidate, ledger, store, _, clock, _ = setup(tmp_path, family, "before")
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at = AT + timedelta(seconds=30)
    trigger(op, candidate)
    trigger(op, newer, "second")
    assert run(op, candidate)["state"] == "outcome_unknown"
    counts = len(store.creates), len(store.reads), ledger.publication_calls
    with pytest.raises(ContractError, match="unknown_requires_reconcile"):
        run(op, newer, "second")
    assert counts == (len(store.creates), len(store.reads), ledger.publication_calls)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_duplicate_evidence_cross_schedule_and_moved_paths_are_rejected(
    tmp_path, family
):
    op, _, candidate, ledger, store, _, _, spec = setup(tmp_path, family)
    trigger(op, candidate)
    alternate = replace(spec, identity="another-schedule")
    op.register(alternate, actor="owner", enabled=True)
    moved_reads = tuple(
        replace(
            r,
            paths=replace(
                r.paths, raw="artifacts/moved/raw", request="artifacts/moved/request"
            ),
        )
        for r in candidate.reads
    )
    moved = replace(candidate, reads=moved_reads)
    with pytest.raises(ContractError, match="already_claimed"):
        trigger(op, moved, "another-slot", schedule="another-schedule")
    assert not store.creates and not ledger.publication_calls


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_cancel_races_source_registration_without_losing_consumed_claim(
    tmp_path, monkeypatch, family
):
    op, handler, candidate, ledger, store, _, _, _ = setup(tmp_path, family)
    trigger(op, candidate)
    engine = handler.engine("slot", create=True)
    submit = engine.submit

    def cancellation_before_submit(*args):
        op.cancel("slot", actor="owner", enabled=True)
        return submit(*args)

    monkeypatch.setattr(engine, "submit", cancellation_before_submit)
    result = run(op, candidate)
    assert result["state"] == "cancelled" and result["cancel_requested"]
    assert not store.creates and ledger.publication_calls == 0
    assert run(op, candidate) == result


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_expired_window_requires_explicit_backfill_at_execution(tmp_path, family):
    op, _, candidate, ledger, store, _, clock, _ = setup(tmp_path, family)
    trigger(op, candidate, action="scheduled")
    clock.at = AT + timedelta(hours=2)
    with pytest.raises(ContractError, match="outside_window"):
        run(op, candidate)
    report = op.status("schedule", at=clock())
    assert "coverage_missing_after_deadline" in report["alerts"]
    assert not ledger.publication_calls and not store.creates


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_retained_repair_budget_one_cannot_be_refilled(tmp_path, family):
    op, _, candidate, _, _, _, _, spec = setup(
        tmp_path, family, "before", max_attempts_per_slot=1
    )
    trigger(op, candidate)
    run(op, candidate)
    op.reconcile("slot", actor="owner", enabled=True)
    with pytest.raises(ContractError, match="budget_or_state"):
        op.repair("slot", actor="owner", enabled=True)
    with pytest.raises(ContractError, match="identity_reused"):
        op.register(replace(spec, max_attempts_per_slot=2), actor="owner", enabled=True)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_older_acquisition_backfill_is_explicitly_unsupported(tmp_path, family):
    op, _, candidate, ledger, store, _, clock, _ = setup(tmp_path, family)
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at = AT + timedelta(seconds=30)
    trigger(op, candidate, "old", action="backfill")
    trigger(op, newer, "new")
    assert run(op, newer, "new")["state"] == "succeeded"
    counts = len(store.creates), ledger.publication_calls
    with pytest.raises(ContractError, match="older_acquisition_backfill_unsupported"):
        run(op, candidate, "old")
    assert counts == (len(store.creates), ledger.publication_calls)


def test_handler_binding_drift_is_rejected_before_any_effect(tmp_path):
    op, handler, candidate, ledger, store, _, clock, _ = setup(tmp_path)
    trigger(op, candidate)
    handler.root = tmp_path / "new-empty-root"
    with pytest.raises(ContractError, match="binding_drift"):
        run(op, candidate)
    assert not store.creates and not ledger.publication_calls
    assert not handler.root.exists()
    assert op.status("schedule", at=clock())["slots"][0]["state"] == "pending"


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_slots", 0),
        ("max_slots", 17),
        ("max_slots", True),
        ("max_attempts_per_slot", 3),
        ("max_attempts_per_slot", True),
        ("freshness_seconds", -1),
        ("not_before", None),
        ("owner", "../escape"),
        ("evidence_id", ""),
    ],
)
def test_bad_schedule_inputs_are_rejected(tmp_path, field, value):
    _, _, _, _, _, _, _, spec = setup(tmp_path)
    with pytest.raises(ContractError):
        replace(spec, **{field: value})


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
@pytest.mark.parametrize("snapshot_phase", ["before_run", "before_publication"])
def test_stale_absence_cannot_clear_unknown_or_duplicate_effects(
    tmp_path, monkeypatch, family, snapshot_phase
):
    from contextlib import contextmanager

    from market_data_foundation.canonical.repository import InMemoryLedger

    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family, "after"
    )
    stale = InMemoryLedger.snapshot(ledger)
    publish = ledger.publish

    def capture(request, at):
        nonlocal stale
        if snapshot_phase == "before_publication":
            stale = InMemoryLedger.snapshot(ledger)
        return publish(request, at)

    monkeypatch.setattr(ledger, "publish", capture)
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at += timedelta(seconds=30)
    trigger(op, candidate)
    trigger(op, newer, "next")
    assert run(op, candidate)["state"] == "outcome_unknown"
    counts = len(store.creates), ledger.publication_calls
    original = handler.readback

    @contextmanager
    def stale_reader():
        yield InMemoryLedger.from_snapshot(stale)

    handler.readback = stale_reader
    for _ in range(2):
        result = op.reconcile("slot", actor="owner", enabled=True)
        assert result["reconciliation"]["outcome"] == "unresolved"
        assert op.status("schedule", at=clock())["unknown_slots"] == ["slot"]
        with pytest.raises(ContractError):
            op.repair("slot", actor="owner", enabled=True)
        with pytest.raises(ContractError, match="unknown_requires_reconcile"):
            run(op, newer, "next")
        assert counts == (len(store.creates), ledger.publication_calls)
    # The original writer tickets were not released on stale absence evidence.
    assert not protection.all_released()
    handler.readback = original
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert result["reconciliation"]["scheduler_evidence"]["binding_sha256"]
    assert protection.all_released()
    with pytest.raises(ContractError):
        op.repair("slot", actor="owner", enabled=True)
    assert counts == (len(store.creates), ledger.publication_calls)
    assert counts == (handler.raw_budget, 1)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_reader_methods_cannot_invent_absence(tmp_path, family):
    import copy
    from contextlib import contextmanager

    op, handler, candidate, ledger, store, _, _, _ = setup(tmp_path, family, "after")
    trigger(op, candidate)
    run(op, candidate)

    @contextmanager
    def lying_reader():
        reader = copy.deepcopy(ledger)
        reader.publication_by_occurrence = lambda _: None
        reader.snapshot = lambda: "claimed fresh"
        yield reader

    handler.readback = lying_reader
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert (len(store.creates), ledger.publication_calls) == (handler.raw_budget, 1)
    with pytest.raises(ContractError):
        op.repair("slot", actor="owner", enabled=True)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
@pytest.mark.parametrize("phase", ["successful_readback", "reconcile_readback"])
def test_readback_release_failure_restart_repeat_and_recovery(
    tmp_path, monkeypatch, family, phase
):
    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family, "after" if phase == "reconcile_readback" else None
    )
    acquire = protection.acquire
    failed = []

    def acquire_with_failure(identity):
        ticket = acquire(identity)
        ticket.release_calls = 0
        release = ticket.release
        index = len(protection.tickets)

        def fail_twice(other):
            ticket.release_calls += 1
            if index == 2 * handler.raw_budget and ticket.release_calls <= 2:
                failed.append(ticket)
                raise OSError("offline release fault")
            release(other)

        ticket.release = fail_twice
        return ticket

    monkeypatch.setattr(protection, "acquire", acquire_with_failure)
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at += timedelta(seconds=30)
    trigger(op, candidate)
    trigger(op, newer, "next")
    result = run(op, candidate)
    if phase == "reconcile_readback":
        result = op.reconcile("slot", actor="owner", enabled=True)
    job_id = result["attempt_jobs"][0]
    engine = handler.engine("slot")
    source = engine.journal.get(job_id)
    assert source["state"] == "outcome_unknown"
    assert source["reconciliation"]["readback_protection_pending"] is True
    assert job_id in engine._readback_release_handles
    assert len(failed) == 1 and failed[0].held
    assert sum(t.held for t in protection.tickets) == 1
    readback_tickets = tuple(protection.tickets[handler.raw_budget :])
    counts = len(store.creates), ledger.publication_calls
    assert counts == (handler.raw_budget, 1)
    for _ in range(2):
        fresh = restart(op, handler, clock)
        unresolved = fresh.reconcile("slot", actor="owner", enabled=True)
        assert unresolved["reconciliation"]["outcome"] == "unresolved"
        assert fresh.status("schedule", at=clock())["unknown_slots"] == ["slot"]
        assert failed[0].held
        assert len(protection.tickets) == 2 * handler.raw_budget
        with pytest.raises(ContractError):
            fresh.repair("slot", actor="owner", enabled=True)
        with pytest.raises(ContractError, match="unknown_requires_reconcile"):
            run(fresh, newer, "next")
    # The original process can retry its retained handle; another failure must
    # preserve it, and must not reacquire new tickets or report a resolved slot.
    unresolved = op.reconcile("slot", actor="owner", enabled=True)
    assert unresolved["reconciliation"]["outcome"] == "unresolved"
    assert failed[0].held and len(failed) == 2
    assert len(protection.tickets) == 2 * handler.raw_budget
    resolved = op.reconcile("slot", actor="owner", enabled=True)
    assert resolved["reconciliation"]["outcome"] == "confirmed_published"
    assert resolved["state"] == resolved["job_state"] == "outcome_unknown"
    assert resolved["last_success"] and protection.all_released()
    assert op.status("schedule", at=clock())["unknown_slots"] == []
    assert [t.release_calls for t in readback_tickets] == [1] * (
        handler.raw_budget - 1
    ) + [3]
    again = op.reconcile("slot", actor="owner", enabled=True)
    assert again["reconciliation"]["outcome"] == "confirmed_published"
    assert protection.all_released()
    assert counts == (len(store.creates), ledger.publication_calls)
    ledger.publication_failure = None
    assert run(op, newer, "next")["state"] == "succeeded"


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
@pytest.mark.parametrize("action", ["manual", "scheduled", "backfill"])
@pytest.mark.parametrize("delay_at", ["after_claim", "after_candidate_validation"])
def test_deadline_is_effect_start_gate_with_explicit_backfill_exception(
    tmp_path, monkeypatch, family, action, delay_at
):
    op, handler, candidate, ledger, store, _, clock, spec = setup(tmp_path, family)
    trigger(op, candidate, action=action)
    engine = handler.engine("slot", create=True)
    if delay_at == "after_claim":
        original = engine.submit

        def delayed(*args, **kwargs):
            assert op.journal.slot("slot")["state"] == "claimed"
            result = original(*args, **kwargs)
            clock.at = spec.deadline + timedelta(seconds=1)
            return result

        monkeypatch.setattr(engine, "submit", delayed)
    else:
        original = engine._candidate

        def delayed(*args, **kwargs):
            result = original(*args, **kwargs)
            clock.at = spec.deadline + timedelta(seconds=1)
            return result

        monkeypatch.setattr(engine, "_candidate", delayed)
    result = run(op, candidate)
    if action == "backfill":
        assert result["state"] == "succeeded"
        assert (len(store.creates), ledger.publication_calls) == (handler.raw_budget, 1)
    else:
        assert result["state"] == "failed"
        assert result["attempt_observations"][result["attempt_jobs"][0]]["stage"] in {
            "preflight",
            "acquired",
        }
        assert not store.creates and ledger.publication_calls == 0
        assert not ledger._records
        assert run(op, candidate) == result


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_writer_changes_on_reader_exit_cannot_resolve_absence(tmp_path, family):
    from contextlib import contextmanager

    from market_data_foundation.canonical.repository import InMemoryLedger

    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family, "before"
    )
    trigger(op, candidate)
    run(op, candidate)
    engine = handler.engine("slot")
    handle = next(iter(engine._unknown_handles.values()))
    original = handler.readback
    changed = False

    @contextmanager
    def racing_readback():
        nonlocal changed
        with original() as reader:
            yield reader
        if not changed:
            changed = True
            # Simulate an authoritative publication becoming visible at exit.
            InMemoryLedger.publish(ledger, handle.plan.publication_request, clock)

    handler.readback = racing_readback
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert op.status("schedule", at=clock())["unknown_slots"] == ["slot"]
    with pytest.raises(ContractError):
        op.repair("slot", actor="owner", enabled=True)
    handler.readback = original
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert protection.all_released()
    assert (len(store.creates), ledger.publication_calls) == (handler.raw_budget, 1)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_repair_rechecks_absence_at_registration_and_effect_start(tmp_path, family):
    from market_data_foundation.canonical.repository import InMemoryLedger

    op, handler, candidate, ledger, store, _, clock, _ = setup(
        tmp_path, family, "before"
    )
    trigger(op, candidate)
    run(op, candidate)
    engine = handler.engine("slot")
    plan = next(iter(engine._unknown_handles.values())).plan
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    repaired = op.repair("slot", actor="owner", enabled=True)
    assert repaired["state"] == "pending"
    InMemoryLedger.publish(ledger, plan.publication_request, clock)
    counts = len(store.creates), ledger.publication_calls
    assert run(op, candidate)["state"] == "failed"
    assert counts == (len(store.creates), ledger.publication_calls)
    with pytest.raises(ContractError):
        op.repair("slot", actor="owner", enabled=True)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
@pytest.mark.parametrize("change", ["publication", "release_failure"])
def test_repair_registration_invalidates_previous_resolution_before_fresh_check(
    tmp_path, monkeypatch, family, change
):
    from market_data_foundation.canonical.repository import InMemoryLedger

    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family, "before"
    )
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at += timedelta(seconds=30)
    trigger(op, candidate)
    trigger(op, newer, "next")
    run(op, candidate)
    engine = handler.engine("slot")
    plan = next(iter(engine._unknown_handles.values())).plan
    op.reconcile("slot", actor="owner", enabled=True)
    if change == "publication":
        InMemoryLedger.publish(ledger, plan.publication_request, clock)
    else:
        acquire = protection.acquire

        def acquire_bad_ticket(identity):
            ticket = acquire(identity)

            def fail_release(_):
                raise OSError("offline release failure")

            ticket.release = fail_release
            return ticket

        monkeypatch.setattr(protection, "acquire", acquire_bad_ticket)
    counts = len(store.creates), ledger.publication_calls
    with pytest.raises(ContractError, match="fresh_absence"):
        op.repair("slot", actor="owner", enabled=True)
    assert op.status("schedule", at=clock())["unknown_slots"] == ["slot"]
    with pytest.raises(ContractError, match="unknown_requires_reconcile"):
        run(op, newer, "next")
    assert counts == (len(store.creates), ledger.publication_calls)
    assert len(op.journal.slot("slot")["attempt_jobs"]) == 1


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
@pytest.mark.parametrize("field", ["attempt_id", "raw", "plan"])
def test_recovery_certificate_rejects_changed_attempt_plan_or_raw(
    tmp_path, family, field
):
    import copy

    op, handler, candidate, ledger, store, _, _, _ = setup(tmp_path, family, "before")
    trigger(op, candidate)
    run(op, candidate)
    slot = op.reconcile("slot", actor="owner", enabled=True)
    document = handler.engine("slot").journal.get(slot["attempt_jobs"][0])
    altered = copy.deepcopy(document)
    if field == "attempt_id":
        altered[field] = "another-attempt"
    elif field == "raw":
        altered[field][0]["sha256"] = "0" * 64
    else:
        altered[field] = None
    counts = len(store.creates), ledger.publication_calls
    with pytest.raises((ContractError, TypeError)):
        handler.recovery_evidence(slot, altered)
    assert counts == (len(store.creates), ledger.publication_calls)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_legacy_readback_without_release_proof_is_quarantined(tmp_path, family):
    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family
    )
    trigger(op, candidate)
    engine = handler.engine("slot", create=True)
    acquire = protection.acquire

    def legacy_failure(identity):
        ticket = acquire(identity)
        if len(protection.tickets) == 2 * handler.raw_budget:

            def fail(_):
                raise OSError("legacy release failure")

            ticket.release = fail
        return ticket

    protection.acquire = legacy_failure
    result = run(op, candidate)
    job_id = result["attempt_jobs"][0]
    # Model an R1 journal, which had no durable marker and lost the handle.
    engine.journal._update(job_id, clock(), reconciliation=None)
    fresh = restart(op, handler, clock)
    counts = len(store.creates), ledger.publication_calls
    result = fresh.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert fresh.status("schedule", at=clock())["unknown_slots"] == ["slot"]
    assert sum(t.held for t in protection.tickets) == 1
    assert (
        fresh.reconcile("slot", actor="owner", enabled=True)["reconciliation"][
            "outcome"
        ]
        == "unresolved"
    )
    with pytest.raises(ContractError):
        fresh.repair("slot", actor="owner", enabled=True)
    assert counts == (len(store.creates), ledger.publication_calls)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_repeat_reconcile_blocks_next_effect_while_holding_readback_tickets(
    tmp_path, monkeypatch, family
):
    op, handler, candidate, ledger, store, protection, clock, _ = setup(
        tmp_path, family, "before"
    )
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at += timedelta(seconds=30)
    trigger(op, candidate)
    trigger(op, newer, "next")
    run(op, candidate)
    resolved = op.reconcile("slot", actor="owner", enabled=True)
    assert resolved["reconciliation"]["outcome"] == "confirmed_not_published"
    acquire = protection.acquire
    raced = []

    def race_next_effect(identity):
        ticket = acquire(identity)
        assert ticket.held
        with pytest.raises(ContractError, match="unknown_requires_reconcile"):
            run(op, newer, "next")
        raced.append(True)
        return ticket

    monkeypatch.setattr(protection, "acquire", race_next_effect)
    counts = len(store.creates), ledger.publication_calls
    result = op.reconcile("slot", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert len(raced) == handler.raw_budget and protection.all_released()
    assert counts == (len(store.creates), ledger.publication_calls)
