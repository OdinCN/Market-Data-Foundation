"""Task opt-in, synthetic SQL proof; default CI never connects to the LAN."""

import json
import os
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from psycopg import sql
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.consumer_read import ReadFailure
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.postgres import PostgresLedger
from tests.test_p6_consumer_operator_consistency import (
    build_graph,
    check_timeline,
    probe,
)
from tests.test_p6_operator_wsgi_postgres_integration import (
    _connect,
    _inventory,
    _storage,
)

pytestmark = [pytest.mark.live_provider, pytest.mark.postgres_integration]
EVIDENCE = Path("artifacts/MDF-P6-CONSUMER-OPERATOR-READ-CONSISTENCY-R1")


def _assert_reader_idle(reader):
    # Observe without SQL/rollback/autocommit changes. Never adopt ambient work.
    assert reader.closed is False
    assert reader.info.transaction_status is TransactionStatus.IDLE
    assert reader._num_transactions == 0


@pytest.fixture
def devpg():
    if os.environ.get("MDF_RUN_CONSUMER_OPERATOR_DEVPG") != "1":
        pytest.skip("requires task-specific development PostgreSQL opt-in")
    schema = "mdf_p3_canonical_kernel_r1_c_" + uuid4().hex
    assert len(schema) <= 63
    observation = {
        "schema": schema,
        "sql_proof": "NOT_EXECUTED",
        "created": False,
        "cases": [],
    }
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    path = EVIDENCE / f"devpg-{schema}.json"

    def save():
        path.write_text(json.dumps(observation, indent=2) + "\n")

    writer = reader = None
    owned = False
    phase = "preflight"
    save()
    try:
        # Reuse only the existing exact dev capability connector and inventory.
        # The connector asserts host/database; no fallback or privilege change.
        with _connect(read_only=True) as preflight:
            assert preflight.execute("SHOW transaction_read_only").fetchone() == ("on",)
            assert preflight.execute("SELECT current_database()").fetchone() == (
                "market_data_foundation_dev",
            )
            baseline = _inventory(preflight)
            assert baseline == {"namespaces": [("public",)], "objects": []}
            assert not PostgresLedger(preflight, schema).namespace_exists()
            assert preflight.execute(
                "SELECT has_database_privilege("
                "current_user, current_database(), 'CREATE')"
            ).fetchone() == (True,)
        observation["preflight"] = baseline
        phase = "setup"
        writer = _connect()
        writer_ledger = PostgresLedger(writer, schema)
        with writer.transaction():
            writer_ledger.create_namespace()
            owned = True  # Reconcile even a lost COMMIT acknowledgement.
        observation["created"] = True
        save()
        reader = _connect(read_only=True)
        reader.autocommit = False
        ledger = PostgresLedger(reader, schema)

        @contextmanager
        def consumer_scope():
            _assert_reader_idle(reader)
            with reader.transaction():
                reader.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                assert reader.execute("SHOW transaction_read_only").fetchone() == (
                    "on",
                )
                yield ledger
            _assert_reader_idle(reader)

        @contextmanager
        def operator_scope():
            _assert_reader_idle(reader)
            with ledger.operator_snapshot() as scope:
                assert scope.execute(
                    sql.SQL(
                        "SELECT current_setting('transaction_isolation'), "
                        "current_setting('transaction_read_only')"
                    ),
                    (),
                ).fetchone() == ("repeatable read", "on")
                yield scope

        anchor = writer_ledger.server_now() + timedelta(minutes=5)
        graph = build_graph(
            CanonicalKernel(writer_ledger, writer_ledger.server_now),
            writer.transaction,
            anchor,
        )
        phase = "proof"
        yield (
            writer,
            schema,
            graph,
            consumer_scope,
            operator_scope,
            observation["cases"],
        )
        observation["sql_proof"] = (
            "PASS"
            if observation["cases"][-1:] == ["cross_surface_complete"]
            else "INCOMPLETE"
        )
    except BaseException as error:
        observation["exception_class"] = type(error).__name__
        observation["failed_phase"] = phase
        if phase != "preflight":
            observation["sql_proof"] = "FAIL"
        pytest.fail(f"devpg_{phase}_{type(error).__name__}", pytrace=False)
    finally:
        for connection in (reader, writer):
            if connection is not None:
                try:
                    connection.close()
                except BaseException as error:
                    observation["close_exception_class"] = type(error).__name__
        if owned:
            try:
                with _connect() as cleanup:
                    target = PostgresLedger(cleanup, schema)
                    if target.namespace_exists():
                        target.drop_namespace()
                observation["cleanup_completed"] = True
                with _connect(read_only=True) as postflight:
                    assert postflight.execute(
                        "SHOW transaction_read_only"
                    ).fetchone() == ("on",)
                    assert not PostgresLedger(postflight, schema).namespace_exists()
                    assert _inventory(postflight) == baseline
                observation["fresh_connection_absence_and_inventory"] = "PASS"
            except BaseException as error:
                observation["cleanup_exception_class"] = type(error).__name__
                observation["sql_proof"] = "FAIL"
                save()
                pytest.fail(f"devpg_cleanup_{type(error).__name__}", pytrace=False)
        save()
        if "close_exception_class" in observation:
            observation["sql_proof"] = "FAIL"
            save()
            pytest.fail("devpg_close_failed", pytrace=False)


def test_same_sql_graph_consumer_and_wsgi_status_report(devpg, monkeypatch):
    writer, schema, graph, consumer_scope, operator_scope, cases = devpg
    plans, replacements, histories, _, anchor = graph
    before = _storage(writer, schema)
    for index in (0, 1):
        check_timeline(graph, index, consumer_scope, operator_scope, monkeypatch)
        cases.append(f"definition_{index}_lifecycle_and_ambiguity")
    assert _storage(writer, schema) == before
    contents = sql.Identifier(schema, "contents")
    for index, plan in enumerate(plans):
        for fault, failure in (
            ("missing", ReadFailure.CONTENT_MISSING),
            ("corrupt", ReadFailure.CONTENT_DIGEST_MISMATCH),
        ):
            try:
                with writer.transaction():
                    if fault == "missing":
                        changed = writer.execute(
                            sql.SQL("DELETE FROM {} WHERE version_identity=%s").format(
                                contents
                            ),
                            (plan.version.identity,),
                        )
                    else:
                        changed = writer.execute(
                            sql.SQL(
                                "UPDATE {} SET payload=%s WHERE version_identity=%s"
                            ).format(contents),
                            (b"synthetic-corrupt-content", plan.version.identity),
                        )
                    assert changed.rowcount == 1
                damaged = _storage(writer, schema)
                probe(
                    consumer_scope,
                    operator_scope,
                    index,
                    anchor + timedelta(seconds=9),
                    {
                        plan.version.key: (
                            "publication_eligible",
                            histories[index][2].key,
                        ),
                        replacements[index].key: ("no_effective_publication", None),
                    },
                    {plan.version.key: failure},
                    failure,
                    monkeypatch,
                )
                assert _storage(writer, schema) == damaged
                cases.append(f"definition_{index}_content_{fault}_non_equivalence")
            finally:
                with writer.transaction():
                    writer.execute(
                        sql.SQL(
                            "INSERT INTO {} (version_identity,digest,payload) "
                            "VALUES (%s,%s,%s) ON CONFLICT (version_identity) "
                            "DO UPDATE SET digest=EXCLUDED.digest,"
                            "payload=EXCLUDED.payload"
                        ).format(contents),
                        (
                            plan.version.identity,
                            plan.version.content_digest,
                            plan.content,
                        ),
                    )
    assert _storage(writer, schema) == before
    cases.append("cross_surface_complete")
