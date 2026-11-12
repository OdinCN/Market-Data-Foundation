"""Product D-01: isolated configuration, Host permissions, real in-process API."""

import json
import os
import socket
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from unittest.mock import Mock

import psycopg
import pytest
from fastapi.testclient import TestClient

from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.package_catalog import (
    PackageCatalog,
    PackageError,
    PackageHistory,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import (
    PackageAccess,
    PackageHost,
    create_package_app,
)
from tests.d01_package_fixture import fixture_host

PATH = "/api/r5/packages"
WORKTREE = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("D-01 deterministic tests attempted external IO")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def command(enabled, revision=None, changes=None):
    return {
        "expected_revision": revision,
        "enabled_capability_ids": enabled,
        "source_changes": changes or [],
    }


def put(client, enabled, revision=None, package="macro", changes=None):
    return client.put(f"{PATH}/{package}", json=command(enabled, revision, changes))


@pytest.mark.parametrize("mask", range(8))
def test_all_macro_combinations_complete_set_persists_and_unknown_is_not_ready(
    tmp_path, mask
):
    root = tmp_path / "configuration"
    host = fixture_host(root, scenario="history_unknown")
    enabled = [c for i, c in enumerate(("cpi", "ppi", "pce")) if mask & (1 << i)]
    with TestClient(create_package_app(host)) as client:
        assert put(client, ["cpi", "ppi", "pce"]).status_code == 200
        response = put(client, enabled, "r1")
        assert response.status_code == 200, response.text
        view = response.json()
        assert view["enabled_capability_ids"] == sorted(enabled)
        assert view["config_revision"] == "r2"
        assert len(view["assets"]) == len(enabled)
        expected = sorted(
            ({"BLS"} if {"cpi", "ppi"} & set(enabled) else set())
            | ({"BEA"} if "pce" in enabled else set())
        )
        assert view["required_sources"] == expected
        assert view["history_completeness"] == "UNKNOWN"
        for asset in view["assets"]:
            revision = asset["revisions"][0]
            assert revision["local_read"]["allowed"] is None
            assert revision["upstream_read"]["allowed"] is not True
            assert revision["collect"]["allowed"] is None
            assert revision["plan"]["allowed"] is None
    reopened = fixture_host(root, initialize=False, scenario="history_unknown")
    with TestClient(create_package_app(reopened)) as client:
        assert client.get(f"{PATH}/macro").json() == view
        assert put(client, enabled, "r1").status_code == 409


def test_first_enable_required_keep_and_package_environment_isolation(tmp_path):
    host = fixture_host(tmp_path / "configuration")
    with TestClient(create_app(package_host=host)) as client:
        before = host.store.path.read_bytes()
        assert put(client, []).json()["classification"] == "first_enable_required"
        assert host.store.path.read_bytes() == before
        ppi = put(
            client, ["ppi"], changes=[{"source_id": "BLS", "credential_action": "keep"}]
        ).json()
        assert ppi["required_sources"] == ["BLS"]
        assert {
            s["source_id"]: s["credential_state"] for s in ppi["credential_states"]
        } == {
            "BLS": "configured",
            "BEA": "missing",
            "Census": "configured",
            "FRED": "configured",
        }
        assert put(client, ["daily_raw"], package="tdx").status_code == 200
        assert client.get(f"{PATH}/macro").json() == ppi
        other = PackageHost(host.store, "other-environment", authorize=host.authorize)
        with TestClient(create_package_app(other)) as other_client:
            assert (
                other_client.get(f"{PATH}/macro").json()["configuration_state"]
                == "ABSENT"
            )
            assert put(other_client, ["cpi"]).status_code == 200
        assert client.get(f"{PATH}/macro").json() == ppi
        assert put(client, [], "r1").status_code == 200
        assert client.get(f"{PATH}/tdx").json()["enabled_capability_ids"] == [
            "daily_raw"
        ]
        assert client.get(f"{PATH}/missing").status_code == 404
    persisted = json.loads(host.store.path.read_text())
    assert set(persisted) == {"schema_revision", "records"}
    for record in persisted["records"]:
        assert set(record) == {
            "environment_id",
            "package_id",
            "revision",
            "enabled_capability_ids",
            "credential_refs",
        }


def test_real_identities_source_variants_and_other_code_catalog_are_preserved(tmp_path):
    host = fixture_host(tmp_path / "configuration", scenario="history_unknown")
    with TestClient(create_package_app(host)) as client:
        macro = put(client, ["cpi", "pce"]).json()
        # Select by trusted capability references, not display-name aliases.
        refs = {c["capability_id"]: c["definition_ref"] for c in macro["capabilities"]}
        assert refs["pce"] == {
            "identity": "mdf.macro.bea.pce.headline-core",
            "revision": "t04-r1",
        }
        assert refs["gdp"] == {
            "identity": "mdf.macro.bea.gdp-growth",
            "revision": "r1",
        }
        assert "gdp" not in macro["enabled_capability_ids"]
        cpi = next(
            a for a in macro["assets"] if a["identity"] == refs["cpi"]["identity"]
        )
        assert len(cpi["revisions"]) == 1
        assert len(cpi["revisions"][0]["source_entries"]) == 4
        assert len({r["operation"] for r in cpi["revisions"][0]["source_entries"]}) > 1
        tdx = put(client, ["daily_raw"], package="tdx").json()
        assert len(tdx["assets"]) == 1
        assert len(tdx["assets"][0]["revisions"][0]["source_entries"]) == 2
        kd = put(client, ["current_rank"], package="kd").json()
        assert kd["assets"][0]["revisions"][0]["upstream_read"] == {
            "allowed": False,
            "reasons": ["candidate_has_no_runtime_binding"],
        }
        directory = client.get("/api/r5/sources").json()
        assert directory["evidence_mode"] == "CODE_CONTRACTS_ONLY"
        assert len(directory["entries"]) == 28
        assert any(
            e["identity"] == "mdf.source.census.m3.capital-goods@r1"
            for e in directory["entries"]
        )
        assert (
            len(
                {
                    (e["asset"]["identity"], e["asset"]["revision"])
                    for e in directory["entries"]
                    if e["asset"]
                }
            )
            == 18
        )


@pytest.mark.parametrize(
    "scenario,allowed",
    [
        ("default", True),
        ("read_denied", False),
        ("read_unknown", None),
        ("unpublished", False),
    ],
)
def test_disabled_retained_history_visible_independently_of_content_read(
    tmp_path, scenario, allowed
):
    host = fixture_host(tmp_path / "configuration", scenario=scenario)
    with TestClient(create_package_app(host)) as client:
        assert put(client, ["ppi"]).status_code == 200
        view = put(client, [], "r1").json()
        assert view["enabled_capability_ids"] == view["required_sources"] == []
        assert len(view["assets"]) == 1
        history = view["assets"][0]["revisions"][0]
        assert history["visibility_basis"] == ["RETAINED_HISTORY"]
        assert history["local_holding"] == "PRESENT"
        assert history["local_read"]["allowed"] is allowed
        assert history["upstream_read"]["allowed"] is False
        assert history["collect"]["allowed"] is False
        assert view["history_completeness"] == "COMPLETE"


@pytest.mark.parametrize("scenario", ["metadata_denied", "metadata_unknown"])
def test_history_metadata_denied_or_unknown_does_not_disclose_holding(
    tmp_path, scenario
):
    host = fixture_host(tmp_path / "configuration", scenario=scenario)
    with TestClient(create_package_app(host)) as client:
        visible = put(client, ["ppi"]).json()["assets"][0]["revisions"][0]
        assert visible["local_holding"] == "UNKNOWN"
        assert visible["local_read"]["allowed"] is None
        assert visible["visibility_basis"] == ["ENABLED_MAPPING"]
        off = put(client, [], "r1").json()
        assert off["assets"] == [] and off["history_completeness"] == "UNKNOWN"
        # Hidden present and hidden absent evidence produce the same projection.
        original = host.history
        host.history = lambda *args: PackageHistory(
            completeness="COMPLETE",
            history=[
                h.model_copy(update={"holding": "absent"})
                for h in original(*args).history
            ],
        )
        assert client.get(f"{PATH}/macro").json() == off


@pytest.mark.parametrize("scenario", ["read_only", "write_unknown"])
def test_authorization_unknown_or_denied_prevents_writes(tmp_path, scenario):
    host = fixture_host(tmp_path / "configuration", scenario=scenario)
    before = host.store.path.read_bytes()
    with TestClient(create_package_app(host)) as client:
        view = client.get(f"{PATH}/macro").json()
        assert view["write_eligible"] is (
            None if scenario == "write_unknown" else False
        )
        assert put(client, ["cpi"]).status_code == 403
    assert host.store.path.read_bytes() == before


def test_default_unmounted_and_untrusted_identity_fail_closed_without_storage_access(
    tmp_path,
):
    with TestClient(create_app()) as client:
        for response in (
            client.get(PATH),
            client.get(f"{PATH}/macro"),
            put(client, ["cpi"]),
        ):
            assert response.status_code == 503
            assert response.json()["classification"] == "package_host_not_configured"
            assert response.json()["effect"] == "NONE"
    catalog = PackageCatalog()
    store = LocalPackageStore(tmp_path / "never-initialized", catalog)
    host = PackageHost(store, "synthetic-d01")
    host.store.snapshot = Mock(side_effect=AssertionError("unauthorized storage read"))
    with TestClient(create_package_app(host)) as client:
        for path in (PATH, f"{PATH}/macro"):
            response = client.get(
                path, headers={"X-Role": "owner", "X-Operator": "owner"}
            )
            assert response.status_code == 403
            assert (
                response.json()["classification"]
                == "package_metadata_permission_unknown"
            )
        assert put(client, ["cpi"]).status_code == 403
    host.store.snapshot.assert_not_called()


@pytest.mark.parametrize(
    "mutation",
    [
        {"enabled_capability_ids": ["cpi", "cpi"]},
        {"enabled_capability_ids": ["daily_raw"]},
        {"environment_id": "browser-environment"},
        {"store_dir": "/tmp/browser-selected"},
        {
            "source_changes": [
                {
                    "source_id": "BEA",
                    "credential_action": "replace",
                    "value": "synthetic-sensitive",
                }
            ]
        },
        {
            "source_changes": [
                {
                    "source_id": "BLS",
                    "credential_action": "keep",
                    "value": "synthetic-sensitive",
                }
            ]
        },
        {"source_changes": [{"source_id": "TDX", "credential_action": "keep"}]},
        {"source_changes": [{"source_id": "BLS", "credential_action": "keep"}] * 2},
    ],
)
def test_invalid_commands_are_rejected_with_no_change_and_no_input_echo(
    tmp_path, mutation
):
    host = fixture_host(tmp_path / "configuration")
    before = host.store.path.read_bytes()
    with TestClient(create_package_app(host)) as client:
        response = client.put(f"{PATH}/macro", json=command(["cpi"]) | mutation)
        assert response.status_code == 422, response.text
        assert response.json()["effect"] == "NONE"
        assert "synthetic-sensitive" not in response.text
    assert host.store.path.read_bytes() == before


def test_duplicate_json_query_and_invalid_revision_are_typed_and_redacted(tmp_path):
    host = fixture_host(tmp_path / "configuration")
    with TestClient(create_package_app(host)) as client:
        response = client.put(
            f"{PATH}/macro",
            content='{"expected_revision":null,"enabled_capability_ids":["ppi"],"expected_revision":null}',
            headers={"content-type": "application/json"},
        )
        assert response.status_code == 422
        assert (
            put(client, ["cpi"], "synthetic-sensitive").json()["classification"]
            == "invalid_request"
        )
        assert client.get(PATH + "?environment_id=elsewhere").status_code == 422
        assert host.store.snapshot().records == []


def test_two_independent_instances_cas_serializes_and_only_one_wins(tmp_path):
    first = fixture_host(tmp_path / "configuration")
    second = fixture_host(tmp_path / "configuration", initialize=False)
    barrier = Barrier(2)

    def write(host, cap):
        barrier.wait(timeout=5)
        try:
            return host.store.save(
                host.environment_id, "macro", SaveCommand(**command([cap]))
            ).revision
        except PackageError as error:
            return error.failure.classification

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda pair: write(*pair), [(first, "cpi"), (second, "ppi")])
        )
    assert sorted(results) == ["config_revision_conflict", "r1"]
    assert first.store.snapshot() == second.store.snapshot()
    assert len(first.store.snapshot().records) == 1


@pytest.mark.parametrize(
    "damage",
    [
        "file_missing",
        "lock_missing",
        "invalid_json",
        "missing_schema",
        "duplicate_members",
        "duplicate_records",
        "illegal_capability",
    ],
)
def test_unreadable_or_corrupt_storage_is_not_absent_or_empty(tmp_path, damage):
    host = fixture_host(tmp_path / "configuration")
    if damage == "file_missing":
        host.store.path.unlink()
    elif damage == "lock_missing":
        host.store.lock_path.unlink()
    elif damage == "invalid_json":
        host.store.path.write_text("{")
    elif damage == "missing_schema":
        host.store.path.write_text('{"records":[]}')
    elif damage == "duplicate_members":
        host.store.path.write_text('{"records":[],"records":[]}')
    else:
        record = {
            "environment_id": host.environment_id,
            "package_id": "macro",
            "revision": "r1",
            "enabled_capability_ids": ["invalid"]
            if damage == "illegal_capability"
            else ["ppi"],
        }
        host.store.path.write_text(
            json.dumps(
                {
                    "schema_revision": "d01-local-v1",
                    "records": [record, record]
                    if damage == "duplicate_records"
                    else [record],
                }
            )
        )
    with TestClient(create_package_app(host)) as client:
        for response in (
            client.get(PATH),
            client.get(f"{PATH}/macro"),
            put(client, ["ppi"]),
        ):
            assert response.status_code == 503
            assert response.json()["classification"] == (
                "config_storage_unavailable"
                if damage.endswith("missing")
                else "config_storage_corrupt"
            )
            assert "packages" not in response.json()


def test_commit_before_replace_failure_has_no_effect(tmp_path, monkeypatch):
    host = fixture_host(tmp_path / "configuration")
    before = host.store.path.read_bytes()

    def fail(*args):
        raise OSError("synthetic-sensitive-precommit")

    monkeypatch.setattr("market_data_foundation.web.package_store.os.replace", fail)
    with TestClient(create_package_app(host)) as client:
        response = put(client, ["ppi"])
        assert response.status_code == 503
        assert response.json() == {
            "status": "UNAVAILABLE",
            "classification": "config_save_failed",
            "effect": "NONE",
        }
        assert host.store.path.read_bytes() == before
        assert client.get(f"{PATH}/macro").json()["config_revision"] is None
    assert sorted(p.name for p in host.store.root.iterdir()) == [
        "packages.json",
        "packages.lock",
    ]


def test_post_replace_confirmation_failure_is_unknown_re_read_then_old_cas_conflicts(
    tmp_path, monkeypatch
):
    host = fixture_host(tmp_path / "configuration")
    original = os.fsync
    count = 0

    def fail_directory(fd):
        nonlocal count
        count += 1
        if count == 2:
            raise OSError("synthetic-sensitive-postcommit")
        return original(fd)

    monkeypatch.setattr(
        "market_data_foundation.web.package_store.os.fsync", fail_directory
    )
    with TestClient(create_package_app(host)) as client:
        response = put(client, ["ppi"])
        assert response.status_code == 503
        assert response.json() == {
            "status": "UNKNOWN",
            "classification": "config_commit_outcome_unknown",
            "effect": "UNKNOWN",
        }
        assert client.get(f"{PATH}/macro").json()["config_revision"] == "r1"
        assert put(client, ["ppi"]).status_code == 409
        assert host.store.read(host.environment_id, "macro").revision == "r1"


def test_evidence_failure_after_commit_is_unknown_but_get_failure_is_unavailable(
    tmp_path,
):
    host = fixture_host(tmp_path / "configuration")
    original = host.history
    host.history = Mock(side_effect=OSError("synthetic-sensitive-evidence"))
    with TestClient(create_package_app(host)) as client:
        failed = put(client, ["ppi"])
        assert failed.json()["effect"] == "UNKNOWN"
        assert failed.json()["classification"] == "config_commit_outcome_unknown"
        unavailable = client.get(PATH)
        assert unavailable.json()["classification"] == "package_evidence_unavailable"
        assert "synthetic-sensitive" not in unavailable.text
        host.history = original
        assert client.get(f"{PATH}/macro").json()["config_revision"] == "r1"
        assert put(client, ["ppi"]).status_code == 409


def test_package_directory_uses_one_snapshot_even_when_store_changes_during_projection(
    tmp_path,
):
    host = fixture_host(tmp_path / "configuration")
    original = host.history
    writes = []

    def history(env, package, record):
        if package == "macro" and not writes:
            writes.append(
                host.store.save(env, "tdx", SaveCommand(**command(["daily_raw"])))
            )
        return original(env, package, record)

    host.history = history
    host.store.snapshot = Mock(wraps=host.store.snapshot)
    with TestClient(create_package_app(host)) as client:
        response = client.get(PATH)
        assert response.status_code == 200
        views = {p["package_id"]: p for p in response.json()["packages"]}
        assert all(p["configuration_state"] == "ABSENT" for p in views.values())
        host.store.snapshot.assert_called_once()
        assert client.get(f"{PATH}/tdx").json()["config_revision"] == "r1"


def test_openapi_and_types_come_from_product_models_and_preserve_existing_routes():
    schema = create_app().openapi()
    assert json.loads((WORKTREE / "web/openapi.json").read_text()) == schema
    assert PATH in schema["paths"] and PATH + "/{package_id}" in schema["paths"]
    assert "/api/r5/cpi/read" in schema["paths"]
    assert not any("__research__" in path for path in schema["paths"])
    assert (
        schema["components"]["schemas"]["KeepChange"]["properties"][
            "credential_action"
        ]["const"]
        == "keep"
    )
    assert (
        '"/api/r5/packages/{package_id}"'
        in (WORKTREE / "web/src/api.generated.ts").read_text()
    )


def test_json_lines_bridge_persists_across_process_restart(tmp_path):
    root = tmp_path / "mdf-d01-bridge"
    env = os.environ | {
        "MDF_TEST_PIPE_LOOP": "1",
        "PYTHONPATH": f"{WORKTREE}/tests/sandbox_support:{WORKTREE}/src:{WORKTREE}",
    }

    def run(requests):
        result = subprocess.run(
            [sys.executable, "tests/d01_ui_exchange.py"],
            input="".join(
                json.dumps({"store_dir": str(root)} | r) + "\n" for r in requests
            ),
            text=True,
            capture_output=True,
            cwd=WORKTREE,
            env=env,
            timeout=20,
            check=True,
        )
        return [json.loads(line) for line in result.stdout.splitlines()]

    first = run(
        [
            {"method": "GET", "path": PATH},
            {"method": "PUT", "path": PATH + "/macro", "body": command(["ppi"])},
        ]
    )
    assert first[0]["status"] == 200 and first[1]["status"] == 200
    second = run(
        [
            {"method": "GET", "path": PATH + "/macro"},
            {"method": "PUT", "path": PATH + "/macro", "body": command(["ppi"])},
        ]
    )
    assert second[0]["body"] == first[1]["body"]
    assert second[1]["status"] == 409
    assert (
        run(
            [
                {
                    "store_dir": "/srv/market-data-foundation/config/local",
                    "method": "GET",
                    "path": PATH,
                }
            ]
        )[0]["status"]
        == 422
    )


@pytest.mark.parametrize("origin_kind", ["candidate", "dev"])
def test_native_composition_package_put_does_not_grant_other_writes(
    tmp_path, origin_kind
):
    from types import SimpleNamespace

    from market_data_foundation.historical.pce_pg_binding import BINDING
    from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, ORIGIN
    from market_data_foundation.web.pce_r5_host import create_native_preview
    from tests.b3_dev_fixture import fixture

    # Existing offline PG-protocol fixture: no connection, registration or worker.
    server, rig, _ = fixture(tmp_path)
    before = json.dumps(server.data, sort_keys=True)
    pce_journal = Mock(binding=BINDING)
    pce_journal.reader.return_value.snapshot.return_value = []
    pce_host = SimpleNamespace(journal=lambda: pce_journal, read_scope=Mock())
    cpi_host = SimpleNamespace(journal=lambda: rig.journal, read_scope=lambda: Mock())
    package_host = fixture_host(tmp_path / "package-configuration")
    origin = CANDIDATE_ORIGIN if origin_kind == "candidate" else ORIGIN
    app = create_native_preview(
        pce_host,
        cpi_host,
        ui_directory=None,
        enabled=True,
        origin=origin,
        package_host=package_host,
    )
    with TestClient(app, base_url=origin, client=("192.168.10.25", 12345)) as client:
        assert client.get(PATH).status_code == 200
        saved = client.put(
            f"{PATH}/macro", json=command(["ppi"]), headers={"Origin": origin}
        )
        assert saved.status_code == 200
        assert client.get("/api/r5/cpi/jobs").json()["write_eligible"] is False
        assert client.get("/api/pce/catalog").json()["write_eligible"] is (
            origin == ORIGIN
        )
        # Every PUT outside the exact package route still fails at the old gate.
        for path in (
            "/api/r5/cpi/jobs",
            "/api/pce/jobs",
            "/api/r3/management",
            "/api/r5/sources",
            f"{PATH}/macro/extra",
        ):
            assert (
                client.put(path, json={}, headers={"Origin": origin}).status_code == 403
            )
        assert (
            client.post(
                "/api/r5/cpi/jobs", json={}, headers={"Origin": origin}
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/r3/management", json={}, headers={"Origin": origin}
            ).status_code
            == 403
        )
        # Missing or spoofed Origin/identity never reaches the package write.
        for headers in (
            {},
            {"Origin": "http://untrusted.invalid"},
            {"Origin": origin, "X-Role": "owner"},
        ):
            denied = client.put(
                f"{PATH}/macro", json=command(["cpi"], "r1"), headers=headers
            )
            assert denied.status_code == 403 and denied.json()["effect"] == "NONE"
        assert client.get(f"{PATH}/macro").json()["enabled_capability_ids"] == ["ppi"]
    assert json.dumps(server.data, sort_keys=True) == before
    pce_journal.submit.assert_not_called()
    pce_journal.submit_backfill.assert_not_called()
    pce_host.read_scope.assert_not_called()
    assert rig.transport.calls == 0


def test_pce_default_host_preserves_old_mutation_gate_and_packages_fail_closed():
    from datetime import UTC, datetime

    from market_data_foundation.historical.pce_pg_binding import BINDING
    from market_data_foundation.web.pce_api import create_preview
    from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN

    journal = Mock(binding=BINDING)
    app = create_preview(
        journal,
        Mock(),
        lambda: datetime.now(UTC),
        enabled=True,
        origin=CANDIDATE_ORIGIN,
    )
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 12345)
    ) as client:
        assert (
            client.get(PATH).json()["classification"] == "package_host_not_configured"
        )
        assert (
            client.put(
                f"{PATH}/macro",
                json=command(["ppi"]),
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 403
        )
        assert (
            client.post(
                "/api/pce/jobs", json={}, headers={"Origin": CANDIDATE_ORIGIN}
            ).status_code
            == 403
        )
    journal.submit.assert_not_called()


def test_package_catalog_permission_denied_never_calls_evidence_or_returns_partial_list(
    tmp_path,
):
    host = fixture_host(tmp_path / "configuration")
    host.authorize = lambda request, package: PackageAccess(
        metadata_view_permission="denied" if package == "tdx" else "allowed",
        write_eligible=True,
        write_reasons=["synthetic"],
    )
    host.history = Mock(side_effect=AssertionError("must not read history"))
    with TestClient(create_package_app(host)) as client:
        response = client.get(PATH)
        assert response.status_code == 403
        assert response.json()["classification"] == "package_metadata_permission_denied"
        assert "packages" not in response.json()
    host.history.assert_not_called()


def test_missing_providers_and_authorization_callback_fault_are_explicit_unknown(
    tmp_path,
):
    host = fixture_host(tmp_path / "configuration")
    host.credentials = host.history = None
    with TestClient(create_package_app(host)) as client:
        response = put(client, ["cpi"])
        assert response.status_code == 200
        view = response.json()
        assert {s["credential_state"] for s in view["credential_states"]} == {"unknown"}
        assert view["history_completeness"] == "UNKNOWN"
        assert view["assets"][0]["revisions"][0]["upstream_read"]["allowed"] is None
        host.authorize = Mock(side_effect=OSError("synthetic-sensitive-authorization"))
        failed = client.get(PATH)
        assert failed.status_code == 503
        assert failed.json()["classification"] == "package_authorization_unavailable"
        assert "synthetic-sensitive" not in failed.text
