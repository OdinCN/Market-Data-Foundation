"""Offline fault model for the PG protocol; never a PostgreSQL integration proof."""

import copy
import re
import socket
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import psycopg
import pytest
from psycopg.pq import TransactionStatus
from test_cpi_local_job import NOW, Rig, spec
from test_postgres_runtime_config import ca_file, runtime_file

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError
from market_data_foundation.canonical.postgres import PostgresLedger, validate_schema
from market_data_foundation.historical.cpi_local_job import CpiLocalJob
from market_data_foundation.historical.cpi_pg_binding import (
    BACKFILL_JOB,
    BACKFILL_SCOPE,
    DOMAIN,
    INITIAL_JOB,
    SCHEMA,
    CpiPgBinding,
    CpiPgTarget,
    backfill_constraint,
)
from market_data_foundation.historical.cpi_pg_journal import (
    PgJournalIdentity,
    PgJournalUnavailable,
    PostgresJobJournal,
)
from market_data_foundation.historical.cpi_pg_port import CpiPgJobsPort, CpiPgOperator
from market_data_foundation.storage.postgres_runtime_config import load_pg_config

PRIVATE = "synthetic-private-driver-detail"
BINDING = CpiPgBinding("offline-owner")
IDENTITY = PgJournalIdentity("offline_schema_owner", 12345)
SPEC = spec(effect_domain="owner_nonproduction")


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("external_io_forbidden")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return list(self.rows)


class Server:
    """Transactional in-memory double. SQL matching is deliberately strict.

    Locks serialize transactions conservatively; this does not test PostgreSQL's
    isolation/locking implementation, DDL parsing, durability or actual ACLs.
    """

    def __init__(self):
        self.data = dict(jobs={}, attempts={}, events={})
        self.transaction_lock = threading.RLock()
        self.executor_lock = threading.Lock()
        self.connections = []
        self.commands = []
        self.fail_sql = None
        self.fail_commit = None
        self.fail_commit_when = lambda data: None
        self.commits = 0
        self.mutate_connection = lambda c: None
        self.permission = True
        self.successor_supported = False
        self.constraint_override = None
        self.owner = IDENTITY.schema_owner
        self.oid = IDENTITY.schema_oid

    def connect(self, **kwargs):
        c = Connection(self, kwargs)
        self.connections.append(c)
        self.mutate_connection(c)
        return c


class Connection:
    def __init__(self, server, kwargs):
        self.server = server
        self.info = SimpleNamespace(
            host=kwargs["host"],
            hostaddr=kwargs["hostaddr"],
            port=kwargs["port"],
            dbname=kwargs["dbname"],
            user=kwargs["user"],
            transaction_status=TransactionStatus.IDLE,
            get_parameters=lambda: kwargs,
        )
        self.pgconn = SimpleNamespace(ssl_in_use=True)
        self.autocommit = kwargs["autocommit"]
        self.closed = False
        self._num_transactions = 0
        self._tpc = self._pipeline = None
        self.data = None
        self.locked = False
        self.readonly = False

    def execute(self, text, params=()):
        assert not self.closed
        self.server.commands.append((text, params))
        if self.server.fail_sql and self.server.fail_sql in text:
            self.server.fail_sql = None
            raise psycopg.OperationalError(PRIVATE)
        if text.startswith("BEGIN"):
            self.server.transaction_lock.acquire()
            self.data = copy.deepcopy(self.server.data)
            self.info.transaction_status = TransactionStatus.INTRANS
            self.readonly = "READ ONLY" in text
        elif text.startswith("SET LOCAL"):
            pass
        elif text == "SELECT 1":
            return Result([(1,)])
        elif text.startswith("SELECT pg_try_advisory_lock"):
            self.locked = self.server.executor_lock.acquire(blocking=False)
            return Result([(self.locked,)])
        elif text.startswith("SELECT current_user"):
            return Result(
                [
                    (
                        self.info.user,
                        self.info.user,
                        self.info.dbname,
                        self.info.host,
                        self.info.port,
                    )
                ]
            )
        elif "pg_constraint" in text:
            return Result(
                self.server.constraint_override
                if self.server.constraint_override is not None
                else [
                    (
                        "jobs_scope_check",
                        backfill_constraint()
                        if self.server.successor_supported
                        else "CHECK ((scope = 'mdf-b3-cpi-05-dev-r1'::text))",
                        True,
                        False,
                    ),
                    ("jobs_scope_key", "UNIQUE (scope)", True, False),
                ]
            )
        elif "pg_namespace" in text:
            assert params[1] == SCHEMA
            return Result(
                [
                    (
                        self.server.oid,
                        self.server.owner,
                        t,
                        "r",
                        "p",
                        False,
                        False,
                        self.server.owner,
                        True,
                        self.server.permission,
                    )
                    for t in ("attempts", "job_events", "jobs")
                ]
            )
        elif text.startswith("LOCK TABLE"):
            assert text == f"LOCK TABLE {SCHEMA}.jobs IN SHARE ROW EXCLUSIVE MODE"
        elif text.startswith(f"SELECT identity FROM {SCHEMA}.jobs"):
            return Result([(k,) for k in self.data["jobs"]])
        elif text.startswith(f"SELECT document, event_count FROM {SCHEMA}.jobs"):
            return Result(
                [self.data["jobs"][params[0]]] if params[0] in self.data["jobs"] else []
            )
        elif text.startswith(f"SELECT event FROM {SCHEMA}.job_events"):
            return Result(
                [
                    (v,)
                    for _, v in sorted(self.data["events"].get(params[0], {}).items())
                ]
            )
        elif text.startswith(f"SELECT document FROM {SCHEMA}.attempts"):
            return Result(
                [(self.data["attempts"][params[0]],)]
                if params[0] in self.data["attempts"]
                else []
            )
        elif text.startswith("INSERT INTO") or text.startswith("UPDATE"):
            assert not self.readonly
            self.write(text, params)
        else:
            pytest.fail("unexpected_sql: " + text)
        return Result()

    def write(self, text, params):
        if text.startswith(f"INSERT INTO {SCHEMA}.jobs "):
            job, scope, doc, count = params
            if self.data["jobs"]:
                assert self.server.successor_supported and job == BACKFILL_JOB
                assert (
                    set(self.data["jobs"]) == {INITIAL_JOB} and scope == BACKFILL_SCOPE
                )
                assert doc.obj["parent_job_id"] == INITIAL_JOB
            else:
                assert scope == DOMAIN
            self.data["jobs"][job] = (copy.deepcopy(doc.obj), count)
        elif text.startswith(f"INSERT INTO {SCHEMA}.job_events "):
            job, sequence, doc = params
            events = self.data["events"].setdefault(job, {})
            assert sequence not in events and 1 <= sequence <= 256
            events[sequence] = copy.deepcopy(doc.obj)
        elif text.startswith(f"INSERT INTO {SCHEMA}.attempts "):
            job, attempt, doc = params
            assert job not in self.data["attempts"] and attempt == job + "-a1"
            self.data["attempts"][job] = copy.deepcopy(doc.obj)
        elif text.startswith(f"UPDATE {SCHEMA}.attempts "):
            doc, job = params
            assert job in self.data["attempts"]
            self.data["attempts"][job] = copy.deepcopy(doc.obj)
        elif text.startswith(f"UPDATE {SCHEMA}.jobs "):
            doc, count, job = params
            assert job in self.data["jobs"]
            self.data["jobs"][job] = (copy.deepcopy(doc.obj), count)
        else:
            pytest.fail("unexpected_mutation")

    def commit(self):
        mode, self.server.fail_commit = self.server.fail_commit, None
        mode = mode or self.server.fail_commit_when(self.data)
        if mode != "before":
            self.server.data = self.data
            self.server.commits += 1
        self.rollback()
        if mode:
            raise psycopg.OperationalError(PRIVATE)

    def rollback(self):
        if self.data is not None:
            self.data = None
            self.info.transaction_status = TransactionStatus.IDLE
            self.server.transaction_lock.release()

    def close(self):
        self.rollback()
        if self.locked:
            self.server.executor_lock.release()
            self.locked = False
        self.closed = True


@pytest.fixture
def setup(tmp_path):
    profile = load_pg_config(
        runtime_file(tmp_path, "verify-full", root=ca_file(tmp_path))
    )
    server = Server()

    def journal(**kwargs):
        return PostgresJobJournal(
            server.connect,
            profile,
            IDENTITY,
            kwargs.pop("binding", BINDING),
            enabled=True,
            **kwargs,
        )

    return server, journal


def submit(journal, identity="first"):
    return journal.submit(identity, SPEC.document(), SPEC.scope, NOW)


def start(journal, identity="first"):
    return journal._update(
        identity,
        NOW,
        state="running",
        stage="preflight",
        attempts=1,
        attempt_id=identity + "-a1",
        started_at=NOW.isoformat(),
        worker_identity="offline-worker",
    )


def test_authoritative_accept_restart_snapshot_and_no_sqlite(setup):
    server, factory = setup
    j = factory()
    assert not server.connections
    doc = submit(j)
    assert doc["state"] == "pending" and doc["attempts"] == 0
    assert server.data["jobs"]["first"][0] == doc
    assert factory(read_only=True).get("first") == doc
    assert len(j.history("first")) == 1
    assert all(c.closed for c in server.connections)
    assert j.binding.document() == doc["binding"]
    assert not hasattr(j, "directory")


def test_concurrent_same_identity_is_idempotent_and_different_identity_rejected(setup):
    server, factory = setup
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: submit(factory()), range(16)))
    assert all(doc == results[0] for doc in results)
    assert len(server.data["jobs"]) == len(server.data["events"]["first"]) == 1
    with pytest.raises(ContractError, match="job_scope_already_registered"):
        submit(factory(), "another")
    with pytest.raises(ContractError, match="binding_mismatch"):
        submit(factory(binding=replace(BINDING, operator="another-owner")))
    assert len(server.data["jobs"]) == 1


def test_executor_exclusion_single_attempt_and_restart_unknown(setup):
    server, factory = setup
    first, second = factory(), factory()
    submit(first)
    with first.executor("first"):
        start(first)
        with (
            pytest.raises(ContractError, match="executor_busy"),
            second.executor("first"),
        ):
            pytest.fail("second executor admitted")
        with pytest.raises(ContractError, match="attempt_immutable"):
            start(first)
    restarted = factory()
    with restarted.executor("first"):
        restarted._update(
            "first", NOW, state="outcome_unknown", stage="restart_detected"
        )
        with pytest.raises(ContractError, match="state_regression"):
            start(restarted)
        restarted._update(
            "first", NOW, stage="reconciled", reconciliation={"outcome": "unresolved"}
        )
    assert restarted.get("first")["state"] == "outcome_unknown"
    assert len(server.data["attempts"]) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
def test_accept_commit_unknown_never_claims_durable_or_leaks_details(setup, mode):
    server, factory = setup
    server.fail_commit = mode
    j = factory()
    with pytest.raises(PgJournalUnavailable, match="commit_outcome_unknown") as error:
        submit(j)
    assert PRIVATE not in str(error.value) and error.value.__context__ is None
    with pytest.raises(ContractError, match="observation_required"):
        submit(j)
    assert len(factory(read_only=True).list_jobs()) == (mode == "after")
    # Same immutable acceptance can be observed/resubmitted; it creates no Attempt.
    submit(factory())
    assert not server.data["attempts"] and len(server.data["events"]["first"]) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
def test_attempt_commit_unknown_stops_before_effect(setup, mode):
    server, factory = setup
    j = factory()
    submit(j)
    effects = []
    with j.executor("first"):
        server.fail_commit = mode
        with pytest.raises(PgJournalUnavailable):
            start(j)
            effects.append("forbidden")
    assert not effects
    observed = factory(read_only=True).get("first")
    assert observed["attempts"] == (mode == "after")
    assert observed["state"] == ("running" if mode == "after" else "pending")


def bound_rig(tmp_path, factory):
    rig = Rig(tmp_path / "rig")
    rig.journal = factory()
    old = rig.engine
    rig.engine = CpiLocalJob(
        rig.journal,
        old.clock,
        old.kernel,
        old.store,
        old.protection,
        old.readback,
        old.commit,
        target_binding=BINDING,
    )
    rig.engine.submit("first", SPEC)
    return rig


def test_existing_engine_durable_intents_and_single_execution(setup, tmp_path):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)

    def inspect_before_source():
        durable = factory(read_only=True).get("first")
        assert durable["attempts"] == 1
        assert durable["effects"]["provider_request_possible"] == 1
        assert durable == server.data["attempts"]["first"]
        assert server.commits >= 4

    rig.transport.before = inspect_before_source
    result = rig.run()
    assert result["state"] == "succeeded"
    assert rig.transport.calls == rig.store.create_attempts == rig.commits == 1
    assert result["raw"]["sha256"] and result["raw"]["locator"]
    assert result["reconciliation"]["evidence"]
    assert factory(read_only=True).get("first") == result
    assert rig.run() == result
    assert rig.transport.calls == 1


def test_effect_reservation_failure_never_invokes_source(setup, tmp_path):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)
    # First event after the Attempt is allocated cannot be durably recorded.
    server.fail_sql = f"INSERT INTO {SCHEMA}.attempts"
    with pytest.raises(PgJournalUnavailable):
        rig.run()
    assert rig.transport.calls == rig.store.create_attempts == 0
    assert factory(read_only=True).get("first")["state"] == "pending"


def test_restart_engine_cannot_run_interrupted_attempt(setup, tmp_path):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)
    with rig.journal.executor("first"):
        start(rig.journal)
        rig.journal._update("first", NOW, effects={"provider_request_possible": 1})
    with pytest.raises(ContractError, match="restart_reconciliation_required"):
        rig.run()
    doc = rig.engine.reconcile("first", enabled=True)
    assert doc["state"] == "outcome_unknown"
    assert doc["reconciliation"]["outcome"] == "unresolved"
    assert rig.run()["state"] == "outcome_unknown"
    assert rig.transport.calls == 0 and len(server.data["attempts"]) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"schema": "mdf_local_first_cpi_operable_r1"},
        {"schema": "public"},
        {"host": "127.0.0.1"},
        {"database": "another"},
        {"port": 5433},
        {"domain": "another-domain"},
    ],
)
def test_cross_target_rejected(changes):
    with pytest.raises(ContractError, match="target_not_admitted"):
        CpiPgTarget(**changes)


@pytest.mark.parametrize(
    "changes",
    [
        {"operation": "timeseries.data:PPI"},
        {"reference": "2026-08"},
        {"source_revision": "another"},
        {"source": "another"},
        {"definition_revision": "another"},
        {"release_slot": "another"},
        {"binding_revision": "another"},
        {"binding": "active-binding"},
        {"provider_revision": "inferred"},
    ],
)
def test_cross_operation_reference_and_revision_rejected(changes):
    with pytest.raises(ContractError, match="binding_not_admitted"):
        replace(BINDING, **changes)


def test_spec_scope_and_immutable_identity_guards(setup):
    server, factory = setup
    j = factory()
    with pytest.raises(ContractError, match="scope_mismatch"):
        j.submit("first", SPEC.document(), "invented", NOW)
    for key, value in [
        ("reference", "2026-08"),
        ("source_identity", "other"),
        ("mode", "retained"),
        ("effect_domain", "synthetic"),
    ]:
        with pytest.raises(ContractError, match="binding_not_admitted"):
            j.submit("first", SPEC.document() | {key: value}, SPEC.scope, NOW)
    assert not server.connections
    submit(j)
    with j.executor("first"):
        with pytest.raises(ContractError, match="immutable_identity"):
            j._update("first", NOW, binding={})
        with pytest.raises(ContractError, match="effect_requires_attempt"):
            j._update("first", NOW, effects={"provider_request_possible": 1})


def test_read_only_observation_no_state_changes(setup):
    server, factory = setup
    submit(factory())
    before = copy.deepcopy(server.data)
    server.commands.clear()
    reader = factory(read_only=True)
    reader.snapshot()
    reader.history("first")
    for operation in [lambda: submit(reader), lambda: reader.cancel("first", NOW)]:
        with pytest.raises(ContractError, match="read_only"):
            operation()
    assert before == server.data
    assert not any(
        sql.startswith(("INSERT", "UPDATE", "LOCK TABLE")) for sql, _ in server.commands
    )


@pytest.mark.parametrize("fault", ["role", "host", "database", "tls", "busy", "pool"])
def test_connection_identity_and_credentials_fail_closed(setup, fault):
    server, factory = setup

    def mutate(c):
        if fault == "role":
            c.info.user = "untrusted"
        elif fault == "host":
            c.info.hostaddr = "127.0.0.1"
        elif fault == "database":
            c.info.dbname = "other"
        elif fault == "tls":
            c.pgconn.ssl_in_use = False
        elif fault == "busy":
            c.info.transaction_status = TransactionStatus.INTRANS
        elif fault == "pool":
            c._pool = object()

    server.mutate_connection = mutate
    with pytest.raises(ContractError):
        submit(factory())
    assert not server.data["jobs"]
    assert not server.commands


def test_auth_failure_no_secrets_and_permission_failures(setup):
    server, factory = setup
    j = factory()

    def denied(**kwargs):
        raise psycopg.OperationalError(PRIVATE)

    j.connector = denied
    with pytest.raises(PgJournalUnavailable) as exc:
        submit(j)
    assert str(exc.value) == "cpi_pg_storage_unavailable"
    assert exc.value.__context__ is None
    server.permission = False
    with pytest.raises(ContractError, match="schema_or_permission_mismatch"):
        submit(factory())
    assert not server.data["jobs"]


def test_port_requires_trusted_operator_and_exact_binding(setup):
    server, factory = setup
    grant = None
    port = CpiPgJobsPort(factory(), SPEC, lambda: NOW, lambda: grant)
    for grant in [
        None,
        CpiPgOperator("another", True),
        CpiPgOperator("offline-owner", False),
    ]:
        with pytest.raises(ContractError, match="operator_not_authorized"):
            port.accept("first", BINDING)
    assert not server.connections
    grant = CpiPgOperator("offline-owner", True)
    with pytest.raises(ContractError, match="binding_mismatch"):
        port.accept("first", replace(BINDING, operator="another"))
    assert port.accept("first", BINDING)["state"] == "pending"
    grant = CpiPgOperator("offline-owner", False)
    assert port.snapshot("first")[0][0]["binding"]["operator"] == "offline-owner"


def test_schema_admission_and_existing_worker_requires_explicit_target(setup, tmp_path):
    server, factory = setup
    assert validate_schema(SCHEMA) == SCHEMA
    with pytest.raises(ContractError):
        validate_schema(SCHEMA + "_other")
    rig = bound_rig(tmp_path, factory)
    conn = server.connect(**factory().profile.connection_kwargs(), autocommit=True)
    pg = PostgresLedger(conn, SCHEMA)
    rig.engine._check_pg_target(pg)
    with pytest.raises(ContractError, match="target_not_admitted"):
        rig.engine._check_pg_target(
            PostgresLedger(conn, "mdf_local_first_cpi_operable_r1")
        )
    rig.engine.target_binding = None
    with pytest.raises(ContractError, match="nonproduction_target_required"):
        rig.engine._check_pg_target(pg)
    conn.close()


def test_event_budget_atomic_and_cancellation_never_releases_scope(setup):
    server, factory = setup
    j = factory()
    submit(j)
    with j.executor("first"):
        start(j)
        for _ in range(254):
            j._update("first", NOW, stage="preflight")
        before = copy.deepcopy(server.data)
        with pytest.raises(ContractError, match="event_budget_exhausted"):
            j._update(
                "first",
                NOW,
                stage="acquiring",
                effects={"provider_request_possible": 1},
            )
    assert before == server.data
    assert len(j.history("first")) == 256
    with pytest.raises(ContractError, match="scope_already_registered"):
        submit(factory(), "second")


def test_disabled_and_shared_canonical_connection_never_touched(setup):
    server, factory = setup
    j = factory()
    j.enabled = False
    with pytest.raises(ContractError, match="opt_in"):
        submit(j)
    assert not server.connections
    j.enabled = True
    c = server.connect(**j.profile.connection_kwargs(), autocommit=True)
    j.connector = lambda **kwargs: c
    j.exclude_connection(c)
    with pytest.raises(ContractError, match="shared_connection"):
        submit(j)
    assert not c.closed and not server.commands
    c.close()


@pytest.mark.parametrize("mode", ["before", "after"])
def test_provider_intent_commit_unknown_blocks_effect_and_restart(
    setup, tmp_path, mode
):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)
    server.fail_commit_when = lambda data: (
        mode
        if data["jobs"]["first"][0]["effects"].get("provider_request_possible")
        else None
    )
    with pytest.raises(ContractError):
        rig.run()
    assert rig.transport.calls == rig.store.create_attempts == 0
    observed = factory(read_only=True).get("first")
    assert observed["state"] == "running" and observed["attempts"] == 1
    assert bool(observed["effects"].get("provider_request_possible")) == (
        mode == "after"
    )
    rig.engine.journal = factory()
    server.fail_commit_when = lambda data: None
    with pytest.raises(ContractError, match="restart_reconciliation_required"):
        rig.run()
    assert rig.engine.reconcile("first", enabled=True)["state"] == "outcome_unknown"
    assert rig.transport.calls == 0


def test_canonical_commit_unknown_records_original_attempt_without_retry(
    setup, tmp_path
):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)

    def ambiguous_commit():
        raise psycopg.OperationalError(PRIVATE)

    rig.engine.commit = ambiguous_commit
    result = rig.run()
    assert result["state"] == "outcome_unknown"
    assert result["classification"] == "ledger_commit_outcome_unknown"
    assert PRIVATE not in str(factory(read_only=True).snapshot())
    assert rig.transport.calls == 1
    assert rig.run()["state"] == "outcome_unknown"
    assert rig.engine.reconcile("first", enabled=True)["state"] == "outcome_unknown"
    assert len(server.data["attempts"]) == rig.transport.calls == 1


def test_attempt_and_event_failure_roll_back_all_three_tables(setup):
    server, factory = setup
    j = factory()
    submit(j)
    before = copy.deepcopy(server.data)
    server.fail_sql = f"INSERT INTO {SCHEMA}.job_events"
    with j.executor("first"), pytest.raises(PgJournalUnavailable):
        start(j)
    assert server.data == before
    assert factory(read_only=True).get("first")["attempts"] == 0


def test_new_target_reader_uses_exact_schema_and_stays_read_only(setup, monkeypatch):
    import test_b3_a2_cpi_pg_readonly_scope as existing

    from market_data_foundation.web.cpi_pg_readonly_scope import dev_read_scope_factory
    from tests.r5_fixture import fixture_ledger

    _, factory = setup
    profile = factory().profile
    monkeypatch.setattr(existing, "PROFILE", profile)
    ledger, _, _ = fixture_ledger()
    connections = []

    def connect(**kwargs):
        c = existing.Connection(ledger)
        connections.append(c)
        return c

    scope = dev_read_scope_factory(
        connect,
        existing.IDENTITY,
        enabled=True,
        pg_profile=profile,
        target=CpiPgTarget(),
    )
    with scope() as snapshot:
        assert snapshot is not None
    c = connections[0]
    assert c.closed
    assert c.cleanup == ["rollback", "close"]
    for query, params in c.commands:
        text = query if isinstance(query, str) else query.as_string()
        assert "mdf_local_first_cpi_operable_r1" not in text
        if "pg_namespace" in text:
            assert params == (SCHEMA,)
        if "WITH bounded" in text:
            assert f'"{SCHEMA}"' in text
        assert not text.lstrip().startswith(("INSERT", "UPDATE", "DELETE", "CREATE"))


def test_candidate_source_limitations_are_unchanged():
    from market_data_foundation.historical.bls_cpi_direct import (
        BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
    )

    assert (
        "candidate_only_not_active"
        in BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE.limitations
    )


def test_concurrent_different_job_ids_cannot_reopen_scope(setup):
    server, factory = setup

    def attempt(identity):
        try:
            return submit(factory(), identity)["job_id"]
        except ContractError as error:
            return str(error)

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, ["one", "two"]))
    assert outcomes.count("job_scope_already_registered") == 1
    assert len(server.data["jobs"]) == 1


@pytest.mark.parametrize("mode", ["before", "after"])
def test_final_status_commit_unknown_does_not_repeat_successful_effects(
    setup, tmp_path, mode
):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)
    server.fail_commit_when = lambda data: (
        mode if data["jobs"]["first"][0]["state"] == "succeeded" else None
    )
    with pytest.raises(ContractError):
        rig.run()
    assert rig.transport.calls == rig.store.create_attempts == rig.commits == 1
    server.fail_commit_when = lambda data: None
    rig.engine.journal = factory()
    durable = rig.engine.journal.get("first")
    if mode == "before":
        assert durable["state"] == "running"
        with pytest.raises(ContractError, match="restart_reconciliation_required"):
            rig.run()
        observed = rig.engine.reconcile("first", enabled=True)
        assert observed["state"] == "outcome_unknown"
        assert observed["reconciliation"]["outcome"] == "confirmed_published"
    else:
        assert durable["state"] == "succeeded"
        assert rig.run() == durable
    assert rig.transport.calls == rig.store.create_attempts == rig.commits == 1


def test_garage_and_canonical_callbacks_observe_committed_intents(setup, tmp_path):
    _, factory = setup
    rig = bound_rig(tmp_path, factory)
    original_create = rig.store.create_if_absent
    original_commit = rig.engine.commit

    def create(target, payload):
        doc = factory(read_only=True).get("first")
        assert doc["effects"]["raw_create_calls_possible"] == 1
        assert doc["effects"]["canonical_possible"] is True
        assert doc["plan"] and doc["raw"]["locator"] == target.locator
        return original_create(target, payload)

    def commit():
        doc = factory(read_only=True).get("first")
        assert doc["effects"]["ledger_commit_possible"] is True
        assert doc["effects"]["raw_state"] == "verified"
        return original_commit()

    rig.store.create_if_absent = create
    rig.engine.commit = commit
    assert rig.run()["state"] == "succeeded"


def test_cancellation_is_durable_and_does_not_allocate_attempt(setup, tmp_path):
    server, factory = setup
    rig = bound_rig(tmp_path, factory)
    rig.journal.cancel("first", NOW)
    assert factory(read_only=True).get("first")["cancel_requested"] is True
    assert rig.run()["state"] == "cancelled"
    assert not server.data["attempts"] and rig.transport.calls == 0
    with pytest.raises(ContractError, match="scope_already_registered"):
        submit(factory(), "second")


def test_journal_rejects_shared_executor_without_closing_it(setup):
    server, factory = setup
    j = factory()
    submit(j)
    with j.executor("first"):
        executor = j._executor_connection
        j.connector = lambda **kwargs: executor
        before = copy.deepcopy(server.data)
        with pytest.raises(ContractError, match="shared_connection"):
            start(j)
        assert not executor.closed and server.data == before
    assert executor.closed


@pytest.mark.parametrize("created_before_executor", [True, False])
@pytest.mark.parametrize("depth", [1, 2])
def test_derived_reader_rejects_executor_without_touching_it(
    setup, monkeypatch, created_before_executor, depth
):
    server, factory = setup
    j = factory()
    submit(j)

    def derive():
        reader = j
        for _ in range(depth):
            reader = reader.reader()
        return reader

    reader = derive() if created_before_executor else None
    before = copy.deepcopy(server.data)
    with j.executor("first"):
        reader = reader or derive()
        executor = j._executor_connection
        reader.connector = lambda **kwargs: executor
        with monkeypatch.context() as patch:
            calls = {}
            for name in ("execute", "commit", "rollback", "close"):
                calls[name] = Mock(wraps=getattr(executor, name))
                patch.setattr(executor, name, calls[name])
            for observe in (
                lambda: reader.snapshot("first"),
                lambda: reader.get("first"),
                lambda: reader.history("first"),
                reader.list_jobs,
            ):
                with pytest.raises(ContractError, match="shared_connection_forbidden"):
                    observe()
            for call in calls.values():
                call.assert_not_called()
        assert not executor.closed and executor.locked
        assert server.executor_lock.locked() and server.data == before
        # Rejection leaves the owner usable and a genuinely independent read valid.
        start(j)
        reader.connector = server.connect
        assert reader.get("first")["state"] == "running"
        assert not executor.closed and executor.locked
    assert executor.closed and not server.executor_lock.locked()


def register_canonical_writer(journal, connection):
    return CpiLocalJob(
        journal,
        lambda: NOW,
        CanonicalKernel(PostgresLedger(connection, SCHEMA), lambda: NOW),
        Mock(),
        Mock(),
        Mock(),
        Mock(),
        target_binding=BINDING,
    )


@pytest.mark.parametrize("created_before_writer", [True, False])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("in_transaction", [False, True])
def test_derived_reader_rejects_late_writer_without_touching_it(
    setup, monkeypatch, created_before_writer, depth, in_transaction
):
    server, factory = setup
    j = factory()
    expected = submit(j)

    def derive():
        reader = j
        for _ in range(depth):
            reader = reader.reader()
        return reader

    reader = derive() if created_before_writer else None
    writer = server.connect(**j.profile.connection_kwargs(), autocommit=True)
    engine = register_canonical_writer(j, writer)
    reader = reader or derive()
    if in_transaction:
        writer.execute("BEGIN")
    writer_data = writer.data
    writer_status = writer.info.transaction_status
    before = copy.deepcopy(server.data)
    try:
        with j.executor("first"):
            executor = j._executor_connection
            reader.connector = lambda **kwargs: writer
            with monkeypatch.context() as patch:
                calls = {}
                for name in ("execute", "commit", "rollback", "close"):
                    calls[name] = Mock(wraps=getattr(writer, name))
                    patch.setattr(writer, name, calls[name])
                for observe in (
                    lambda: reader.snapshot("first"),
                    lambda: reader.get("first"),
                    lambda: reader.history("first"),
                    reader.list_jobs,
                ):
                    with pytest.raises(
                        ContractError, match="^cpi_pg_shared_connection_forbidden$"
                    ):
                        observe()
                for call in calls.values():
                    call.assert_not_called()
                assert server.data == before
                assert not j._poisoned and not reader._poisoned
                assert not executor.closed and executor.locked
                assert server.executor_lock.locked()
                # An independent read and owner update still work after rejection.
                reader.connector = server.connect
                assert reader.get("first") == expected
                independent = server.connections[-1]
                assert independent is not writer and independent is not executor
                assert independent.closed and independent.readonly
                start(j)
                assert reader.get("first")["state"] == "running"
                for call in calls.values():
                    call.assert_not_called()
            assert not writer.closed and writer.data is writer_data
            assert writer.info.transaction_status is writer_status
            assert engine.kernel.ledger.connection is writer
            assert not executor.closed and executor.locked
        assert executor.closed and not server.executor_lock.locked()
        assert j._executor_connection is None and j._executor_job is None
        assert not writer.closed and writer.data is writer_data
    finally:
        writer.close()


@pytest.mark.parametrize("created_before_executor", [True, False])
@pytest.mark.parametrize("depth", [1, 2])
@pytest.mark.parametrize("fault", [None, "sql", "contract", "close"])
def test_derived_reader_independent_connection_cleanup(
    setup, monkeypatch, created_before_executor, depth, fault
):
    server, factory = setup
    j = factory()
    expected = submit(j)

    def derive():
        reader = j
        for _ in range(depth):
            reader = reader.reader()
        return reader

    reader = derive() if created_before_executor else None
    writer = server.connect(**j.profile.connection_kwargs(), autocommit=True)
    register_canonical_writer(j, writer)
    writer_calls = {}
    for name in ("execute", "commit", "rollback", "close"):
        writer_calls[name] = Mock(wraps=getattr(writer, name))
        monkeypatch.setattr(writer, name, writer_calls[name])
    before = copy.deepcopy(server.data)
    commits = server.commits
    with j.executor("first"):
        reader = reader or derive()
        executor = j._executor_connection

        def connect(**kwargs):
            c = server.connect(**kwargs)
            if fault == "close":
                close = c.close

                def fail_close():
                    close()
                    raise psycopg.OperationalError(PRIVATE)

                c.close = fail_close
            return c

        reader.connector = connect
        if fault == "sql":
            server.fail_sql = f"SELECT document, event_count FROM {SCHEMA}.jobs"
        elif fault == "contract":
            server.permission = False
        if fault is None:
            assert reader.get("first") == expected
        else:
            error = ContractError if fault == "contract" else PgJournalUnavailable
            with pytest.raises(error) as exc:
                reader.get("first")
            assert PRIVATE not in str(exc.value)
            assert exc.value.__context__ is None or exc.value.__suppress_context__
        independent = server.connections[-1]
        assert independent is not executor and independent.closed
        assert independent.info.transaction_status is TransactionStatus.IDLE
        assert independent.readonly
        assert not executor.closed and executor.locked
        assert server.executor_lock.locked()
        assert server.data == before and server.commits == commits
        assert not writer.closed and writer.data is None
        server.permission = True
        reader.connector = server.connect
        assert reader.get("first") == expected
        start(j)
    assert executor.closed and not server.executor_lock.locked()
    assert not writer.closed and not j._poisoned and not reader._poisoned
    for call in writer_calls.values():
        call.assert_not_called()
    writer.close()


@pytest.mark.parametrize("created_before_executor", [True, False])
def test_derived_reader_rejection_preserves_owner_exception_cleanup(
    setup, monkeypatch, created_before_executor
):
    server, factory = setup
    j = factory()
    reader = j.reader() if created_before_executor else None
    with pytest.raises(PgJournalUnavailable, match="cpi_pg_storage_unavailable"):
        with j.executor("first"):
            reader = reader or j.reader()
            executor = j._executor_connection
            close = Mock(wraps=executor.close)
            monkeypatch.setattr(executor, "close", close)
            reader.connector = lambda **kwargs: executor
            with pytest.raises(ContractError, match="shared_connection_forbidden"):
                reader.snapshot()
            close.assert_not_called()
            assert executor.locked and not executor.closed
            raise RuntimeError("offline_executor_failure")
    close.assert_called_once_with()
    assert executor.closed and not server.executor_lock.locked()
    assert j._executor_connection is None and j._executor_job is None


def _exact_binding_lhs(sql):
    expression = re.search(
        r"ADD CONSTRAINT exact_binding\s+CHECK\s*\((.*?)\s+IS NOT DISTINCT FROM",
        sql,
        re.DOTALL,
    )
    assert expression is not None
    return expression.group(1)


def _assert_exact_binding_grouping(sql):
    # Targeted source guard only, not a PostgreSQL parser/execution claim.
    assert re.sub(r"\s+", "", _exact_binding_lhs(sql)) == (
        "((document->'binding')-'operator')"
    )


def test_candidate_exact_binding_groups_jsonb_before_subtraction():
    sql = Path(__file__).resolve().parents[1] / "db/candidates/b3_05_cpi_dev_r1.sql"
    _assert_exact_binding_grouping(sql.read_text())


@pytest.mark.parametrize(
    "bad_lhs",
    ["(document->'binding' - 'operator')", "(document->('binding' - 'operator'))"],
)
def test_candidate_exact_binding_rejects_string_subtraction_mutants(bad_lhs):
    sql = Path(__file__).resolve().parents[1] / "db/candidates/b3_05_cpi_dev_r1.sql"
    text = sql.read_text()
    mutant = text.replace(_exact_binding_lhs(text), bad_lhs, 1)
    with pytest.raises(AssertionError):
        _assert_exact_binding_grouping(mutant)


def test_close_failure_after_commit_requires_fresh_observation(setup):
    server, factory = setup
    j = factory()

    def mutate(c):
        close = c.close

        def fail_close():
            close()
            raise psycopg.OperationalError(PRIVATE)

        c.close = fail_close

    server.mutate_connection = mutate
    with pytest.raises(PgJournalUnavailable) as exc:
        submit(j)
    assert PRIVATE not in str(exc.value)
    assert exc.value.__context__ is None
    server.mutate_connection = lambda c: None
    with pytest.raises(ContractError, match="observation_required"):
        submit(j)
    assert factory(read_only=True).get("first")["state"] == "pending"


def test_worker_claim_one_real_clock_instant_durable_before_source(setup, tmp_path):
    """An actually advancing clock must not invalidate a legitimate PG Attempt.

    This is a pure protocol double: abort immediately AFTER durable admission and
    BEFORE any provider or Garage call. Never count it as a real DB trial.
    """
    from datetime import timedelta

    server, factory = setup
    rig = bound_rig(tmp_path, factory)
    first = NOW + timedelta(seconds=1)
    next_instant = first + timedelta(microseconds=1)
    ticks = iter((first, next_instant))
    rig.engine.clock = lambda: next(ticks, next_instant)

    class StopAfterDurableClaim(BaseException):
        pass

    original_update = rig.engine._update

    def stop_before_source(job_id, **changes):
        accepted = original_update(job_id, **changes)
        if changes.get("state") == "running":
            raise StopAfterDurableClaim
        return accepted

    rig.engine._update = stop_before_source
    with pytest.raises(StopAfterDurableClaim):
        rig.run()
    durable = factory(read_only=True).get("first")
    assert durable["state"] == "running"
    assert durable["attempts"] == 1
    assert durable["started_at"] == first.isoformat()
    assert durable["updated_at"] == first.isoformat()
    assert len(server.data["attempts"]) == 1
    assert rig.transport.calls == rig.store.create_attempts == rig.commits == 0
