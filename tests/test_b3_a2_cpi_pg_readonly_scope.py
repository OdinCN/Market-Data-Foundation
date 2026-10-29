"""Offline SQL/connection doubles + actual canonical Reader, never real PG data."""

import socket
from dataclasses import replace
from datetime import datetime, timedelta
from types import SimpleNamespace

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg.pq import TransactionStatus

from market_data_foundation.canonical.codec import dumps, loads
from market_data_foundation.canonical.consumer_read import ReadContractError
from market_data_foundation.canonical.model import Kind
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.cpi_local_read import read_local_cpi
from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web import cpi_pg_readonly_scope as pg
from market_data_foundation.web.cpi_api import create_app
from tests.r5_fixture import AT, BASE, fixture_ledger
from tests.test_postgres_runtime_config import ca_file, runtime_file

PROFILE: pg.PgConfig | None = None


@pytest.fixture(autouse=True)
def configured_profile(tmp_path, monkeypatch):
    profile = load_pg_config(
        runtime_file(tmp_path, "verify-full", root=ca_file(tmp_path))
    )
    monkeypatch.setitem(globals(), "PROFILE", profile)


IDENTITY = pg.DevReadIdentity("market_data_foundation_dev", "synthetic_owner", 12345)
PARAMS = {"reference": "2026-08", "as_of": AT}


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("external_io_forbidden")

    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.description = None
        self.rows = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def execute(self, query, params):
        text = query if isinstance(query, str) else query.as_string()
        self.connection.commands.append((text, params))
        if self.connection.fault == "execute":
            raise TimeoutError("synthetic-private-driver-detail")
        if text.startswith("BEGIN"):
            assert self.connection.info.transaction_status is TransactionStatus.IDLE
            self.connection.info.transaction_status = TransactionStatus.INTRANS
        elif text.startswith("SET LOCAL"):
            pass
        elif "inet_server_addr" in text:
            self.rows = [self.connection.server]
        elif "pg_namespace" in text:
            self.rows = self.connection.schema
        elif "WITH bounded" in text:
            self.connection.business_reads += 1
            table = next(t for t in self.connection.tables if f'."{t}"' in text)
            limit, maximum, size_limit, remaining = params
            rows = self.connection.tables[table][:limit]
            sizes = [sum(len(str(v).encode()) for v in r) for r in rows]
            admitted = (
                len(rows) <= maximum
                and max(sizes, default=0) <= size_limit
                and sum(sizes) <= remaining
            )
            self.rows = [
                (*row, size, admitted)
                if admitted
                else (*(None for _ in row), size, False)
                for row, size in zip(rows, sizes, strict=True)
            ]
        else:
            pytest.fail("unexpected_sql")
        if text.lstrip().startswith(("SELECT", "WITH")):
            self.description = ("synthetic",)

    def fetchmany(self, maximum):
        if self.connection.fault == "fetch":
            raise OSError("synthetic-private-driver-detail")
        return self.rows[:maximum]


class Connection:
    """A SQL return-shape model, explicitly not PostgreSQL isolation evidence."""

    def __init__(self, ledger):
        self.closed = False
        self.autocommit = True
        self._num_transactions = 0
        self._tpc = self._pipeline = self._pool = None
        self.fault = None
        self.commands, self.cursors, self.cleanup = [], [], []
        self.business_reads = 0
        self.info = SimpleNamespace(
            host=pg.DEV_HOST,
            hostaddr=pg.DEV_HOST,
            port=5432,
            dbname=pg.DEV_DATABASE,
            user=IDENTITY.role,
            server_version=pg.SERVER_VERSION,
            ssl_in_use=True,
            transaction_status=TransactionStatus.IDLE,
            get_parameters=lambda: PROFILE.connection_kwargs(),
        )
        self.server = (
            pg.DEV_HOST,
            5432,
            pg.DEV_DATABASE,
            IDENTITY.role,
            IDENTITY.role,
            str(pg.SERVER_VERSION),
            "repeatable read",
            "on",
            "3s",
            True,
            "TLSv1.3",
        )
        self.schema = [
            (
                IDENTITY.schema_oid,
                IDENTITY.schema_owner,
                table,
                "r",
                "p",
                False,
                False,
                IDENTITY.schema_owner,
                True,
                True,
            )
            for table in ("contents", "publications", "records")
        ]
        self.tables = {"records": [], "publications": [], "contents": []}
        for key, payload in ledger._records.items():
            if key[0] == Kind.PUBLICATION.value:
                value = loads(payload)
                self.tables["publications"].append(
                    (
                        value.key.identity,
                        value.request.occurrence,
                        value.request.version.identity,
                        value.decision_at,
                        payload,
                    )
                )
            else:
                self.tables["records"].append((*key, payload))
        self.tables["contents"] = [
            (identity, sha, content)
            for identity, (sha, content) in ledger._contents.items()
        ]
        self.pgconn = SimpleNamespace(finish=self.finish, ssl_in_use=True)

    def cursor(self):
        cursor = Cursor(self)
        self.cursors.append(cursor)
        return cursor

    def rollback(self):
        self.cleanup.append("rollback")
        if self.fault == "rollback":
            raise OSError("synthetic-private-driver-detail")
        self.info.transaction_status = TransactionStatus.IDLE

    def close(self):
        self.cleanup.append("close")
        if self.fault == "close":
            raise OSError("synthetic-private-driver-detail")
        self.closed = True

    def finish(self):
        self.cleanup.append("finish")
        self.closed = True


def factory(ledger, mutate=lambda conn: None):
    connections = []

    def connect(**options):
        assert options == {
            "host": pg.DEV_HOST,
            "hostaddr": pg.DEV_HOST,
            "port": 5432,
            "dbname": pg.DEV_DATABASE,
            "user": IDENTITY.role,
            "sslmode": PROFILE.sslmode,
            "gssencmode": "disable",
            **(
                {"sslrootcert": str(PROFILE.sslrootcert)} if PROFILE.sslrootcert else {}
            ),
            "connect_timeout": 5,
            "autocommit": True,
            "options": "-c statement_timeout=3000",
        }
        connection = Connection(ledger)
        mutate(connection)
        connections.append(connection)
        return connection

    return pg.dev_read_scope_factory(
        connect, IDENTITY, enabled=True, pg_profile=PROFILE
    ), connections


def test_pg18_server_inet_identity_uses_host_only_without_ipv4_mask():
    """PostgreSQL18 inet_server_addr()::text can include /32; host() omits mask."""
    ledger, _, _ = fixture_ledger()
    scope, connections = factory(ledger)
    with scope() as snapshot:
        assert snapshot is not None
    identity_queries = [
        statement
        for statement, _ in connections[0].commands
        if "inet_server_addr()" in statement
    ]
    assert len(identity_queries) == 1
    assert "host(inet_server_addr())" in identity_queries[0]
    assert "inet_server_addr()::text" not in identity_queries[0]


def test_canonical_positive_catalog_and_exact_asof_fresh_scopes():
    ledger, plan, publication = fixture_ledger()
    before = ledger.snapshot()
    scope, connections = factory(ledger)
    with TestClient(create_app(scope, evidence_mode="OFFLINE_FIXTURE")) as client:
        catalog = client.get("/api/r5/cpi", params=PARAMS).json()
        assert catalog["status"] == "OK"
        assert catalog["service"]["publication_identity"] == publication.key.identity
        assert catalog["asset"]["local_holding"] == "VERIFIED_FOR_REQUEST"
        for mode in ("as_of", "exact_version"):
            response = client.get(
                "/api/r5/cpi/read",
                params={
                    **PARAMS,
                    "mode": mode,
                    **(
                        {"version_identity": plan.version.identity}
                        if mode == "exact_version"
                        else {}
                    ),
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["evidence_mode"] == "OFFLINE_FIXTURE"
            assert data["version_identity"] == plan.version.identity
            assert data["content_sha256"] == plan.version.content_digest
            assert len(data["measurements"]) == 4
            assert {m["reference_period"] for m in data["measurements"]} == {"2026-08"}
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/r5/cpi").status_code == 405
        assert client.get("/api/r5/cpi/jobs").json()["status"] == "UNKNOWN"
    assert len(connections) == 3
    for conn in connections:
        assert conn.closed and all(c.closed for c in conn.cursors)
        assert conn.cleanup == ["rollback", "close"]
        assert conn.commands[0][0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        assert conn.business_reads == 3
        assert not any(
            q.lstrip().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "COMMIT")
            )
            for q, _ in conn.commands
        )
    assert ledger.snapshot() == before


def test_disabled_factory_and_default_app_never_call_connector():
    def forbidden(**options):
        pytest.fail("disabled_connector_called")

    with pytest.raises(ReadContractError):
        with pg.dev_read_scope_factory(forbidden, IDENTITY)():
            pytest.fail("disabled_scope_entered")
    with TestClient(create_app()) as client:
        assert (
            client.get("/api/r5/cpi", params=PARAMS).json()["classification"]
            == "capability_not_configured"
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("host", "127.0.0.1"),
        ("hostaddr", "127.0.0.1"),
        ("port", 5433),
        ("dbname", "wrong"),
        ("user", "wrong"),
        ("server_version", 180005),
        ("get_parameters", lambda: {"sslmode": "require"}),
    ],
)
def test_client_identity_fail_closed_before_business(field, value):
    scope, connections = factory(
        fixture_ledger()[0], lambda c: setattr(c.info, field, value)
    )
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("identity_admitted")
    assert connections[0].business_reads == 0
    assert connections[0].closed


def test_actual_libpq_tls_flag_checked():
    scope, connections = factory(
        fixture_ledger()[0], lambda c: setattr(c.pgconn, "ssl_in_use", False)
    )
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("plaintext_admitted")
    assert connections[0].business_reads == 0


@pytest.mark.parametrize("index", range(11))
def test_server_identity_fail_closed_before_business(index):
    def mutate(conn):
        conn.server = tuple(
            "wrong" if i == index else value for i, value in enumerate(conn.server)
        )

    scope, connections = factory(fixture_ledger()[0], mutate)
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("server_admitted")
    assert connections[0].business_reads == 0
    assert connections[0].closed


@pytest.mark.parametrize("index", range(10))
def test_schema_oid_owner_relation_rls_privileges_checked(index):
    def mutate(conn):
        conn.schema[0] = tuple(
            "wrong" if i == index else value for i, value in enumerate(conn.schema[0])
        )

    scope, connections = factory(fixture_ledger()[0], mutate)
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("schema_admitted")
    assert connections[0].business_reads == 0
    assert connections[0].closed


@pytest.mark.parametrize("fault", ["execute", "fetch", "rollback", "close"])
def test_driver_errors_discard_results_and_cleanup(fault):
    scope, connections = factory(
        fixture_ledger()[0], lambda c: setattr(c, "fault", fault)
    )
    with TestClient(create_app(scope)) as client:
        for path, params in (
            ("/api/r5/cpi", PARAMS),
            ("/api/r5/cpi/read", {**PARAMS, "mode": "as_of"}),
        ):
            response = client.get(path, params=params)
            assert response.json()["classification"] == "storage_unavailable"
            assert "synthetic-private" not in response.text
            assert "p6-cpi-live:version" not in response.text
    assert all(c.closed and all(cur.closed for cur in c.cursors) for c in connections)


def test_connect_timeout_is_classified_without_raw_context():
    def timeout(**options):
        raise TimeoutError("synthetic-private-driver-detail")

    with pytest.raises(ReadContractError) as caught:
        with pg.dev_read_scope_factory(
            timeout, IDENTITY, enabled=True, pg_profile=PROFILE
        )():
            pass
    assert str(caught.value) == "storage_unavailable"
    assert caught.value.__context__ is caught.value.__cause__ is None


@pytest.mark.parametrize(
    "scenario,classification",
    [
        ("missing_content", "content_missing"),
        ("corrupt_content", "content_digest_mismatch"),
        ("withdrawn", "version_withdrawn"),
        ("missing_material", "lineage_missing_or_mismatched"),
        ("cross_version", "version_definition_mismatch"),
        ("old_timestamp", "unpublished_or_unavailable_as_of"),
        ("wrong_month", "reference_stale_or_mismatched"),
    ],
)
def test_exact_reader_denials_preserved(scenario, classification):
    ledger, plan, _ = fixture_ledger(
        scenario
        if scenario in {"missing_content", "corrupt_content", "withdrawn"}
        else "available"
    )
    if scenario == "missing_material":
        key = plan.version.materials[0]
        del ledger._records[(key.kind.value, key.identity, key.revision)]
    if scenario == "cross_version":
        other = replace(plan.version, definition=RAW_DAILY_BARS.key)
        ledger._records[(Kind.VERSION.value, other.identity, "")] = dumps(other)
    params = {
        **PARAMS,
        "mode": "exact_version",
        "version_identity": plan.version.identity,
    }
    if scenario == "old_timestamp":
        params["as_of"] = (BASE + timedelta(seconds=3)).isoformat()
    if scenario == "wrong_month":
        params["reference"] = "2026-09"
    scope, connections = factory(ledger)
    with TestClient(create_app(scope, evidence_mode="OFFLINE_FIXTURE")) as client:
        response = client.get("/api/r5/cpi/read", params=params)
        assert response.status_code == 409
        assert response.json()["classification"] == classification
        catalog = client.get(
            "/api/r5/cpi", params={k: params[k] for k in PARAMS}
        ).json()
        assert catalog["service"] is None
    assert all(c.closed for c in connections)


@pytest.mark.parametrize("table", ["records", "contents", "publications"])
@pytest.mark.parametrize("overflow", ["rows", "bytes"])
def test_bounded_snapshot_never_returns_partial_data(table, overflow):
    def mutate(conn):
        row = conn.tables[table][0]
        if overflow == "rows":
            conn.tables[table] = [row] * 130
        else:
            conn.tables[table][0] = (*row[:-1], "x" * (pg.MAX_ROW_BYTES + 1))

    scope, connections = factory(fixture_ledger()[0], mutate)
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("overflow_admitted")
    assert connections[0].closed


def test_scope_has_no_writer_or_connection_and_expires():
    scope, _ = factory(fixture_ledger()[0])
    with scope() as snapshot:
        for name in (
            "put",
            "publish",
            "put_content",
            "create_namespace",
            "drop_namespace",
            "connection",
            "execute",
            "commit",
            "rollback",
        ):
            assert not hasattr(snapshot, name)
        operator = snapshot.operator_reads()
        assert snapshot.get(BLS_CPI_HEADLINES.key) == BLS_CPI_HEADLINES
        view = read_local_cpi(
            snapshot, reference="2026-08", as_of=datetime.fromisoformat(AT)
        )
        assert view.definition == BLS_CPI_HEADLINES
    with pytest.raises(ReadContractError):
        operator.get(BLS_CPI_HEADLINES.key)


@pytest.mark.parametrize(
    "field,value",
    [
        ("_pool", object()),
        ("_num_transactions", 1),
        ("_pipeline", object()),
        ("_tpc", object()),
        ("autocommit", False),
    ],
)
def test_reject_nonfresh_or_managed_connection_without_adopting(field, value):
    scope, connections = factory(
        fixture_ledger()[0], lambda c: setattr(c, field, value)
    )
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("ambient_connection_adopted")
    assert connections[0].commands == []
    assert "rollback" not in connections[0].cleanup
    assert connections[0].closed


def test_dev_preview_has_no_write_routes_and_is_disabled_by_default():
    def forbidden(**options):
        pytest.fail("disabled_connector_called")

    app = pg.create_dev_readonly_app(forbidden, IDENTITY)
    for operations in app.openapi()["paths"].values():
        assert set(operations) <= {"get", "head"}
    with TestClient(app) as client:
        assert (
            client.get("/api/r5/cpi", params=PARAMS).json()["evidence_mode"]
            == "UNCONFIGURED"
        )
        for path in ("/api/r5/cpi/jobs", "/api/r5/cpi/plans", "/api/r3/instances"):
            assert client.post(path, json={}).status_code == 405
    with pytest.raises(ValueError, match="dev_read_identity_and_connector_required"):
        pg.create_dev_readonly_app(enabled=True)


@pytest.mark.parametrize("table", ["records", "publications"])
def test_corrupt_index_or_payload_fails_before_scope(table):
    def mutate(conn):
        row = conn.tables[table][0]
        conn.tables[table][0] = ("wrong-identity", *row[1:])

    scope, connections = factory(fixture_ledger()[0], mutate)
    with pytest.raises(ReadContractError) as caught:
        with scope():
            pytest.fail("corrupt_snapshot_admitted")
    assert str(caught.value) == "storage_unavailable"
    assert caught.value.__context__ is None
    assert connections[0].closed


def test_total_byte_budget_spans_tables(monkeypatch):
    monkeypatch.setattr(pg, "MAX_TOTAL_BYTES", 1)
    scope, connections = factory(fixture_ledger()[0])
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("byte_budget_ignored")
    assert connections[0].closed


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_body_error_and_cancellation_always_close_and_expire(error):
    scope, connections = factory(fixture_ledger()[0])
    with pytest.raises(
        KeyboardInterrupt if error is KeyboardInterrupt else ReadContractError
    ) as caught:
        with scope() as snapshot:
            raise error("synthetic-private-body-detail")
    if error is RuntimeError:
        assert caught.value.__context__ is caught.value.__cause__ is None
    with pytest.raises(ReadContractError):
        snapshot.get(BLS_CPI_HEADLINES.key)
    assert connections[0].cleanup == ["rollback", "close"]


def test_reused_closed_connection_is_rejected():
    conn = Connection(fixture_ledger()[0])
    factory = pg.dev_read_scope_factory(
        lambda **kwargs: conn, IDENTITY, enabled=True, pg_profile=PROFILE
    )
    with factory():
        pass
    before = list(conn.commands)
    with pytest.raises(ReadContractError):
        with factory():
            pytest.fail("connection_reused")
    assert conn.commands == before


@pytest.mark.parametrize(
    "status", [TransactionStatus.INTRANS, TransactionStatus.INERROR]
)
def test_ambient_transaction_is_discarded_without_rollback_or_sql(status):
    scope, connections = factory(
        fixture_ledger()[0], lambda c: setattr(c.info, "transaction_status", status)
    )
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("ambient_transaction_adopted")
    assert connections[0].commands == []
    assert connections[0].cleanup == ["close"]


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_shared_transport_modes_preserve_readonly_reader(tmp_path, monkeypatch, mode):
    root = None if mode == "require" else ca_file(tmp_path)
    profile = load_pg_config(runtime_file(tmp_path, mode, root=root))
    monkeypatch.setitem(globals(), "PROFILE", profile)
    ledger, plan, _ = fixture_ledger()
    before = ledger.snapshot()
    scope, connections = factory(ledger)
    with TestClient(create_app(scope, evidence_mode="OFFLINE_FIXTURE")) as client:
        for read_mode in ("as_of", "exact_version"):
            response = client.get(
                "/api/r5/cpi/read",
                params={
                    **PARAMS,
                    "mode": read_mode,
                    **(
                        {"version_identity": plan.version.identity}
                        if read_mode == "exact_version"
                        else {}
                    ),
                },
            )
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "OK"
            assert data["evidence_mode"] == "OFFLINE_FIXTURE"
            assert data["version_identity"] == plan.version.identity
            assert data["content_sha256"] == plan.version.content_digest
            assert len(data["measurements"]) == 4
            assert {m["reference_period"] for m in data["measurements"]} == {"2026-08"}
    assert ledger.snapshot() == before
    assert len(connections) == 2
    assert all(c.closed and all(cur.closed for cur in c.cursors) for c in connections)
    assert all(c.cleanup == ["rollback", "close"] for c in connections)
    assert all(
        c.commands[0][0] == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        for c in connections
    )
    assert not any(
        q.lstrip().startswith(
            ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "COMMIT")
        )
        for c in connections
        for q, _ in c.commands
    )


def test_enabled_app_requires_profile_and_binds_it():
    ledger, _, _ = fixture_ledger()
    calls = []

    def connector(**kwargs):
        calls.append(kwargs)
        return Connection(ledger)

    with pytest.raises(ValueError, match="dev_read_pg_profile_required"):
        pg.create_dev_readonly_app(connector, IDENTITY, enabled=True)
    with pytest.raises(ReadContractError):
        with pg.dev_read_scope_factory(connector, IDENTITY, enabled=True)():
            pytest.fail("missing_profile_accepted")
    assert calls == []
    app = pg.create_dev_readonly_app(
        connector, IDENTITY, enabled=True, pg_profile=PROFILE
    )
    with TestClient(app) as client:
        assert client.get("/api/r5/cpi", params=PARAMS).json()["status"] == "OK"
        assert client.post("/api/r5/cpi/jobs", json={}).status_code == 405
    assert calls[0]["sslrootcert"] == str(PROFILE.sslrootcert)


@pytest.mark.parametrize("change", ["removed_ca", "environment", "role"])
def test_profile_failure_precedes_connector(monkeypatch, change):
    def forbidden(**kwargs):
        pytest.fail("invalid_profile_connected")

    identity = IDENTITY
    if change == "removed_ca":
        PROFILE.sslrootcert.unlink()
    elif change == "environment":
        monkeypatch.setenv("PGHOSTADDR", "203.0.113.9")
    else:
        identity = replace(IDENTITY, role="wrong")
    with pytest.raises(ReadContractError) as caught:
        with pg.dev_read_scope_factory(
            forbidden, identity, enabled=True, pg_profile=PROFILE
        )():
            pytest.fail("invalid_profile_accepted")
    assert caught.value.__cause__ is caught.value.__context__ is None


def test_connector_cannot_replace_trust_root():
    scope, connections = factory(
        fixture_ledger()[0],
        lambda c: setattr(
            c.info,
            "get_parameters",
            lambda: {"sslmode": "verify-full", "sslrootcert": "/wrong.crt"},
        ),
    )
    with pytest.raises(ReadContractError):
        with scope():
            pytest.fail("replaced_trust_accepted")
    assert connections[0].business_reads == 0
    assert connections[0].closed


@pytest.mark.parametrize("mode", ["require", "verify-ca", "verify-full"])
def test_transport_direct_canonical_reads(tmp_path, monkeypatch, mode):
    """Scope-level proof is separate from the required HTTP/Host acceptance."""
    root = None if mode == "require" else ca_file(tmp_path)
    profile = load_pg_config(runtime_file(tmp_path, mode, root=root))
    monkeypatch.setitem(globals(), "PROFILE", profile)
    ledger, plan, _ = fixture_ledger()
    before = ledger.snapshot()
    scope, connections = factory(ledger)
    for version in (None, plan.version.identity):
        with scope() as snapshot:
            view = read_local_cpi(
                snapshot,
                reference="2026-08",
                as_of=datetime.fromisoformat(AT),
                version_identity=version,
            )
            assert view.version == plan.version
    assert ledger.snapshot() == before
    assert len(connections) == 2
    for connection in connections:
        assert connection.cleanup == ["rollback", "close"]
        assert (
            connection.commands[0][0]
            == "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
        )
        assert not any(
            q.lstrip().startswith(
                ("INSERT", "UPDATE", "DELETE", "CREATE", "DROP", "COMMIT")
            )
            for q, _ in connection.commands
        )


def test_missing_profile_fails_before_connector():
    def forbidden(**kwargs):
        pytest.fail("missing_profile_connected")

    with pytest.raises(ValueError, match="dev_read_pg_profile_required"):
        pg.create_dev_readonly_app(forbidden, IDENTITY, enabled=True)
    with pytest.raises(ReadContractError):
        with pg.dev_read_scope_factory(forbidden, IDENTITY, enabled=True)():
            pytest.fail("missing_profile_accepted")
