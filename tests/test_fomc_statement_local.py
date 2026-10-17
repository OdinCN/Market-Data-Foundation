"""T07 Stage-2 synthetic multi-raw fault matrix; no external service IO."""

import base64
import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from test_fomc_statement import CALENDAR, POLLUTED_STATEMENTS, STATEMENT

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Grade, digest, require
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import fomc_statement as statement
from market_data_foundation.historical import fomc_statement_live as live
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterialIdentity,
    MaterialWriteOutcomeUnknown,
    WriteDisposition,
)
from market_data_foundation.historical.fomc_statement_contract import (
    MAX_BODY_BYTES,
    STATEMENT_DOCUMENT,
)
from market_data_foundation.historical.fomc_statement_local_job import (
    DEV_SCHEMA,
    DirectStatementAcquirer,
    StatementJobJournal,
    StatementJobSpec,
    StatementLocalJob,
)
from market_data_foundation.historical.fomc_statement_local_read import (
    StatementReadRejected,
    read_local_statement,
)
from market_data_foundation.historical.fomc_statement_material_store import (
    StatementMaterialIdentity,
)
from market_data_foundation.historical.fomc_statement_materialization import (
    StatementMaterializationOutcomeUnknown,
    StatementProtectionReleaseFailure,
    materialize_persist_publish,
)

AT = datetime(2026, 10, 17, tzinfo=UTC)


class Clock:
    def __init__(self, at=AT):
        self.at = at

    def __call__(self):
        self.at += timedelta(microseconds=1)
        return self.at


class Source:
    def __init__(self, retry=False, failure=False):
        self.calls = []
        self.retry = retry
        self.failure = failure
        self.counts = {}

    def __call__(self, request):
        self.calls.append(request)
        key = request.url
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.failure:
            return live.Response(401, b"no")
        if self.retry and self.counts[key] == 1:
            return live.Response(503, b"retry")
        return live.Response(
            200, CALENDAR if request.url == statement.CALENDAR_URL else STATEMENT
        )


class Retain:
    def retain(self, raw, artifact):
        return live.ArtifactPaths(
            f"artifacts/read-{artifact.logical_read}/raw.json",
            f"artifacts/read-{artifact.logical_read}/request.json",
        )


def candidate(clock=None):
    return live.qualify_statement(
        request_source=Source(retry=True),
        artifact_store=Retain(),
        live_opt_in=True,
        clock=clock or Clock(),
        sleep=lambda _: None,
    )


def plan(identity="test", at=AT):
    c = candidate(Clock(at))
    return statement.build_live_plan(
        c,
        identity=identity,
        finished_at=at + timedelta(seconds=1),
        frozen_at=at + timedelta(seconds=2),
    )


def raws(p):
    return tuple(r.raw for r in statement.candidate_from_content(p.content).reads)


class Protection:
    def __init__(self, fail=None):
        self.tickets = []
        self.fail = fail
        self.events = []

    def acquire(self, identity):
        if len(self.tickets) + 1 == self.fail:
            raise RuntimeError("acquire_failed")
        owner = self

        class Ticket:
            held = True
            unknown = False

            def assert_held(self, other):
                require(self.held and identity == other, "ticket_gap")

            def release(self, other):
                self.assert_held(other)
                owner.events.append("release")
                self.held = False

            def mark_outcome_unknown(self, other):
                self.assert_held(other)
                self.unknown = True

        ticket = Ticket()
        self.tickets.append(ticket)
        self.events.append("acquire")
        return ticket

    def all_released(self):
        return all(not t.held for t in self.tickets)


class Store:
    def __init__(self, fail=None, unknown=None, mismatch=None, absent=None):
        self.objects = {}
        self.creates = []
        self.reads = []
        self.fail, self.unknown, self.mismatch, self.absent = (
            fail,
            unknown,
            mismatch,
            absent,
        )

    def create_if_absent(self, identity, payload):
        self.creates.append(identity)
        index = len(self.creates)
        if index == self.fail:
            raise RuntimeError("store_failed")
        disposition = (
            WriteDisposition.ALREADY_PRESENT
            if identity in self.objects
            else WriteDisposition.CREATED
        )
        self.objects.setdefault(identity, payload)
        if index == self.unknown:
            raise MaterialWriteOutcomeUnknown()
        return disposition

    def read_exact(self, identity):
        self.reads.append(identity)
        if len(self.reads) == self.mismatch:
            return b"wrong"
        if len(self.reads) == self.absent:
            return None
        return self.objects.get(identity)


class Ledger(InMemoryLedger):
    def __init__(self, publication_failure=None):
        super().__init__()
        self.publication_failure = publication_failure
        self.publication_calls = 0

    def publish(self, request, clock):
        self.publication_calls += 1
        if self.publication_failure == "before":
            raise RuntimeError("publication_unknown")
        value = super().publish(request, clock)
        if self.publication_failure == "after":
            raise RuntimeError("publication_unknown")
        return value


def bindings(p=None, **store_kw):
    p = p or plan()
    ledger = Ledger()
    kernel = CanonicalKernel(ledger, lambda: AT + timedelta(seconds=10))
    return p, ledger, kernel, Store(**store_kw), Protection()


def materialize(p, kernel, store, protection, **kw):
    return materialize_persist_publish(kernel, p, raws(p), store, protection, **kw)


def test_plan_exact_two_raw_bundle():
    p = plan()
    statement.validate_live_plan(p, raws(p))
    assert p.run.budget == 2
    assert len(p.acquisitions) == len(p.raw_materials) == 2
    assert len(p.version.materials) == 3
    assert [a.provider_data_attempts for a in p.acquisitions] == [2] * 2
    assert p.run.started_at == min(a.requested_at for a in p.acquisitions)
    assert len(set(a.request_identity for a in p.acquisitions)) == 2
    assert all(not m.durable and not m.replayable for m in p.raw_materials)
    assert p.publication_request.evidence == p.version.materials
    assert p.publication_request.claimed_grades == frozenset(
        {Grade.ORIGINAL, Grade.PROJECTED}
    )
    assert not p.binding_candidate.silent_fallback
    assert not STATEMENT_DOCUMENT.source_qualified
    doc = json.loads(p.content)
    assert "api_key" not in p.content.decode()
    assert p.version.finite_scope.members == (statement.DOCUMENT_ID,)
    assert doc["provider"] == "official federalreserve.gov"
    assert doc["document"]["document_kind"] == "FOMC_STATEMENT"
    assert doc["document"]["official_published_at"] < doc["requests"][0]["requested_at"]


@pytest.mark.parametrize("index", range(2))
@pytest.mark.parametrize("axis", ["raw", "request", "timing", "selection", "attempts"])
def test_plan_tamper_fails(index, axis):
    p = plan()
    doc = json.loads(p.content)
    if axis == "raw":
        doc["raw"][index]["sha256"] = "0" * 64
    elif axis == "request":
        doc["requests"][index]["url"] = "https://evil.example/"
    elif axis == "timing":
        doc["requests"][index]["requested_at"] = (AT - timedelta(seconds=1)).isoformat()
    elif axis == "selection":
        doc["discovery"]["statement_url"] = "https://evil.example/"
    else:
        doc["requests"][index]["attempts"] = 4
    with pytest.raises((ValueError, ContractError)):
        statement.validate_live_plan(
            replace(p, content=statement._encode(doc)), raws(p)
        )


def test_two_creates_readbacks_and_callback_order():
    p, ledger, kernel, store, protection = bindings()
    proof = []

    def released():
        assert protection.all_released()
        proof.append(True)

    result = materialize(p, kernel, store, protection, protection_released=released)
    assert len(store.creates) == len(store.reads) == len(result.verification) == 2
    assert all(m.durable and m.replayable for m in result.plan.raw_materials)
    assert ledger.publication_calls == 1 and proof == [True]
    assert all(v.disposition == WriteDisposition.CREATED for v in result.verification)


@pytest.mark.parametrize("index", range(1, 3))
@pytest.mark.parametrize("axis", ["fail", "mismatch", "absent", "acquire"])
def test_any_raw_failure_never_writes_canonical(index, axis):
    p, ledger, kernel, store, protection = bindings(
        **({axis: index} if axis != "acquire" else {})
    )
    if axis == "acquire":
        protection.fail = index
    proof = []
    with pytest.raises((RuntimeError, ContractError)):
        materialize(
            p,
            kernel,
            store,
            protection,
            protection_released=lambda: proof.append(protection.all_released()),
        )
    assert ledger.publication_calls == 0 and ledger.get(p.version.key) is None
    assert not ledger._records and proof == [True]
    assert protection.all_released()


@pytest.mark.parametrize("index", range(1, 3))
def test_unknown_put_exact_present_continues(index):
    p, ledger, kernel, store, protection = bindings(unknown=index)
    result = materialize(p, kernel, store, protection)
    assert result.verification[index - 1].reconciled_outcome_unknown
    assert result.verification[index - 1].disposition is None
    assert ledger.publication_calls == 1


@pytest.mark.parametrize("phase", ["commit", "publication_before", "publication_after"])
def test_authoritative_unknown_retains_all_tickets_until_reconciled(phase):
    p, ledger, kernel, store, protection = bindings()
    if phase.startswith("publication"):
        ledger.publication_failure = phase.split("_")[1]
    proof = []

    def commit():
        if phase == "commit":
            raise RuntimeError("commit_unknown")

    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(
            p,
            kernel,
            store,
            protection,
            authoritative_commit=commit,
            protection_released=lambda: proof.append(protection.all_released()),
        )
    handle = error.value
    assert len(handle.tickets) == 2 and all(
        t.held and t.unknown for t in protection.tickets
    )
    assert not proof
    independent = copy.deepcopy(ledger)
    expected = {
        "commit": "confirmed_committed",
        "publication_before": "confirmed_not_published",
        "publication_after": "confirmed_published",
    }[phase]
    assert handle.reconcile(independent) == expected
    assert proof == [True] and protection.all_released()
    with pytest.raises(ContractError):
        handle.reconcile(independent)


def test_pre_authoritative_persist_failure_releases_all():
    p, ledger, kernel, store, protection = bindings()
    kernel.add = lambda _: (_ for _ in ()).throw(RuntimeError("persist"))
    proof = []
    with pytest.raises(RuntimeError):
        materialize(
            p,
            kernel,
            store,
            protection,
            protection_released=lambda: proof.append(protection.all_released()),
        )
    assert len(protection.tickets) == 2 and proof == [True]
    assert ledger.publication_calls == 0


def test_exact_as_of_read_rebuild_and_timing():
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)
    at = AT + timedelta(seconds=20)
    exact = read_local_statement(ledger, as_of=at, version_identity=p.version.identity)
    assert exact == read_local_statement(ledger, as_of=at)
    assert [a.obtained_at for a in exact.acquisitions] == [
        a.obtained_at for a in p.acquisitions
    ]
    assert [a.requested_at for a in exact.acquisitions] == [
        a.requested_at for a in p.acquisitions
    ]
    assert all(a.provider_data_attempts == 2 for a in exact.acquisitions)
    assert exact.content == result.plan.content
    assert len(store.reads) == 2  # Local Read does not touch Garage.


@pytest.mark.parametrize("index", range(2))
def test_local_read_rejects_acquisition_tamper(index):
    p, ledger, kernel, store, protection = bindings()
    materialize(p, kernel, store, protection)
    a = p.acquisitions[index]
    bad = replace(a, provider_data_attempts=3)
    ledger._records[(a.key.kind.value, a.key.identity, a.key.revision)] = codec.dumps(
        bad
    )
    with pytest.raises(StatementReadRejected):
        read_local_statement(ledger, as_of=AT + timedelta(seconds=20))


def test_as_of_latest_and_equal_ambiguity():
    p, ledger, kernel, store, protection = bindings()
    materialize(p, kernel, store, protection)
    p2 = plan("newer", AT + timedelta(seconds=3))
    materialize(p2, kernel, store, protection)
    assert (
        read_local_statement(ledger, as_of=AT + timedelta(seconds=20)).version
        == p2.version
    )
    p3 = plan("equal", AT + timedelta(seconds=3))
    materialize(p3, kernel, store, protection)
    with pytest.raises(ReadContractError) as error:
        read_local_statement(ledger, as_of=AT + timedelta(seconds=20))
    assert error.value.classification == ReadFailure.AS_OF_AMBIGUOUS


@pytest.mark.parametrize("suffix", ["_x", "0", "_r2", ";drop", " ", "\n"])
def test_schema_exact_allowlist(suffix):
    assert validate_schema(DEV_SCHEMA) == DEV_SCHEMA
    with pytest.raises(ContractError):
        validate_schema(DEV_SCHEMA + suffix)


def job_bindings(tmp_path, publication_failure=None, commit_failure=False):
    clock = Clock()
    ledger = Ledger(publication_failure)
    kernel = CanonicalKernel(ledger, clock)
    store, protection = Store(), Protection()
    journal = StatementJobJournal(tmp_path / "journal")

    @contextmanager
    def readback():
        yield copy.deepcopy(ledger)

    def commit():
        if commit_failure:
            raise RuntimeError("commit")

    job = StatementLocalJob(journal, clock, kernel, store, protection, readback, commit)
    return job, journal, ledger, store, protection, clock


def test_job_acquire_two_logical_reads_with_retries_and_no_repeat(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("acquire", StatementJobSpec())
    source = Source(retry=True)
    acquirer = DirectStatementAcquirer(source, Retain(), clock)
    result = job.run(
        "acquire", enabled=True, preflight=lambda _: None, acquirer=acquirer
    )
    assert result["state"] == "succeeded"
    assert len(source.calls) == 4
    assert result["effects"]["provider_requests_possible"] == 2
    assert result["effects"]["provider_requests_returned"] == 2
    assert result["effects"]["raw_creates_possible"] == 2
    assert len(result["raw"]) == 2 and store.creates and protection.all_released()
    assert result["writer_session"]["lock_release_proven"] is True
    assert (
        job.run("acquire", enabled=True, preflight=lambda _: None, acquirer=acquirer)
        == result
    )
    assert len(source.calls) == 4 and ledger.publication_calls == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_job_failure_cancel_no_source_repeat(tmp_path, cancel):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("stopped", StatementJobSpec())
    if cancel:
        journal.cancel("stopped", clock())
    source = Source(failure=True)
    acquirer = DirectStatementAcquirer(source, Retain(), clock)
    result = job.run(
        "stopped", enabled=True, preflight=lambda _: None, acquirer=acquirer
    )
    assert result["state"] == ("cancelled" if cancel else "failed")
    job.run("stopped", enabled=True, preflight=lambda _: None, acquirer=acquirer)
    assert len(source.calls) == (0 if cancel else 1)
    assert not store.creates and ledger.publication_calls == 0


def test_job_unknown_reconcile_durable_proof_restart_repair_zero_provider(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path, "before")
    job.submit("original", StatementJobSpec())
    source = Source()
    result = job.run(
        "original",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(source, Retain(), clock),
    )
    assert result["state"] == "outcome_unknown" and not protection.all_released()
    with pytest.raises(ContractError):
        job.repair("original", "too-early", enabled=True)
    # A fresh process has no synthetic release proof and must refuse reconciliation.
    restarted = StatementLocalJob(
        StatementJobJournal(journal.directory),
        clock,
        job.kernel,
        store,
        protection,
        job.readback,
        job.commit,
    )
    assert (
        restarted.reconcile("original", enabled=True)["reconciliation"]["outcome"]
        == "unresolved"
    )
    result = job.reconcile("original", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert (
        result["reconciliation"]["lock_release_proven"] is True
        and protection.all_released()
    )
    result = restarted.reconcile("original", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    repaired = restarted.repair("original", "repair", enabled=True)
    assert repaired["spec"]["max_provider_requests"] == 0
    c = statement.candidate_from_content(codec.loads(result["plan"]["content"]))
    ledger.publication_failure = None
    done = restarted.run("repair", enabled=True, preflight=lambda _: None, retained=c)
    assert done["state"] == "succeeded"
    assert done["plan"] == result["plan"] and done["raw"] == result["raw"]
    assert len(source.calls) == 2
    assert journal.get("original")["state"] == "outcome_unknown"


@pytest.mark.parametrize("index", range(1, 3))
@pytest.mark.parametrize("state", ["absent", "mismatch"])
def test_unknown_put_requires_each_exact_raw(index, state):
    p, ledger, kernel, store, protection = bindings(unknown=index, **{state: index})
    with pytest.raises(ContractError):
        materialize(p, kernel, store, protection)
    assert not ledger._records and ledger.publication_calls == 0
    assert protection.all_released()


def test_unknown_requires_independent_ledger_and_exact_content_before_release():
    p, ledger, kernel, store, protection = bindings()
    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(
            p,
            kernel,
            store,
            protection,
            authoritative_commit=lambda: (_ for _ in ()).throw(RuntimeError()),
        )
    handle = error.value
    with pytest.raises(ContractError, match="independent"):
        handle.reconcile(ledger)
    independent = copy.deepcopy(ledger)
    independent._contents.clear()
    with pytest.raises(ContractError, match="readback"):
        handle.reconcile(independent)
    assert all(ticket.held for ticket in protection.tickets)
    assert handle.reconcile(copy.deepcopy(ledger)) == "confirmed_committed"
    assert protection.all_released()


def test_unknown_absent_commit_releases_only_after_exact_absence():
    p, ledger, kernel, store, protection = bindings()
    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(
            p,
            kernel,
            store,
            protection,
            authoritative_commit=lambda: (_ for _ in ()).throw(RuntimeError()),
        )
    independent = Ledger()
    independent._contents = copy.deepcopy(ledger._contents)
    with pytest.raises(ContractError, match="absence"):
        error.value.reconcile(independent)
    independent._contents.clear()
    assert error.value.reconcile(independent) == "confirmed_not_committed"
    assert protection.all_released()


def test_release_proof_callback_can_retry_after_local_failure():
    p, ledger, kernel, store, protection = bindings()
    calls = []

    def released():
        assert protection.all_released()
        calls.append(True)
        if len(calls) == 1:
            raise RuntimeError("journal_unavailable")

    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(
            p,
            kernel,
            store,
            protection,
            protection_released=released,
            authoritative_commit=lambda: (_ for _ in ()).throw(RuntimeError()),
        )
    with pytest.raises(RuntimeError, match="journal"):
        error.value.reconcile(copy.deepcopy(ledger))
    assert protection.all_released()
    assert error.value.reconcile(copy.deepcopy(ledger)) == "confirmed_committed"
    assert calls == [True, True]


@pytest.mark.parametrize(
    "axis",
    [
        "calendar_raw",
        "statement_raw",
        "request",
        "timing",
        "discovery",
        "official_published_at",
        "text",
        "text_sha256",
        "extraction_revision",
        "metadata",
    ],
)
def test_local_read_rebuild_rejects_self_consistent_content_digest_tamper(axis):
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)
    doc = json.loads(p.content)
    if axis in {"calendar_raw", "statement_raw"}:
        import base64

        index = 0 if axis == "calendar_raw" else 1
        raw = base64.b64decode(doc["raw"][index]["base64"])
        raw = raw.replace(b"September", b"July")
        doc["raw"][index] = {
            "base64": base64.b64encode(raw).decode(),
            "sha256": digest(raw),
            "size": len(raw),
        }
    elif axis == "request":
        doc["requests"][1]["url"] = doc["requests"][0]["url"]
    elif axis == "timing":
        doc["requests"][1]["obtained_at"] = (AT + timedelta(seconds=1)).isoformat()
    elif axis == "discovery":
        doc["discovery"]["statement_url"] = "https://evil.example/"
    elif axis == "metadata":
        doc["document"]["last_update"] = "invented"
    else:
        doc["document"][axis] = "invented"
        if axis == "text":
            doc["document"]["text_sha256"] = digest(b"invented")
    content = statement._encode(doc)
    changed = replace(p.version, content_digest=digest(content))
    semantic = replace(result.plan.semantic_material, digest=digest(content))
    for record in (changed, semantic):
        ledger._records[
            (record.key.kind.value, record.key.identity, record.key.revision)
        ] = codec.dumps(record)
    ledger._contents[changed.identity] = (digest(content), content)
    with pytest.raises(StatementReadRejected):
        read_local_statement(ledger, as_of=AT + timedelta(seconds=20))


def test_invalid_plan_before_acquire_still_calls_release_proof():
    p, ledger, kernel, store, protection = bindings()
    proof = []
    with pytest.raises(ContractError):
        materialize(
            replace(p, run=replace(p.run, budget=3)),
            kernel,
            store,
            protection,
            protection_released=lambda: proof.append(protection.all_released()),
        )
    assert proof == [True] and not protection.tickets and not store.creates


@pytest.mark.parametrize("phase", ["before", "after", "commit"])
def test_job_unknown_handle_release_proof_precedes_readback_exit_failure(
    tmp_path, phase
):
    job, journal, ledger, store, protection, clock = job_bindings(
        tmp_path, None if phase == "commit" else phase, phase == "commit"
    )
    job.submit("unknown", StatementJobSpec())
    source = Source()
    job.run(
        "unknown",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(source, Retain(), clock),
    )

    @contextmanager
    def bad_exit():
        yield copy.deepcopy(ledger)
        raise RuntimeError("readback_exit")

    original_readback = job.readback
    job.readback = bad_exit
    result = job.reconcile("unknown", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert result["reconciliation"]["lock_release_proven"] is True
    assert protection.all_released()
    restarted = StatementLocalJob(
        StatementJobJournal(journal.directory),
        clock,
        job.kernel,
        store,
        protection,
        original_readback,
        job.commit,
    )
    result = restarted.reconcile("unknown", enabled=True)
    assert result["reconciliation"]["outcome"] == (
        "confirmed_published" if phase == "after" else "confirmed_not_published"
    )
    assert len(source.calls) == 2


def test_synthetic_restart_after_pre_authoritative_failure_has_proof(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    store.fail = 2
    job.submit("partial", StatementJobSpec())
    result = job.run(
        "partial",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(Source(), Retain(), clock),
    )
    assert result["state"] == "outcome_unknown"
    assert result["writer_session"]["lock_release_proven"] is True
    restarted = StatementLocalJob(
        StatementJobJournal(journal.directory),
        clock,
        job.kernel,
        store,
        protection,
        job.readback,
        job.commit,
    )
    result = restarted.reconcile("partial", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert ledger.publication_calls == 0
    with pytest.raises(ContractError):
        restarted.repair("partial", "repair", enabled=True)


def test_repair_rejects_changed_candidate_and_single_successor(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path, "before")
    job.submit("original", StatementJobSpec())
    job.run(
        "original",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(Source(), Retain(), clock),
    )
    job.reconcile("original", enabled=True)
    job.repair("original", "repair", enabled=True)
    with pytest.raises(ContractError, match="scope"):
        job.repair("original", "second", enabled=True)
    # Same raw bytes with different retained acquisition timings must fail.
    changed = candidate(clock)
    result = job.run("repair", enabled=True, preflight=lambda _: None, retained=changed)
    assert result["state"] == "failed"
    assert ledger.publication_calls == 1 and len(store.creates) == 2


def test_acquirer_meter_failure_is_terminal_without_source_retry():
    source = Source()
    acquirer = DirectStatementAcquirer(source, Retain(), Clock())

    def broken(*_):
        raise OSError("journal")

    with pytest.raises(live.QualificationError):
        acquirer.acquire("test", lambda: None, broken)
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_provider_requests", 3),
        ("max_raw_creates", 1),
        ("event", "other"),
        ("reference", "2026-09"),
        ("mode", "other"),
        ("max_publications", 2),
        ("max_ledger_handoffs", 2),
        ("max_provider_requests", True),
    ],
)
def test_job_fixed_spec_gate(field, value):
    with pytest.raises(ContractError):
        replace(StatementJobSpec(), **{field: value})


@pytest.mark.parametrize("index", [0, 1])
def test_retained_job_zero_source_and_each_journal_observed_time(tmp_path, index):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    c = candidate(clock)
    spec = StatementJobSpec(
        mode="retained",
        max_provider_requests=0,
        expected_raw_sha256=tuple(r.raw_sha256 for r in c.reads),
    )
    assert StatementJobSpec.from_document(spec.document()) == spec
    job.submit("retained", spec)
    result = job.run("retained", enabled=True, preflight=lambda _: None, retained=c)
    assert result["state"] == "succeeded"
    assert "provider_requests_possible" not in result["effects"]
    assert len(result["raw"]) == 2
    assert (
        result["raw"][index]["observed_at"]
        == c.reads[index].artifact.obtained_at.isoformat()
    )
    view = read_local_statement(copy.deepcopy(ledger), as_of=clock())
    assert statement.candidate_from_content(view.content).document == c.document
    assert statement.candidate_from_content(view.content).discovery == c.discovery
    assert protection.all_released()


@pytest.mark.parametrize("failure", ["journal", "storage", "readback"])
def test_successful_source_local_failure_does_not_repeat(tmp_path, failure):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    source = Source(retry=True)
    job.submit("local-failure", StatementJobSpec())
    if failure == "journal":
        original = journal._update

        def broken(identity, at, **changes):
            if changes.get("stage") == "acquired":
                raise OSError("synthetic journal failure")
            return original(identity, at, **changes)

        journal._update = broken
    elif failure == "storage":
        store.fail = 2
    else:

        @contextmanager
        def broken_readback():
            raise OSError("synthetic readback failure")
            yield

        job.readback = broken_readback
    acquirer = DirectStatementAcquirer(source, Retain(), clock)
    result = job.run(
        "local-failure", enabled=True, preflight=lambda _: None, acquirer=acquirer
    )
    assert result["state"] in {"failed", "outcome_unknown"}
    assert len(source.calls) == 4
    assert (
        job.run(
            "local-failure", enabled=True, preflight=lambda _: None, acquirer=acquirer
        )
        == result
    )
    assert len(source.calls) == 4


@pytest.mark.parametrize("at_stage", ["after_preflight", "after_source"])
def test_cancel_between_stages_no_storage_or_repeat(tmp_path, at_stage):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("cancel", StatementJobSpec())
    source = Source()

    class CancellingRetain(Retain):
        def retain(self, raw, artifact):
            paths = super().retain(raw, artifact)
            if artifact.logical_read == 2:
                journal.cancel("cancel", clock())
            return paths

    def preflight(_):
        if at_stage == "after_preflight":
            journal.cancel("cancel", clock())

    acquirer = DirectStatementAcquirer(source, CancellingRetain(), clock)
    result = job.run("cancel", enabled=True, preflight=preflight, acquirer=acquirer)
    assert result["state"] == "cancelled"
    assert len(source.calls) == (0 if at_stage == "after_preflight" else 2)
    assert not store.creates and ledger.publication_calls == 0


def test_publication_gate_requires_both_durable_and_exact_lineage():
    p, ledger, kernel, store, protection = bindings()
    with pytest.raises(ContractError):
        statement.persist_before_publication(kernel, p)
    with pytest.raises(ContractError):
        statement.publish(kernel, p)
    assert not ledger._records and ledger.publication_calls == 0
    result = materialize(p, kernel, store, protection)
    for index in range(2):
        materials = list(result.plan.raw_materials)
        materials[index] = replace(materials[index], durable=False)
        with pytest.raises(ContractError):
            statement.publish(
                kernel, replace(result.plan, raw_materials=tuple(materials))
            )
    assert ledger.publication_calls == 1


@pytest.mark.parametrize("axis", ["decision", "usable", "frozen", "acquisition"])
def test_local_read_rejects_temporal_inversions(axis):
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)
    if axis in {"decision", "usable"}:
        publication = result.publication
        bad = replace(
            publication,
            **{
                "decision_at" if axis == "decision" else "usable_at": AT
                - timedelta(days=1)
            },
        )
        ledger._records[
            (
                publication.key.kind.value,
                publication.key.identity,
                publication.key.revision,
            )
        ] = codec.dumps(bad)
    else:
        record = p.version if axis == "frozen" else p.acquisitions[0]
        bad = replace(
            record,
            **{
                "frozen_at" if axis == "frozen" else "obtained_at": AT
                - timedelta(days=1)
            },
        )
        ledger._records[
            (record.key.kind.value, record.key.identity, record.key.revision)
        ] = codec.dumps(bad)
    with pytest.raises((StatementReadRejected, ReadContractError, ContractError)):
        read_local_statement(ledger, as_of=AT + timedelta(seconds=20))


def test_official_time_independent_of_acquisition_freeze_decision_and_usable():
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)
    doc = statement.candidate_from_content(result.plan.content).document
    assert doc.official_published_at < p.run.started_at
    assert p.run.started_at < max(a.obtained_at for a in p.acquisitions)
    assert max(a.obtained_at for a in p.acquisitions) < p.version.frozen_at
    assert p.version.frozen_at < result.publication.decision_at
    assert result.publication.decision_at <= result.publication.usable_at
    with pytest.raises(ReadContractError):
        read_local_statement(ledger, as_of=doc.official_published_at)
    assert (
        read_local_statement(ledger, as_of=result.publication.usable_at).version
        == p.version
    )


def test_local_read_has_no_source_or_storage_calls(monkeypatch):
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)

    def forbidden(*_, **__):
        raise AssertionError("external capability called")

    monkeypatch.setattr(live, "qualify_statement", forbidden)
    monkeypatch.setattr(live.ExactFomcHttpsSource, "__call__", forbidden)
    store.create_if_absent = forbidden
    store.read_exact = forbidden
    at = AT + timedelta(seconds=20)
    exact = read_local_statement(ledger, as_of=at, version_identity=p.version.identity)
    assert exact == read_local_statement(ledger, as_of=at)
    assert raws(result.plan) == (CALENDAR, STATEMENT)


def fake_pg(*, pid=101, read_only=False, host="192.168.10.4", schema=DEV_SCHEMA):
    from types import SimpleNamespace

    from market_data_foundation.canonical.postgres import PostgresLedger

    return PostgresLedger(
        SimpleNamespace(
            info=SimpleNamespace(host=host, backend_pid=pid),
            read_only=read_only,
        ),
        schema,
    )


@pytest.mark.parametrize(
    "axis", ["same_ledger", "same_pid", "writable", "host", "schema"]
)
def test_independent_readback_connection_gate_without_pg_io(tmp_path, axis):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    writer = fake_pg()
    job.kernel.ledger = writer
    reader = fake_pg(pid=102, read_only=True)
    if axis == "same_ledger":
        reader = writer
    elif axis == "same_pid":
        reader.connection.info.backend_pid = 101
    elif axis == "writable":
        reader.connection.read_only = False
    elif axis == "host":
        reader.connection.info.host = "127.0.0.1"
    else:
        reader.schema = "mdf_macro_fred_alfred_vintage_r1"
    with pytest.raises(ContractError):
        job._check_independent(
            reader, StatementJobSpec(effect_domain="owner_nonproduction")
        )
    job._check_independent(
        fake_pg(pid=102, read_only=True),
        StatementJobSpec(effect_domain="owner_nonproduction"),
    )


@pytest.mark.parametrize("current_pid", [101, 102])
def test_restart_owner_old_writer_pid_gate_without_pg_io(tmp_path, current_pid):
    from market_data_foundation.historical.fomc_statement_local_job import _encode_plan

    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    c = candidate(clock)
    p = statement.build_live_plan(
        c, identity="owner", finished_at=clock(), frozen_at=clock()
    )
    spec = StatementJobSpec(effect_domain="owner_nonproduction")
    job.submit("owner", spec)
    identities = []
    for read in c.reads:
        identity = StatementMaterialIdentity(read.raw_sha256, read.raw_size)
        store.objects[identity] = read.raw
        identities.append(
            {
                "sha256": identity.sha256,
                "size": identity.size,
                "locator": identity.locator,
                "observed_at": read.artifact.obtained_at.isoformat(),
            }
        )
    journal._update(
        "owner",
        clock(),
        state="running",
        plan=_encode_plan(p),
        raw=identities,
        writer_session={"kind": "postgres", "host": "192.168.10.4", "backend_pid": 101},
    )
    journal._update("owner", clock(), state="outcome_unknown")
    # Only connection identity is simulated. No PG connection or command exists.
    job.kernel.ledger = fake_pg(pid=current_pid)
    reader = fake_pg(pid=103, read_only=True)
    reader.publication_by_occurrence = lambda _: None
    reader.get = lambda _: None

    @contextmanager
    def owner_readback():
        yield reader

    job.readback = owner_readback
    job._check_bindings = lambda _: None
    result = job.reconcile("owner", enabled=True)
    if current_pid == 101:
        assert result["reconciliation"]["outcome"] == "unresolved"
        assert not store.reads and not protection.tickets
    else:
        assert result["reconciliation"]["outcome"] == "confirmed_not_published"
        assert len(store.reads) == 2 and protection.all_released()


def test_success_observation_failure_retains_current_raw_and_job_never_repeats(
    tmp_path, monkeypatch
):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("observation", StatementJobSpec())
    source = Source()
    retained = []

    class RecordingRetain(Retain):
        def retain(self, raw, artifact):
            paths = super().retain(raw, artifact)
            retained.append((raw, artifact, paths))
            return paths

    errors = []
    original_acquire = DirectStatementAcquirer.acquire

    def acquire(self, *args):
        try:
            return original_acquire(self, *args)
        except live.QualificationError as exc:
            errors.append(exc)
            raise

    monkeypatch.setattr(DirectStatementAcquirer, "acquire", acquire)
    original_update = journal._update

    def update(identity, at, **changes):
        if changes.get("effects", {}).get("provider_response_received") is True:
            assert len(retained) == 1
            raise OSError("journal unavailable")
        return original_update(identity, at, **changes)

    monkeypatch.setattr(journal, "_update", update)
    acquirer = DirectStatementAcquirer(source, RecordingRetain(), clock)
    result = job.run(
        "observation", enabled=True, preflight=lambda _: None, acquirer=acquirer
    )
    assert result["state"] == "failed"
    assert len(source.calls) == len(retained) == len(errors) == 1
    error = errors[0]
    assert error.classification == "request_observation_error"
    assert len(error.retained) == 1
    read = error.retained[0]
    assert read.raw == CALENDAR and read.raw_sha256 == digest(CALENDAR)
    assert read.raw_size == len(CALENDAR) and read.paths == retained[0][2]
    assert read.artifact == retained[0][1] == error.request_artifacts[0]
    assert (
        job.run(
            "observation", enabled=True, preflight=lambda _: None, acquirer=acquirer
        )
        == result
    )
    assert len(source.calls) == 1 and ledger.publication_calls == 0


@pytest.mark.parametrize("transport", [False, True])
def test_failed_response_observation_failure_is_terminal_without_evidence(transport):
    calls, retained = [], []

    def source(request):
        calls.append(request)
        if transport:
            raise TimeoutError("transport")
        return live.Response(503, b"failed")

    class NoRetain:
        def retain(self, *args):
            retained.append(args)
            raise AssertionError("failed response retained")

    def broken(*args):
        raise OSError("journal")

    with pytest.raises(live.QualificationError) as error:
        DirectStatementAcquirer(source, NoRetain(), Clock()).acquire(
            "failure", lambda: None, broken
        )
    assert len(calls) == 1 and not retained
    assert not error.value.retained and not error.value.request_artifacts


def test_published_release_proof_failure_is_retryable_exact_and_already_released():
    p, ledger, kernel, store, protection = bindings()
    callbacks = []

    def released():
        assert protection.all_released()
        callbacks.append(True)
        if len(callbacks) < 3:
            raise OSError("proof unavailable")

    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(p, kernel, store, protection, protection_released=released)
    handle = error.value
    assert handle.phase.value == "release_proof" and handle.tickets_already_released
    assert protection.all_released() and not any(t.unknown for t in protection.tickets)
    assert (
        ledger.publication_by_occurrence(p.publication_request.occurrence) is not None
    )
    events = list(protection.events)
    with pytest.raises(ContractError, match="independent"):
        handle.reconcile(ledger)
    with pytest.raises(ContractError, match="publication_readback_missing"):
        handle.reconcile(Ledger())
    wrong = copy.deepcopy(ledger)
    publication = wrong.publication_by_occurrence(p.publication_request.occurrence)
    # A different exact request under the same occurrence must fail closed.
    monkey_request = replace(publication.request, identity="different-request")
    wrong.publication_by_occurrence = lambda _: replace(
        publication, request=monkey_request
    )
    with pytest.raises(ContractError, match="publication_readback_mismatch"):
        handle.reconcile(wrong)
    with pytest.raises(OSError, match="proof"):
        handle.reconcile(copy.deepcopy(ledger))
    assert handle.reconcile(copy.deepcopy(ledger)) == "confirmed_published"
    assert protection.events == events and ledger.publication_calls == 1
    assert callbacks == [True] * 3


def test_job_published_release_proof_failure_preserves_handle_for_retries(
    tmp_path, monkeypatch
):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("release-proof", StatementJobSpec())
    source = Source()
    original_update = journal._update
    failures = []

    def update(identity, at, **changes):
        proof = changes.get("writer_session") or changes.get("reconciliation") or {}
        if proof.get("lock_release_proven") is True and len(failures) < 3:
            assert protection.all_released()
            failures.append(True)
            raise OSError("proof unavailable")
        return original_update(identity, at, **changes)

    monkeypatch.setattr(journal, "_update", update)
    result = job.run(
        "release-proof",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(source, Retain(), clock),
    )
    assert result["state"] == "outcome_unknown"
    assert result["failure_stage"] == "release_proof"
    handle = job._unknown_handles["release-proof"]
    original_tickets = tuple(protection.tickets)
    assert protection.all_released() and len(source.calls) == 2
    assert ledger.publication_calls == 1
    restarted = StatementLocalJob(
        StatementJobJournal(journal.directory),
        clock,
        job.kernel,
        store,
        protection,
        job.readback,
        job.commit,
    )
    result = restarted.reconcile("release-proof", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert not result["reconciliation"].get("lock_release_proven")
    for _ in range(2):
        result = job.reconcile("release-proof", enabled=True)
        assert result["reconciliation"]["outcome"] == "unresolved"
        assert job._unknown_handles["release-proof"] is handle
        assert tuple(protection.tickets) == original_tickets
        assert protection.events.count("release") == 2
    result = job.reconcile("release-proof", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert result["reconciliation"]["lock_release_proven"] is True
    assert "release-proof" not in job._unknown_handles
    assert all(not t.held for t in original_tickets)
    assert protection.all_released()
    assert len(source.calls) == 2 and ledger.publication_calls == 1


def test_release_proof_success_then_secondary_journal_failure_drops_resolved_handle(
    tmp_path, monkeypatch
):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("proof-secondary", StatementJobSpec())
    source = Source()
    original_update = journal._update
    proof_calls = []

    def update(identity, at, **changes):
        proof = changes.get("writer_session") or changes.get("reconciliation") or {}
        if proof.get("lock_release_proven") is True:
            proof_calls.append(True)
            # First proof write creates the RELEASE_PROOF handle. The second is
            # its successful retry. The third is local-job secondary bookkeeping.
            if len(proof_calls) in {1, 3}:
                raise OSError("journal boundary")
        return original_update(identity, at, **changes)

    monkeypatch.setattr(journal, "_update", update)
    result = job.run(
        "proof-secondary",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(source, Retain(), clock),
    )
    assert result["state"] == "outcome_unknown"
    assert "proof-secondary" in job._unknown_handles
    original_tickets = tuple(protection.tickets)
    assert len(original_tickets) == 2 and all(not t.held for t in original_tickets)
    assert protection.all_released()
    assert ledger.publication_calls == 1
    assert len(source.calls) == 2

    result = job.reconcile("proof-secondary", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert result["reconciliation"]["lock_release_proven"] is True
    assert "proof-secondary" not in job._unknown_handles
    assert tuple(protection.tickets) == original_tickets
    assert all(not t.held for t in original_tickets)
    assert protection.events.count("release") == 2
    assert ledger.publication_calls == 1
    assert len(source.calls) == 2

    result = job.reconcile("proof-secondary", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert result["reconciliation"]["lock_release_proven"] is True
    assert "proof-secondary" not in job._unknown_handles
    assert len(protection.tickets) == 4
    assert tuple(protection.tickets[:2]) == original_tickets
    assert all(not t.held for t in original_tickets)
    assert protection.events.count("release") == 4
    assert ledger.publication_calls == 1
    assert len(source.calls) == 2


class PartialReleaseProtection(Protection):
    def acquire(self, identity):
        ticket = super().acquire(identity)
        original = ticket.release
        fail_once = len(self.tickets) == 2
        ticket.release_calls = 0

        def release(other):
            ticket.release_calls += 1
            if fail_once and ticket.release_calls == 1:
                raise OSError("synthetic release failure")
            original(other)

        ticket.release = release
        return ticket


def test_partial_release_preserves_publication_and_ticket_progress():
    p, ledger, kernel, store, _ = bindings()
    protection = PartialReleaseProtection()
    proof = []
    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(
            p,
            kernel,
            store,
            protection,
            protection_released=lambda: proof.append(protection.all_released()),
        )
    handle = error.value
    assert handle.phase.value == "release_proof"
    assert (
        ledger.publication_by_occurrence(p.publication_request.occurrence) is not None
    )
    first, second = protection.tickets
    assert not first.held and second.held and second.unknown
    assert not first.unknown and not proof
    assert [t.release_calls for t in protection.tickets] == [1, 1]
    with pytest.raises(ContractError, match="publication_readback_missing"):
        handle.reconcile(Ledger())
    assert second.held
    assert handle.reconcile(copy.deepcopy(ledger)) == "confirmed_published"
    assert protection.all_released() and proof == [True]
    assert [t.release_calls for t in protection.tickets] == [1, 2]


def test_job_partial_release_reconciles_to_durable_proof(tmp_path):
    job, journal, ledger, store, _, clock = job_bindings(tmp_path)
    job.protection = protection = PartialReleaseProtection()
    job.submit("partial-release", StatementJobSpec())
    result = job.run(
        "partial-release",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(Source(), Retain(), clock),
    )
    assert result["state"] == "outcome_unknown"
    assert result["failure_stage"] == "release_proof"
    assert "partial-release" in job._unknown_handles
    assert ledger.publication_calls == 1
    assert [t.held for t in protection.tickets] == [False, True]
    assert not result["writer_session"]["lock_release_proven"]
    original_tickets = tuple(protection.tickets)
    result = job.reconcile("partial-release", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert result["reconciliation"]["lock_release_proven"] is True
    assert journal.get("partial-release") == result
    assert protection.all_released()
    assert [t.release_calls for t in original_tickets] == [1, 2]
    assert [t.release_calls for t in protection.tickets[2:]] == [1, 1]
    assert "partial-release" not in job._unknown_handles


def test_pre_authoritative_partial_release_is_explicit_and_never_proves_release():
    p, ledger, kernel, store, _ = bindings(fail=2)
    protection = PartialReleaseProtection()
    proof = []
    with pytest.raises(StatementProtectionReleaseFailure) as error:
        materialize(
            p, kernel, store, protection, protection_released=lambda: proof.append(True)
        )
    assert not ledger._records and ledger.publication_calls == 0
    assert not proof
    assert [t.held for t in protection.tickets] == [False, True]
    error.value.retry_release()
    assert protection.all_released()
    assert [t.release_calls for t in protection.tickets] == [1, 2]
    assert not proof


@pytest.mark.parametrize("raw", POLLUTED_STATEMENTS)
def test_polluted_raw_cannot_build_persist_publish_or_read(raw):
    p, ledger, kernel, store, protection = bindings()
    c = statement.candidate_from_content(p.content)
    polluted = replace(c, reads=(c.reads[0], replace(c.reads[1], raw=raw)))
    with pytest.raises(statement.DocumentError):
        statement.build_live_plan(
            polluted,
            identity="polluted",
            finished_at=p.run.finished_at,
            frozen_at=p.version.frozen_at,
        )
    doc = json.loads(p.content)
    doc["raw"][1] = {
        "base64": base64.b64encode(raw).decode(),
        "sha256": digest(raw),
        "size": len(raw),
    }
    content = statement._encode(doc)
    forged = replace(
        p, content=content, version=replace(p.version, content_digest=digest(content))
    )
    forged = statement.durable_plan(
        forged, tuple(m.controlled_ref for m in p.raw_materials)
    )
    for gate in (statement.persist_before_publication, statement.publish):
        with pytest.raises(statement.DocumentError):
            gate(kernel, forged)
    assert not ledger._records and ledger.publication_calls == 0
    result = materialize(p, kernel, store, protection)
    # Simulate a retained legacy publication with internally matching carrier
    # digests. Local Read must reparse the HTML before admitting its lineage.
    for record in (
        replace(result.version, content_digest=digest(content)),
        replace(result.plan.semantic_material, digest=digest(content)),
    ):
        ledger._records[
            (record.key.kind.value, record.key.identity, record.key.revision)
        ] = codec.dumps(record)
    ledger._contents[p.version.identity] = (digest(content), content)
    with pytest.raises(StatementReadRejected):
        read_local_statement(ledger, as_of=AT + timedelta(seconds=20))


def test_partial_release_then_callback_failure_keeps_all_release_progress():
    p, ledger, kernel, store, _ = bindings()
    protection = PartialReleaseProtection()
    callbacks = []

    def released():
        assert protection.all_released()
        callbacks.append(True)
        if len(callbacks) == 1:
            raise OSError("synthetic proof failure")

    with pytest.raises(StatementMaterializationOutcomeUnknown) as error:
        materialize(p, kernel, store, protection, protection_released=released)
    handle = error.value
    with pytest.raises(OSError, match="proof failure"):
        handle.reconcile(copy.deepcopy(ledger))
    assert protection.all_released() and handle.tickets_already_released
    assert [t.release_calls for t in protection.tickets] == [1, 2]
    assert handle.reconcile(copy.deepcopy(ledger)) == "confirmed_published"
    assert [t.release_calls for t in protection.tickets] == [1, 2]
    assert callbacks == [True, True]


class ReconcileReadbackPartialReleaseProtection(Protection):
    def acquire(self, identity):
        ticket = super().acquire(identity)
        index = len(self.tickets)
        original = ticket.release
        ticket.release_calls = 0

        def release(other):
            ticket.release_calls += 1
            if index == 4 and ticket.release_calls == 1:
                raise OSError("synthetic readback release failure")
            original(other)

        ticket.release = release
        return ticket


def test_reconcile_readback_partial_release_never_confirms_while_ticket_held(tmp_path):
    job, journal, ledger, store, _, clock = job_bindings(tmp_path, "after")
    protection = ReconcileReadbackPartialReleaseProtection()
    job.protection = protection
    job.submit("readback-partial-release", StatementJobSpec())
    result = job.run(
        "readback-partial-release",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectStatementAcquirer(Source(), Retain(), clock),
    )
    assert result["state"] == "outcome_unknown"
    assert "readback-partial-release" in job._unknown_handles

    first = job.reconcile("readback-partial-release", enabled=True)
    assert first["reconciliation"]["outcome"] == "unresolved"
    assert first["classification"] == "readback_protection_release_incomplete"
    assert "readback-partial-release" in job._readback_release_handles
    original_readback_tickets = tuple(protection.tickets[2:4])
    assert [ticket.held for ticket in original_readback_tickets] == [False, True]

    second = job.reconcile("readback-partial-release", enabled=True)
    assert "readback-partial-release" not in job._readback_release_handles
    assert second["reconciliation"]["outcome"] == "confirmed_published"
    assert second["reconciliation"]["lock_release_proven"] is True
    assert all(not ticket.held for ticket in original_readback_tickets)
    assert [ticket.release_calls for ticket in original_readback_tickets] == [1, 2]
    assert protection.all_released()
    assert ledger.publication_calls == 1


REAL_CALENDAR_SHA256 = (
    "326c98e372bf6ab01b4ba6b7c88214296e6983e4cde743bec3120f3e68037fc7"
)
REAL_STATEMENT_SHA256 = (
    "d82280a92adada1f904b25895fdf96de5f7789d785195cc796f008186218b7f9"
)


@pytest.mark.parametrize(
    ("sha", "size"),
    [
        (REAL_CALENDAR_SHA256, 167856),
        (REAL_STATEMENT_SHA256, 84741),
        (REAL_CALENDAR_SHA256, MAX_BODY_BYTES),
    ],
)
def test_statement_material_identity_admits_contract_sizes(sha, size):
    identity = StatementMaterialIdentity(sha, size)
    assert isinstance(identity, MaterialIdentity)
    assert identity.key == MaterialIdentity(sha, 1).key
    assert identity.locator == MaterialIdentity(sha, 1).locator
    assert (
        StatementMaterialIdentity.from_locator(
            identity.locator, expected_sha256=sha, expected_size=size
        )
        == identity
    )


@pytest.mark.parametrize("size", [MAX_BODY_BYTES + 1, 0, -1, True, 1.0])
def test_statement_material_identity_rejects_invalid_size(size):
    with pytest.raises(ContractError, match="^statement_material_size_invalid$"):
        StatementMaterialIdentity(REAL_CALENDAR_SHA256, size)


def test_statement_identity_preserves_base_cpi_size_boundary():
    with pytest.raises(ContractError, match="^material_size_invalid$"):
        MaterialIdentity(REAL_CALENDAR_SHA256, 167856)


@pytest.mark.parametrize(
    ("kwargs", "classification"),
    [
        ({"sha256": "A" * 64}, "material_digest_invalid"),
        ({"bucket": "other"}, "material_bucket_not_admitted"),
        ({"key": "other"}, "material_key_not_admitted"),
    ],
)
def test_statement_identity_reuses_base_address_allowlist(kwargs, classification):
    args = {"sha256": REAL_CALENDAR_SHA256, "size": 167856} | kwargs
    with pytest.raises(ContractError, match=f"^{classification}$"):
        StatementMaterialIdentity(**args)
    identity = StatementMaterialIdentity(REAL_CALENDAR_SHA256, 167856)
    with pytest.raises(ContractError, match="^material_locator_not_admitted$"):
        StatementMaterialIdentity.from_locator(
            identity.locator + "/other",
            expected_sha256=identity.sha256,
            expected_size=identity.size,
        )


@pytest.mark.parametrize("publication_failure", [None, "before"])
def test_real_sized_retained_local_plan_and_reconcile(
    tmp_path, monkeypatch, publication_failure
):
    # Synthetic valid HTML padded to retained real body sizes; no provider IO.
    monkeypatch.setitem(globals(), "CALENDAR", CALENDAR.ljust(167856, b" "))
    monkeypatch.setitem(globals(), "STATEMENT", STATEMENT.ljust(84741, b" "))
    job, journal, ledger, store, protection, clock = job_bindings(
        tmp_path, publication_failure
    )
    c = candidate(clock)
    p = statement.build_live_plan(
        c, identity="real-sized", finished_at=clock(), frozen_at=clock()
    )
    raw = tuple(r.raw for r in c.reads)
    assert tuple(map(len, raw)) == (167856, 84741)
    statement.validate_live_plan(p, raw)
    identities = tuple(StatementMaterialIdentity(digest(b), len(b)) for b in raw)
    spec = StatementJobSpec(
        mode="retained",
        max_provider_requests=0,
        expected_raw_sha256=tuple(i.sha256 for i in identities),
    )
    job.submit("real-sized", spec)
    result = job.run("real-sized", enabled=True, preflight=lambda _: None, retained=c)
    assert "provider_requests_possible" not in result["effects"]
    assert tuple(i["locator"] for i in result["raw"]) == tuple(
        i.locator for i in identities
    )
    assert store.creates == list(identities)
    if publication_failure:
        assert result["state"] == "outcome_unknown"
        result = job.reconcile("real-sized", enabled=True)
        assert result["reconciliation"]["outcome"] == "confirmed_not_published"
        restarted = StatementLocalJob(
            StatementJobJournal(journal.directory),
            clock,
            job.kernel,
            store,
            protection,
            job.readback,
            job.commit,
        )
        result = restarted.reconcile("real-sized", enabled=True)
        assert result["reconciliation"]["raw_state"] == "verified"
        assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    else:
        assert result["state"] == "succeeded"
        view = read_local_statement(copy.deepcopy(ledger), as_of=clock())
        assert (
            tuple(r.raw for r in statement.candidate_from_content(view.content).reads)
            == raw
        )
    assert protection.all_released()
