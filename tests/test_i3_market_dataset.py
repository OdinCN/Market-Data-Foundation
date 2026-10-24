"""Deterministic SQL algorithm tests using SQLite as a transaction double.

SQLite does not qualify PostgreSQL locking, ACL, TLS, durability or query plans.
The production SQL statements, FK/unique constraints and rollback paths execute;
only PG connection/control syntax and schema qualifiers are translated here.
"""

import sqlite3
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.market_material import (
    MemoryMaterialBytes,
    byte_digest,
)
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    Verification,
    required_admissions,
)
from market_data_foundation.canonical.market_projection import ProjectionError, SlotKey
from market_data_foundation.canonical.model import ContractError, Decision, Key, Kind
from market_data_foundation.domain_contracts.market_universe import content_id
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.storage.market_dataset_codec import dumps, loads
from market_data_foundation.storage.market_dataset_garage import (
    GarageMarketMaterials,
    StorageUnknown,
)
from market_data_foundation.storage.market_dataset_postgres import (
    BINDING,
    DDL_PATH,
    SCHEMA,
    SCHEMA_REVISION,
    PostgresMarketDataset,
)
from market_data_foundation.storage.market_dataset_probe import synthetic_fixture

RUN = "i3-" + "a" * 32


class Cursor:
    def __init__(self, rows):
        self.rows = rows

    def fetchone(self):
        return self.rows[0] if self.rows else None

    def fetchall(self):
        return self.rows


class SQLDouble:
    def __init__(self):
        self.db = sqlite3.connect(":memory:", isolation_level=None)
        self.db.execute("PRAGMA foreign_keys=ON")
        self.info = SimpleNamespace(transaction_status=TransactionStatus.IDLE)
        self.clock = datetime(2026, 10, 22, tzinfo=UTC)
        self.queries = []
        self.fail_commit = False
        self.fail_sql = None
        ddl = DDL_PATH.read_text().replace("{s}.", "")
        self.db.executescript(ddl)
        self.db.execute(
            "INSERT INTO schema_metadata VALUES (1,?,?,?,?,?)",
            (
                SCHEMA_REVISION,
                byte_digest(DDL_PATH.read_bytes()),
                BINDING,
                RUN,
                self.clock.isoformat(),
            ),
        )
        from market_data_foundation.canonical.market_projection import IndexGeneration

        self.db.execute(
            "INSERT INTO projection_generations VALUES (?,?,true)",
            ("initial", dumps(IndexGeneration(BINDING, "initial", False))),
        )

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN")
        self.info.transaction_status = TransactionStatus.INTRANS
        try:
            yield
            self.db.execute("COMMIT")
            if self.fail_commit:
                self.fail_commit = False
                raise OSError("sensitive-test-message")
        except BaseException:
            if self.db.in_transaction:
                self.db.execute("ROLLBACK")
            raise
        finally:
            self.info.transaction_status = TransactionStatus.IDLE

    def execute(self, sql, args=()):
        self.queries.append(sql)
        if self.fail_sql and self.fail_sql in sql:
            raise OSError("sensitive-test-message")
        if sql.startswith("SET TRANSACTION") or "pg_advisory_xact_lock" in sql:
            return Cursor([])
        if sql == "SELECT clock_timestamp()":
            return Cursor([(self.clock,)])
        if sql.startswith("DROP SCHEMA "):
            return Cursor([])
        return self.db.execute(sql.replace(SCHEMA + ".", "").replace("%s", "?"), args)


@pytest.fixture
def scenario():
    connection = SQLDouble()
    store = MemoryMaterialBytes()
    values = synthetic_fixture(RUN, lambda *args: "memory://i3")
    receipt, raw, decoder, version, request, month = values
    ledger = PostgresMarketDataset(connection, store, (decoder,), owner_run=RUN)
    return SimpleNamespace(
        connection=connection,
        store=store,
        ledger=ledger,
        receipt=receipt,
        raw=raw,
        decoder=decoder,
        version=version,
        request=request,
        month=month,
        slot=SlotKey(
            month.definition, month.instrument, version.assessment.unit.window.start
        ),
    )


def ready(s, *, publish=True):
    s.ledger.retain(s.receipt, s.raw)
    s.ledger.commit_version(s.version)
    for subject in s.version.admission_subjects:
        s.ledger.set_admission(subject, "synthetic-dev-r1", True)
    if publish:
        s.ledger.publish(s.request)
    s.ledger.rebuild("g1", (s.month,))


def test_codec_full_graph_roundtrip_unknown_clocks_and_utc(scenario):
    s = scenario
    for value in (
        s.receipt,
        s.version,
        s.request,
        s.month,
        replace(s.receipt, requested_at=None, obtained_at=None),
    ):
        assert loads(dumps(value)) == value
        assert content_id(loads(dumps(value))) == content_id(value)
    with pytest.raises(ContractError):
        loads('["arbitrary", ["record", "os.system", {}]]')
    with pytest.raises(ContractError):
        loads(dumps(s.version) + " ")


def test_end_to_end_exact_asof_restart_readonly_and_idempotence(scenario):
    s = scenario
    ready(s)
    before = s.connection.db.total_changes
    fresh = PostgresMarketDataset(s.connection, s.store, (s.decoder,), owner_run=RUN)
    with fresh.read_snapshot("g1") as view:
        assert view.select(s.slot, s.connection.clock) == ("SELECTED", (s.version.key,))
        assert view.content(s.version.key) == s.version.assessment.content
        assert view.versions((s.version.key,), exact=True)[0].version == s.version.key
        assert view.lineage(s.version.key) == (s.receipt,)
        assert len(view.month_directory((s.month,))) == 31
    assert before == s.connection.db.total_changes
    with pytest.raises(ProjectionError):
        view.content(s.version.key)
    old = fresh.publish(s.request)
    assert fresh.publish(s.request) == old
    assert before == s.connection.db.total_changes


@pytest.mark.parametrize("operation", ["retain", "version", "publish", "rebuild"])
@pytest.mark.parametrize(
    "fault",
    [
        Fault.AFTER_CANONICAL,
        Fault.AFTER_PROJECTION,
        Fault.BEFORE_COMMIT,
        Fault.AFTER_COMMIT,
    ],
)
def test_atomic_failures_and_original_identity_reconciliation(
    scenario, operation, fault
):
    s = scenario
    if operation == "retain":

        def action():
            return s.ledger.retain(s.receipt, s.raw, fault=fault)

        op, identity, payload = (
            "material",
            content_id(s.receipt.key),
            (s.receipt, "raw"),
        )
        applicable = fault != Fault.AFTER_PROJECTION
    else:
        s.ledger.retain(s.receipt, s.raw)
        if operation == "version":

            def action():
                return s.ledger.commit_version(s.version, fault)

            op, identity, payload = "version", content_id(s.version.key), s.version
            applicable = True
        else:
            s.ledger.commit_version(s.version)
            for subject in s.version.admission_subjects:
                s.ledger.set_admission(subject, "test", True)
            if operation == "publish":

                def action():
                    return s.ledger.publish(s.request, fault)

                op, identity, payload = (
                    "publication",
                    content_id(s.request.key),
                    s.request,
                )
                applicable = True
            else:

                def action():
                    return s.ledger.rebuild("g1", (s.month,), fault)

                op, identity, payload = "rebuild", "g1", ("g1", (s.month,))
                applicable = fault != Fault.AFTER_CANONICAL
    snapshot = tuple(s.connection.db.iterdump())
    if applicable:
        with pytest.raises(Exception):
            action()
    else:
        action()
    committed = fault == Fault.AFTER_COMMIT or not applicable
    assert s.ledger.verify(op, identity, content_id(payload)) == (
        Verification.COMMITTED if committed else Verification.ABSENT
    )
    if not committed:
        assert tuple(s.connection.db.iterdump()) == snapshot


def test_lost_driver_commit_ack_is_unknown_then_readonly_verified(scenario):
    s = scenario
    s.connection.fail_commit = True
    with pytest.raises(CommitUnknown, match="postgres_commit_acknowledgement_unknown"):
        s.ledger.retain(s.receipt, s.raw)
    assert (
        s.ledger.verify(
            "material", content_id(s.receipt.key), content_id((s.receipt, "raw"))
        )
        == Verification.COMMITTED
    )
    assert (
        s.ledger.verify("material", content_id(s.receipt.key), "wrong")
        == Verification.CONFLICT
    )
    s.connection.fail_sql = "schema_metadata"
    assert (
        s.ledger.verify("material", content_id(s.receipt.key), "wrong")
        == Verification.UNKNOWN
    )


def test_same_identity_different_bytes_level_or_occurrence_rejected(scenario):
    s = scenario
    ready(s)
    with pytest.raises(ContractError, match="same_identity_different_content"):
        s.ledger.retain(s.receipt, s.raw, level="source-projection")
    with pytest.raises(ContractError, match="same_identity_different_content"):
        s.ledger.commit_version(replace(s.version, admission_subjects=("different",)))
    s.connection.clock += timedelta(seconds=1)
    with pytest.raises(sqlite3.IntegrityError):
        s.ledger.publish(
            replace(
                s.request,
                key=Key(Kind.PUBLICATION, "another"),
                decision=Decision.WITHDRAW,
                predecessor=s.request.key,
            )
        )


def test_unknown_acquisition_clock_retained_but_no_version(scenario):
    s = scenario
    receipt = replace(s.receipt, requested_at=None)
    s.ledger.retain(receipt, s.raw)
    version = replace(
        s.version, assessment=replace(s.version.assessment, materials=(receipt,))
    )
    with pytest.raises(ContractError, match="material_acquisition_timing_unverified"):
        s.ledger.commit_version(version)


@pytest.mark.parametrize(
    "table",
    [
        "projection_versions",
        "projection_rows",
        "projection_slots",
        "projection_publications",
        "projection_guards",
    ],
)
def test_single_table_damage_fails_closed_explicit_rebuild_recovers(scenario, table):
    s = scenario
    ready(s)
    s.connection.db.execute(f"DELETE FROM {table}")
    with pytest.raises(ContractError):
        with s.ledger.read_snapshot() as view:
            view.select(s.slot, s.connection.clock)
    s.ledger.suspend()
    with pytest.raises(ProjectionError):
        with s.ledger.read_snapshot():
            pass
    s.ledger.rebuild("g2", (s.month,))
    with s.ledger.read_snapshot("g2") as view:
        assert view.content(s.version.key) == s.version.assessment.content
    with pytest.raises(ProjectionError):
        with s.ledger.read_snapshot("g1"):
            pass


def test_canonical_tail_loss_cannot_be_repaired_away(scenario):
    s = scenario
    ready(s)
    s.connection.db.execute("DELETE FROM projection_publications")
    s.connection.db.execute("UPDATE publication_heads SET head_id=NULL,event_count=0")
    s.connection.db.execute("DELETE FROM publication_events")
    with pytest.raises(ContractError, match="canonical_receipt_graph_mismatch"):
        s.ledger.rebuild("g2", (s.month,))


def test_history_and_guard_rollback_detected_by_independent_head(scenario):
    s = scenario
    ready(s)
    s.connection.db.execute("DELETE FROM projection_publications")
    from market_data_foundation.canonical.market_projection import VersionGuard

    s.connection.db.execute(
        "UPDATE projection_guards SET payload=?",
        (dumps(VersionGuard(s.version.key, 0, content_id(()), 0)),),
    )
    with pytest.raises(ContractError):
        with s.ledger.read_snapshot() as view:
            view.histories((s.version.key,))


def test_unpublished_candidate_not_lost_and_missing_month_not_zero(scenario):
    s = scenario
    ready(s, publish=False)
    with s.ledger.read_snapshot() as view:
        assert len(view.candidates((s.slot,))[0]) == 1
        assert view.select(s.slot, s.connection.clock) == ("NO_PUBLISHED_CANDIDATE", ())
        with pytest.raises(ProjectionError):
            view.month_directory((replace(s.month, month=2),))


class WireDouble:
    def __init__(self):
        self.objects = {}
        self.enforce = True
        self.fail = None
        self.calls = []

    def __call__(self, method, url, headers, body, maximum, timeout):
        self.calls.append((method, url))
        if method == self.fail:
            raise OSError("sensitive-test-message")
        if method == "GET":
            return (200, self.objects[url]) if url in self.objects else (404, b"")
        if method == "PUT":
            assert headers["if-none-match"] == "*"
            assert "if-none-match" in headers["authorization"]
            if url in self.objects and self.enforce:
                return 412, b""
            self.objects[url] = body
            return 200, b""
        assert method == "DELETE"
        self.objects.pop(url, None)
        return 204, b""


def garage(wire, **kwargs):
    return GarageMarketMaterials(
        GarageCredentials("invented", "invented"), RUN, transport=wire, **kwargs
    )


def test_garage_condition_exact_readback_dedup_and_precise_cleanup():
    wire = WireDouble()
    wire.objects["unrelated"] = b"leave-alone"
    store = garage(wire)
    store.qualify_conditional_create()
    raw = b"invented-source-projection"
    locator = store.locator("source-projection", *(["a" * 64] * 3), byte_digest(raw))
    store.put(locator, raw)
    store.put(locator, raw)
    assert store.read(locator, len(raw)) == raw
    with pytest.raises(ContractError):
        store.put(locator, b"changed")
    store.cleanup_owned()
    assert wire.objects == {"unrelated": b"leave-alone"}


def test_garage_unsupported_conditional_create_stops_before_materials():
    wire = WireDouble()
    wire.enforce = False
    store = garage(wire)
    with pytest.raises(ContractError, match="conditional_create_not_enforced"):
        store.qualify_conditional_create()
    assert not store.conditional_verified
    assert len(wire.objects) == 1  # Only the disposable sentinel was touched.
    store.cleanup_owned()


def test_garage_budget_unknown_no_retry_and_foreign_namespace():
    wire = WireDouble()
    store = garage(wire, max_requests=1)
    with pytest.raises(ContractError, match="garage_budget_exhausted"):
        store.qualify_conditional_create()
    assert len(wire.calls) == 1
    wire = WireDouble()
    wire.fail = "GET"
    store = garage(wire)
    with pytest.raises(StorageUnknown) as exc:
        store.qualify_conditional_create()
    assert "sensitive" not in str(exc.value)
    assert len(wire.calls) == 1
    with pytest.raises(ContractError, match="garage_namespace_mismatch"):
        store.read("s3://other-bucket/old-material", 100)


def test_garage_bounded_corrupt_read_and_preexisting_key_not_owned():
    wire = WireDouble()
    store = garage(wire)
    store.qualify_conditional_create()
    raw = b"abc"
    locator = store.locator("raw", *(["b" * 64] * 3), byte_digest(raw))
    url = "http://192.168.10.8:3900/mdf-artifacts/" + store._key(locator)
    wire.objects[url] = raw
    with pytest.raises(ContractError, match="material_preexisting_or_unreadable"):
        store.put(locator, raw)
    wire.objects[url] = b"bad"
    with pytest.raises(ContractError):
        store.read(locator, 3)
    store.cleanup_owned()
    assert url in wire.objects


def test_65_candidates_retained_asof_overflow_exact_still_reads(scenario):
    s = scenario
    ready(s)
    for number in range(64):
        version = replace(s.version, key=Key(Kind.VERSION, f"sibling-{number}"))
        s.ledger.commit_version(
            replace(version, admission_subjects=required_admissions(version))
        )
    assert (
        s.connection.db.execute("SELECT count(*) FROM projection_rows").fetchone()[0]
        == 65
    )
    with s.ledger.read_snapshot() as view:
        assert view.content(s.version.key) == s.version.assessment.content
    s.connection.queries.clear()
    with pytest.raises(ProjectionError):
        with s.ledger.read_snapshot() as view:
            view.candidates((s.slot,))
    assert not any(
        "SELECT payload FROM" in q and "projection_rows WHERE" in q
        for q in s.connection.queries
    )


def test_65_events_retained_history_preflight_before_event_enumeration(scenario):
    s = scenario
    ready(s)
    previous = s.request.key
    for number in range(64):
        s.connection.clock += timedelta(seconds=1)
        request = replace(
            s.request,
            key=Key(Kind.PUBLICATION, f"event-{number}"),
            occurrence=f"event-occurrence-{number}",
            predecessor=previous,
            decision=Decision.WITHDRAW if number % 2 == 0 else Decision.RESTORE,
        )
        s.ledger.publish(request)
        previous = request.key
    with s.ledger.read_snapshot() as view:
        assert view.version_guards((s.version.key,))[0].count == 65
        s.connection.queries.clear()
        with pytest.raises(ProjectionError):
            view.histories((s.version.key,))
        assert not any(
            "SELECT id FROM" in q and "publication_events" in q
            for q in s.connection.queries
        )


def test_current_revocation_blocks_content_and_new_publish(scenario):
    s = scenario
    ready(s)
    subject = s.version.admission_subjects[0]
    s.ledger.set_admission(subject, "revoked", False)
    with pytest.raises(ProjectionError):
        with s.ledger.read_snapshot() as view:
            view.content(s.version.key)
    # Withdrawal remains possible even when material storage is unavailable.
    s.connection.clock += timedelta(seconds=1)
    s.store._blobs.clear()
    request = replace(
        s.request,
        key=Key(Kind.PUBLICATION, "withdraw"),
        occurrence="withdraw",
        predecessor=s.request.key,
        decision=Decision.WITHDRAW,
    )
    s.ledger.publish(request)


def test_corrupt_directory_cannot_be_silently_repaired_by_new_version(scenario):
    s = scenario
    ready(s)
    s.connection.db.execute("UPDATE projection_slots SET candidate_count=2")
    version = replace(s.version, key=Key(Kind.VERSION, "sibling"))
    with pytest.raises(ContractError, match="candidate_directory_inconsistent"):
        s.ledger.commit_version(
            replace(version, admission_subjects=required_admissions(version))
        )
    assert s.connection.db.execute("SELECT count(*) FROM versions").fetchone()[0] == 1


def test_cumulative_candidate_capacity_fails_before_fetch(scenario):
    s = scenario
    ready(s)
    with s.ledger.read_snapshot() as view:
        view._budget.references = 32768
        s.connection.queries.clear()
        with pytest.raises(ProjectionError):
            view.candidates((s.slot,))
        assert not any(
            "SELECT payload FROM" in q and "projection_rows WHERE" in q
            for q in s.connection.queries
        )


def test_failure_writing_one_projection_table_rolls_back_canonical(scenario):
    s = scenario
    s.ledger.retain(s.receipt, s.raw)
    before = tuple(s.connection.db.iterdump())
    s.connection.fail_sql = "INSERT INTO " + SCHEMA + ".projection_rows"
    with pytest.raises(OSError):
        s.ledger.commit_version(s.version)
    s.connection.fail_sql = None
    assert tuple(s.connection.db.iterdump()) == before


def test_material_commit_receipt_missing_blocks_version(scenario):
    s = scenario
    s.ledger.retain(s.receipt, s.raw)
    s.connection.db.execute("DELETE FROM commit_receipts")
    with pytest.raises(ContractError, match="canonical_material_receipt_mismatch"):
        s.ledger.commit_version(s.version)


def test_rebuild_generation_names_never_reused_and_replay_does_not_switch(scenario):
    s = scenario
    ready(s)
    s.ledger.rebuild("g2", (s.month,))
    s.ledger.rebuild("g1", (s.month,))
    with s.ledger.read_snapshot() as view:
        assert view.generation.generation == "g2"
    with pytest.raises(ContractError):
        s.ledger.rebuild("g1", ())


def test_default_reader_snapshot_is_repeatable_read_readonly(scenario):
    s = scenario
    ready(s)
    s.connection.queries.clear()
    with s.ledger.read_snapshot() as view:
        view.content(s.version.key)
    assert (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
        in s.connection.queries
    )
    assert not any(
        q.startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "DROP"))
        for q in s.connection.queries
    )


def test_host_launcher_missing_credentials_stops_before_network(tmp_path, monkeypatch):
    from scripts import mdf_i3_storage_probe as probe

    monkeypatch.chdir(tmp_path)
    for name in probe.ENV:
        monkeypatch.delenv(name, raising=False)

    def forbidden(*_):
        raise AssertionError("network_must_not_be_called")

    monkeypatch.setattr(probe, "connect_dev", forbidden)
    options = SimpleNamespace(
        receipt=tmp_path / "artifacts/intake.json",
        run_id=RUN,
        action="intake",
        opt_in_dev_write=False,
        host_intake=None,
        hold=False,
    )
    result = probe.run(options)
    assert result["status"] == "BLOCKED"
    assert result["exception_classification"] == "CREDENTIALS_NOT_INJECTED"
    assert result["effect_ledger"] == []
    assert result["candidate_sha256"]


@pytest.mark.parametrize(
    "wrong",
    [
        "db",
        "user",
        "session",
        "host",
        "port",
        "tls",
        "create",
        "superuser",
        "default_acl",
    ],
)
def test_live_preflight_fails_closed_without_any_ddl(wrong):
    from contextlib import nullcontext

    from market_data_foundation.storage.market_dataset_postgres import preflight

    expected = [
        "market_data_foundation_dev",
        "market_data_foundation_dev",
        "market_data_foundation_dev",
        "192.168.10.4",
        5432,
        True,
        True,
        False,
    ]
    index = {
        "db": 0,
        "user": 1,
        "session": 2,
        "host": 3,
        "port": 4,
        "tls": 5,
        "create": 6,
        "superuser": 7,
    }
    if wrong in index:
        expected[index[wrong]] = True if wrong == "superuser" else False
    queries = []

    def execute(query, args=()):
        queries.append(query)
        if "current_database()" in query:
            return Cursor([tuple(expected)])
        if "pg_default_acl" in query:
            return Cursor([(1 if wrong == "default_acl" else 0,)])
        return Cursor([])

    connection = SimpleNamespace(
        info=SimpleNamespace(transaction_status=TransactionStatus.IDLE),
        transaction=nullcontext,
        execute=execute,
    )
    with pytest.raises(ContractError):
        preflight(connection)
    assert not any(
        q.startswith(("CREATE", "DROP", "INSERT", "REVOKE")) for q in queries
    )


def test_host_intake_must_match_run_and_exact_targets(tmp_path):
    import json

    from scripts.mdf_i3_storage_probe import check_host_intake

    path = tmp_path / "intake.json"
    path.write_text(json.dumps({"run_id": RUN, "bucket": "other"}))
    with pytest.raises(ContractError, match="host_intake_mismatch"):
        check_host_intake(path, RUN)


def test_database_exception_is_classified_without_raw_message(scenario):
    import psycopg

    s = scenario

    def fail(*_):
        raise psycopg.OperationalError("password-like-sensitive-test-value")

    s.connection.execute = fail
    with pytest.raises(ContractError) as exc:
        s.ledger._sql("SELECT 1")
    assert str(exc.value) == "market_postgres_statement_failed"
    assert exc.value.__suppress_context__


def test_complete_host_probe_orchestration_with_sql_and_wire_doubles(
    tmp_path, monkeypatch
):
    import json

    from scripts import mdf_i3_storage_probe as probe

    connection = SQLDouble()
    connection.close = lambda: None
    wire = WireDouble()
    migrated = False

    def migrate_double(*_):
        nonlocal migrated
        migrated = True
        return {"schema_revision": SCHEMA_REVISION}

    def preflight_double(*_):
        exists = connection.db.execute(
            "SELECT name FROM sqlite_master WHERE name='schema_metadata'"
        ).fetchone()
        return {"namespace_exists": migrated and exists is not None}

    monkeypatch.setattr(probe, "connect_dev", lambda *_: connection)
    monkeypatch.setattr(probe, "preflight", preflight_double)
    monkeypatch.setattr(probe, "migrate", migrate_double)
    monkeypatch.setattr(
        probe,
        "GarageMarketMaterials",
        lambda credentials, run_id, **kwargs: GarageMarketMaterials(
            credentials, run_id, transport=wire, **kwargs
        ),
    )
    for name in probe.ENV:
        monkeypatch.setenv(name, "invented-test-only")
    monkeypatch.chdir(tmp_path)
    intake = tmp_path / "host-intake.json"
    intake.write_text(
        json.dumps(
            {
                "run_id": RUN,
                "schema": SCHEMA,
                "bucket": "mdf-artifacts",
                "endpoint": "http://192.168.10.8:3900",
                "credential_scope": "MDF_DEDICATED",
                "bucket_read_write_delete": "VERIFIED",
                "probe_prefix_absent": "VERIFIED",
                "existing_materials_isolated": "VERIFIED",
                "disposable_probe_no_backup_dependency": True,
                "evidence_ref": "invented-test-double-only",
            }
        )
    )
    options = SimpleNamespace(
        receipt=tmp_path / "artifacts/probe.json",
        run_id=RUN,
        action="probe",
        opt_in_dev_write=True,
        host_intake=intake,
        hold=False,
    )
    result = probe.run(options)
    assert result["status"] == "DEV_SYNTHETIC_PROBE_COMPLETE", result
    assert result["version_reconciliation"] == "COMMITTED"
    assert result["single_table_recovery"] == result["readback"]
    assert result["cleanup"] == "EXACT_OBJECTS_ABSENT_VERIFIED"
    assert all(step["effect"] == "CONFIRMED" for step in result["effect_ledger"])
    assert not wire.objects
    assert (
        connection.db.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table'"
        ).fetchone()[0]
        == 0
    )
    assert result["garage_requests"] <= 96


def test_dev_pg_hostaddr_pinned_against_libpq_environment(monkeypatch, tmp_path):
    """A libpq env override cannot redirect authentication to a different IP."""
    import psycopg
    from psycopg.conninfo import conninfo_attempts, conninfo_to_dict, make_conninfo

    from market_data_foundation.storage.market_dataset_postgres import connect_dev

    monkeypatch.setenv("PGHOSTADDR", "203.0.113.9")
    monkeypatch.setenv("PGHOST", "untrusted.invalid")
    kwargs = {}
    witness = object()

    def capture_connection(**values):
        kwargs.update(values)
        return witness

    monkeypatch.setattr(psycopg, "connect", capture_connection)
    from market_data_foundation.storage.postgres_runtime_config import PgConfigError
    from tests.test_postgres_runtime_config import runtime_file

    path = runtime_file(tmp_path)
    with pytest.raises(PgConfigError, match="postgres_environment_not_allowed"):
        connect_dev("test-only-nonsecret", runtime_path=path)
    assert kwargs == {}
    monkeypatch.delenv("PGHOSTADDR")
    monkeypatch.delenv("PGHOST")
    assert connect_dev("test-only-nonsecret", runtime_path=path) is witness
    assert kwargs["host"] == "192.168.10.4"
    assert kwargs["hostaddr"] == "192.168.10.4"
    assert kwargs["sslmode"] == "require"
    assert kwargs["dbname"] == "market_data_foundation_dev"
    assert kwargs["user"] == "market_data_foundation_dev"
    libpq_options = {k: v for k, v in kwargs.items() if k != "autocommit"}
    attempts = conninfo_attempts(conninfo_to_dict(make_conninfo("", **libpq_options)))
    assert len(attempts) == 1
    assert attempts[0]["hostaddr"] == "192.168.10.4"
    assert attempts[0]["host"] == "192.168.10.4"
