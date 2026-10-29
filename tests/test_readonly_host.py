"""Read composition over real PG snapshot machinery and synthetic wire rows."""

import socket
from datetime import datetime, timedelta
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient
from test_bea_pce_vertical import BASE as PCE_BASE
from test_bea_pce_vertical import publish

from market_data_foundation.storage.postgres_runtime_config import load_pg_config
from market_data_foundation.web.cpi_pg_readonly_scope import DevReadIdentity
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.package_catalog import PackageCatalog, SaveCommand
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, ORIGIN
from market_data_foundation.web.readonly_host import (
    ReadonlySettings,
    create_readonly_preview,
)
from tests import test_b3_a2_cpi_pg_readonly_scope as wire
from tests.r5_fixture import AT, fixture_ledger

ALLOW = CpiQueryAccess(metadata_permission="allowed", read_permission="allowed")
PATH = "/api/r5/packages/macro"


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external_io_forbidden")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def setup(
    tmp_path,
    monkeypatch,
    *,
    cpi_access=ALLOW,
    pce_access=ALLOW,
    initialize=True,
    enabled_caps=(),
    scenario="default",
    package_config_save=False,
):
    config = tmp_path / "trusted-config"
    config.mkdir()
    (config / "runtime.toml").write_text("""[postgres]
host="192.168.10.4"
port=5432
database="market_data_foundation_dev"
user="market_data_foundation_dev"
sslmode="require"
""")
    (config / "secret.toml").write_text("""[postgres]
password="synthetic-pg-password"
[bls]
api_key="synthetic-bls-key"
[bea]
api_key="synthetic-bea-key"
""")
    profile = load_pg_config(config / "runtime.toml")
    monkeypatch.setattr(wire, "PROFILE", profile)
    cpi, cpi_plan, _ = fixture_ledger(
        scenario if scenario in {"empty", "partial", "missing_content"} else "available"
    )
    pce, _, pce_result = publish()
    pce_plan = pce_result.plan
    cpi_before, pce_before = cpi.snapshot(), pce.snapshot()
    identity = DevReadIdentity(profile.user, "synthetic_owner", 12345)
    pce_identity = DevReadIdentity(profile.user, "synthetic_owner", 12346)
    settings = ReadonlySettings(
        config,
        tmp_path / "package-store",
        "synthetic-host",
        identity,
        pce_identity,
        package_config_save=package_config_save,
    )
    if initialize:
        store = LocalPackageStore.initialize(
            settings.package_store_directory, PackageCatalog()
        )
        if enabled_caps:
            store.save(
                settings.environment_id,
                "macro",
                SaveCommand(
                    expected_revision=None, enabled_capability_ids=list(enabled_caps)
                ),
            )
    connections = []

    class Cursor(wire.Cursor):
        def execute(self, query, params):
            text = query if isinstance(query, str) else query.as_string()
            if "pg_namespace" in text:
                self.connection.selected_schema = params[0]
                is_pce = params[0] == "mdf_macro_bea_pce_vertical_r1"
                selected = pce if is_pce else cpi
                self.connection.tables = wire.Connection(selected).tables
                oid = pce_identity.schema_oid if is_pce else identity.schema_oid
                self.connection.schema = [
                    (
                        oid,
                        "synthetic_owner",
                        table,
                        "r",
                        "p",
                        False,
                        False,
                        "synthetic_owner",
                        True,
                        True,
                    )
                    for table in ("contents", "publications", "records")
                ]
                if scenario == "wrong_oid":
                    self.connection.schema = [
                        (x[0] + 1, *x[1:]) for x in self.connection.schema
                    ]
            return super().execute(query, params)

    class Connection(wire.Connection):
        def cursor(self):
            cursor = Cursor(self)
            self.cursors.append(cursor)
            return cursor

    def connect(**kwargs):
        assert (
            kwargs["options"]
            == "-c statement_timeout=3000 -c default_transaction_read_only=on"
        )
        assert kwargs["password"] == "synthetic-pg-password"
        connection = Connection(cpi)
        if scenario in {"execute", "rollback", "close"}:
            connection.fault = scenario
        connections.append(connection)
        return connection

    at = max(datetime.fromisoformat(AT), PCE_BASE + timedelta(days=1))
    app = create_readonly_preview(
        settings,
        enabled=True,
        connector=connect,
        cpi_access=cpi_access,
        pce_access=pce_access,
        clock=lambda: at,
    )
    runtime = app.state.readonly_runtime
    return (
        app,
        runtime,
        connections,
        (cpi, cpi_before, pce, pce_before),
        cpi_plan,
        pce_plan,
    )


def client(app, **kwargs):
    return TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234), **kwargs
    )


def test_constructor_is_opt_in_and_does_not_load_a_secret_or_connect_before_request(
    tmp_path, monkeypatch
):
    with pytest.raises(ValueError, match="readonly_host_opt_in_required"):
        create_readonly_preview(None)
    app, runtime, connections, _, _, _ = setup(tmp_path, monkeypatch)
    assert connections == []
    (runtime.settings.config_directory / "secret.toml").unlink()
    assert app is not None and connections == []


def test_real_scope_projection_local_query_and_both_exact_readers_are_isolated(
    tmp_path, monkeypatch
):
    app, runtime, connections, ledgers, cpi_plan, pce_plan = setup(
        tmp_path, monkeypatch
    )
    with client(app) as c:
        package = c.get(PATH)
        assert package.status_code == 200, package.text
        data = package.json()
        assert data["enabled_capability_ids"] == []
        held = {a["identity"]: a for a in data["assets"]}
        assert set(held) == {
            "mdf.macro.bls.cpi.headlines",
            "mdf.macro.bea.pce.headline-core",
        }
        for asset in held.values():
            r = asset["revisions"][0]
            assert r["visibility_basis"] == ["RETAINED_HISTORY"]
            assert r["local_read"]["allowed"] is True
            assert r["upstream_read"]["allowed"] is False
        assert (
            data["history_completeness"] == "UNKNOWN"
            and data["write_eligible"] is False
        )
        assert all(not s["replace_supported"] for s in data["credential_states"])
        query = c.get("/api/r5/cpi/query", params={"reference": "2026-08"})
        assert query.status_code == 200, query.text
        assert query.json()["channel"] == "local" and query.json()["durable"] is True
        assert query.json()["version_identity"] == cpi_plan.version.identity
        exact = c.get(
            "/api/r5/cpi/read",
            params={
                "reference": "2026-08",
                "as_of": runtime.clock().isoformat(),
                "mode": "exact_version",
                "version_identity": cpi_plan.version.identity,
            },
        )
        assert exact.status_code == 200, exact.text
        pce = c.get(
            "/api/pce/read",
            params={
                "reference": "2026-07",
                "as_of": runtime.clock().isoformat(),
                "version": pce_plan.version.identity,
            },
        )
        assert pce.status_code == 200, pce.text
        assert pce.json()["version_identity"] == pce_plan.version.identity
        assert c.get("/api/pce/catalog").json()["write_eligible"] is False
        assert "synthetic-pg-password" not in package.text
        assert "synthetic-bls-key" not in package.text
    assert {conn.selected_schema for conn in connections} == {
        "mdf_b3_cpi_05_dev_r1",
        "mdf_macro_bea_pce_vertical_r1",
    }
    assert all(
        conn.closed and conn.cleanup == ["rollback", "close"] for conn in connections
    )
    assert all(
        not any(
            sql.lstrip().startswith(("INSERT", "UPDATE", "DELETE", "CREATE", "ALTER"))
            for sql, _ in conn.commands
        )
        for conn in connections
    )
    assert ledgers[0].snapshot() == ledgers[1] and ledgers[2].snapshot() == ledgers[3]


@pytest.mark.parametrize(
    "method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE"]
)
@pytest.mark.parametrize(
    "path",
    [
        PATH,
        "/api/pce/jobs",
        "/api/pce/jobs/backfill",
        "/api/r5/cpi/jobs/backfill",
        "/api/r3/management",
    ],
)
def test_every_mutation_rejected_before_storage(method, path, tmp_path, monkeypatch):
    app, runtime, connections, _, _, _ = setup(tmp_path, monkeypatch)
    before = runtime.packages.store.path.read_bytes()
    with client(app) as c:
        response = c.request(
            method, path, headers={"Origin": CANDIDATE_ORIGIN}, json={}
        )
        assert response.status_code == 405 and response.headers["allow"] == "GET, HEAD"
    assert connections == [] and runtime.packages.store.path.read_bytes() == before

    def walk(application):
        for route in getattr(application, "routes", []):
            assert getattr(route, "methods", set()) <= {"GET", "HEAD"}
            if hasattr(route, "app"):
                walk(route.app)

    walk(app)


@pytest.mark.parametrize(
    "headers",
    [
        {"Host": ORIGIN.removeprefix("http://")},
        {"Origin": ORIGIN},
        {"X-Role": "owner"},
        {"X-Forwarded-For": "192.168.10.25"},
        {"Authorization": "synthetic"},
    ],
)
def test_transport_forgery_never_reaches_data(headers, tmp_path, monkeypatch):
    app, _, connections, _, _, _ = setup(tmp_path, monkeypatch)
    with client(app) as c:
        assert c.get(PATH, headers=headers).status_code == 403
    assert connections == []


def test_unknown_or_denied_asset_rights_do_not_become_absence_or_read_access(
    tmp_path, monkeypatch
):
    app, _, connections, _, _, _ = setup(
        tmp_path,
        monkeypatch,
        cpi_access=CpiQueryAccess(),
        pce_access=CpiQueryAccess(
            metadata_permission="denied", read_permission="denied"
        ),
    )
    with client(app) as c:
        result = c.get(PATH).json()
        assert result["assets"] == [] and result["history_completeness"] == "UNKNOWN"
        for path in [
            "/api/r5/cpi/query?reference=2026-08",
            "/api/r5/cpi/read",
            "/api/pce/read",
            "/api/pce/assets",
        ]:
            assert c.get(path).status_code == 403
    assert connections == []


def test_missing_config_store_is_not_created_but_local_query_remains_independent(
    tmp_path, monkeypatch
):
    app, runtime, _, _, _, _ = setup(tmp_path, monkeypatch, initialize=False)
    with client(app) as c:
        response = c.get(PATH)
        assert (
            response.status_code == 503
            and response.json()["classification"] == "config_storage_unavailable"
        )
        query = c.get("/api/r5/cpi/query", params={"reference": "2026-08"})
        assert query.status_code == 200 and query.json()["channel"] == "local"
    assert not runtime.settings.package_store_directory.exists()


@pytest.mark.parametrize("scenario", ["wrong_oid", "execute", "rollback", "close"])
def test_failed_pg_scope_keeps_history_unknown_and_query_cannot_transmit(
    scenario, tmp_path, monkeypatch
):
    app, _, connections, _, _, _ = setup(
        tmp_path, monkeypatch, scenario=scenario, enabled_caps=("cpi",)
    )
    with client(app) as c:
        response = c.get(PATH)
        assert response.status_code == 200
        asset = next(
            a
            for a in response.json()["assets"]
            if a["identity"] == "mdf.macro.bls.cpi.headlines"
        )
        assert asset["revisions"][0]["local_holding"] == "UNKNOWN"
        assert asset["revisions"][0]["local_read"]["allowed"] is None
        query = c.get("/api/r5/cpi/query", params={"reference": "2026-08"})
        assert (
            query.status_code == 503
            and query.json()["classification"] == "storage_unavailable"
        )
    assert connections and all(conn.closed for conn in connections)


def test_local_miss_does_not_enable_a_source_and_does_not_change_package_snapshot(
    tmp_path, monkeypatch
):
    app, runtime, _, _, _, _ = setup(
        tmp_path, monkeypatch, scenario="empty", enabled_caps=("cpi",)
    )
    runtime.packages.credential_loader = Mock(
        side_effect=AssertionError("source loader must not run")
    )
    before = runtime.packages.store.path.read_bytes()
    with client(app) as c:
        response = c.get(
            "/api/r5/cpi/query",
            params={
                "reference": "2026-08",
                "operation_id": "mdf.source-candidate.bls-v2.cpi.direct@p4-direct-r1",
            },
        )
        assert response.status_code == 503
        assert response.json()["classification"] == "source_binding_unconfigured"
        assert response.json()["local_reason"] == "local_reference_missing"
    runtime.packages.credential_loader.assert_not_called()
    assert runtime.packages.store.path.read_bytes() == before


def test_runtime_transport_change_fails_before_connect_and_does_not_use_old_profile(
    tmp_path, monkeypatch
):
    app, runtime, connections, _, _, _ = setup(tmp_path, monkeypatch)
    path = runtime.settings.config_directory / "runtime.toml"
    path.write_text(
        path.read_text().replace('sslmode="require"', 'sslmode="verify-full"')
    )
    with client(app) as c:
        response = c.get("/api/r5/cpi/query", params={"reference": "2026-08"})
        assert (
            response.status_code == 503
            and response.json()["classification"] == "storage_unavailable"
        )
    assert connections == []


def test_readonly_host_uses_published_key_reference_not_legacy_value(
    tmp_path, monkeypatch
):
    app, runtime, _, _, _, _ = setup(tmp_path, monkeypatch, enabled_caps=("cpi",))
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(
            expected_revision="r1",
            enabled_capability_ids=["cpi"],
            source_changes=[
                {
                    "source_id": "BLS",
                    "credential_action": "replace",
                    "credential_value": "synthetic-managed-key",
                }
            ],
        ),
        allow_replace=True,
    )
    assert runtime.packages.load_credential("macro", "BLS") == "synthetic-managed-key"
    current = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    (
        runtime.packages.store.root / "credentials" / current.credential_refs["BLS"]
    ).unlink()
    with pytest.raises(Exception, match="credential_storage_unavailable"):
        runtime.packages.load_credential("macro", "BLS")
    with client(app) as c:
        package = c.get(PATH)
        assert package.status_code == 200
        state = next(
            s for s in package.json()["credential_states"] if s["source_id"] == "BLS"
        )
        assert state["credential_state"] == "unknown" and not state["replace_supported"]
        assert "synthetic-managed-key" not in package.text
        assert "credential_refs" not in package.text
        assert (
            c.get("/api/r5/cpi/query", params={"reference": "2026-08"}).status_code
            == 200
        )


def test_builder_never_constructs_live_write_or_provider_adapters(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.bls_cpi_direct_live import (
        ExactOneHttpsTransport,
    )
    from market_data_foundation.historical.bls_cpi_material_store import (
        GarageMaterialStore,
    )
    from market_data_foundation.historical.cpi_pg_journal import (
        PostgresJobJournal as CpiJournal,
    )
    from market_data_foundation.historical.pce_pg_journal import (
        PostgresJobJournal as PceJournal,
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("writer_or_source_constructed")

    for cls in (CpiJournal, PceJournal, ExactOneHttpsTransport, GarageMaterialStore):
        monkeypatch.setattr(cls, "__init__", forbidden)
    app, _, _, _, _, _ = setup(tmp_path, monkeypatch)
    with client(app) as c:
        assert c.get(PATH).status_code == 200
        assert (
            c.get("/api/r5/cpi/query", params={"reference": "2026-08"}).status_code
            == 200
        )


def test_static_shell_is_last_and_does_not_capture_pce_api_routes(
    tmp_path, monkeypatch
):
    _, runtime, _, _, _, _ = setup(tmp_path, monkeypatch)
    ui = tmp_path / "web"
    (ui / "assets").mkdir(parents=True)
    (ui / "index.html").write_text("<main>MDF shell</main>")
    app = create_readonly_preview(
        runtime.settings,
        enabled=True,
        connector=runtime.connector,
        clock=runtime.clock,
        cpi_access=ALLOW,
        pce_access=ALLOW,
        ui_directory=ui,
    )
    with client(app) as c:
        root = c.get("/", follow_redirects=False)
        assert root.status_code == 307 and root.headers["location"] == "/pce"
        assert c.get("/pce").text == "<main>MDF shell</main>"
        assert c.get("/api/pce/catalog").json()["source_qualified"] is False
        assert (
            c.get(
                "/api/pce/read",
                params={"reference": "2026-07", "as_of": runtime.clock().isoformat()},
            ).status_code
            == 200
        )


@pytest.mark.parametrize("scenario", ["partial", "missing_content"])
def test_history_retained_when_known_version_is_not_readable(
    tmp_path, monkeypatch, scenario
):
    app, _, _, _, _, _ = setup(tmp_path, monkeypatch, scenario=scenario)
    with client(app) as c:
        response = c.get(PATH)
        assert response.status_code == 200
        row = next(
            a
            for a in response.json()["assets"]
            if a["identity"] == "mdf.macro.bls.cpi.headlines"
        )
        revision = row["revisions"][0]
        assert revision["local_holding"] == "PRESENT"
        assert revision["visibility_basis"] == ["RETAINED_HISTORY"]
        assert revision["local_read"]["allowed"] is not True


def test_history_retained_when_content_permission_is_denied(tmp_path, monkeypatch):
    access = CpiQueryAccess(metadata_permission="allowed", read_permission="denied")
    app, _, _, _, _, _ = setup(tmp_path, monkeypatch, cpi_access=access)
    with client(app) as c:
        response = c.get(PATH)
        assert response.status_code == 200
        row = next(
            a
            for a in response.json()["assets"]
            if a["identity"] == "mdf.macro.bls.cpi.headlines"
        )
        assert row["revisions"][0]["local_holding"] == "PRESENT"
        assert row["revisions"][0]["local_read"]["allowed"] is False
        assert (
            c.get("/api/r5/cpi/query", params={"reference": "2026-08"}).status_code
            == 403
        )


def test_config_save_persists_and_reopens_without_enabling_source_or_jobs(
    tmp_path, monkeypatch
):
    app, runtime, _, ledgers, _, _ = setup(
        tmp_path, monkeypatch, package_config_save=True
    )
    headers = {"Origin": CANDIDATE_ORIGIN}
    with client(app) as c:
        assert c.get(PATH).json()["write_eligible"] is True
        saved = c.put(
            PATH,
            headers=headers,
            json={
                "expected_revision": None,
                "enabled_capability_ids": ["cpi", "ppi", "pce"],
                "source_changes": [
                    {"source_id": "BLS", "credential_action": "keep"},
                    {"source_id": "BEA", "credential_action": "keep"},
                ],
            },
        )
        assert saved.status_code == 200, saved.text
        assert set(saved.json()["enabled_capability_ids"]) == {"cpi", "ppi", "pce"}
        assert len(saved.json()["assets"]) == 3
        assert (
            c.put(
                PATH,
                headers=headers,
                json={"expected_revision": None, "enabled_capability_ids": ["cpi"]},
            ).status_code
            == 409
        )
        for path in [
            "/api/pce/jobs",
            "/api/pce/jobs/backfill",
            "/api/r5/cpi/jobs/backfill",
        ]:
            assert c.post(path, headers=headers, json={}).status_code == 405
        unavailable = c.get("/api/r5/cpi/query", params={"reference": "2026-09"})
        assert unavailable.status_code == 503
        assert unavailable.json()["classification"] == "source_binding_unconfigured"
        assert (
            c.get("/api/r5/cpi/query", params={"reference": "2026-08"}).json()[
                "channel"
            ]
            == "local"
        )
    reopened = create_readonly_preview(
        runtime.settings,
        enabled=True,
        connector=runtime.connector,
        cpi_access=ALLOW,
        pce_access=ALLOW,
        clock=runtime.clock,
    )
    with client(reopened) as c:
        current = c.get(PATH).json()
        assert current["config_revision"] == saved.json()["config_revision"]
        assert set(current["enabled_capability_ids"]) == {"cpi", "ppi", "pce"}
    assert ledgers[0].snapshot() == ledgers[1] and ledgers[2].snapshot() == ledgers[3]


def test_config_save_replaces_private_key_and_disable_retains_it(tmp_path, monkeypatch):
    app, runtime, _, ledgers, _, _ = setup(
        tmp_path, monkeypatch, package_config_save=True
    )
    value = "synthetic-rework-private-value"
    headers = {"Origin": CANDIDATE_ORIGIN}
    with client(app) as c:
        saved = c.put(
            PATH,
            headers=headers,
            json={
                "expected_revision": None,
                "enabled_capability_ids": ["cpi"],
                "source_changes": [
                    {
                        "source_id": "BLS",
                        "credential_action": "replace",
                        "credential_value": value,
                    }
                ],
            },
        )
        assert saved.status_code == 200
        assert value not in saved.text and value not in c.get(PATH).text
        assert runtime.packages.load_credential("macro", "BLS") == value
        disabled = c.put(
            PATH,
            headers=headers,
            json={
                "expected_revision": saved.json()["config_revision"],
                "enabled_capability_ids": [],
            },
        )
        assert disabled.status_code == 200
        assert runtime.packages.load_credential("macro", "BLS") == value
        assert {a["identity"] for a in disabled.json()["assets"]} == {
            "mdf.macro.bls.cpi.headlines",
            "mdf.macro.bea.pce.headline-core",
        }
    assert ledgers[0].snapshot() == ledgers[1] and ledgers[2].snapshot() == ledgers[3]


@pytest.mark.parametrize(
    "path,headers",
    [
        (PATH, {}),
        (PATH, {"Origin": "http://untrusted.invalid"}),
        (PATH, {"Origin": CANDIDATE_ORIGIN, "X-Role": "owner"}),
        (PATH, {"Origin": CANDIDATE_ORIGIN, "Content-Type": "text/plain"}),
        (PATH + "?environment=other", {"Origin": CANDIDATE_ORIGIN}),
        ("/api/r5/packages/unknown", {"Origin": CANDIDATE_ORIGIN}),
        ("/api/r3/management", {"Origin": CANDIDATE_ORIGIN}),
    ],
)
def test_config_put_requires_exact_origin_and_route(
    tmp_path, monkeypatch, path, headers
):
    app, runtime, connections, _, _, _ = setup(
        tmp_path, monkeypatch, package_config_save=True
    )
    before = (runtime.settings.package_store_directory / "packages.json").read_bytes()
    with client(app) as c:
        assert c.put(
            path,
            headers=headers,
            json={"expected_revision": None, "enabled_capability_ids": ["cpi"]},
        ).status_code in {403, 405}
    assert (
        before
        == (runtime.settings.package_store_directory / "packages.json").read_bytes()
    )
    assert connections == []


def test_config_http_adapter_accepts_one_json_put_and_never_delegates_job_writes(
    tmp_path, monkeypatch
):
    import json
    from email.message import Message
    from io import BytesIO
    from pathlib import Path
    from runpy import run_path
    from types import SimpleNamespace

    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    handler_type = run_path(str(scripts / "mdf_readonly_preview.py"))[
        "ConfigurationHandler"
    ]
    app, runtime, _, ledgers, _, _ = setup(
        tmp_path, monkeypatch, package_config_save=True
    )
    body = json.dumps(
        {"expected_revision": None, "enabled_capability_ids": ["cpi", "ppi", "pce"]}
    ).encode()
    handler = handler_type.__new__(handler_type)
    handler.command = "PUT"
    handler.path = PATH
    handler.client_address = ("192.168.10.25", 1234)
    handler.server = SimpleNamespace(app=app)
    handler.headers = Message()
    for name, value in {
        "Host": CANDIDATE_ORIGIN.removeprefix("http://"),
        "Origin": CANDIDATE_ORIGIN,
        "Content-Type": "application/json",
        "Content-Length": str(len(body)),
    }.items():
        handler.headers[name] = value
    handler.rfile = BytesIO(body)
    calls = []
    handler.respond = lambda status, *args: calls.append((status, args))
    handler.do_PUT()
    assert calls[0][0] == 200
    assert set(
        runtime.packages.store.read("synthetic-host", "macro").enabled_capability_ids
    ) == {"cpi", "ppi", "pce"}
    for method in ("do_POST", "do_PATCH", "do_DELETE", "do_OPTIONS"):
        getattr(handler, method)()
    assert [status for status, _ in calls[1:]] == [403] * 4
    assert ledgers[0].snapshot() == ledgers[1] and ledgers[2].snapshot() == ledgers[3]
