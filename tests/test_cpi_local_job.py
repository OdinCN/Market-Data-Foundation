"""Deterministic local-first vertical; every external client is a fake."""

import json
import socket
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from test_p4_bls_cpi_direct import encode, response, september_response

from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Key, Kind, digest
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.bls_cpi_direct import (
    AUGUST_REFERENCE_CONTRACT,
    MAX_RESPONSE_BYTES,
    SEPTEMBER_REFERENCE_CONTRACT,
    SERIES_IDS,
    build_offline_replay_plan,
    persist_offline_replay,
    publish_offline_replay,
)
from market_data_foundation.historical.bls_cpi_direct_live import (
    TIMEOUT_SECONDS,
    HttpResult,
)
from market_data_foundation.historical.bls_cpi_material_store import (
    GarageCredentials,
    GarageMaterialStore,
    InMemoryMaterialStore,
    InMemoryWriterProtection,
    MaterialIdentity,
    PostgresAdvisoryWriterProtection,
    WireResponse,
)
from market_data_foundation.historical.bls_cpi_release_runner import (
    RELEASE,
    SLOT_IDENTITY,
)
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.cpi_local_job import (
    _PG_SESSION_OWNERS,
    CONTRACT,
    DEV_SCHEMA,
    QUALITY,
    CpiJobSpec,
    CpiLocalJob,
    DirectCpiAcquirer,
    _decode_plan,
    _encode_plan,
)
from market_data_foundation.historical.cpi_local_read import (
    CpiReadRejected,
    read_local_cpi,
)
from market_data_foundation.historical.cpi_local_status import local_cpi_status

NOW = RELEASE + timedelta(seconds=10)
RAW = encode(september_response())


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("deterministic CPI tests attempted external IO")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def spec(**changes):
    return replace(
        CpiJobSpec(
            definition=BLS_CPI_HEADLINES.identity,
            definition_revision=BLS_CPI_HEADLINES.revision,
            series=SERIES_IDS,
            reference="2026-09",
            source_identity=CONTRACT.source_identity,
            source_revision=CONTRACT.contract_revision,
            provider_revision="not_declared",
            completeness=CONTRACT.completeness,
            quality=QUALITY,
            release_slot=SLOT_IDENTITY,
            mode="acquire",
            max_provider_requests=1,
            max_response_bytes=MAX_RESPONSE_BYTES,
            timeout_seconds=TIMEOUT_SECONDS,
            effect_domain="synthetic",
        ),
        **changes,
    )


class FakeTransport:
    def __init__(self, outcome=None, before=None):
        self.outcome = outcome or HttpResult(200, RAW, {})
        self.before = before
        self.calls = 0

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        self.calls += 1
        assert timeout == TIMEOUT_SECONDS and max_response_bytes == MAX_RESPONSE_BYTES
        if self.before:
            self.before()
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


class Rig:
    def __init__(
        self,
        path,
        *,
        ledger=None,
        store=None,
        protection=None,
        readback=None,
        commit=None,
    ):
        self.path = path
        path.mkdir(parents=True, exist_ok=True)
        self.journal = FileJobJournal(path / "journal")
        self.ledger = ledger if ledger is not None else InMemoryLedger()
        self.store = store if store is not None else InMemoryMaterialStore()
        self.protection = protection or InMemoryWriterProtection()
        self.commits = 0

        @contextmanager
        def independent():
            yield InMemoryLedger.from_snapshot(self.ledger.snapshot())

        def confirmed_commit():
            self.commits += 1

        self.engine = CpiLocalJob(
            self.journal,
            lambda: NOW,
            CanonicalKernel(self.ledger, lambda: NOW),
            self.store,
            self.protection,
            readback or independent,
            commit or confirmed_commit,
        )
        self.transport = FakeTransport()
        # Synthetic credential only, never the repository/host secret.
        key = path / "synthetic-key.toml"
        key.write_text('[bls]\napi_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n')
        self.acquirer = DirectCpiAcquirer(
            self.transport, key, path / "artifacts", lambda: NOW
        )

    def submit(self, job="first", target=None):
        return self.engine.submit(job, target or spec())

    def run(self, job="first", **kwargs):
        return self.engine.run(
            job,
            enabled=True,
            preflight=lambda _spec: None,
            acquirer=self.acquirer,
            **kwargs,
        )


def test_source_to_verified_raw_version_publication_consumer_wire_and_restart(tmp_path):
    rig = Rig(tmp_path)
    pending = rig.submit()
    assert pending["state"] == "pending" and pending["attempts"] == 0
    assert pending["spec"]["max_provider_requests"] == 1
    assert pending["spec"]["max_publications"] == 1

    def check_persisted_intent():
        durable = FileJobJournal(tmp_path / "journal", read_only=True).get("first")
        assert durable["attempt_id"] == "first-a1"
        assert durable["effects"]["provider_request_possible"] == 1
        assert rig.store.create_attempts == 0

    rig.transport.before = check_persisted_intent
    result = rig.run()
    assert result["state"] == "succeeded"
    assert rig.transport.calls == rig.store.create_attempts == rig.commits == 1
    assert rig.store.reads == 2  # core verification, then independent raw readback
    assert result["effects"]["raw_state"] == "verified"
    assert result["reconciliation"]["evidence"]["known_at"] == NOW.isoformat()
    assert result["reconciliation"]["evidence"]["usable_at"] == NOW.isoformat()
    restarted = InMemoryLedger.from_snapshot(rig.ledger.snapshot())
    version = Key(
        Kind.VERSION, result["reconciliation"]["evidence"]["version_identity"]
    )
    exact = read_exact_version(restarted, BLS_CPI_HEADLINES.key, version)
    assert read_as_of(restarted, BLS_CPI_HEADLINES.key, as_of=NOW) == exact
    assert len(exact.version.finite_scope.members) == 4
    assert "candidate_only_not_active" in exact.binding.limitations
    assert FileJobJournal(tmp_path / "journal", read_only=True).get("first") == result
    history = rig.journal.history("first")
    assert history[0]["state"] == "pending" and history[-1]["state"] == "succeeded"
    assert any(item["stage"] == "ledger_commit" for item in history)


def test_duplicate_job_identical_result_and_scope_rejection(tmp_path):
    rig = Rig(tmp_path)
    rig.submit()
    succeeded = rig.run()
    assert rig.submit() == rig.run() == succeeded
    assert rig.transport.calls == rig.store.create_attempts == 1
    with pytest.raises(ContractError, match="job_scope_already_registered"):
        rig.submit("another")
    with pytest.raises(ContractError, match="job_identity_reused"):
        rig.submit(target=spec(expected_raw_sha256=digest(RAW)))
    with pytest.raises(ContractError, match="job_state_regression"):
        rig.journal._update("first", NOW, state="running")


@pytest.mark.parametrize(
    "change",
    [
        {"source_revision": "new-revision"},
        {"provider_revision": "r2"},
        {"source_identity": "another-provider"},
        {"reference": "2026-10"},
        {"series": SERIES_IDS[:1]},
        {"quality": ("projected",)},
        {"completeness": "partial"},
        {"definition_revision": "future"},
        {"release_slot": "invented"},
        {"max_provider_requests": 2},
        {"max_raw_creates": 2},
        {"timeout_seconds": 30},
    ],
)
def test_unknown_revision_scope_and_budget_reject_before_registration(change):
    with pytest.raises(ContractError):
        spec(**change)


def test_opt_in_and_preflight_before_any_client_or_secret(tmp_path):
    rig = Rig(tmp_path)
    rig.submit()
    rig.acquirer.secret_path.unlink()
    with pytest.raises(ContractError, match="explicit_opt_in"):
        rig.engine.run("first", preflight=lambda _spec: pytest.fail("preflight called"))
    assert rig.journal.get("first")["state"] == "pending"

    def denied(_spec):
        raise OSError("private endpoint and credential must never appear")

    result = rig.engine.run(
        "first", enabled=True, preflight=denied, acquirer=rig.acquirer
    )
    assert (
        result["state"] == "failed" and result["classification"] == "preflight_rejected"
    )
    assert result["failure_stage"] == "preflight"
    assert result["effects"] == {} and rig.transport.calls == 0
    assert "credential" not in json.dumps(rig.journal.history("first"))
    assert rig.store.create_attempts == 0


def test_preflight_cancel_and_cancel_during_acquisition(tmp_path):
    rig = Rig(tmp_path / "pending")
    rig.submit()
    rig.journal.cancel("first", NOW)
    assert rig.run()["state"] == "cancelled"
    assert rig.transport.calls == 0
    rig = Rig(tmp_path / "acquiring")
    rig.submit()
    rig.transport.before = lambda: rig.journal.cancel("first", NOW)
    result = rig.run()
    assert result["state"] == "cancelled"
    assert result["effects"]["provider_request_possible"] == 1
    assert rig.store.create_attempts == 0
    assert rig.run() == result


def test_cancel_after_raw_write_cannot_create_publication(tmp_path):
    class CancelAfterRaw(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            result = super().create_if_absent(identity, payload)
            rig.journal.cancel("first", NOW)
            return result

    rig = Rig(tmp_path, store=CancelAfterRaw())
    rig.submit()
    result = rig.run()
    assert result["state"] == "outcome_unknown"
    recovered = rig.engine.reconcile("first", enabled=True)
    assert recovered["reconciliation"]["outcome"] == "confirmed_not_published"
    assert recovered["reconciliation"]["raw_state"] == "verified"
    assert rig.ledger.versions_for_definition(BLS_CPI_HEADLINES.key) == ()
    assert rig.transport.calls == rig.store.create_attempts == 1


def test_garage_real_adapter_fake_wire_and_raw_outcome_unknown(tmp_path):
    class Wire:
        def __init__(self):
            self.calls = []
            self.responses = iter(
                [
                    WireResponse(404, b""),
                    WireResponse(503, b""),
                    WireResponse(200, RAW),
                    WireResponse(200, RAW),
                ]
            )

        def send(self, request):
            self.calls.append(request.method)
            return next(self.responses)

    wire = Wire()
    store = GarageMaterialStore(
        GarageCredentials("synthetic", "synthetic"), transport=wire
    )
    rig = Rig(tmp_path, store=store)
    rig.submit()
    result = rig.run()
    assert result["state"] == "succeeded"
    assert wire.calls == ["GET", "PUT", "GET", "GET"]
    assert rig.transport.calls == rig.commits == 1


def test_raw_success_pg_failure_never_publishes_or_retries(tmp_path):
    class FailedLedger(InMemoryLedger):
        def put_content(self, version, content):
            raise OSError("do not persist this raw exception")

    rig = Rig(tmp_path, ledger=FailedLedger())
    rig.submit()
    failed = rig.run()
    assert failed["state"] == "outcome_unknown"
    assert rig.store.create_attempts == 1
    recovered = rig.engine.reconcile("first", enabled=True)
    assert recovered["state"] == "outcome_unknown"
    assert recovered["reconciliation"]["outcome"] == "confirmed_not_published"
    assert recovered["reconciliation"]["raw_state"] == "verified"
    assert rig.ledger.versions_for_definition(BLS_CPI_HEADLINES.key) == ()
    assert rig.run() == recovered
    assert rig.transport.calls == rig.store.create_attempts == 1


def test_unknown_pg_commit_confirmed_version_still_not_publication(tmp_path):
    def lost_commit_ack():
        raise OSError("private raw exception")

    rig = Rig(tmp_path, commit=lost_commit_ack)
    rig.submit()
    unknown = rig.run()
    assert unknown["state"] == "outcome_unknown"
    assert unknown["classification"] == "ledger_commit_outcome_unknown"
    result = rig.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert result["reconciliation"]["version_present"] is True
    version = _decode_plan(result["plan"]).version
    with pytest.raises(ReadContractError, match="unpublished_or_unavailable_as_of"):
        read_exact_version(rig.ledger, BLS_CPI_HEADLINES.key, version.key)
    assert rig.transport.calls == rig.store.create_attempts == 1


def test_unknown_publication_commit_exact_independent_readback(tmp_path):
    class UnknownPublication(InMemoryLedger):
        def publish(self, request, clock):
            super().publish(request, clock)
            raise OSError("lost ack")

    rig = Rig(tmp_path, ledger=UnknownPublication())
    rig.submit()
    unknown = rig.run()
    assert unknown["state"] == "outcome_unknown"
    assert unknown["classification"] == "publication_outcome_unknown"
    recovered = rig.engine.reconcile("first", enabled=True)
    assert recovered["state"] == "outcome_unknown"  # never rewrite history
    assert recovered["reconciliation"]["outcome"] == "confirmed_published"
    assert rig.transport.calls == rig.store.create_attempts == 1
    status = local_cpi_status(rig.journal, at=NOW, ledger=rig.ledger)
    assert status["freshness"]["latest_successful_job"]["job_id"] == "first"


def test_failed_independent_readback_then_recovery_preserves_publication(tmp_path):
    @contextmanager
    def unavailable():
        raise OSError("private PG failure")
        yield

    rig = Rig(tmp_path, readback=unavailable)
    rig.submit()
    unknown = rig.run()
    assert unknown["state"] == "outcome_unknown"
    snapshot = rig.ledger.snapshot()
    fresh = Rig(
        tmp_path, ledger=InMemoryLedger.from_snapshot(snapshot), store=rig.store
    )
    result = fresh.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert fresh.ledger.snapshot() == snapshot
    assert fresh.transport.calls == 0 and rig.store.create_attempts == 1


class Crash(BaseException):
    pass


@pytest.mark.parametrize("stage", ["source", "raw", "commit", "publication"])
def test_interrupted_process_state_requires_reconciliation_without_reexecution(
    tmp_path, stage
):
    class CrashStore(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            result = super().create_if_absent(identity, payload)
            if stage == "raw":
                raise Crash()
            return result

    class CrashLedger(InMemoryLedger):
        def publish(self, request, clock):
            result = super().publish(request, clock)
            if stage == "publication":
                raise Crash()
            return result

    def commit():
        if stage == "commit":
            raise Crash()

    rig = Rig(tmp_path, ledger=CrashLedger(), store=CrashStore(), commit=commit)
    if stage == "source":
        rig.transport.outcome = Crash()
    rig.submit()
    with pytest.raises(Crash):
        rig.run()
    assert rig.journal.get("first")["state"] == "running"
    fresh = Rig(
        tmp_path,
        ledger=InMemoryLedger.from_snapshot(rig.ledger.snapshot()),
        store=rig.store,
        protection=InMemoryWriterProtection(),
    )
    with pytest.raises(ContractError, match="restart_reconciliation_required"):
        fresh.run()
    result = fresh.engine.reconcile("first", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["classification"] == "interrupted_attempt"
    assert (
        result["reconciliation"]["outcome"]
        == {
            "source": "unresolved",
            "raw": "confirmed_not_published",
            "commit": "confirmed_not_published",
            "publication": "confirmed_published",
        }[stage]
    )
    assert fresh.transport.calls == 0 and rig.transport.calls == 1
    assert rig.store.create_attempts == (0 if stage == "source" else 1)


def test_lost_material_lock_is_unknown_no_publication(tmp_path):
    class Gap(InMemoryWriterProtection):
        def acquire(self, identity):
            ticket = super().acquire(identity)
            ticket.force_gap_for_counterexample()
            return ticket

    rig = Rig(tmp_path, protection=Gap())
    rig.submit()
    result = rig.run()
    assert result["state"] == "outcome_unknown" and rig.store.create_attempts == 0
    fresh = Rig(tmp_path, ledger=rig.ledger, store=rig.store)
    result = fresh.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"


def test_retained_candidate_no_current_provider_and_invalid_plan_rejected(tmp_path):
    rig = Rig(tmp_path / "origin")
    candidate = rig.acquirer.acquire("original", lambda: None)
    retained_spec = spec(
        mode="retained", max_provider_requests=0, expected_raw_sha256=digest(RAW)
    )
    rig = Rig(tmp_path / "retained")
    rig.submit(target=retained_spec)
    result = rig.engine.run(
        "first", enabled=True, preflight=lambda _spec: None, retained=candidate
    )
    assert result["state"] == "succeeded" and rig.transport.calls == 0
    assert "provider_request_possible" not in result["effects"]
    invalid = replace(
        candidate,
        plan=replace(
            candidate.plan,
            publication_request=replace(
                candidate.plan.publication_request, evidence=()
            ),
        ),
    )
    rig = Rig(tmp_path / "invalid")
    rig.submit(target=retained_spec)
    result = rig.engine.run(
        "first", enabled=True, preflight=lambda _spec: None, retained=invalid
    )
    assert result["state"] == "failed" and rig.store.create_attempts == 0
    assert rig.ledger.versions_for_definition(BLS_CPI_HEADLINES.key) == ()


def test_partial_reference_source_rejected_no_raw_or_publication(tmp_path):
    response = september_response()
    response["Results"]["series"][0]["data"] = [
        row
        for row in response["Results"]["series"][0]["data"]
        if not (row["year"] == "2026" and row["period"] == "M09")
    ]
    rig = Rig(tmp_path)
    rig.transport.outcome = HttpResult(200, encode(response), {})
    rig.submit()
    result = rig.run()
    assert result["state"] == "failed"
    assert result["classification"] == "bls_live_response_contract_failed"
    assert rig.store.create_attempts == 0 and rig.commits == 0


def test_existing_publication_unchanged_by_later_source_failure_and_future_asof(
    tmp_path,
):
    rig = Rig(tmp_path / "old")
    rig.submit()
    success = rig.run()
    snapshot = rig.ledger.snapshot()
    exact_key = Key(
        Kind.VERSION, success["reconciliation"]["evidence"]["version_identity"]
    )
    with pytest.raises(ReadContractError, match="unpublished_or_unavailable_as_of"):
        read_exact_version(
            rig.ledger,
            BLS_CPI_HEADLINES.key,
            exact_key,
            as_of=NOW - timedelta(microseconds=1),
        )
    with pytest.raises(ReadContractError, match="as_of_no_version"):
        read_as_of(
            rig.ledger, BLS_CPI_HEADLINES.key, as_of=NOW - timedelta(microseconds=1)
        )
    later = Rig(tmp_path / "later", ledger=rig.ledger)
    later.submit("later")
    later.transport.outcome = OSError("private provider failure")
    assert later.run("later")["state"] == "failed"
    assert later.ledger.snapshot() == snapshot
    assert read_exact_version(later.ledger, BLS_CPI_HEADLINES.key, exact_key).content


def test_multiple_publications_asof_ambiguity_is_not_latest_wins(tmp_path):
    rig = Rig(tmp_path / "old")
    rig.submit()
    result = rig.run()
    old_version = Key(
        Kind.VERSION, result["reconciliation"]["evidence"]["version_identity"]
    )
    old_exact = read_exact_version(rig.ledger, BLS_CPI_HEADLINES.key, old_version)
    later = Rig(tmp_path / "later", ledger=rig.ledger)
    later.submit("later")
    result = later.run("later")
    assert result["state"] == "outcome_unknown"
    assert result["classification"] == "as_of_ambiguous"
    assert (
        read_exact_version(rig.ledger, BLS_CPI_HEADLINES.key, old_version) == old_exact
    )
    assert (
        local_cpi_status(later.journal, at=NOW, ledger=rig.ledger)["availability"]
        == "as_of_ambiguous"
    )
    assert (
        later.engine.reconcile("later", enabled=True)["reconciliation"]["outcome"]
        == "unresolved"
    )


def test_invalid_publication_and_missing_metadata_do_not_pass_readback(tmp_path):
    rig = Rig(tmp_path)
    rig.submit()
    assert rig.run()["state"] == "succeeded"
    original = rig.ledger

    class InvalidPublication(InMemoryLedger):
        def publications_for_version(self, key):
            return tuple(
                replace(item, usable_at=item.decision_at - timedelta(seconds=1))
                for item in original.publications_for_version(key)
            )

    corrupt = InvalidPublication.from_snapshot(original.snapshot())
    request = {
        "wire_revision": "mdf.consumer.read.v1",
        "mode": "as_of",
        "definition": {
            "kind": Kind.DEFINITION.value,
            "identity": BLS_CPI_HEADLINES.identity,
            "revision": BLS_CPI_HEADLINES.revision,
        },
        "as_of": NOW.isoformat(),
    }
    assert (
        json.loads(handle_request(corrupt, json.dumps(request).encode()))[
            "classification"
        ]
        == "publication_state_invalid"
    )
    assert (
        json.loads(handle_request(InMemoryLedger(), json.dumps(request).encode()))[
            "classification"
        ]
        == "definition_not_found"
    )
    assert rig.transport.calls == rig.store.create_attempts == 1


def test_status_readonly_no_provider_no_job_update_no_raw_secret_print(tmp_path):
    rig = Rig(tmp_path)
    rig.submit()
    rig.run()
    before = rig.journal.history("first")
    status = local_cpi_status(
        FileJobJournal(tmp_path / "journal", read_only=True), at=NOW, ledger=rig.ledger
    )
    assert status["availability"] == "available"
    assert status["freshness"]["next_proven_official_slot"] is None
    assert status["freshness"]["provider_revision"] == "not_declared"
    assert status["freshness"]["provider_actual_released_at"] is None
    assert status["freshness"]["ttl"] is None
    assert status["journal_backend"] == "single_host_file_not_pg_job_state"
    assert len(status["coverage"]) == 4
    encoded = json.dumps(status)
    assert "AAAAAAAA" not in encoded and "raw-response.bin" in encoded
    assert '"plan"' not in encoded and '"content"' not in encoded
    assert rig.journal.history("first") == before
    assert rig.transport.calls == 1
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "market_data_foundation.historical.cpi_local_status",
            "--journal",
            str(tmp_path / "journal"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(child.stdout)["availability"] == "not_checked"
    assert rig.journal.history("first") == before


def test_process_lock_and_cancellation_use_durable_shared_journal(tmp_path):
    rig = Rig(tmp_path)
    rig.submit()
    script = """
import sys
from pathlib import Path
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
journal = FileJobJournal(Path(sys.argv[1]))
with journal.executor("first"):
    print("held", flush=True)
    input()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(tmp_path / "journal")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout.readline().strip() == "held"
        with pytest.raises(ContractError, match="executor_busy"):
            rig.run()
        with pytest.raises(ContractError, match="scope_already_registered"):
            rig.submit("duplicate")
        rig.journal.cancel("first", NOW)
    finally:
        child.communicate("release\n", timeout=10)
    assert rig.run()["state"] == "cancelled"
    assert rig.transport.calls == 0


def test_schema_is_exact_and_production_names_rejected():
    assert validate_schema(DEV_SCHEMA) == DEV_SCHEMA
    with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
        validate_schema("production")
    # Constructor needs no connection or SQL; no private resource accessed.
    assert PostgresLedger(None, DEV_SCHEMA).schema == DEV_SCHEMA


def test_confirmed_history_survives_later_reconciliation_failure(tmp_path):
    class UnknownPublication(InMemoryLedger):
        def publish(self, request, clock):
            super().publish(request, clock)
            raise OSError("lost ack")

    rig = Rig(tmp_path, ledger=UnknownPublication())
    rig.submit()
    rig.run()
    first = rig.engine.reconcile("first", enabled=True)
    assert first["last_success"] is not None

    @contextmanager
    def unavailable():
        raise OSError("private")
        yield

    rig.engine.readback = unavailable
    later = rig.engine.reconcile("first", enabled=True)
    assert later["reconciliation"]["outcome"] == "unresolved"
    assert later["last_success"] == first["last_success"]
    status = local_cpi_status(rig.journal, at=NOW)
    assert status["freshness"]["latest_successful_job"]["job_id"] == "first"
    assert any(
        event["reconciliation"]
        and event["reconciliation"]["outcome"] == "confirmed_published"
        for event in rig.journal.history("first")
    )


def test_unavailable_raw_write_remains_unknown_and_recovery_never_puts(tmp_path):
    from market_data_foundation.historical.bls_cpi_material_store import (
        MaterialWriteOutcomeUnknown,
    )

    class UnknownRaw(InMemoryMaterialStore):
        def create_if_absent(self, identity, payload):
            self.create_attempts += 1
            raise MaterialWriteOutcomeUnknown()

    rig = Rig(tmp_path, store=UnknownRaw())
    rig.submit()
    result = rig.run()
    assert result["state"] == "outcome_unknown"
    assert result["effects"]["raw_state"] == "outcome_unknown"
    assert rig.ledger.versions_for_definition(BLS_CPI_HEADLINES.key) == ()
    result = rig.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert result["reconciliation"]["raw_state"] == "absent"
    assert rig.store.create_attempts == 1


def test_reconciliation_cannot_accept_missing_lineage_or_raw(tmp_path):
    @contextmanager
    def initial_failure():
        raise OSError("failed independent read")
        yield

    rig = Rig(tmp_path, readback=initial_failure)
    rig.submit()
    assert rig.run()["state"] == "outcome_unknown"
    snapshot = rig.ledger.snapshot()

    class MissingLineage(InMemoryLedger):
        def get(self, key):
            if key.kind == Kind.SOURCE:
                return None
            return super().get(key)

    @contextmanager
    def missing_lineage_readback():
        yield MissingLineage.from_snapshot(snapshot)

    fresh = Rig(
        tmp_path, ledger=rig.ledger, store=rig.store, readback=missing_lineage_readback
    )
    assert (
        fresh.engine.reconcile("first", enabled=True)["reconciliation"]["outcome"]
        == "unresolved"
    )
    # The canonical graph alone is insufficient if the durable raw read fails.
    fresh = Rig(tmp_path, ledger=InMemoryLedger.from_snapshot(snapshot))
    assert (
        fresh.engine.reconcile("first", enabled=True)["reconciliation"]["outcome"]
        == "unresolved"
    )
    assert rig.store.create_attempts == 1


def test_retained_old_evidence_never_backdates_publication(tmp_path):
    origin = Rig(tmp_path / "origin")
    candidate = origin.acquirer.acquire("old", lambda: None)
    rig = Rig(tmp_path / "later")
    later = NOW + timedelta(hours=2)
    rig.engine.clock = lambda: later
    rig.engine.kernel = CanonicalKernel(rig.ledger, lambda: later)
    rig.submit(
        target=spec(
            mode="retained", max_provider_requests=0, expected_raw_sha256=digest(RAW)
        )
    )
    result = rig.engine.run(
        "first", enabled=True, preflight=lambda _spec: None, retained=candidate
    )
    assert result["state"] == "succeeded"
    assert result["last_success"]["evidence"]["known_at"] == NOW.isoformat()
    assert result["last_success"]["evidence"]["usable_at"] == later.isoformat()
    with pytest.raises(ReadContractError, match="as_of_no_version"):
        read_as_of(rig.ledger, BLS_CPI_HEADLINES.key, as_of=NOW)
    assert rig.transport.calls == 0


def test_cpi_reference_guard_miss_stale_partial_not_admitted_without_effects(tmp_path):
    from market_data_foundation.historical.cpi_local_read import (
        CpiReadRejected,
        read_local_cpi,
    )

    rig = Rig(tmp_path)
    rig.submit()
    rig.run()
    before = rig.journal.history("first")
    assert read_local_cpi(rig.ledger, reference="2026-09", as_of=NOW)
    with pytest.raises(CpiReadRejected, match="reference_stale_or_mismatched"):
        read_local_cpi(rig.ledger, reference="2026-08", as_of=NOW)
    with pytest.raises(CpiReadRejected, match="reference_stale_or_mismatched"):
        read_local_cpi(rig.ledger, reference="2026-10", as_of=NOW)
    with pytest.raises(CpiReadRejected, match="reference_not_admitted"):
        read_local_cpi(rig.ledger, reference="2026-13", as_of=NOW)
    with pytest.raises(ReadContractError, match="definition_not_found"):
        read_local_cpi(InMemoryLedger(), reference="2026-09", as_of=NOW)
    # Use a separately qualified partial version to prove reference-level guard.
    from market_data_foundation.canonical.model import FiniteScope
    from market_data_foundation.historical.bls_cpi_material_store import (
        materialize_persist_publish,
    )

    candidate = rig.acquirer.acquire("partial", lambda: None)
    plan = replace(
        candidate.plan,
        version=replace(
            candidate.plan.version,
            finite_scope=FiniteScope(("2026-09:bls:cpi:core_mom",)),
        ),
    )
    ledger = InMemoryLedger()
    materialize_persist_publish(
        CanonicalKernel(ledger, lambda: NOW),
        plan,
        RAW,
        InMemoryMaterialStore(),
        InMemoryWriterProtection(),
    )
    with pytest.raises(CpiReadRejected, match="coverage_partial_or_mismatched"):
        read_local_cpi(ledger, reference="2026-09", as_of=NOW)
    assert rig.journal.history("first") == before
    assert rig.store.create_attempts == 1


def test_concurrent_scope_submit_has_one_winner(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    rig = Rig(tmp_path)

    def submit(identity):
        try:
            return rig.submit(identity)["state"]
        except ContractError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(submit, ("one", "two")))
    assert sorted(outcomes) == ["pending", "rejected"]
    assert len(rig.journal.list_jobs()) == 1
    assert rig.transport.calls == 0


def test_new_process_reconciles_after_actual_exit_without_a_second_source_or_put(
    tmp_path,
):
    import os
    from pathlib import Path

    environment = os.environ | {
        "PYTHONPATH": str(Path.cwd() / "tests") + ":" + str(Path.cwd() / "src")
    }
    crash_script = """
import os,sys
from pathlib import Path
from test_cpi_local_job import Rig
root = Path(sys.argv[1])
rig = Rig(root)
rig.submit()
original = rig.ledger.publish
def publish(request, clock):
    publication = original(request, clock)
    (root / "fake-ledger.snapshot").write_text(rig.ledger.snapshot())
    os._exit(23)
rig.ledger.publish = publish
rig.run()
"""
    child = subprocess.run(
        [sys.executable, "-c", crash_script, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert child.returncode == 23, child.stderr
    journal = FileJobJournal(tmp_path / "journal", read_only=True)
    assert journal.get("first")["state"] == "running"
    recover_script = """
import json,sys
from pathlib import Path
from test_cpi_local_job import Rig, RAW
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.canonical.model import digest
from market_data_foundation.historical.bls_cpi_material_store import (
    InMemoryMaterialStore, MaterialIdentity,
)
root = Path(sys.argv[1])
# Restore isolated fake snapshots only. No PG/S3 connection exists here.
ledger = InMemoryLedger.from_snapshot((root / "fake-ledger.snapshot").read_text())
store = InMemoryMaterialStore()
store.create_if_absent(MaterialIdentity(digest(RAW), len(RAW)), RAW)
rig = Rig(root, ledger=ledger, store=store)
result = rig.engine.reconcile("first", enabled=True)
assert rig.transport.calls == 0 and store.create_attempts == 1
print(json.dumps({"state": result["state"],
                  "outcome": result["reconciliation"]["outcome"]}))
"""
    child = subprocess.run(
        [sys.executable, "-c", recover_script, str(tmp_path)],
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=True,
    )
    assert json.loads(child.stdout) == {
        "state": "outcome_unknown",
        "outcome": "confirmed_published",
    }
    assert journal.get("first")["attempts"] == 1


def test_readonly_status_missing_journal_creates_nothing(tmp_path):
    missing = tmp_path / "absent"
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "market_data_foundation.historical.cpi_local_status",
            "--journal",
            str(missing),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert json.loads(child.stdout) == {"classification": "journal_unavailable"}
    assert not missing.exists()


def test_recovery_opens_final_snapshot_only_after_material_protection(tmp_path):
    @contextmanager
    def unavailable():
        raise OSError("unavailable")
        yield

    rig = Rig(tmp_path, readback=unavailable)
    rig.submit()
    assert rig.run()["state"] == "outcome_unknown"
    openings = []

    @contextmanager
    def protected_snapshot():
        assert rig.protection._identity is not None
        openings.append("protected")
        yield InMemoryLedger.from_snapshot(rig.ledger.snapshot())

    rig.engine.readback = protected_snapshot
    result = rig.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_published"
    assert openings == ["protected"]
    assert rig.transport.calls == rig.store.create_attempts == 1


def test_host_seam_requires_dedicated_target_and_distinct_pg_connection(tmp_path):
    from types import SimpleNamespace

    rig = Rig(tmp_path)
    connection = SimpleNamespace(info=SimpleNamespace(host="192.168.10.4"))
    writer = PostgresLedger(connection, DEV_SCHEMA)

    def engine(ledger):
        return CpiLocalJob(
            rig.journal,
            lambda: NOW,
            CanonicalKernel(ledger, lambda: NOW),
            rig.store,
            rig.protection,
            rig.engine.readback,
            lambda: None,
        )

    host = engine(writer)
    with pytest.raises(ContractError, match="independent_pg_connection_required"):
        host._independent(PostgresLedger(connection, DEV_SCHEMA))
    with pytest.raises(ContractError, match="nonproduction_target_required"):
        engine(PostgresLedger(connection, "mdf_p6_consumer_read_r1"))
    another_host = SimpleNamespace(info=SimpleNamespace(host="production.invalid"))
    with pytest.raises(ContractError, match="nonproduction_target_required"):
        engine(PostgresLedger(another_host, DEV_SCHEMA))
    host._independent(PostgresLedger(SimpleNamespace(info=connection.info), DEV_SCHEMA))


class FakePgConnection:
    """Transaction effects only; no SQL engine or external connection."""

    def __init__(self, status=TransactionStatus.IDLE):
        self.info = SimpleNamespace(host="192.168.10.4", transaction_status=status)
        self.closed = False
        self.pgconn = object()
        self.info.backend_pid = 100
        self.commits = self.rollbacks = self.closes = 0
        self.statements = []
        self.locks = set()
        self.ambient_work = (
            ["caller work"] if status is TransactionStatus.INTRANS else []
        )

    def execute(self, statement, parameters=()):
        self.statements.append(statement)
        self.info.transaction_status = TransactionStatus.INTRANS
        result = True
        if "pg_try_advisory_lock" in statement:
            self.locks.add(parameters)
        elif "pg_advisory_unlock" in statement:
            result = parameters in self.locks
            self.locks.discard(parameters)
        elif "pg_locks" in statement:
            result = parameters in self.locks
        return SimpleNamespace(fetchone=lambda: (result,))

    def commit(self):
        self.commits += 1
        self.ambient_work.clear()
        self.info.transaction_status = TransactionStatus.IDLE

    def rollback(self):
        self.rollbacks += 1
        self.ambient_work.clear()
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self):
        self.closes += 1
        self.closed = True

    @contextmanager
    def transaction(self):
        assert self.info.transaction_status is TransactionStatus.IDLE
        try:
            yield
        except Exception:
            self.rollback()
            raise
        else:
            self.commit()


class FakePgLedger(InMemoryLedger, PostgresLedger):
    """Exercise PG admission/protection over a deterministic canonical ledger."""

    def __init__(self):
        InMemoryLedger.__init__(self)
        PostgresLedger.__init__(self, FakePgConnection(), DEV_SCHEMA)


def pg_rig(path, *, ledger=None, store=None):
    ledger = ledger if ledger is not None else FakePgLedger()

    @contextmanager
    def independent():
        yield FakePgLedger.from_snapshot(ledger.snapshot())

    return Rig(
        path,
        ledger=ledger,
        store=store,
        protection=PostgresAdvisoryWriterProtection(ledger.connection),
        readback=independent,
        commit=ledger.connection.commit,
    )


def planned_pg_recovery(path, *, state="outcome_unknown"):
    """R2 restart counterexample: typed plan, no live core handle or effects."""
    origin = Rig(path / "fixture")
    candidate = origin.acquirer.acquire("fixture", lambda: None)
    rig = pg_rig(path / "job")
    rig.submit(target=spec(effect_domain="owner_nonproduction"))
    rig.engine._update(
        "first",
        state="running",
        plan=_encode_plan(candidate.plan),
        raw={
            "sha256": digest(RAW),
            "size": len(RAW),
            "locator": MaterialIdentity(digest(RAW), len(RAW)).locator,
        },
    )
    rig.engine._update("first", state=state)
    return rig


@pytest.mark.parametrize("state", ["running", "outcome_unknown"])
@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.INTRANS,
        TransactionStatus.INERROR,
        TransactionStatus.ACTIVE,
        TransactionStatus.UNKNOWN,
        None,
        0,
    ],
)
def test_pg_reconcile_rejects_r2_ambient_writer_before_any_effect(
    tmp_path, state, status
):
    rig = planned_pg_recovery(tmp_path, state=state)
    connection = rig.ledger.connection
    connection.info.transaction_status = status
    connection.ambient_work = ["caller input must survive"]
    before = rig.journal.get("first"), rig.journal.history("first")
    with pytest.raises(ContractError, match="idle_pg_writer_required"):
        rig.engine.reconcile("first", enabled=True)
    assert (rig.journal.get("first"), rig.journal.history("first")) == before
    assert connection.ambient_work == ["caller input must survive"]
    assert connection.info.transaction_status is status
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == []
    assert rig.transport.calls == rig.store.create_attempts == rig.store.reads == 0


@pytest.mark.parametrize(
    "violation", ["domain", "schema", "host", "protection", "closed"]
)
def test_pg_reconcile_checks_owner_before_restart_transition(tmp_path, violation):
    rig = planned_pg_recovery(tmp_path, state="running")
    connection = rig.ledger.connection
    if violation == "domain":
        rig.engine._update("first", spec=spec().document())
    elif violation == "schema":
        rig.ledger.schema = "mdf_p6_consumer_read_r1"
    elif violation == "host":
        connection.info.host = "production.invalid"
    elif violation == "closed":
        connection.closed = True
    else:
        rig.engine.protection = PostgresAdvisoryWriterProtection(FakePgConnection())
    before = rig.journal.get("first"), rig.journal.history("first")
    with pytest.raises(ContractError):
        rig.engine.reconcile("first", enabled=True)
    assert (rig.journal.get("first"), rig.journal.history("first")) == before
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == []
    assert rig.transport.calls == rig.store.create_attempts == rig.store.reads == 0


def material_unknown_pg_rig(path, *, published=False):
    origin = Rig(path / "fixture")
    candidate = origin.acquirer.acquire("fixture", lambda: None)
    rig = pg_rig(path / "job")
    rig.submit(
        target=spec(
            effect_domain="owner_nonproduction",
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=digest(RAW),
        )
    )

    def lost_ack():
        rig.ledger.connection.commit()
        raise OSError("synthetic lost ack")

    if published:

        def lost_publication_ack(request, clock):
            InMemoryLedger.publish(rig.ledger, request, clock)
            raise OSError("synthetic publication ack loss")

        rig.ledger.publish = lost_publication_ack
    else:
        rig.engine.commit = lost_ack
    assert (
        rig.engine.run(
            "first", enabled=True, preflight=lambda _spec: None, retained=candidate
        )["state"]
        == "outcome_unknown"
    )
    assert "first" in rig.engine._unknowns
    assert rig.ledger.connection.locks
    assert rig.transport.calls == 0
    return rig


def pg_effects(rig):
    connection = rig.ledger.connection
    return (
        connection.commits,
        connection.rollbacks,
        connection.closes,
        tuple(connection.statements),
        rig.transport.calls,
        rig.store.create_attempts,
        rig.store.reads,
    )


@pytest.mark.parametrize(
    "status", [TransactionStatus.INTRANS, TransactionStatus.INERROR]
)
def test_pg_materialization_r3_session_swap_preserves_caller_work(
    tmp_path, monkeypatch, status
):
    """Same production control flow reproduces R3's pre-release rollback gap."""
    fixture = Rig(tmp_path / "fixture")
    candidate = fixture.acquirer.acquire("fixture", lambda: None)
    rig = pg_rig(tmp_path / "job")
    connection = rig.ledger.connection
    rig.submit(
        target=spec(
            effect_domain="owner_nonproduction",
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=digest(RAW),
        )
    )
    before = None
    inner_releases = 0

    def failing_store(_identity, _payload):
        nonlocal before
        rig.store.create_attempts += 1
        owner = rig.engine._pg_owners["first"]
        assert owner.materializing and owner.ticket.held
        assert connection.info.transaction_status is TransactionStatus.IDLE
        release = owner.ticket.ticket.release

        def inner_release(identity):
            nonlocal inner_releases
            inner_releases += 1
            return release(identity)

        monkeypatch.setattr(owner.ticket.ticket, "release", inner_release)
        connection.pgconn = object()
        connection.info.backend_pid += 1
        connection.locks.clear()  # The replacement session owns no old lock.
        connection.info.transaction_status = status
        connection.ambient_work = ["replacement session caller work"]
        before = pg_effects(rig)
        raise OSError("synthetic store failure")

    monkeypatch.setattr(rig.store, "create_if_absent", failing_store)
    result = rig.engine.run(
        "first", enabled=True, preflight=lambda _spec: None, retained=candidate
    )
    after = pg_effects(rig)
    print(
        json.dumps(
            {
                "status": status.name,
                "rollback_delta": after[1] - before[1],
                "commit_delta": after[0] - before[0],
                "close_delta": after[2] - before[2],
                "sql_delta": len(after[3]) - len(before[3]),
                "inner_releases": inner_releases,
                "caller_work": connection.ambient_work,
                "result": result["state"],
            },
            sort_keys=True,
        )
    )
    assert result["state"] == "outcome_unknown" and result["last_success"] is None
    assert (
        rig.ledger.publication_by_occurrence(
            candidate.plan.publication_request.occurrence
        )
        is None
    )
    assert connection.ambient_work == ["replacement session caller work"]
    assert connection.info.transaction_status is status
    assert after == before and inner_releases == 0
    assert rig.transport.calls == 0 and rig.store.create_attempts == 1
    owner = rig.engine._pg_owners["first"]
    assert not owner.valid and owner.ticket.held
    # Reverting the observable state cannot revive a revoked owner/ticket.
    connection.pgconn = owner.session
    connection.info.backend_pid = owner.backend_pid
    connection.info.transaction_status = TransactionStatus.IDLE
    identity = MaterialIdentity(digest(RAW), len(RAW))
    for operation in ("assert_held", "release", "mark_outcome_unknown"):
        with pytest.raises(ContractError):
            getattr(owner.ticket, operation)(identity)
    assert pg_effects(rig) == after and inner_releases == 0


@pytest.mark.parametrize(
    "operation", ["assert_held", "release", "mark_outcome_unknown"]
)
@pytest.mark.parametrize("materializing", [False, True])
@pytest.mark.parametrize(
    "drift",
    [
        "connection",
        "pgconn",
        "backend_pid",
        "claim_missing",
        "claim_owner",
        "claim_released",
        "protection_connection",
        "engine_protection",
        "job",
        "job_mapping",
        "ticket",
        "released",
        "identity",
        "invalid",
        "closed",
    ],
)
def test_pg_ticket_drift_never_enters_inner_protection(
    tmp_path, monkeypatch, operation, materializing, drift
):
    rig = pg_rig(tmp_path)
    target = spec(effect_domain="owner_nonproduction")
    rig.submit(target=target)
    owner = rig.engine._job_protection("first", target)
    connection = rig.ledger.connection
    identity = MaterialIdentity(digest(RAW), len(RAW))
    ticket = owner.acquire(identity)
    owner.materializing = materializing
    # These guards also catch mark_outcome_unknown, which normally emits no SQL.
    inner_calls = []
    for name in ("assert_held", "release", "mark_outcome_unknown"):
        monkeypatch.setattr(
            ticket.ticket, name, lambda _identity: inner_calls.append(1)
        )

    if drift == "connection":
        rig.ledger.connection = FakePgConnection()
    elif drift == "pgconn":
        connection.pgconn = object()
    elif drift == "backend_pid":
        connection.info.backend_pid += 1
    elif drift == "claim_missing":
        monkeypatch.delitem(_PG_SESSION_OWNERS, connection)
    elif drift == "claim_owner":
        monkeypatch.setitem(_PG_SESSION_OWNERS, connection, (lambda: None, True))
    elif drift == "claim_released":
        claim = _PG_SESSION_OWNERS[connection]
        monkeypatch.setitem(_PG_SESSION_OWNERS, connection, (claim[0], False))
    elif drift == "protection_connection":
        owner.protection.connection = FakePgConnection()
    elif drift == "engine_protection":
        rig.engine.protection = PostgresAdvisoryWriterProtection(FakePgConnection())
    elif drift == "job":
        owner.job_id = "other-job"
    elif drift == "job_mapping":
        rig.engine._pg_owners["first"] = object()
    elif drift == "ticket":
        owner.ticket = object()
    elif drift == "released":
        ticket.held = False
    elif drift == "identity":
        identity = MaterialIdentity(digest(b"other"), 5)
    elif drift == "invalid":
        owner.valid = False
    elif drift == "closed":
        connection.closed = True
    connection.info.transaction_status = TransactionStatus.INERROR
    connection.ambient_work = ["caller work after ownership drift"]
    before = pg_effects(rig)
    original_effects = (
        connection.commits,
        connection.rollbacks,
        connection.closes,
        tuple(connection.statements),
    )
    with pytest.raises(ContractError):
        getattr(ticket, operation)(identity)
    assert not owner.valid and not inner_calls
    assert pg_effects(rig) == before
    assert (
        connection.commits,
        connection.rollbacks,
        connection.closes,
        tuple(connection.statements),
    ) == original_effects
    assert connection.ambient_work == ["caller work after ownership drift"]


@pytest.mark.parametrize(
    "operation", ["assert_held", "release", "mark_outcome_unknown"]
)
@pytest.mark.parametrize(
    ("materializing", "status"),
    [
        (False, TransactionStatus.INTRANS),
        (False, TransactionStatus.INERROR),
        (False, TransactionStatus.ACTIVE),
        (False, TransactionStatus.UNKNOWN),
        (True, TransactionStatus.ACTIVE),
        (True, TransactionStatus.UNKNOWN),
    ],
)
def test_pg_ticket_unowned_transaction_status_never_enters_inner(
    tmp_path, monkeypatch, operation, materializing, status
):
    rig = pg_rig(tmp_path)
    target = spec(effect_domain="owner_nonproduction")
    rig.submit(target=target)
    owner = rig.engine._job_protection("first", target)
    identity = MaterialIdentity(digest(RAW), len(RAW))
    ticket = owner.acquire(identity)
    owner.materializing = materializing
    inner_calls = []
    monkeypatch.setattr(
        ticket.ticket, operation, lambda _identity: inner_calls.append(1)
    )
    connection = rig.ledger.connection
    connection.info.transaction_status = status
    connection.ambient_work = ["caller work outside admitted core transaction"]
    before = pg_effects(rig)
    with pytest.raises(ContractError):
        getattr(ticket, operation)(identity)
    assert not owner.valid and not inner_calls and pg_effects(rig) == before
    assert connection.info.transaction_status is status
    assert connection.ambient_work == ["caller work outside admitted core transaction"]


@pytest.mark.parametrize(
    "operation", ["assert_held", "release", "mark_outcome_unknown"]
)
def test_pg_inner_failure_permanently_revokes_ticket(tmp_path, monkeypatch, operation):
    rig = pg_rig(tmp_path)
    target = spec(effect_domain="owner_nonproduction")
    rig.submit(target=target)
    owner = rig.engine._job_protection("first", target)
    identity = MaterialIdentity(digest(RAW), len(RAW))
    ticket = owner.acquire(identity)
    owner.materializing = True

    def failed_inner(_identity):
        raise OSError("synthetic protection acknowledgement loss")

    monkeypatch.setattr(ticket.ticket, operation, failed_inner)
    with pytest.raises(OSError):
        getattr(ticket, operation)(identity)
    assert not owner.valid and ticket.held
    before = pg_effects(rig)
    for name in ("assert_held", "release", "mark_outcome_unknown"):
        monkeypatch.setattr(
            ticket.ticket, name, lambda _identity: pytest.fail("revoked ticket used")
        )
        with pytest.raises(ContractError):
            getattr(ticket, name)(identity)
    assert pg_effects(rig) == before


@pytest.mark.parametrize(
    "boundary",
    [
        "store_return",
        "commit_return",
        "commit_raise",
        "publish_return",
        "publish_raise",
    ],
)
@pytest.mark.parametrize(
    "status", [TransactionStatus.INTRANS, TransactionStatus.INERROR]
)
def test_pg_materialization_detects_swap_on_success_and_unknown_paths(
    tmp_path, monkeypatch, boundary, status
):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    rig.submit(target=spec(effect_domain="owner_nonproduction"))
    before = None
    inner_calls = []

    def swap_session():
        nonlocal before
        inner = rig.engine._pg_owners["first"].ticket.ticket
        for operation in ("assert_held", "release", "mark_outcome_unknown"):
            monkeypatch.setattr(
                inner, operation, lambda _identity: inner_calls.append(1)
            )
        connection.pgconn = object()
        connection.info.backend_pid += 1
        connection.locks.clear()
        connection.info.transaction_status = status
        connection.ambient_work = ["caller work after core callback"]
        before = pg_effects(rig)

    if boundary == "store_return":
        create = rig.store.create_if_absent

        def store_return(identity, payload):
            result = create(identity, payload)
            swap_session()
            return result

        monkeypatch.setattr(rig.store, "create_if_absent", store_return)
    elif boundary.startswith("commit"):

        def commit():
            connection.commit()
            swap_session()
            if boundary == "commit_raise":
                raise OSError("synthetic commit acknowledgement loss")

        rig.engine.commit = commit
    else:
        publish = rig.ledger.publish

        def publication(request, clock):
            result = publish(request, clock)
            swap_session()
            if boundary == "publish_raise":
                raise OSError("synthetic publication acknowledgement loss")
            return result

        monkeypatch.setattr(rig.ledger, "publish", publication)

    result = rig.run()
    assert result["state"] == "outcome_unknown" and result["last_success"] is None
    assert not result["effects"].get("publication_returned", False)
    assert not inner_calls and pg_effects(rig) == before
    assert connection.ambient_work == ["caller work after core callback"]
    assert connection.info.transaction_status is status
    owner = rig.engine._pg_owners["first"]
    assert not owner.valid and owner.ticket.held
    assert not owner.ticket.ticket.outcome_unknown  # Inner mark was never reached.
    with pytest.raises(ContractError):
        rig.engine.reconcile("first", enabled=True)
    assert pg_effects(rig) == before


@pytest.mark.parametrize(
    "failure",
    [
        None,
        "write_intrans",
        "write_inerror",
        "commit_intrans",
        "commit_inerror",
        "publication",
    ],
)
def test_pg_core_owned_transactions_complete_or_remain_reconcilable(
    tmp_path, monkeypatch, failure
):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    rig.submit(target=spec(effect_domain="owner_nonproduction"))
    put = rig.ledger.put
    saw_write_transaction = False
    saw_publication_transaction = False
    commit_failure_effects = None

    def write(record):
        nonlocal saw_write_transaction
        assert rig.engine._pg_owners["first"].materializing
        connection.info.transaction_status = TransactionStatus.INTRANS
        connection.ambient_work = ["job canonical writes"]
        saw_write_transaction = True
        if failure in {"write_intrans", "write_inerror"}:
            if failure == "write_inerror":
                connection.info.transaction_status = TransactionStatus.INERROR
            raise OSError("synthetic job write failure")
        return put(record)

    monkeypatch.setattr(rig.ledger, "put", write)

    def commit():
        nonlocal commit_failure_effects
        assert saw_write_transaction
        assert connection.info.transaction_status is TransactionStatus.INTRANS
        if failure in {"commit_intrans", "commit_inerror"}:
            # Same job/session, acknowledgement unknown with a failed transaction.
            if failure == "commit_inerror":
                connection.info.transaction_status = TransactionStatus.INERROR
            commit_failure_effects = pg_effects(rig)
            raise OSError("synthetic job commit failure")
        connection.commit()

    def publish(request, clock):
        nonlocal saw_publication_transaction
        with connection.transaction():
            connection.info.transaction_status = TransactionStatus.INTRANS
            saw_publication_transaction = True
            if failure == "publication":
                connection.info.transaction_status = TransactionStatus.INERROR
                raise OSError("synthetic publication transaction failure")
            return InMemoryLedger.publish(rig.ledger, request, clock)

    rig.engine.commit = commit
    monkeypatch.setattr(rig.ledger, "publish", publish)
    result = rig.run()
    owner = rig.engine._pg_owners["first"]
    assert owner.valid and saw_write_transaction
    assert connection.closes == 0
    if commit_failure_effects is None:
        assert not connection.ambient_work
    if failure is None:
        assert result["state"] == "succeeded" and saw_publication_transaction
        assert not connection.locks and connection.rollbacks == 0
        assert read_local_cpi(rig.ledger, reference="2026-09", as_of=NOW)
    else:
        assert result["state"] == "outcome_unknown" and result["last_success"] is None
        if failure.startswith("write"):
            assert connection.rollbacks == 1 and not connection.locks
            assert not owner.ticket.held and not saw_publication_transaction
        else:
            assert owner.ticket.held and owner.ticket.ticket.outcome_unknown
            assert saw_publication_transaction == (failure == "publication")
            if commit_failure_effects is not None:
                # Once detached, even this same-session INERROR cannot be cleaned
                # by the job. The host must isolate/dispose of it independently.
                assert connection.info.transaction_status is (
                    TransactionStatus.INERROR
                    if failure == "commit_inerror"
                    else TransactionStatus.INTRANS
                )
                assert pg_effects(rig) == commit_failure_effects  # Mark emits no SQL.
                before = pg_effects(rig)
                with pytest.raises(ContractError):
                    rig.engine.reconcile("first", enabled=True)
                assert not owner.valid and pg_effects(rig) == before
            else:
                assert connection.rollbacks == 1
                observed = rig.engine.reconcile("first", enabled=True)
                assert (
                    observed["reconciliation"]["outcome"] == "confirmed_not_published"
                )
                assert not connection.locks


@pytest.mark.parametrize("state", ["running", "outcome_unknown"])
def test_pg_fresh_idle_reconcile_confirms_absence_without_reexecution(tmp_path, state):
    rig = planned_pg_recovery(tmp_path, state=state)
    result = rig.engine.reconcile("first", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"] == {
        "outcome": "confirmed_not_published",
        "raw_state": "absent",
        "version_present": False,
    }
    assert rig.transport.calls == rig.store.create_attempts == 0
    connection = rig.ledger.connection
    assert connection.rollbacks == connection.closes == 0
    assert not connection.locks
    assert connection.info.transaction_status is TransactionStatus.IDLE
    assert FileJobJournal(rig.path / "journal", read_only=True).get("first") == result


@pytest.mark.parametrize("published", [False, True])
def test_pg_owned_idle_unknown_releases_original_and_reacquired_tickets(
    tmp_path, published
):
    rig = material_unknown_pg_rig(tmp_path, published=published)
    ticket = rig.engine._unknowns["first"]._ticket
    puts, source = rig.store.create_attempts, rig.transport.calls
    result = rig.engine.reconcile("first", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert result["reconciliation"]["outcome"] == (
        "confirmed_published" if published else "confirmed_not_published"
    )
    if published:
        assert result["last_success"]["evidence"]["coverage"]
        with rig.engine.readback() as ledger:
            assert read_local_cpi(ledger, reference="2026-09", as_of=NOW)
        assert (
            local_cpi_status(rig.journal, at=NOW, ledger=rig.ledger)["availability"]
            == "available"
        )
    else:
        assert result["reconciliation"]["version_present"] is True
        assert result["last_success"] is None
    assert result["reconciliation"]["raw_state"] == "verified"
    assert not ticket.held and not rig.ledger.connection.locks
    assert rig.engine._unknowns == {}
    assert not rig.engine._pg_owners["first"].ticket.held
    assert (rig.store.create_attempts, rig.transport.calls) == (puts, source)
    assert rig.ledger.connection.rollbacks == rig.ledger.connection.closes == 0
    assert FileJobJournal(rig.path / "journal", read_only=True).get("first") == result


@pytest.mark.parametrize(
    "status", [TransactionStatus.INTRANS, TransactionStatus.INERROR]
)
def test_pg_inprocess_unknown_refuses_ambient_and_revokes_owner(tmp_path, status):
    rig = material_unknown_pg_rig(tmp_path)
    connection = rig.ledger.connection
    connection.info.transaction_status = status
    connection.ambient_work = ["new caller transaction"]
    before = pg_effects(rig), rig.journal.history("first")
    with pytest.raises(ContractError, match="idle_pg_writer_required"):
        rig.engine.reconcile("first", enabled=True)
    assert (pg_effects(rig), rig.journal.history("first")) == before
    assert connection.ambient_work == ["new caller transaction"]
    assert connection.info.transaction_status is status
    # An owner reporting IDLE later does not restore the abandoned handle.
    connection.info.transaction_status = TransactionStatus.IDLE
    with pytest.raises(ContractError, match="protection_owner_lost"):
        rig.engine.reconcile("first", enabled=True)
    assert (pg_effects(rig), rig.journal.history("first")) == before


@pytest.mark.parametrize(
    "loss", ["session", "backend", "connection", "protection", "ledger", "owner_record"]
)
def test_pg_idle_unknown_requires_original_job_ticket_and_session(tmp_path, loss):
    rig = material_unknown_pg_rig(tmp_path)
    connection = rig.ledger.connection
    if loss == "session":
        connection.pgconn = object()
    elif loss == "backend":
        connection.info.backend_pid += 1
    elif loss == "connection":
        rig.ledger.connection = FakePgConnection()
        rig.engine.protection = PostgresAdvisoryWriterProtection(rig.ledger.connection)
    elif loss == "protection":
        rig.engine.protection = PostgresAdvisoryWriterProtection(connection)
    elif loss == "ledger":
        ledger = FakePgLedger.from_snapshot(rig.ledger.snapshot())
        ledger.connection = connection
        rig.engine.kernel = CanonicalKernel(ledger, lambda: NOW)
    else:
        rig.engine._pg_owners.clear()
    before = pg_effects(rig), rig.journal.history("first")
    with pytest.raises(ContractError):
        rig.engine.reconcile("first", enabled=True)
    assert (pg_effects(rig), rig.journal.history("first")) == before
    assert connection.rollbacks == connection.closes == 0
    assert rig.ledger.connection.info.transaction_status is TransactionStatus.IDLE


@pytest.mark.parametrize("published", [False, True])
def test_pg_lost_unknown_lock_is_unresolved_then_new_session_observes(
    tmp_path, published
):
    rig = material_unknown_pg_rig(tmp_path, published=published)
    connection = rig.ledger.connection
    connection.locks.clear()  # Host-confirmed loss model; no automatic re-lock.
    before = pg_effects(rig)
    result = rig.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert not any(
        "pg_try_advisory_lock" in sql for sql in connection.statements[len(before[3]) :]
    )
    assert connection.rollbacks == connection.closes == 0
    fresh = pg_rig(
        rig.path,
        ledger=FakePgLedger.from_snapshot(rig.ledger.snapshot()),
        store=rig.store,
    )
    recovered = fresh.engine.reconcile("first", enabled=True)
    assert recovered["reconciliation"]["outcome"] == (
        "confirmed_published" if published else "confirmed_not_published"
    )
    if published:
        with fresh.engine.readback() as ledger:
            assert read_local_cpi(ledger, reference="2026-09", as_of=NOW)
    else:
        assert recovered["reconciliation"]["version_present"] is True
    assert (rig.store.create_attempts, rig.transport.calls) == (before[5], before[4])
    assert fresh.transport.calls == 0
    assert not fresh.ledger.connection.locks
    assert (
        FileJobJournal(rig.path / "journal", read_only=True).get("first") == recovered
    )


@pytest.mark.parametrize("state", ["running", "outcome_unknown"])
def test_pg_fresh_engine_cannot_reenter_another_engines_unknown_session(
    tmp_path, state
):
    original = material_unknown_pg_rig(tmp_path)
    shared = pg_rig(original.path, ledger=original.ledger, store=original.store)
    if state == "running":
        # An interrupted journal fixture using the same physical writer alias.
        other = planned_pg_recovery(tmp_path / "interrupted", state="running")
        shared.engine.journal = other.journal
    before = pg_effects(shared), shared.engine.journal.history("first")
    with pytest.raises(ContractError, match="session_already_owned"):
        shared.engine.reconcile("first", enabled=True)
    assert (pg_effects(shared), shared.engine.journal.history("first")) == before
    # The original owner remains usable and releases the actual held ticket.
    result = original.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "confirmed_not_published"
    assert not original.ledger.connection.locks


@pytest.mark.parametrize("boundary", ["run", "unknown_exit"])
@pytest.mark.parametrize("loss", ["session", "protection", "intrans", "inerror"])
def test_pg_reacquisition_requires_continuous_owner(
    tmp_path, monkeypatch, boundary, loss
):
    rig = (
        material_unknown_pg_rig(tmp_path)
        if boundary == "unknown_exit"
        else pg_rig(tmp_path)
    )
    connection = rig.ledger.connection
    at_gap = []

    def lose_owner():
        at_gap.append(pg_effects(rig))
        if loss == "session":
            connection.pgconn = object()
        elif loss == "protection":
            rig.engine.protection = PostgresAdvisoryWriterProtection(connection)
        else:
            connection.info.transaction_status = (
                TransactionStatus.INTRANS
                if loss == "intrans"
                else TransactionStatus.INERROR
            )
            connection.ambient_work = ["gap caller work"]

    if boundary == "run":
        rig.submit(target=spec(effect_domain="owner_nonproduction"))
        update = rig.engine._update

        def update_and_lose_owner(*args, **kwargs):
            result = update(*args, **kwargs)
            if kwargs.get("stage") == "independent_readback":
                lose_owner()
            return result

        monkeypatch.setattr(rig.engine, "_update", update_and_lose_owner)
        result = rig.run()
    else:

        @contextmanager
        def lose_after_core_reconciliation():
            yield FakePgLedger.from_snapshot(rig.ledger.snapshot())
            lose_owner()

        rig.engine.readback = lose_after_core_reconciliation
        result = rig.engine.reconcile("first", enabled=True)
    assert result["state"] == "outcome_unknown"
    assert (
        result["reconciliation"] is None
        or result["reconciliation"]["outcome"] == "unresolved"
    )
    assert len(at_gap) == 1
    assert pg_effects(rig) == at_gap[0]  # No SQL, PUT, source, cleanup after loss.
    if loss in {"intrans", "inerror"}:
        assert connection.ambient_work == ["gap caller work"]


def test_pg_abandoned_unknown_engine_does_not_make_its_session_unowned(tmp_path):
    import gc
    import weakref

    original = material_unknown_pg_rig(tmp_path)
    ledger, store, path = original.ledger, original.store, original.path
    engine = weakref.ref(original.engine)
    del original
    gc.collect()
    assert engine() is None
    # A live connection still owns its unknown advisory lock after engine GC.
    shared = pg_rig(path, ledger=ledger, store=store)
    before = pg_effects(shared), shared.journal.history("first")
    with pytest.raises(ContractError, match="session_already_owned"):
        shared.engine.reconcile("first", enabled=True)
    assert (pg_effects(shared), shared.journal.history("first")) == before


def test_pg_acquire_ack_loss_cannot_reenter_the_uncertain_session(tmp_path):
    class LostAcquireAck(PostgresAdvisoryWriterProtection):
        def acquire(self, identity):
            super().acquire(identity)
            raise OSError("synthetic acquire ack loss")

    rig = planned_pg_recovery(tmp_path)
    rig.engine.protection = LostAcquireAck(rig.ledger.connection)
    result = rig.engine.reconcile("first", enabled=True)
    assert result["reconciliation"]["outcome"] == "unresolved"
    assert rig.ledger.connection.locks
    before = pg_effects(rig), rig.journal.history("first")
    with pytest.raises(ContractError, match="protection_owner_lost"):
        rig.engine.reconcile("first", enabled=True)
    assert (pg_effects(rig), rig.journal.history("first")) == before
    assert rig.transport.calls == rig.store.create_attempts == rig.store.reads == 0


@pytest.mark.parametrize(
    "boundary", ["unknown_readback", "final_readback", "success_readback"]
)
@pytest.mark.parametrize(
    "status", [TransactionStatus.INTRANS, TransactionStatus.INERROR]
)
def test_pg_readback_callbacks_cannot_cause_ambient_cleanup(tmp_path, boundary, status):
    rig = (
        material_unknown_pg_rig(tmp_path)
        if boundary == "unknown_readback"
        else planned_pg_recovery(tmp_path)
    )
    connection = rig.ledger.connection

    @contextmanager
    def contaminate():
        connection.info.transaction_status = status
        connection.ambient_work = ["callback caller work"]
        yield FakePgLedger.from_snapshot(rig.ledger.snapshot())

    rig.engine.readback = contaminate
    before = pg_effects(rig)
    if boundary == "success_readback":
        # Separate pending job to exercise run's post-publication reacquisition.
        rig = pg_rig(tmp_path / "success")
        connection = rig.ledger.connection
        rig.submit(target=spec(effect_domain="owner_nonproduction"))
        rig.engine.readback = contaminate
        result = rig.run()
    else:
        result = rig.engine.reconcile("first", enabled=True)
        assert rig.store.create_attempts == before[5]
        assert rig.transport.calls == before[4]
    assert result["state"] == "outcome_unknown"
    if boundary != "success_readback":
        assert result["reconciliation"]["outcome"] == "unresolved"
    assert connection.info.transaction_status is status
    assert connection.ambient_work == ["callback caller work"]
    assert connection.rollbacks == connection.closes == 0
    # No path may revive a contaminated detached ticket merely by seeing IDLE.
    connection.info.transaction_status = TransactionStatus.IDLE
    before_retry = pg_effects(rig), rig.journal.history("first")
    with pytest.raises(ContractError, match="protection_owner_lost"):
        rig.engine.reconcile("first", enabled=True)
    assert (pg_effects(rig), rig.journal.history("first")) == before_retry


@pytest.mark.parametrize(
    "status",
    [
        TransactionStatus.INTRANS,
        TransactionStatus.INERROR,
        TransactionStatus.ACTIVE,
        TransactionStatus.UNKNOWN,
        None,
        0,  # An untyped/unknown value is not psycopg's actual IDLE status.
        "IDLE",
    ],
)
def test_pg_ambient_or_unknown_session_rejected_before_preflight(tmp_path, status):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    connection.info.transaction_status = status
    connection.ambient_work = ["existing uncommitted caller work"]
    before = rig.ledger.snapshot()
    rig.submit(target=spec(effect_domain="owner_nonproduction"))
    result = rig.engine.run(
        "first",
        enabled=True,
        preflight=lambda _spec: pytest.fail("ambient session reached preflight"),
        acquirer=rig.acquirer,
    )
    assert result["state"] == "failed"
    assert result["classification"] == "preflight_rejected"
    assert connection.ambient_work == ["existing uncommitted caller work"]
    assert connection.info.transaction_status is status
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == []  # No protection acquisition/cleanup either.
    assert rig.transport.calls == rig.store.create_attempts == rig.store.reads == 0
    assert not result["effects"].get("canonical_possible")
    assert rig.ledger.snapshot() == before


@pytest.mark.parametrize("unreadable", ["missing", "raises", "closed"])
def test_pg_uninspectable_session_fails_closed(tmp_path, unreadable):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    if unreadable == "missing":
        del connection.info.transaction_status
    elif unreadable == "raises":

        class PrivateInfo:
            host = "192.168.10.4"

            @property
            def transaction_status(self):
                raise OSError("private connection detail")

        connection.info = PrivateInfo()
    else:
        connection.closed = True
    rig.submit(target=spec(effect_domain="owner_nonproduction"))
    result = rig.engine.run(
        "first",
        enabled=True,
        preflight=lambda _spec: pytest.fail("unknown session reached preflight"),
        acquirer=rig.acquirer,
    )
    assert result["state"] == "failed"
    assert result["classification"] == "preflight_rejected"
    assert "private connection detail" not in json.dumps(result)
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == []
    assert rig.transport.calls == rig.store.create_attempts == 0


def test_pg_retained_ambient_work_preserved_before_failing_store(tmp_path):
    origin = Rig(tmp_path / "origin")
    candidate = origin.acquirer.acquire("retained", lambda: None)
    rig = pg_rig(tmp_path / "target")
    connection = rig.ledger.connection
    connection.info.transaction_status = TransactionStatus.INTRANS
    connection.ambient_work = ["existing caller work"]

    class FailingStore(InMemoryMaterialStore):
        def create_if_absent(self, target, payload):
            self.create_attempts += 1
            raise OSError("private store failure")

    rig.engine.store = rig.store = FailingStore()
    rig.submit(
        target=spec(
            effect_domain="owner_nonproduction",
            mode="retained",
            max_provider_requests=0,
            expected_raw_sha256=digest(RAW),
        )
    )
    result = rig.engine.run(
        "first",
        enabled=True,
        preflight=lambda _spec: pytest.fail("ambient session reached preflight"),
        retained=candidate,
    )
    assert result["state"] == "failed"
    assert result["classification"] == "preflight_rejected"
    assert connection.ambient_work == ["existing caller work"]
    assert connection.info.transaction_status is TransactionStatus.INTRANS
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == []
    assert rig.transport.calls == rig.store.create_attempts == rig.store.reads == 0


@pytest.mark.parametrize("violation", ["domain", "schema", "host", "protection"])
def test_pg_owner_and_target_rechecked_before_preflight(tmp_path, violation):
    rig = pg_rig(tmp_path)
    target = spec(effect_domain="owner_nonproduction")
    if violation == "domain":
        target = spec()
    elif violation == "schema":
        rig.ledger.schema = "mdf_p6_consumer_read_r1"
    elif violation == "host":
        rig.ledger.connection.info.host = "production.invalid"
    else:
        rig.engine.protection = PostgresAdvisoryWriterProtection(FakePgConnection())
    rig.submit(target=target)
    result = rig.engine.run(
        "first",
        enabled=True,
        preflight=lambda _spec: pytest.fail("unowned session reached preflight"),
        acquirer=rig.acquirer,
    )
    assert result["state"] == "failed"
    assert result["classification"] == "preflight_rejected"
    assert rig.transport.calls == rig.store.create_attempts == 0
    connection = rig.ledger.connection
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == []


def test_pg_preflight_must_clean_its_own_read_transaction(tmp_path):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    rig.submit(target=spec(effect_domain="owner_nonproduction"))

    def preflight(_spec):
        connection.execute("SELECT 1")
        connection.ambient_work.append("preflight probe")

    result = rig.engine.run(
        "first", enabled=True, preflight=preflight, acquirer=rig.acquirer
    )
    assert result["state"] == "failed"
    assert result["classification"] == "preflight_rejected"
    assert connection.info.transaction_status is TransactionStatus.INTRANS
    assert connection.ambient_work == ["preflight probe"]
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.statements == ["SELECT 1"]
    assert rig.transport.calls == rig.store.create_attempts == rig.store.reads == 0


def test_pg_idle_writer_with_isolated_preflight_completes_fake_closed_loop(tmp_path):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    rig.submit(target=spec(effect_domain="owner_nonproduction"))

    def preflight(_spec):
        # The host owns only this newly started, read-only probe transaction.
        with connection.transaction():
            connection.execute("SET TRANSACTION READ ONLY")
            connection.execute("SELECT 1")
        assert connection.info.transaction_status is TransactionStatus.IDLE

    result = rig.engine.run(
        "first", enabled=True, preflight=preflight, acquirer=rig.acquirer
    )
    assert result["state"] == "succeeded"
    assert rig.transport.calls == rig.store.create_attempts == 1
    assert connection.commits > 0 and connection.rollbacks == connection.closes == 0
    assert connection.info.transaction_status is TransactionStatus.IDLE
    assert read_local_cpi(rig.ledger, reference="2026-09", as_of=NOW)


@pytest.mark.parametrize("boundary", ["request", "materialization"])
def test_pg_session_rechecked_at_source_and_materialization_boundaries(
    tmp_path, monkeypatch, boundary
):
    rig = pg_rig(tmp_path)
    connection = rig.ledger.connection
    rig.submit(target=spec(effect_domain="owner_nonproduction"))
    if boundary == "request":
        acquire = DirectCpiAcquirer.acquire

        def acquire_with_ambient(self, *args):
            connection.info.transaction_status = TransactionStatus.INTRANS
            return acquire(self, *args)

        monkeypatch.setattr(DirectCpiAcquirer, "acquire", acquire_with_ambient)
    else:
        rig.transport.before = lambda: setattr(
            connection.info, "transaction_status", TransactionStatus.INTRANS
        )
    result = rig.run()
    assert result["state"] == "failed"
    assert rig.transport.calls == (0 if boundary == "request" else 1)
    assert rig.store.create_attempts == rig.store.reads == 0
    assert connection.commits == connection.rollbacks == connection.closes == 0
    assert connection.info.transaction_status is TransactionStatus.INTRANS
    assert connection.statements == []
    assert not result["effects"].get("canonical_possible")


def published_cpi_payload(reference, change=lambda payload: payload):
    """Canonical qualification succeeds; the CPI wrapper must inspect semantics."""
    contract, raw = (
        (AUGUST_REFERENCE_CONTRACT, encode(response()))
        if reference == "2026-08"
        else (SEPTEMBER_REFERENCE_CONTRACT, RAW)
    )
    plan = build_offline_replay_plan(
        raw,
        identity="synthetic-cpi-content",
        started_at=NOW,
        finished_at=NOW,
        frozen_at=NOW,
        reference_contract=contract,
    )
    payload = change(json.loads(plan.content))
    content = payload if isinstance(payload, bytes) else encode(payload)
    data = payload.get("data") if isinstance(payload, dict) else None
    plan = replace(
        plan,
        content=content,
        version=replace(plan.version, content_digest=digest(content)),
        semantic_material=replace(plan.semantic_material, digest=digest(encode(data))),
    )
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: NOW)
    persist_offline_replay(kernel, plan)
    publish_offline_replay(kernel, plan)
    assert read_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=NOW).content == content
    return ledger, plan


@pytest.mark.parametrize("reference", ["2026-08", "2026-09"])
def test_cpi_valid_projection_matches_each_explicit_reference(tmp_path, reference):
    ledger, plan = published_cpi_payload(reference)
    snapshot = ledger.snapshot()
    for version in (None, plan.version.identity):
        assert (
            read_local_cpi(
                ledger, reference=reference, as_of=NOW, version_identity=version
            ).content
            == plan.content
        )
    status = local_cpi_status(
        FileJobJournal(tmp_path / "journal"), at=NOW, ledger=ledger
    )
    assert status["availability"] == (
        "available" if reference == "2026-09" else "reference_stale_or_mismatched"
    )
    assert status["publications"][0]["cpi_read_classification"] == "available"
    assert status["publications"][0]["storage_state"] == "canonical_readable"
    assert ledger.snapshot() == snapshot


@pytest.mark.parametrize(
    "change",
    [
        lambda p: p["data"].update(measurements=[]) or p,
        lambda p: p["data"].update(reference_period="2026-08") or p,
        lambda p: p["data"]["measurements"][0].update(reference_period="2026-08") or p,
        lambda p: p["data"]["measurements"][0].update(series_id="wrong") or p,
        lambda p: p["data"]["measurements"][0].update(value="private payload") or p,
        lambda p: p["data"]["measurements"][0].update(value=0.3) or p,
        lambda p: p["data"]["measurements"][0].update(unit="index") or p,
        lambda p: p["data"]["measurements"][0].update(availability="unavailable") or p,
        lambda p: p["data"].update(availability={"state": "available"}) or p,
        lambda p: (
            p["data"].update(measurements=[p["data"]["measurements"][0]] * 4) or p
        ),
        lambda p: p.update(data=None) or p,
        lambda p: p.pop("data") and p,
        lambda p: [],
        lambda p: b"private invalid JSON",
        lambda p: b'{"data":null,"data":' + encode(p["data"]) + b"}",
    ],
    ids=[
        "empty_measurements",
        "wrong_payload_reference",
        "wrong_measurement_reference",
        "wrong_series",
        "invalid_value",
        "numeric_value",
        "wrong_unit",
        "unavailable_measurement",
        "wrong_availability",
        "duplicate_measurements",
        "null_data",
        "missing_data",
        "non_object_envelope",
        "malformed_json",
        "duplicate_json_data",
    ],
)
def test_cpi_semantic_payload_rejected_despite_matching_canonical_digests(
    tmp_path, change
):
    ledger, plan = published_cpi_payload("2026-09", change)
    journal = FileJobJournal(tmp_path / "journal")
    snapshot = ledger.snapshot()
    for version in (None, plan.version.identity):
        with pytest.raises(CpiReadRejected) as rejected:
            read_local_cpi(
                ledger, reference="2026-09", as_of=NOW, version_identity=version
            )
        assert rejected.value.classification == "cpi_payload_invalid_or_mismatched"
        assert str(rejected.value) == "cpi_payload_invalid_or_mismatched"
        assert rejected.value.__suppress_context__
    status = local_cpi_status(journal, at=NOW, ledger=ledger)
    assert status["availability"] == "cpi_payload_invalid_or_mismatched"
    assert status["selected_publication"] is None
    assert status["coverage"] == status["quality"] == []
    assert status["publications"][0]["storage_state"] == "canonical_readable"
    assert (
        status["publications"][0]["cpi_read_classification"]
        == "cpi_payload_invalid_or_mismatched"
    )
    assert "private" not in json.dumps(status)
    assert journal.list_jobs() == ()
    assert ledger.snapshot() == snapshot
