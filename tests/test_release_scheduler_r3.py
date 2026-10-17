"""R3 direct-source and cross-schedule counterexamples; strictly offline."""

import copy
import multiprocessing
import shutil
import traceback
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
import test_fomc_statement_local as fomc
import test_fred_alfred_vintage_local as fred
from test_release_scheduler import AT, restart, run, setup, trigger
from test_release_scheduler import no_external_effects as no_external_effects

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import local_job_recovery as recovery
from market_data_foundation.historical.release_handlers import FOMC, VINTAGE

pytestmark = pytest.mark.usefixtures("no_external_effects")


def _fork_observe_after(parent_action, child_observation):
    """Fork before the parent effect, then inspect the inherited stale objects."""
    context = multiprocessing.get_context("fork")
    result_in, result_out = context.Pipe(duplex=False)
    proceed_in, proceed_out = context.Pipe(duplex=False)

    def child():
        result_in.close()
        proceed_out.close()
        try:
            result_out.send("forked")
            assert proceed_in.poll(15), "parent effect timed out"
            assert proceed_in.recv() == "done"
            child_observation()
            result_out.send(None)
        except BaseException:
            result_out.send(traceback.format_exc())
        finally:
            result_out.close()
            proceed_in.close()

    process = context.Process(target=child)
    process.start()
    result_out.close()
    proceed_in.close()
    try:
        assert result_in.poll(15), "fork child did not start"
        assert result_in.recv() == "forked"
        parent_action()
        proceed_out.send("done")
        assert result_in.poll(15), "fork child observation timed out"
        assert result_in.recv() is None
    finally:
        process.join(2)
        if process.is_alive():
            process.terminate()
            process.join(2)
        result_in.close()
        proceed_out.close()
    assert process.exitcode == 0


def _standalone(job):
    return type(job)(
        type(job.journal)(job.journal.directory),
        job.clock,
        job.kernel,
        job.store,
        job.protection,
        job.readback,
        job.commit,
    )


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
@pytest.mark.parametrize("check", ["absence", "registration", "pending_repair"])
def test_fork_stale_writer_cannot_certify_absence_or_execute_registered_repair(
    tmp_path, module, budget, check
):
    job, journal, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="before"
    )
    retained = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in retained.reads),
        ),
    )
    assert (
        job.run("original", enabled=True, preflight=lambda _: None, retained=retained)[
            "state"
        ]
        == "outcome_unknown"
    )
    plan = next(iter(job._unknown_handles.values())).plan
    assert job.reconcile("original", enabled=True)["reconciliation"]["outcome"] == (
        "confirmed_not_published"
    )
    job.repair("original", "repair", enabled=True)
    document, history = journal.get("original"), journal.history("original")
    inherited_registry = recovery._WRITER_AUTHORITIES.copy()
    # Independent disk copy preserves the legally registered pending repair.
    child_directory = tmp_path / "child-journal"
    shutil.copytree(journal.directory, child_directory)

    def parent_publish():
        writer.publication_failure = None
        assert (
            job.run(
                "repair", enabled=True, preflight=lambda _: None, retained=retained
            )["state"]
            == "succeeded"
        )
        assert writer.publication_by_occurrence(plan.publication_request.occurrence)
        assert len(writer.publications_for_version(plan.version.key)) == 1
        assert journal.get("original") == document
        assert journal.history("original") == history
        assert protection.all_released()

    def child_observe():
        # Even a copied registry reinserted with identical ids/weakrefs and
        # tokens cannot replace proof of the original live process.
        recovery._WRITER_AUTHORITIES.update(inherited_registry)
        assert (
            writer.publication_by_occurrence(plan.publication_request.occurrence)
            is None
        )
        assert journal.get("original") == document
        fresh = _standalone(job)
        counts = len(store.creates), len(store.reads), writer.publication_calls
        if check == "absence":
            observed = fresh.reconcile("original", enabled=True)
            assert observed["reconciliation"]["outcome"] == "unresolved"
            assert "source_recovery" not in observed["reconciliation"]
            assert observed["last_success"] is None
            with pytest.raises(ContractError):
                fresh.repair("original", "child-repair", enabled=True)
            assert len(journal.list_jobs()) == 2
        elif check == "registration":
            with pytest.raises(ContractError):
                recovery.synthetic_writer_session(writer)
            # The copied certificate cannot register a new successor either.
            fresh.journal = type(journal)(child_directory)
            with pytest.raises(ContractError):
                fresh.repair("original", "child-repair", enabled=True)
        else:
            fresh.journal = type(journal)(child_directory)
            writer.publication_failure = None
            result = fresh.run(
                "repair", enabled=True, preflight=lambda _: None, retained=retained
            )
            assert result["state"] in {"failed", "outcome_unknown"}
        assert counts == (
            len(store.creates),
            len(store.reads),
            writer.publication_calls,
        )
        assert len(store.creates) == budget

    _fork_observe_after(parent_publish, child_observe)


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
@pytest.mark.parametrize("standalone", [False, True])
def test_fork_cannot_release_inherited_unknown_tickets(
    tmp_path, module, budget, standalone
):
    job, journal, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="before"
    )
    retained = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in retained.reads),
        ),
    )
    job.run("original", enabled=True, preflight=lambda _: None, retained=retained)
    plan = next(iter(job._unknown_handles.values())).plan
    tickets = tuple(protection.tickets)
    assert tickets and all(t.held for t in tickets)

    def parent_publish():
        InMemoryLedger.publish(writer, plan.publication_request, clock)
        assert writer.publication_by_occurrence(plan.publication_request.occurrence)

    def child_observe():
        observer = _standalone(job) if standalone else job
        counts = len(store.creates), len(store.reads), writer.publication_calls
        for _ in range(2):
            result = observer.reconcile("original", enabled=True)
            assert result["reconciliation"]["outcome"] == "unresolved"
            assert "source_recovery" not in result["reconciliation"]
            assert result["last_success"] is None
            assert all(t.held for t in tickets)
            assert tuple(protection.tickets) == tickets
            with pytest.raises(ContractError):
                observer.repair("original", "child-repair", enabled=True)
        assert counts == (
            len(store.creates),
            len(store.reads),
            writer.publication_calls,
        )
        assert len(store.creates) == budget

    _fork_observe_after(parent_publish, child_observe)
    # The real source owner still recovers with its original writer and handles.
    clock.at += timedelta(seconds=1)
    observed = job.reconcile("original", enabled=True)
    assert observed["reconciliation"]["outcome"] == "confirmed_published"
    assert protection.all_released()


@pytest.mark.parametrize("module", [fomc, fred])
@pytest.mark.parametrize("change", ["copied_writer", "lost_registry"])
def test_recreated_session_cannot_recover_original_attempt(
    tmp_path, monkeypatch, module, change
):
    job, journal, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="before"
    )
    retained = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in retained.reads),
        ),
    )
    job.run("original", enabled=True, preflight=lambda _: None, retained=retained)
    job.reconcile("original", enabled=True)
    job.repair("original", "repair", enabled=True)
    original_session = journal.get("original")["writer_session"]
    if change == "copied_writer":
        monkeypatch.setattr(job.kernel, "ledger", copy.deepcopy(writer))
    else:
        monkeypatch.setattr(recovery, "_WRITER_AUTHORITIES", {})
    session = recovery.synthetic_writer_session(job.kernel.ledger)
    assert session["authority"] != original_session["authority"]
    fresh = _standalone(job)
    counts = len(store.creates), len(store.reads), writer.publication_calls
    assert fresh.reconcile("original", enabled=True)["reconciliation"]["outcome"] == (
        "unresolved"
    )
    with pytest.raises(ContractError):
        fresh.repair("original", "new-repair", enabled=True)
    result = fresh.run(
        "repair", enabled=True, preflight=lambda _: None, retained=retained
    )
    assert result["state"] in {"failed", "outcome_unknown"}
    assert counts == (len(store.creates), len(store.reads), writer.publication_calls)
    assert protection.all_released()


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
@pytest.mark.parametrize("snapshot_at", ["before_run", "before_publication"])
def test_direct_source_stale_absence_never_releases_or_repairs(
    tmp_path, monkeypatch, module, budget, snapshot_at
):
    job, journal, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="after"
    )
    retained = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in retained.reads),
        ),
    )
    stale = copy.deepcopy(writer)
    publish = writer.publish

    def capture(request, publication_clock):
        nonlocal stale
        if snapshot_at == "before_publication":
            stale = copy.deepcopy(writer)
        return publish(request, publication_clock)

    monkeypatch.setattr(writer, "publish", capture)
    unknown = job.run(
        "original", enabled=True, preflight=lambda _: None, retained=retained
    )
    assert unknown["state"] == "outcome_unknown"
    original_tickets = tuple(protection.tickets)
    assert all(t.held for t in original_tickets)

    @contextmanager
    def old_snapshot():
        yield stale

    fresh = job.readback
    job.readback = old_snapshot
    for _ in range(2):
        result = job.reconcile("original", enabled=True)
        assert result["reconciliation"]["outcome"] == "unresolved"
        assert result["last_success"] is None
        assert all(t.held for t in original_tickets)
        with pytest.raises(ContractError):
            job.repair("original", "repair", enabled=True)
        assert len(journal.list_jobs()) == 1
        assert (len(store.creates), writer.publication_calls) == (budget, 1)
    job.readback = fresh
    assert job.reconcile("original", enabled=True)["reconciliation"]["outcome"] == (
        "confirmed_published"
    )
    assert protection.all_released()
    with pytest.raises(ContractError):
        job.repair("original", "repair", enabled=True)
    assert (len(store.creates), writer.publication_calls) == (budget, 1)


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
def test_direct_source_repair_rechecks_old_absence_certificate(
    tmp_path, module, budget
):
    job, journal, writer, store, _, clock = module.job_bindings(
        tmp_path, publication_failure="before"
    )
    retained = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in retained.reads),
        ),
    )
    job.run("original", enabled=True, preflight=lambda _: None, retained=retained)
    plan = next(iter(job._unknown_handles.values())).plan
    assert job.reconcile("original", enabled=True)["reconciliation"]["outcome"] == (
        "confirmed_not_published"
    )
    InMemoryLedger.publish(writer, plan.publication_request, clock)
    with pytest.raises(ContractError):
        job.repair("original", "repair", enabled=True)
    assert len(journal.list_jobs()) == 1
    assert (len(store.creates), writer.publication_calls) == (budget, 1)


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
@pytest.mark.parametrize("boundary", ["submit", "candidate"])
def test_claimed_b_cannot_write_after_a_reconcile_becomes_unknown(
    tmp_path, monkeypatch, family, boundary
):
    op, handler, candidate, writer, store, protection, clock, spec = setup(
        tmp_path, family, "before"
    )
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at += timedelta(seconds=30)
    op.register(replace(spec, identity="other-schedule"), actor="owner", enabled=True)
    trigger(op, candidate, "a")
    trigger(op, newer, "b", schedule="other-schedule")
    run(op, candidate, "a")
    assert (
        op.reconcile("a", actor="owner", enabled=True)["reconciliation"]["outcome"]
        == "confirmed_not_published"
    )
    writer.publication_failure = None
    engine = handler.engine("b", create=True)
    original = engine.submit if boundary == "submit" else engine._candidate
    acquire = protection.acquire
    held = []

    def failing_release(identity):
        ticket = acquire(identity)
        if not held:
            held.append(ticket)

            def fail(_):
                raise OSError("offline release failure")

            ticket.release = fail
        return ticket

    def interleave(*args, **kwargs):
        result = original(*args, **kwargs)
        assert op.journal.slot("b")["state"] == "claimed"
        monkeypatch.setattr(protection, "acquire", failing_release)
        try:
            a = op.reconcile("a", actor="owner", enabled=True)
        except ContractError as exc:
            # Once B holds family admission, A must be refused before acquiring
            # protection or invalidating its still valid observation.
            assert "busy" in str(exc)
            assert boundary == "candidate" and not held
        else:
            assert a["reconciliation"]["outcome"] == "unresolved"
            assert held[0].held
        monkeypatch.setattr(protection, "acquire", acquire)
        return result

    monkeypatch.setattr(
        engine, "submit" if boundary == "submit" else "_candidate", interleave
    )
    counts = len(store.creates), writer.publication_calls
    result = run(op, newer, "b")
    if held:
        assert result["state"] == "outcome_unknown"
        assert op.status("schedule", at=clock())["unknown_slots"] == ["a"]
        assert counts == (len(store.creates), writer.publication_calls)
    else:
        assert result["state"] == "succeeded"
        assert protection.all_released()
    assert run(op, newer, "b") == result


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
@pytest.mark.parametrize("change", ["clone_writer", "missing_session", "attempt"])
def test_direct_source_requires_original_writer_and_attempt(
    tmp_path, monkeypatch, module, budget, change
):
    job, journal, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="after"
    )
    job.submit("original", job.spec_type())
    acquirer = (
        module.DirectStatementAcquirer
        if module is fomc
        else module.DirectVintageAcquirer
    )(module.Source(), module.Retain(), clock)
    job.run("original", enabled=True, preflight=lambda _: None, acquirer=acquirer)
    get = journal.get
    if change == "clone_writer":
        job.kernel.ledger = copy.deepcopy(writer)
    else:

        def altered(identity):
            document = get(identity)
            if change == "missing_session":
                document["writer_session"] = None
            else:
                document["attempt_id"] = "another-attempt"
            return document

        monkeypatch.setattr(journal, "get", altered)
    result = job.reconcile("original", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert all(t.held for t in protection.tickets)
    with pytest.raises(ContractError):
        job.repair("original", "repair", enabled=True)
    assert (len(store.creates), writer.publication_calls) == (budget, 1)


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
def test_direct_source_registered_repair_cannot_use_later_stale_absence(
    tmp_path, module, budget
):
    job, _, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="before"
    )
    retained = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in retained.reads),
        ),
    )
    job.run("original", enabled=True, preflight=lambda _: None, retained=retained)
    plan = next(iter(job._unknown_handles.values())).plan
    job.reconcile("original", enabled=True)
    job.repair("original", "repair", enabled=True)
    stale = copy.deepcopy(writer)
    InMemoryLedger.publish(writer, plan.publication_request, clock)

    @contextmanager
    def old_snapshot():
        yield stale

    job.readback = old_snapshot
    result = job.run(
        "repair", enabled=True, preflight=lambda _: None, retained=retained
    )
    assert result["state"] in {"failed", "outcome_unknown"}
    assert (len(store.creates), writer.publication_calls) == (budget, 1)
    assert protection.all_released()
    assert (
        job.run("repair", enabled=True, preflight=lambda _: None, retained=retained)
        == result
    )


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_family_admission_excludes_independent_reconciler_through_effects(
    tmp_path, monkeypatch, family
):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    op, handler, candidate, writer, store, protection, clock, spec = setup(
        tmp_path, family, "before"
    )
    module = fomc if family == FOMC else fred
    newer = module.candidate(module.Clock(AT + timedelta(seconds=20)))
    clock.at += timedelta(seconds=30)
    op.register(replace(spec, identity="other-schedule"), actor="owner", enabled=True)
    trigger(op, candidate, "a")
    trigger(op, newer, "b", schedule="other-schedule")
    run(op, candidate, "a")
    op.reconcile("a", actor="owner", enabled=True)
    other = restart(op, handler, clock)
    writer.publication_failure = None
    entered, proceed = Event(), Event()
    create = store.create_if_absent

    def hold_effect(*args):
        entered.set()
        assert proceed.wait(10), "test interleaving timed out"
        assert other.journal.slot("a")["reconciliation"]["outcome"] == (
            "confirmed_not_published"
        )
        return create(*args)

    monkeypatch.setattr(store, "create_if_absent", hold_effect)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(run, op, newer, "b")
        try:
            assert entered.wait(10)
            tickets = tuple(protection.tickets)
            for action in (other.reconcile, other.repair):
                with pytest.raises(ContractError, match="busy"):
                    action("a", actor="owner", enabled=True)
                assert tuple(protection.tickets) == tickets
                assert other.journal.slot("a")["reconciliation"].get(
                    "scheduler_evidence"
                )
        finally:
            proceed.set()
        assert future.result(timeout=10)["state"] == "succeeded"
    assert protection.all_released()
    # Actual flock is released at normal exit, also to another Journal object.
    with other.journal.executor("family-" + family):
        pass


@pytest.mark.parametrize("family", [FOMC, VINTAGE])
def test_family_lock_exception_exit_and_no_stale_success(tmp_path, monkeypatch, family):
    op, handler, candidate, writer, store, protection, clock, spec = setup(
        tmp_path, family, "after"
    )
    trigger(op, candidate, "a")
    run(op, candidate, "a")
    assert op.reconcile("a", actor="owner", enabled=True)["last_success"]
    acquire = protection.acquire

    def bad_release(identity):
        ticket = acquire(identity)

        def fail(_):
            raise OSError("offline release failure")

        ticket.release = fail
        return ticket

    monkeypatch.setattr(protection, "acquire", bad_release)
    counts = len(store.creates), writer.publication_calls
    result = op.reconcile("a", actor="owner", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert result["last_success"] is None
    status = op.status("schedule", at=clock())
    assert status["unknown_slots"] == ["a"] and status["last_success"] is None
    assert counts == (len(store.creates), writer.publication_calls)
    # Readback-release exceptions cannot leak family admission.
    with op.journal.executor("family-" + family):
        pass


@pytest.mark.parametrize("module,budget", [(fomc, 2), (fred, 4)])
def test_source_pg_recovery_without_qualified_capability_stops_before_release(
    tmp_path, monkeypatch, module, budget
):
    job, journal, writer, store, protection, clock = module.job_bindings(
        tmp_path, publication_failure="after"
    )
    candidate = module.candidate(clock)
    job.submit(
        "original",
        job.spec_type(
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=tuple(r.raw_sha256 for r in candidate.reads),
        ),
    )
    job.run("original", enabled=True, preflight=lambda _: None, retained=candidate)
    # Negative capability test only: even an apparently distinct read-only PG
    # reader or a changed writer PID is insufficient. Never execute a PG query.
    from types import SimpleNamespace

    from market_data_foundation.canonical.postgres import PostgresLedger

    job.kernel.ledger = PostgresLedger(
        SimpleNamespace(info=SimpleNamespace(backend_pid=102)), module.DEV_SCHEMA
    )
    monkeypatch.setattr(job, "_check_bindings", lambda _: None)

    @contextmanager
    def unqualified_reader():
        pytest.fail("unqualified PG readback was entered")
        yield

    job.readback = unqualified_reader
    counts = len(store.creates), len(store.reads), writer.publication_calls
    for _ in range(2):
        assert (
            job.reconcile("original", enabled=True)["reconciliation"]["outcome"]
            == "unresolved"
        )
        assert all(t.held for t in protection.tickets)
    assert counts == (len(store.creates), len(store.reads), writer.publication_calls)
