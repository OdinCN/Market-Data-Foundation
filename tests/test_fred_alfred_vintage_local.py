"""T06 Stage-2 synthetic multi-raw fault matrix; no external service IO."""

import copy
import json
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Grade, digest, require
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import fred_alfred_vintage as vintage
from market_data_foundation.historical import fred_alfred_vintage_live as live
from market_data_foundation.historical.bls_cpi_material_store import (
    MaterialWriteOutcomeUnknown,
    WriteDisposition,
)
from market_data_foundation.historical.vintage_contract import VINTAGE_BUNDLE
from market_data_foundation.historical.vintage_local_job import (
    DEV_SCHEMA,
    DirectVintageAcquirer,
    VintageJobJournal,
    VintageJobSpec,
    VintageLocalJob,
)
from market_data_foundation.historical.vintage_local_read import (
    VintageReadRejected,
    read_local_vintage,
)
from market_data_foundation.historical.vintage_materialization import (
    VintageMaterializationOutcomeUnknown,
    materialize_persist_publish,
)

AT = datetime(2026, 10, 17, tzinfo=UTC)
VINTAGES = ("2026-09-30", "2026-08-26")


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
        key = (request.series, request.endpoint)
        self.counts[key] = self.counts.get(key, 0) + 1
        if self.failure:
            return live.Response(401, b"no")
        if self.retry and self.counts[key] == 1:
            return live.Response(503, b"retry")
        if request.endpoint == "vintagedates":
            document = {"vintage_dates": VINTAGES, "series": request.series}
        else:
            document = {
                "realtime_start": VINTAGES[1],
                "realtime_end": VINTAGES[0],
                "observation_start": "2025-01-01",
                "observation_end": "2026-12-31",
                "units": "lin",
                "output_type": 2,
                "file_type": "json",
                "order_by": "observation_date",
                "sort_order": "asc",
                "observations": [
                    {
                        "date": "2025-01-01",
                        f"{request.series}_20260930": "123.4500",
                        f"{request.series}_20260826": ".",
                    }
                ],
                "series": request.series,
            }
        if request.endpoint == "observations" and request.series == "PAYEMS":
            document["observations"].append(
                {"date": "2026-09-01", "PAYEMS_20260930": "234.5000"}
            )
        return live.Response(200, json.dumps(document).encode())


class Retain:
    def retain(self, raw, artifact):
        return live.ArtifactPaths(
            f"artifacts/read-{artifact.logical_read}/raw.json",
            f"artifacts/read-{artifact.logical_read}/request.json",
        )


def candidate(clock=None):
    return live.qualify_vintages(
        request_source=Source(retry=True),
        artifact_store=Retain(),
        live_opt_in=True,
        clock=clock or Clock(),
        sleep=lambda _: None,
    )


def plan(identity="test", at=AT):
    c = candidate(Clock(at))
    return vintage.build_live_plan(
        c,
        identity=identity,
        finished_at=at + timedelta(seconds=1),
        frozen_at=at + timedelta(seconds=2),
    )


def raws(p):
    return tuple(r.raw for r in vintage.candidate_from_content(p.content).reads)


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


def test_plan_exact_four_raw_bundle():
    p = plan()
    vintage.validate_live_plan(p, raws(p))
    assert p.run.budget == 4
    assert len(p.acquisitions) == len(p.raw_materials) == 4
    assert len(p.version.materials) == 5
    assert [a.provider_data_attempts for a in p.acquisitions] == [2] * 4
    assert p.run.started_at == min(a.requested_at for a in p.acquisitions)
    assert len(set(a.request_identity for a in p.acquisitions)) == 4
    assert all(not m.durable and not m.replayable for m in p.raw_materials)
    assert p.publication_request.evidence == p.version.materials
    assert p.publication_request.claimed_grades == frozenset(
        {Grade.ORIGINAL, Grade.PROJECTED}
    )
    assert not p.binding_candidate.silent_fallback
    assert not VINTAGE_BUNDLE.source_qualified
    doc = json.loads(p.content)
    assert "api_key" not in p.content.decode()
    assert all(
        type(v) is str for values in doc["selected_vintages"].values() for v in values
    )
    assert len(p.version.finite_scope.members) == 5
    assert "PAYEMS|2026-09-30|2026-09-01" in p.version.finite_scope.members
    assert "PAYEMS|2026-08-26|2026-09-01" not in p.version.finite_scope.members
    assert sum(row["unavailable"] for row in doc["observations"]) == 2
    assert "PAYEMS|2026-08-26|2025-01-01" in p.version.finite_scope.members


@pytest.mark.parametrize("index", range(4))
@pytest.mark.parametrize("axis", ["raw", "request", "timing", "selection", "attempts"])
def test_plan_tamper_fails(index, axis):
    p = plan()
    doc = json.loads(p.content)
    if axis == "raw":
        doc["raw"][index]["sha256"] = "0" * 64
    elif axis == "request":
        doc["requests"][index]["parameters"]["api_key"] = "forbidden"
    elif axis == "timing":
        doc["requests"][index]["requested_at"] = (AT - timedelta(seconds=1)).isoformat()
    elif axis == "selection":
        doc["selected_vintages"]["GDP" if index < 2 else "PAYEMS"][0] = "2026-10-01"
    else:
        doc["requests"][index]["attempts"] = 4
    with pytest.raises((ValueError, ContractError)):
        vintage.validate_live_plan(replace(p, content=vintage._encode(doc)), raws(p))


def test_four_creates_readbacks_and_callback_order():
    p, ledger, kernel, store, protection = bindings()
    proof = []

    def released():
        assert protection.all_released()
        proof.append(True)

    result = materialize(p, kernel, store, protection, protection_released=released)
    assert len(store.creates) == len(store.reads) == len(result.verification) == 4
    assert all(m.durable and m.replayable for m in result.plan.raw_materials)
    assert ledger.publication_calls == 1 and proof == [True]
    assert all(v.disposition == WriteDisposition.CREATED for v in result.verification)


@pytest.mark.parametrize("index", range(1, 5))
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


@pytest.mark.parametrize("index", range(1, 5))
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

    with pytest.raises(VintageMaterializationOutcomeUnknown) as error:
        materialize(
            p,
            kernel,
            store,
            protection,
            authoritative_commit=commit,
            protection_released=lambda: proof.append(protection.all_released()),
        )
    handle = error.value
    assert len(handle.tickets) == 4 and all(
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
    assert len(protection.tickets) == 4 and proof == [True]
    assert ledger.publication_calls == 0


def test_exact_as_of_read_rebuild_and_timing():
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)
    at = AT + timedelta(seconds=20)
    exact = read_local_vintage(ledger, as_of=at, version_identity=p.version.identity)
    assert exact == read_local_vintage(ledger, as_of=at)
    assert [a.obtained_at for a in exact.acquisitions] == [
        a.obtained_at for a in p.acquisitions
    ]
    assert [a.requested_at for a in exact.acquisitions] == [
        a.requested_at for a in p.acquisitions
    ]
    assert all(a.provider_data_attempts == 2 for a in exact.acquisitions)
    assert exact.content == result.plan.content
    assert len(store.reads) == 4  # Local Read does not touch Garage.


@pytest.mark.parametrize("index", range(4))
def test_local_read_rejects_acquisition_tamper(index):
    p, ledger, kernel, store, protection = bindings()
    materialize(p, kernel, store, protection)
    a = p.acquisitions[index]
    bad = replace(a, provider_data_attempts=3)
    ledger._records[(a.key.kind.value, a.key.identity, a.key.revision)] = codec.dumps(
        bad
    )
    with pytest.raises(VintageReadRejected):
        read_local_vintage(ledger, as_of=AT + timedelta(seconds=20))


def test_as_of_latest_and_equal_ambiguity():
    p, ledger, kernel, store, protection = bindings()
    materialize(p, kernel, store, protection)
    p2 = plan("newer", AT + timedelta(seconds=3))
    materialize(p2, kernel, store, protection)
    assert (
        read_local_vintage(ledger, as_of=AT + timedelta(seconds=20)).version
        == p2.version
    )
    p3 = plan("equal", AT + timedelta(seconds=3))
    materialize(p3, kernel, store, protection)
    with pytest.raises(ReadContractError) as error:
        read_local_vintage(ledger, as_of=AT + timedelta(seconds=20))
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
    journal = VintageJobJournal(tmp_path / "journal")

    @contextmanager
    def readback():
        yield copy.deepcopy(ledger)

    def commit():
        if commit_failure:
            raise RuntimeError("commit")

    job = VintageLocalJob(journal, clock, kernel, store, protection, readback, commit)
    return job, journal, ledger, store, protection, clock


def test_job_acquire_four_logical_reads_with_retries_and_no_repeat(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("acquire", VintageJobSpec())
    source = Source(retry=True)
    acquirer = DirectVintageAcquirer(source, Retain(), clock)
    result = job.run(
        "acquire", enabled=True, preflight=lambda _: None, acquirer=acquirer
    )
    assert result["state"] == "succeeded"
    assert len(source.calls) == 8
    assert result["effects"]["provider_requests_possible"] == 4
    assert result["effects"]["provider_requests_returned"] == 4
    assert result["effects"]["raw_creates_possible"] == 4
    assert len(result["raw"]) == 4 and store.creates and protection.all_released()
    assert result["writer_session"]["lock_release_proven"] is True
    assert (
        job.run("acquire", enabled=True, preflight=lambda _: None, acquirer=acquirer)
        == result
    )
    assert len(source.calls) == 8 and ledger.publication_calls == 1


@pytest.mark.parametrize("cancel", [False, True])
def test_job_failure_cancel_no_source_repeat(tmp_path, cancel):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    job.submit("stopped", VintageJobSpec())
    if cancel:
        journal.cancel("stopped", clock())
    source = Source(failure=True)
    acquirer = DirectVintageAcquirer(source, Retain(), clock)
    result = job.run(
        "stopped", enabled=True, preflight=lambda _: None, acquirer=acquirer
    )
    assert result["state"] == ("cancelled" if cancel else "failed")
    job.run("stopped", enabled=True, preflight=lambda _: None, acquirer=acquirer)
    assert len(source.calls) == (0 if cancel else 1)
    assert not store.creates and ledger.publication_calls == 0


def test_job_unknown_reconcile_durable_proof_restart_repair_zero_provider(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path, "before")
    job.submit("original", VintageJobSpec())
    source = Source()
    result = job.run(
        "original",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectVintageAcquirer(source, Retain(), clock),
    )
    assert result["state"] == "outcome_unknown" and not protection.all_released()
    with pytest.raises(ContractError):
        job.repair("original", "too-early", enabled=True)
    # A fresh process has no synthetic release proof and must refuse reconciliation.
    restarted = VintageLocalJob(
        VintageJobJournal(journal.directory),
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
    c = vintage.candidate_from_content(codec.loads(result["plan"]["content"]))
    ledger.publication_failure = None
    done = restarted.run("repair", enabled=True, preflight=lambda _: None, retained=c)
    assert done["state"] == "succeeded"
    assert done["plan"] == result["plan"] and done["raw"] == result["raw"]
    assert len(source.calls) == 4
    assert journal.get("original")["state"] == "outcome_unknown"


@pytest.mark.parametrize("index", range(1, 5))
@pytest.mark.parametrize("state", ["absent", "mismatch"])
def test_unknown_put_requires_each_exact_raw(index, state):
    p, ledger, kernel, store, protection = bindings(unknown=index, **{state: index})
    with pytest.raises(ContractError):
        materialize(p, kernel, store, protection)
    assert not ledger._records and ledger.publication_calls == 0
    assert protection.all_released()


def test_unknown_requires_independent_ledger_and_exact_content_before_release():
    p, ledger, kernel, store, protection = bindings()
    with pytest.raises(VintageMaterializationOutcomeUnknown) as error:
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
    with pytest.raises(VintageMaterializationOutcomeUnknown) as error:
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

    with pytest.raises(VintageMaterializationOutcomeUnknown) as error:
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


@pytest.mark.parametrize("axis", ["raw", "request", "timing", "selection", "metadata"])
def test_local_read_rebuild_rejects_self_consistent_content_digest_tamper(axis):
    p, ledger, kernel, store, protection = bindings()
    result = materialize(p, kernel, store, protection)
    doc = json.loads(p.content)
    if axis == "raw":
        doc["raw"][3]["base64"] = doc["raw"][1]["base64"]
    elif axis == "request":
        doc["requests"][3]["parameters"]["units"] = "chg"
    elif axis == "timing":
        doc["requests"][1]["obtained_at"] = (AT + timedelta(seconds=1)).isoformat()
    elif axis == "selection":
        doc["selected_vintages"]["PAYEMS"][0] = "2026-10-01"
    else:
        doc["metadata"][0]["first_party_release"] = True
    content = vintage._encode(doc)
    changed = replace(p.version, content_digest=digest(content))
    semantic = replace(result.plan.semantic_material, digest=digest(content))
    for record in (changed, semantic):
        ledger._records[
            (record.key.kind.value, record.key.identity, record.key.revision)
        ] = codec.dumps(record)
    ledger._contents[changed.identity] = (digest(content), content)
    with pytest.raises(VintageReadRejected):
        read_local_vintage(ledger, as_of=AT + timedelta(seconds=20))


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
    job.submit("unknown", VintageJobSpec())
    source = Source()
    job.run(
        "unknown",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectVintageAcquirer(source, Retain(), clock),
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
    restarted = VintageLocalJob(
        VintageJobJournal(journal.directory),
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
    assert len(source.calls) == 4


def test_synthetic_restart_after_pre_authoritative_failure_has_proof(tmp_path):
    job, journal, ledger, store, protection, clock = job_bindings(tmp_path)
    store.fail = 3
    job.submit("partial", VintageJobSpec())
    result = job.run(
        "partial",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectVintageAcquirer(Source(), Retain(), clock),
    )
    assert result["state"] == "outcome_unknown"
    assert result["writer_session"]["lock_release_proven"] is True
    restarted = VintageLocalJob(
        VintageJobJournal(journal.directory),
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
    job.submit("original", VintageJobSpec())
    job.run(
        "original",
        enabled=True,
        preflight=lambda _: None,
        acquirer=DirectVintageAcquirer(Source(), Retain(), clock),
    )
    job.reconcile("original", enabled=True)
    job.repair("original", "repair", enabled=True)
    with pytest.raises(ContractError, match="scope"):
        job.repair("original", "second", enabled=True)
    # Same raw bytes with different retained acquisition timings must fail.
    changed = candidate(clock)
    result = job.run("repair", enabled=True, preflight=lambda _: None, retained=changed)
    assert result["state"] == "failed"
    assert ledger.publication_calls == 1 and len(store.creates) == 4


def test_acquirer_meter_failure_is_terminal_without_source_retry():
    source = Source()
    acquirer = DirectVintageAcquirer(source, Retain(), Clock())

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
        ("series", ("GDP",)),
        ("reference", "2026-09"),
        ("mode", "other"),
        ("max_publications", 2),
        ("max_ledger_handoffs", 2),
        ("max_provider_requests", True),
    ],
)
def test_job_fixed_spec_gate(field, value):
    with pytest.raises(ContractError):
        replace(VintageJobSpec(), **{field: value})
