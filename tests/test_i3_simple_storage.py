"""Offline fault regressions. SQLite is only a SQL algorithm double, never PG proof."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical.market_material import byte_digest
from market_data_foundation.canonical.market_persistence import (
    CommitUnknown,
    Fault,
    InjectedFailure,
    Verification,
)
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_universe import content_id
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.integration.market_vertical import verify_original
from market_data_foundation.storage.market_dataset_garage import (
    SimpleGarageMaterials,
    StorageUnknown,
)
from market_data_foundation.storage.market_dataset_postgres import SCHEMA
from market_data_foundation.storage.market_dataset_probe import synthetic_fixture
from market_data_foundation.storage.market_dataset_simple import (
    PENDING_DDL,
    REVISION,
    SimplePostgresDataset,
    ddl_digest,
    schema_for,
)
from tests.test_i3_market_dataset import RUN, SQLDouble


class Wire:
    def __init__(self):
        self.objects, self.calls, self.delayed = {}, [], []
        self.get_status = None
        self.put_failure = None
        self.late = False

    def __call__(self, method, url, headers, body, maximum, timeout):
        assert method in ("GET", "PUT", "HEAD")
        assert "if-none-match" not in headers
        self.calls.append((method, url, body))
        if method == "GET":
            if self.get_status == "unknown":
                raise OSError("private-exception")
            if self.get_status:
                return self.get_status, b""
            return (200, self.objects[url]) if url in self.objects else (404, b"")
        if self.late:
            self.late = False
            self.delayed.append((url, body))
            raise OSError("private-exception")
        if self.put_failure == "after":
            self.objects[url] = body
            self.put_failure = None
            raise OSError("private-exception")
        if self.put_failure == "before":
            raise OSError("private-exception")
        self.objects[url] = body
        return 200, b""


def store_for(wire, **kw):
    return SimpleGarageMaterials(
        GarageCredentials("synthetic", "synthetic"), RUN, transport=wire, **kw
    )


class SimpleSQL(SQLDouble):
    def __init__(self):
        super().__init__()
        self.db.executescript(PENDING_DDL.read_text().replace("{s}.", ""))
        self.db.execute(
            "UPDATE schema_metadata SET schema_revision=?,ddl_digest=?",
            (REVISION, ddl_digest()),
        )

    def execute(self, sql, args=()):
        return super().execute(sql.replace(schema_for(RUN) + ".", SCHEMA + "."), args)


@pytest.fixture
def scenario():
    db, wire = SimpleSQL(), Wire()
    store = store_for(wire)

    def locator(source, binding, contract, raw):
        return store.locator(
            "raw",
            content_id(source),
            content_id(binding),
            content_id(
                (
                    contract.mapping_revision,
                    contract.upstream_revision,
                    contract.decoder_revision,
                )
            ),
            byte_digest(raw),
        )

    receipt, raw, decoder, version, request, month = synthetic_fixture(RUN, locator)
    ledger = SimplePostgresDataset(db, store, (decoder,), owner_run=RUN)
    yield SimpleNamespace(**locals())
    db.db.close()


@pytest.mark.parametrize("mode", ["after", "late", "normal"])
def test_same_bytes_duplicate_unknown_and_late_put(mode):
    wire = Wire()
    store = store_for(wire)
    raw = b"frozen-entire-payload"
    locator = store.locator("raw", *(["a" * 64] * 3), byte_digest(raw))
    wire.put_failure = "after" if mode == "after" else None
    wire.late = mode == "late"
    store.put(locator, raw)
    for url, delayed in wire.delayed:
        wire.objects[url] = delayed  # Old request arrives after newer same-byte PUT.
    store.put(locator, raw)
    assert store.read(locator, len(raw)) == raw
    assert len(wire.objects) == 1
    assert all(body == raw for method, _, body in wire.calls if method == "PUT")
    assert len([x for x in wire.calls if x[0] == "PUT"]) == (2 if mode == "late" else 1)
    with pytest.raises(ContractError):
        store.put(locator, b"different-payload")


@pytest.mark.parametrize("status", [403, 401, 500, "unknown"])
def test_get_non404_never_authorizes_write(status):
    wire = Wire()
    wire.get_status = status
    store = store_for(wire)
    raw = b"abc"
    locator = store.locator("raw", *(["a" * 64] * 3), byte_digest(raw))
    with pytest.raises(ContractError) as exc:
        store.put(locator, raw)
    assert "private" not in str(exc.value)
    assert [x[0] for x in wire.calls] == ["GET"]


def test_bounded_put_unknown_and_corruption_no_overwrite():
    wire = Wire()
    wire.put_failure = "before"
    store = store_for(wire)
    raw = b"abc"
    locator = store.locator("raw", *(["a" * 64] * 3), byte_digest(raw))
    with pytest.raises(StorageUnknown):
        store.put(locator, raw)
    assert len([x for x in wire.calls if x[0] == "PUT"]) == 2
    wire.put_failure = None
    store.put(locator, raw)
    url = next(iter(wire.objects))
    wire.objects[url] = b"bad"
    before = len(wire.calls)
    with pytest.raises(ContractError):
        store.put(locator, raw)
    assert len(wire.calls) == before + 1
    assert wire.objects[url] == b"bad"
    for action in (store.cleanup_owned, store.qualify_conditional_create):
        with pytest.raises(ContractError):
            action()


@pytest.mark.parametrize(
    "fault", [Fault.AFTER_CANONICAL, Fault.BEFORE_COMMIT, Fault.AFTER_COMMIT]
)
def test_pg_failure_keeps_object_restart_recovers_original(scenario, fault):
    s = scenario
    intent = (s.receipt, "raw")
    expected = CommitUnknown if fault == Fault.AFTER_COMMIT else InjectedFailure
    with pytest.raises(expected):
        s.ledger.retain(s.receipt, s.raw, fault=fault)
    assert len(s.wire.objects) == 1
    # Late COMMIT is represented separately below; ACK loss uses real SQL commit.
    fresh = SimplePostgresDataset(s.db, store_for(s.wire), (s.decoder,), owner_run=RUN)
    assert verify_original(fresh, intent) == (
        Verification.COMMITTED if fault == Fault.AFTER_COMMIT else Verification.ABSENT
    )
    fresh.recover(content_id(s.receipt.key))
    assert verify_original(fresh, intent) == Verification.COMMITTED
    assert fresh.pending_inventory()[0][3] == "complete"
    assert len(s.wire.objects) == 1
    assert all(x[0] != "DELETE" for x in s.wire.calls)


def test_stable_identity_conflict_and_other_job_after_failure(scenario):
    s = scenario
    s.wire.get_status = 403
    with pytest.raises(ContractError):
        s.ledger.retain(s.receipt, s.raw)
    assert s.ledger.pending_inventory()[0][3] == "pending"
    s.wire.get_status = None
    other = replace(s.receipt, key=Key(Kind.MATERIAL, "other-job"), job="other-job")
    s.ledger.retain(other, s.raw)
    before = len(s.wire.calls)
    with pytest.raises(ContractError, match="same_identity_different_content"):
        s.ledger.retain(replace(s.receipt, fixture_id="refetched"), s.raw)
    assert len(s.wire.calls) == before
    s.ledger.recover(content_id(s.receipt.key))
    s.ledger.retain(s.receipt, s.raw)
    assert len(s.ledger.pending_inventory()) == 2
    assert len(s.wire.objects) == 1


def test_full_existing_facts_publication_and_read_contract(scenario):
    s = scenario
    s.ledger.retain(s.receipt, s.raw)
    s.ledger.commit_version(s.version)
    for subject in s.version.admission_subjects:
        s.ledger.set_admission(subject, "synthetic-only", True)
    s.ledger.publish(s.request)
    s.ledger.rebuild("g1", (s.month,))
    with s.ledger.read_snapshot("g1") as view:
        assert view.content(s.version.key) == s.version.assessment.content
        assert view.histories((s.version.key,))[0][0].request == s.request
    with pytest.raises(ContractError):
        s.ledger.cleanup_probe_schema()


def test_late_commit_absent_verification_then_original_retry(scenario):
    s = scenario
    # Prepare durable bytes and an object, but no business COMMIT yet.
    with pytest.raises(InjectedFailure):
        s.ledger.retain(s.receipt, s.raw, fault=Fault.BEFORE_COMMIT)
    assert verify_original(s.ledger, (s.receipt, "raw")) == Verification.ABSENT
    # The old task completes after the ABSENT observation; retry must arbitrate.
    s.ledger.retain(s.receipt, s.raw)
    retry = SimplePostgresDataset(s.db, store_for(s.wire), owner_run=RUN)
    retry.recover(content_id(s.receipt.key))
    assert s.db.db.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
    assert len(s.wire.objects) == 1


def test_pending_corruption_rejected(scenario):
    s = scenario
    s.wire.get_status = 403
    with pytest.raises(ContractError):
        s.ledger.retain(s.receipt, s.raw)
    s.wire.get_status = None
    s.db.db.execute("UPDATE ingest_pending SET raw=?", (b"corrupt",))
    before = len(s.wire.calls)
    with pytest.raises(ContractError):
        s.ledger.recover(content_id(s.receipt.key))
    assert len(s.wire.calls) == before


def test_pending_commit_unknown_stops_before_object_and_recovers(scenario):
    s = scenario
    s.db.fail_commit = True
    with pytest.raises(CommitUnknown):
        s.ledger.retain(s.receipt, s.raw)
    assert not s.wire.calls
    assert s.ledger.pending_inventory()[0][3] == "pending"
    s.ledger.recover(content_id(s.receipt.key))
    assert s.ledger.pending_inventory()[0][3] == "complete"


def test_missing_original_and_semantic_drift_no_effect(scenario):
    s = scenario
    with pytest.raises(ContractError, match="pending_original_missing"):
        s.ledger.recover("a" * 64)
    wrong = replace(s.receipt, locator=s.receipt.locator.replace("/raw/", "/status/"))
    with pytest.raises(ContractError, match="material_semantic_address_mismatch"):
        s.ledger.retain(wrong, s.raw)
    assert not s.ledger.pending_inventory()
    assert not s.wire.calls


def test_put_403_stops_and_budget_enforced():
    wire = Wire()

    def forbidden_put(method, url, headers, body, maximum, timeout):
        if method == "PUT":
            wire.calls.append((method, url, body))
            return 403, b""
        return wire(method, url, headers, body, maximum, timeout)

    store = store_for(forbidden_put)
    raw = b"abc"
    locator = store.locator("raw", *(["a" * 64] * 3), byte_digest(raw))
    with pytest.raises(ContractError, match="garage_write_rejected"):
        store.put(locator, raw)
    assert [x[0] for x in wire.calls] == ["GET", "PUT"]
    wire = Wire()
    store = store_for(wire, max_requests=1)
    locator = store.locator("raw", *(["a" * 64] * 3), byte_digest(raw))
    with pytest.raises(ContractError, match="garage_budget_exhausted"):
        store.put(locator, raw)
    assert len(wire.calls) == 1
    wire = Wire()
    store = store_for(wire)
    for n in range(16):
        raw = str(n).encode()
        store.put(store.locator("raw", *(["a" * 64] * 3), byte_digest(raw)), raw)
    raw = b"over-budget"
    before = len(wire.calls)
    with pytest.raises(ContractError, match="garage_object_budget"):
        store.put(store.locator("raw", *(["a" * 64] * 3), byte_digest(raw)), raw)
    assert len(wire.calls) == before


def test_different_bytes_same_business_identity_rejected_before_put(scenario):
    s = scenario
    s.ledger.retain(s.receipt, s.raw)
    raw = s.raw + b" "
    locator = s.receipt.locator.rsplit("/", 1)[0] + "/" + byte_digest(raw)
    changed = replace(
        s.receipt, locator=locator, digest=byte_digest(raw), byte_length=len(raw)
    )
    before = len(s.wire.calls)
    with pytest.raises(ContractError, match="same_identity_different_content"):
        s.ledger.retain(changed, raw)
    assert len(s.wire.calls) == before
    assert len(s.wire.objects) == 1


def test_original_commit_delayed_until_after_absent_snapshot(tmp_path):
    """An actual SQLite transaction delays COMMIT; this is not real-PG evidence."""
    import sqlite3
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import contextmanager
    from threading import Event

    path = tmp_path / "delayed.sqlite"
    original = SimpleSQL()
    persistent = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    original.db.backup(persistent)
    original.db.close()
    original.db = persistent
    reached, release = Event(), Event()
    normal_transaction = original.transaction

    @contextmanager
    def delayed():
        with normal_transaction():
            yield
            # Stage transaction has no material receipt; only hold facts COMMIT.
            if original.db.execute("SELECT count(*) FROM commit_receipts").fetchone()[
                0
            ]:
                reached.set()
                assert release.wait(10)

    original.transaction = delayed
    observer = SimpleSQL()
    observer.db.close()
    observer.db = sqlite3.connect(path, isolation_level=None)
    wire = Wire()
    store = store_for(wire)

    def locator(source, binding, contract, raw):
        return store.locator(
            "raw",
            content_id(source),
            content_id(binding),
            content_id(
                (
                    contract.mapping_revision,
                    contract.upstream_revision,
                    contract.decoder_revision,
                )
            ),
            byte_digest(raw),
        )

    receipt, raw, *_ = synthetic_fixture(RUN, locator)
    writer = SimplePostgresDataset(original, store, owner_run=RUN)
    verifier = SimplePostgresDataset(observer, store, owner_run=RUN)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(writer.retain, receipt, raw)
            try:
                assert reached.wait(10)
                assert (
                    verify_original(verifier, (receipt, "raw")) == Verification.ABSENT
                )
                assert len(wire.objects) == 1
            finally:
                release.set()
            future.result(timeout=10)
        # Original transaction now commits after the earlier ABSENT observation.
        verifier.recover(content_id(receipt.key))
        assert verify_original(verifier, (receipt, "raw")) == Verification.COMMITTED
        assert observer.db.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
        assert len(wire.objects) == 1
    finally:
        release.set()
        original.db.close()
        observer.db.close()


def test_direct_duplicate_transport_cannot_bypass_content_or_capacity():
    wire = Wire()
    store = store_for(wire)
    raw = b"fixed"
    locator = store.locator("raw", *(["a" * 64] * 3), byte_digest(raw))
    key = store._key(locator)
    with pytest.raises(ContractError, match="material_address_mismatch"):
        store._send("PUT", key, b"changed")
    assert not wire.calls
    store._send("PUT", key, raw)
    assert store.intents == {key: len(raw)}


def test_http_error_xml_is_not_subject_to_material_size(monkeypatch):
    from market_data_foundation.storage import market_dataset_garage as module

    class Response:
        def __init__(self, code):
            self.code = code

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def read(self, maximum):
            return b"large-error-xml-body"[:maximum]

    class Opener:
        code = 404

        def open(self, *args, **kwargs):
            return Response(self.code)

    opener = Opener()
    monkeypatch.setattr(module, "build_opener", lambda *args: opener)
    assert module._transport("GET", "http://synthetic", {}, b"", 3, 1) == (404, b"")
    opener.code = 403
    assert module._transport("GET", "http://synthetic", {}, b"", 3, 1) == (403, b"")
    opener.code = 200
    with pytest.raises(ContractError, match="garage_response_capacity"):
        module._transport("GET", "http://synthetic", {}, b"", 3, 1)


def test_dev_deadline_rejects_expired_and_preserves_fractional_alarm(monkeypatch):
    from scripts import mdf_i3_storage_simple_dev as launcher

    monkeypatch.setattr(launcher.time, "time", lambda: 1000)
    assert launcher.remaining_seconds(705.25) == 0.25
    with pytest.raises(ContractError, match="original_run_wall_budget_exhausted"):
        launcher.remaining_seconds(705)
    with pytest.raises(ContractError, match="original_run_wall_budget_exhausted"):
        launcher.remaining_seconds(1001)


def test_recovery_child_timeout_kills_and_reaps_before_return(monkeypatch):
    from scripts import mdf_i3_storage_simple_dev as launcher

    calls = []

    class Process:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            calls.append("closed")

        def communicate(self, **kwargs):
            if kwargs:
                calls.append(("wait", kwargs["timeout"]))
                raise TimeoutError
            calls.append("reaped")

        def kill(self):
            calls.append("killed")

    monkeypatch.setattr(launcher.time, "time", lambda: 1000)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda *a, **kw: Process())
    with pytest.raises(TimeoutError):
        launcher.recovery_child(["synthetic-child"], 705.25)
    assert calls == [("wait", 0.25), "killed", "reaped", "closed"]


@pytest.mark.parametrize(
    "drift,role",
    [
        ((0, 0, 0), (False, False, False)),
        ((1, 0, 0), (False, False, False)),
        ((0, 1, 0), (False, False, False)),
        ((0, 0, 1), (False, False, False)),
        ((0, 0, 0), (True, False, False)),
        ((0, 0, 0), (False, True, False)),
        ((0, 0, 0), (False, False, True)),
    ],
)
def test_preflight_permissions_drift_refuses_without_mutation(monkeypatch, drift, role):
    from contextlib import nullcontext

    from market_data_foundation.storage import market_dataset_simple as module
    from tests.test_i3_market_dataset import Cursor

    queries = []

    class Connection:
        def transaction(self):
            return nullcontext()

        def execute(self, query, args=()):
            queries.append(query)
            if query.startswith("SET TRANSACTION"):
                return Cursor([])
            if "current_setting" in query:
                return Cursor(
                    [("on", "on", "on", "off", "replica", False, schema_for(RUN))]
                )
            if "pg_stat_archiver" in query:
                return Cursor([(0, None, 0, None)])
            if "rolcreatedb" in query:
                return Cursor([role])
            if "SELECT pg_get_userbyid" in query:
                return Cursor([("market_data_foundation_dev", "owner-only")])
            if "aclexplode" in query:
                assert args == (schema_for(RUN),) * 3
                return Cursor([drift])
            if "pg_stat_ssl" in query:
                return Cursor([("TLSv1.3", "synthetic-cipher")])
            raise AssertionError("unexpected_query")

    monkeypatch.setattr(
        module, "preflight", lambda connection: {"identity_tls": "VERIFIED"}
    )
    if any(drift) or any(role):
        with pytest.raises(ContractError, match="postgres_permission_drift"):
            module.simple_preflight(Connection(), RUN)
    else:
        assert (
            module.simple_preflight(Connection(), RUN)["namespace_table_column_acl"]
            == "ISOLATED"
        )
    assert all(query.startswith(("SELECT", "SET TRANSACTION")) for query in queries)


@pytest.mark.parametrize("pending_failure", [True, False])
def test_original_attempt_cannot_be_refetched_under_new_material_key(
    scenario, pending_failure
):
    s = scenario
    if pending_failure:
        s.wire.get_status = 403
        with pytest.raises(ContractError):
            s.ledger.retain(s.receipt, s.raw)
        s.wire.get_status = None
    else:
        s.ledger.retain(s.receipt, s.raw)
    raw = s.raw + b" "
    forged = replace(
        s.receipt,
        key=Key(Kind.MATERIAL, "forged-original-request"),
        digest=byte_digest(raw),
        byte_length=len(raw),
        locator=s.receipt.locator.rsplit("/", 1)[0] + "/" + byte_digest(raw),
    )
    fresh = SimplePostgresDataset(s.db, store_for(s.wire), owner_run=RUN)
    before = len(s.wire.calls)
    with pytest.raises(ContractError, match="same_identity_different_content"):
        fresh.retain(forged, raw)
    assert len(s.wire.calls) == before
    assert len(fresh.pending_inventory()) == 1
    assert len(s.wire.objects) == (0 if pending_failure else 1)
    fresh.recover(content_id(s.receipt.key))
    assert verify_original(fresh, (s.receipt, "raw")) == Verification.COMMITTED
    assert s.db.db.execute("SELECT count(*) FROM materials").fetchone()[0] == 1
    assert len(s.wire.objects) == 1
    # A new request is an independent original identity, even within the job.
    independent = replace(forged, request="new-independent-request")
    fresh.retain(independent, raw)
    assert len(fresh.pending_inventory()) == 2
    assert len(s.wire.objects) == 2


def test_original_attempt_slot_has_database_unique_constraint(scenario):
    import sqlite3

    s = scenario
    s.ledger.retain(s.receipt, s.raw)
    row = s.db.db.execute("SELECT * FROM ingest_pending").fetchone()
    with pytest.raises(sqlite3.IntegrityError):
        s.db.db.execute(
            "INSERT INTO ingest_pending VALUES (?,?,?,?,?,?)",
            ("changed-material-id", *row[1:]),
        )


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_credentials_shared_transport_without_network(tmp_path, monkeypatch, mode):
    import socket

    import psycopg

    from scripts import mdf_i3_storage_simple_dev as script
    from tests.test_postgres_runtime_config import ca_file, runtime_file

    def forbidden(*args, **kwargs):
        pytest.fail("credentials_must_not_open_connections")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(script, "CONFIG", tmp_path)
    root = None if mode == "require" else ca_file(tmp_path)
    runtime = runtime_file(tmp_path, mode, root=root)
    garage = (
        '\n[storage]\nbackend = "s3"\n[s3]\n'
        f'endpoint_url = "{script.ENDPOINT}"\nbucket = "{script.BUCKET}"\n'
        'region = "garage"\nkey_prefix = ""\naddressing_style = "path"\n'
    )
    runtime.write_text(runtime.read_text() + garage)
    (tmp_path / "secret.toml").write_text(
        '[postgres]\npassword = "synthetic"\n[s3]\n'
        'access_key_id = "synthetic-id"\nsecret_access_key = "synthetic-key"\n'
    )
    original = runtime.read_text()
    assert script.credentials() == (
        "synthetic",
        GarageCredentials("synthetic-id", "synthetic-key"),
    )
    # Every historical Garage/S3 contract field remains independently enforced.
    for old, new in (
        ('backend = "s3"', 'backend = "other"'),
        (script.ENDPOINT, "http://untrusted.invalid"),
        (script.BUCKET, "other-bucket"),
        ('region = "garage"', 'region = "other"'),
        ('key_prefix = ""', 'key_prefix = "other"'),
        ('addressing_style = "path"', 'addressing_style = "virtual"'),
        ('host = "192.168.10.4"', 'host = "127.0.0.1"'),
    ):
        runtime.write_text(original.replace(old, new))
        with pytest.raises(ContractError):
            script.credentials()
    # Config rejection precedes even opening the synthetic secret file.
    (tmp_path / "secret.toml").unlink()
    with pytest.raises(ContractError):
        script.credentials()
