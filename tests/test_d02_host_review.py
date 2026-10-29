"""Host独立业务反例：具名选择、即时凭据、权限分类及无写入。"""

import socket
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.cpi_pg_journal import PostgresJobJournal
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.package_catalog import CredentialState
from market_data_foundation.web.packages import PackageAccess
from market_data_foundation.web.source_catalog import DirectoryQuery, directory
from tests.d02_query_fixture import AT, fixture

DIRECT = "mdf.source-candidate.bls-v2.cpi.direct@p4-direct-r1"
RELEASE = (
    "mdf.source-candidate.bls-v2.cpi.release-2026-09@p4-release-reference-2026-09-r1"
)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("network_forbidden")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)


def get(app, operation=DIRECT):
    parameters = {"reference": "2026-08"}
    if operation is not None:
        parameters["operation_id"] = operation
    with TestClient(app) as client:
        return client.get("/api/r5/cpi/query", params=parameters)


@pytest.mark.parametrize("selection", ["unknown", "other_asset"])
def test_invalid_selected_contract_is_not_ignored_on_a_local_hit(tmp_path, selection):
    app, state = fixture(tmp_path, "local_hit")
    operation = (
        "unknown-user-selected-operation"
        if selection == "unknown"
        else next(
            e.identity
            for e in directory(DirectoryQuery()).entries
            if e.domain == "TDX" and e.binding_contract is not None
        )
    )
    response = get(app, operation)
    assert response.status_code == 422
    assert response.json()["classification"] == "source_binding_not_admitted"
    assert state.source == 0


@pytest.mark.parametrize("new_state", ["missing", "unknown"])
def test_each_source_request_rechecks_current_bls_credential_state(tmp_path, new_state):
    app, state = fixture(tmp_path, "miss")
    current = ["configured"]
    calls = []

    def credentials(_environment, _package, _record):
        calls.append(current[0])
        return [
            CredentialState(source_id="BLS", credential_state=current[0]),
            CredentialState(source_id="BEA", credential_state="missing"),
        ]

    state.host.packages.credentials = credentials
    assert get(app).status_code == 200
    current[0] = new_state
    response = get(app)
    assert response.status_code == 409
    assert response.json()["classification"] == "credential_" + new_state
    assert state.source == 1
    assert calls == ["configured", new_state]


def test_missing_credential_provider_does_not_become_configured_from_old_admission(
    tmp_path,
):
    app, state = fixture(tmp_path, "miss")
    state.host.packages.credentials = None
    response = get(app)
    assert response.status_code == 409
    assert response.json()["classification"] == "credential_unknown"
    assert state.source == 0


def test_missing_bea_does_not_block_bls_source_query(tmp_path):
    app, state = fixture(tmp_path, "miss")
    response = get(app)
    assert response.status_code == 200
    assert response.json()["channel"] == "upstream"
    assert state.source == 1


def test_package_metadata_denial_keeps_permission_classification(tmp_path):
    app, state = fixture(tmp_path, "miss")
    state.host.packages.authorize = lambda _request, _package: PackageAccess(
        metadata_view_permission="denied",
        write_eligible=False,
        write_reasons=["host_counterexample"],
    )
    response = get(app)
    assert response.status_code == 403
    assert response.json()["status"] == "REJECTED"
    assert response.json()["classification"] == "package_metadata_permission_denied"
    assert state.source == 0


def test_unavailable_source_config_does_not_block_qualified_local_read(tmp_path):
    app, state = fixture(tmp_path, "local_hit")
    state.host.packages.store.path.write_text("broken synthetic config")
    response = get(app)
    assert response.status_code == 200
    assert response.json()["channel"] == "local"
    assert state.source == 0


def test_newer_other_month_does_not_hide_the_requested_local_version(tmp_path):
    app, state = fixture(tmp_path, "local_other_reference")
    response = get(app, RELEASE)
    assert response.status_code == 200
    assert response.json()["version_identity"] == state.plan.version.identity
    assert response.json()["reference"] == "2026-08"
    assert state.source == 0


def test_source_query_has_one_decision_clock_and_cannot_construct_write_paths(
    tmp_path, monkeypatch
):
    _app, state = fixture(tmp_path, "miss")
    writes = []
    clocks = []

    def no_write(*_args, **_kwargs):
        writes.append("unexpected_write_path")
        raise AssertionError("ordinary_query_mutation_forbidden")

    def clock():
        clocks.append(AT)
        return AT

    for name in ("add", "persist_version", "publish", "reconcile_publication"):
        monkeypatch.setattr(CanonicalKernel, name, no_write)
    monkeypatch.setattr(FileJobJournal, "__init__", no_write)
    monkeypatch.setattr(PostgresJobJournal, "__init__", no_write)
    monkeypatch.setattr(state.host.packages.store, "save", no_write)
    host = replace(state.host, clock=clock)
    app = create_app(
        state.scope,
        query_host=host,
        package_host=host.packages,
        evidence_mode="OFFLINE_FIXTURE",
    )
    response = get(app)
    assert response.status_code == 200
    assert response.json()["decision_at"] == AT.isoformat().replace("+00:00", "Z")
    assert len(clocks) == 1
    assert writes == []
    assert state.ledger.snapshot() == state.before
    assert state.source == 1
