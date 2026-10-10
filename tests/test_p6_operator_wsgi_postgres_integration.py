"""Opt-in real SQL composition; synthetic data and test-only host authority."""

import json
import os
import tomllib
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path
from uuid import uuid4
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator

import psycopg
import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical import operator_status, operator_web
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
)
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Decision
from market_data_foundation.canonical.operator_wsgi import OperatorWSGIGateway
from market_data_foundation.canonical.postgres import PostgresLedger
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical import bls_cpi_direct as cpi
from market_data_foundation.historical.daily_bars import (
    persist_replay_before_publication,
    publish_replay,
)
from tests.test_p6_consumer_read_postgres_integration import (
    BASE,
    _cpi_plan,
    _daily_bars_plan,
)

EVIDENCE = Path("artifacts/MDF-P6-OPERATOR-WSGI-DEVPG-INTEGRATION-R1")


def _withdrawal_request(publication, index, at):
    # Bound CPI evidence requirements also apply to WITHDRAW. Preserve its
    # admitted scope/evidence/grades/replay/completeness; change only the event.
    return replace(
        publication.request,
        identity=f"wsgi-withdraw-{index}",
        occurrence=f"wsgi-withdraw-occurrence-{index}",
        rule_revision="synthetic-wsgi",
        decision=Decision.WITHDRAW,
        predecessor=publication.key,
        reason="synthetic withdrawal",
        effective_at=at,
        usable_at=None,  # The new decision gets its own server usability instant.
    )


@pytest.mark.parametrize("index", [0, 1])
def test_withdrawal_fixture_obeys_delivered_evidence_contract(index, monkeypatch):
    """Memory-only publication semantics regression; no SQL/isolation claim."""

    def no_network(*args, **kwargs):
        pytest.fail("offline fixture regression must not connect")

    monkeypatch.setattr("socket.socket", no_network)
    monkeypatch.setattr("socket.create_connection", no_network)
    monkeypatch.setattr(psycopg, "connect", no_network)
    monkeypatch.setattr(psycopg.Connection, "connect", no_network)
    plan = (_cpi_plan if index == 0 else _daily_bars_plan)()
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=5))
    if index == 0:
        cpi.persist_live_plan_before_publication(kernel, plan)
        publication = cpi.publish_live_plan(kernel, plan)
    else:
        persist_replay_before_publication(kernel, plan)
        publication = publish_replay(kernel, plan)
    kernel.clock = lambda: BASE + timedelta(seconds=10)
    at = BASE + timedelta(days=1)
    request = _withdrawal_request(publication, index, at)
    if index == 0:
        stripped = replace(
            request, evidence=(), claimed_grades=frozenset(), claims_replayable=False
        )
        with pytest.raises(ContractError, match="^publication_evidence_required$"):
            kernel.publish(stripped)
        assert ledger.publications_for_version(plan.version.key) == (publication,)
    assert (
        operator_status.inspect_operator_status(
            ledger, plan.version.definition, as_of=at
        )
        .as_of.candidates[0]
        .selection_state
        == "publication_eligible"
    )
    withdrawal = kernel.publish(request)
    assert withdrawal.request == request
    status = operator_status.inspect_operator_status(
        ledger, plan.version.definition, as_of=at
    )
    assert status.as_of.candidates[0].selection_state == "version_withdrawn"
    assert status.as_of.candidates[0].effective_publication == withdrawal.key


def _connect(*, read_only=False):
    # Only the existing owner-provisioned development capability. No DSN fallback.
    runtime = tomllib.loads(Path("config/local/runtime.toml").read_text())["postgres"]
    assert runtime["host"] == "192.168.10.4"
    assert runtime["database"] == "market_data_foundation_dev"
    password = os.environ.get("MDF_POSTGRES_PASSWORD")
    if password is None:
        password = tomllib.loads(Path("config/local/secret.toml").read_text())[
            "postgres"
        ]["password"]
    return psycopg.connect(
        host=runtime["host"],
        port=runtime["port"],
        dbname=runtime["database"],
        user=runtime["user"],
        password=password,
        sslmode=runtime["sslmode"],
        connect_timeout=5,
        autocommit=True,
        options=(
            "-c statement_timeout=5000 -c lock_timeout=5000 "
            f"-c default_transaction_read_only={'on' if read_only else 'off'}"
        ),
    )


def _inventory(connection):
    namespaces = connection.execute(
        "SELECT nspname FROM pg_namespace WHERE nspname NOT LIKE 'pg_%' "
        "AND nspname <> 'information_schema' ORDER BY nspname"
    ).fetchall()
    objects = connection.execute(
        "SELECT n.nspname, c.relname, c.relkind FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname <> 'information_schema' "
        "ORDER BY 1, 2, 3"
    ).fetchall()
    return {"namespaces": namespaces, "objects": objects}


def _storage(connection, schema):
    # Observe only our synthetic namespace, including content bytes, for equality.
    return tuple(
        connection.execute(
            sql.SQL("SELECT * FROM {}.{} ORDER BY 1, 2").format(
                sql.Identifier(schema), sql.Identifier(table)
            )
        ).fetchall()
        for table in ("records", "contents", "publications")
    )


@pytest.fixture
def devpg():
    if os.environ.get("MDF_RUN_WSGI_DEVPG_INTEGRATION") != "1":
        pytest.skip("requires task-specific development PostgreSQL opt-in")
    schema = "mdf_p3_canonical_kernel_r1_w_" + uuid4().hex
    assert len(schema) <= 63  # PostgreSQL must not truncate our cleanup identity.
    observation = {"schema": schema, "sql_proof": "NOT_EXECUTED", "cases": []}
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"devpg-{schema}.json"

    def save():
        path.write_text(json.dumps(observation, indent=2) + "\n")

    writer = reader = None
    owned = False
    phase = "preflight"
    save()
    try:
        with _connect(read_only=True) as preflight:
            assert preflight.execute("SHOW transaction_read_only").fetchone() == ("on",)
            assert preflight.execute("SELECT current_database()").fetchone() == (
                "market_data_foundation_dev",
            )
            assert int(preflight.info.server_version) >= 120000
            assert preflight.execute(
                "SELECT has_database_privilege("
                "current_user, current_database(), 'CREATE')"
            ).fetchone() == (True,)
            baseline = _inventory(preflight)
            # Fail before mutation if the dedicated capability is no longer empty.
            assert baseline == {"namespaces": [("public",)], "objects": []}
            assert not PostgresLedger(preflight, schema).namespace_exists()
        observation["preflight"] = baseline
        phase = "setup"
        save()
        writer = _connect()
        ledger = PostgresLedger(writer, schema)
        with writer.transaction():
            ledger.create_namespace()  # Reject existing objects; never reuse/drop them.
            # Reconcile our own unique schema even after a lost COMMIT acknowledgement.
            owned = True
        observation["created"] = True
        save()
        plans = (_cpi_plan(), _daily_bars_plan())
        kernel = CanonicalKernel(ledger, ledger.server_now)
        with writer.transaction():
            cpi.persist_live_plan_before_publication(kernel, plans[0])
            persist_replay_before_publication(kernel, plans[1])
        publications = (
            cpi.publish_live_plan(kernel, plans[0]),
            publish_replay(kernel, plans[1]),
        )
        at = ledger.server_now().astimezone(UTC)
        reader = _connect(read_only=True)
        reader.autocommit = False
        phase = "requests"
        yield reader, writer, schema, plans, publications, at, observation["cases"]
        observation["sql_proof"] = (
            "PASS"
            if observation["cases"]
            and observation["cases"][-1] == "composition_complete"
            else "INCOMPLETE"
        )
    except BaseException as error:
        observation["exception_class"] = type(error).__name__
        if isinstance(error, psycopg.OperationalError):
            # Classify locally; never store the libpq message or connection options.
            if "Operation not permitted" in str(error):
                observation["exception_classification"] = "SOCKET_EPERM"
        observation["failed_phase"] = phase
        if phase != "preflight":
            observation["sql_proof"] = "FAIL"
        # No raw libpq errors, credential values or traceback in live test output.
        pytest.fail(f"devpg_{phase}_{type(error).__name__}", pytrace=False)
    finally:
        close_error = None
        for connection in (reader, writer):
            if connection is not None:
                try:
                    connection.close()
                except BaseException as error:
                    observation["close_exception_class"] = type(error).__name__
                    close_error = type(error).__name__
        if owned:
            try:
                with _connect() as cleanup:
                    cleanup_ledger = PostgresLedger(cleanup, schema)
                    if cleanup_ledger.namespace_exists():
                        cleanup_ledger.drop_namespace()
                observation["cleanup_completed"] = True
                with _connect(read_only=True) as postflight:
                    assert not PostgresLedger(postflight, schema).namespace_exists()
                    assert _inventory(postflight) == baseline
                observation["fresh_connection_absence_and_inventory"] = "PASS"
            except BaseException as error:
                observation["cleanup_exception_class"] = type(error).__name__
                observation["sql_proof"] = "FAIL"
                save()
                pytest.fail(f"devpg_cleanup_{type(error).__name__}", pytrace=False)
        if close_error is not None:
            observation["sql_proof"] = "FAIL"
        save()
        if close_error is not None:
            pytest.fail(f"devpg_close_{close_error}", pytrace=False)


@pytest.mark.live_provider
@pytest.mark.postgres_integration
@pytest.mark.parametrize(
    "index,selector,definition",
    [
        (0, "bls-cpi-headlines", BLS_CPI_HEADLINES),
        (1, "raw-daily-bars", RAW_DAILY_BARS),
    ],
)
def test_wsgi_real_sql_authorized_snapshot_response(
    devpg, index, selector, definition, monkeypatch
):
    reader, writer, schema, plans, publications, at, cases = devpg
    ledger = PostgresLedger(reader, schema)
    capability = object()  # Test host only; never obtained from environ.
    active = True
    injection = capability
    scopes, events, snapshot_counts = [], [], []
    fault = None
    real_render = operator_web.render_operator_report

    def authorize(context, key, instant):
        events.append("authorize")
        return (
            active and context is capability and key == definition.key and instant == at
        )

    @contextmanager
    def snapshot():
        events.append("scope")
        with ledger.operator_snapshot() as scope:
            scopes.append(scope)
            assert reader.info.transaction_status is TransactionStatus.INTRANS
            assert scope.execute(
                sql.SQL(
                    "SELECT current_setting('transaction_isolation'), "
                    "current_setting('transaction_read_only')"
                ),
                (),
            ).fetchone() == ("repeatable read", "on")
            query = sql.SQL("SELECT count(*) FROM {}.publications").format(
                sql.Identifier(schema)
            )
            initial_count = scope.execute(query, ()).fetchone()[0]
            yield scope
            final_count = scope.execute(query, ()).fetchone()[0]
            assert final_count == initial_count
            snapshot_counts.append((initial_count, final_count))
            if fault == "readonly_sql":
                try:
                    scope.execute(
                        sql.SQL("DELETE FROM {}.records WHERE false").format(
                            sql.Identifier(schema)
                        ),
                        (),
                    )
                except psycopg.errors.ReadOnlySqlTransaction:
                    assert reader.info.transaction_status is TransactionStatus.INERROR
                    cases.append("server_readonly_sqlstate_25006")
                    raise
            elif fault == "exit":
                reader.close()  # Real lost connection at scope exit; no fake SQL.
        events.append("exit")

    def render(status):
        assert events[-1] == "exit"
        assert reader.info.transaction_status is TransactionStatus.IDLE
        assert reader.autocommit is False
        events.append("render")
        return real_render(status)

    monkeypatch.setattr(operator_web, "render_operator_report", render)
    entry = operator_web.OperatorWebEntry(authorize=authorize, snapshot=snapshot)
    app = OperatorWSGIGateway(entry, context_factory=lambda: injection)

    def call(*, instant=at, selected=selector, start_fault=False):
        env = {}
        setup_testing_defaults(env)
        stamp = instant.isoformat().replace("+00:00", "Z")
        env.update(
            PATH_INFO="/operator/report",
            QUERY_STRING=f"definition={selected}&as_of={stamp}",
            HTTP_AUTHORIZATION="Operator",
            HTTP_COOKIE="role=Operator",
            REMOTE_USER="Operator",
        )
        env["mdf.operator_context"] = capability
        responses = []

        def start(status, headers, exc_info=None):
            assert exc_info is None
            assert (
                reader.closed
                or reader.info.transaction_status is TransactionStatus.IDLE
            )
            events.append("start")
            if start_fault:
                raise RuntimeError("synthetic_transport_private")
            responses.append((status, dict(headers)))
            return lambda data: pytest.fail("partial write")

        result = validator(app)(env, start)
        try:
            chunks = list(result)
        finally:
            result.close()
        assert len(responses) == 1 and len(chunks) == 1
        status, headers = responses[0]
        body = chunks[0]
        assert headers["Content-Length"] == str(len(body))
        assert headers["Cache-Control"] == "no-store"
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
        for plan in plans:
            for material in plan.version.materials:
                # Exact controlled locators, rather than a generic substring check.
                record = PostgresLedger(writer, schema).get(material)
                assert record.controlled_ref.encode() not in body
        assert plans[0].acquisition.external_receipt.encode() not in body
        assert b"synthetic_transport_private" not in body
        return status, body

    def error(result, status, classification):
        assert result == (status, (classification + "\n").encode())

    before = _storage(writer, schema)
    for _ in range(2):
        status, body = call()
        assert status == "200 OK"
        assert definition.identity.encode() in body
        assert plans[index].version.identity.encode() in body
        assert b"publication_eligible" in body and b"unobservable" in body
        assert events[-5:] == ["authorize", "scope", "exit", "render", "start"]
        with pytest.raises(ReadContractError) as expired:
            scopes[-1].operator_reads()
        assert expired.value.classification is ReadFailure.STORAGE_UNAVAILABLE
    assert scopes[0] is not scopes[1]
    assert _storage(writer, schema) == before
    cases.append("repeated_html_expired_scopes_nonleak_unchanged_storage")

    count = len(scopes)
    active = False  # Revoke between requests: no cached authorization or context.
    error(call(), "403 Forbidden", "operator_forbidden")
    active = True
    injection = None  # Even the exact capability in environ cannot authorize.
    error(call(), "403 Forbidden", "operator_forbidden")
    injection = capability
    other = "raw-daily-bars" if index == 0 else "bls-cpi-headlines"
    error(call(selected=other), "403 Forbidden", "operator_forbidden")
    error(call(instant=at - timedelta(days=1)), "403 Forbidden", "operator_forbidden")
    assert len(scopes) == count
    cases.append("revoked_spoofed_exact_selection_denied_without_scope")

    fault = "readonly_sql"
    error(call(), "503 Service Unavailable", "operator_status_unavailable")
    assert reader.info.transaction_status is TransactionStatus.IDLE
    assert reader.autocommit is False
    assert events[-1] == "start" and events[-2] == "scope"
    assert cases[-1] == "server_readonly_sqlstate_25006"
    assert _storage(writer, schema) == before
    fault = None
    assert call()[0] == "200 OK"  # The real SQL-aborted scope was rolled back.
    cases.append("sql_error_rollback_recovery_no_partial_html")

    # Commit a withdrawal between the real as-of and binding inspections.
    # The current request stays eligible; the next fresh snapshot sees withdrawal.
    original_asof = operator_status._inspect_asof
    written = False

    def concurrent_withdrawal(reads, key, instant):
        nonlocal written
        diagnostic = original_asof(reads, key, instant)
        if not written:
            kernel = CanonicalKernel(PostgresLedger(writer, schema), lambda: at)
            kernel.publish(_withdrawal_request(publications[index], index, at))
            written = True
        return diagnostic

    # Future exact as_of includes the intervening server-timed withdrawal.
    at += timedelta(days=1)
    monkeypatch.setattr(operator_status, "_inspect_asof", concurrent_withdrawal)
    assert b"publication_eligible" in call(instant=at)[1]
    assert written
    assert snapshot_counts[-1] == (2, 2)
    assert b"withdrawn" in call(instant=at)[1]
    assert snapshot_counts[-1] == (3, 3)
    cases.append("intervening_writer_old_snapshot_stable_new_snapshot_withdrawn")
    with pytest.raises(RuntimeError, match="synthetic_transport_private"):
        call(instant=at, start_fault=True)
    assert reader.info.transaction_status is TransactionStatus.IDLE
    assert reader.autocommit is False
    cases.append("transport_exception_after_cleanup")

    fault = "exit"
    error(call(instant=at), "503 Service Unavailable", "operator_status_unavailable")
    assert reader.closed
    # No render or partial success on exit failure.
    assert events[-2:] == ["scope", "start"]
    cases.append("lost_connection_exit_no_render_no_partial_html")
    cases.append("composition_complete")
