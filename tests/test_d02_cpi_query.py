"""D02 real ASGI/domain contract and zero-write/source boundary proofs."""

import json
import socket
from dataclasses import replace

import psycopg
import pytest
from fastapi.testclient import TestClient

from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import Grade
from market_data_foundation.historical import bls_cpi_direct as direct
from market_data_foundation.historical import cpi_history
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_query import CpiSourceAdmission, CpiSourceResponse
from market_data_foundation.web.source_catalog import DirectoryQuery, directory
from tests.d02_query_fixture import AT, effects, fixture


def operation_id(source):
    return f"{source.identity}@{source.contract_revision}"


DIRECT = operation_id(direct.BLS_CPI_DIRECT_SOURCE)
RELEASE = operation_id(direct.BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE)
HISTORY = operation_id(cpi_history.CPI_HISTORY_INDEX_SOURCE)


@pytest.fixture(autouse=True)
def no_external_io(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("external_io_forbidden")

    for method in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, method, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


def query(app, operation=DIRECT, reference="2026-08", **options):
    params = {"reference": reference}
    if operation is not None:
        params["operation_id"] = operation
    with TestClient(app) as client:
        response = client.get("/api/r5/cpi/query", params=params, **options)
    assert response.headers["cache-control"] == "no-store"
    return response


def assert_effects(state, source=0):
    assert effects(state) == {
        "source": source,
        "jobs": 0,
        "versions": 0,
        "publications": 0,
    }
    assert state.ledger.snapshot() == state.before


@pytest.mark.parametrize(
    "scenario", ["local_hit", "local_other_reference", "source_disabled"]
)
def test_local_hit_and_correct_month_never_touch_source(
    tmp_path, monkeypatch, scenario
):
    app, state = fixture(tmp_path, scenario)

    def forbidden(*_args, **_kwargs):
        pytest.fail("local_read_touched_source_or_configuration")

    monkeypatch.setattr(state.host.packages.store, "read", forbidden)
    # The selected browser source is not used to fabricate local provenance.
    response = query(app, operation=RELEASE)
    assert response.status_code == 200
    data = response.json()
    assert data["channel"] == "local"
    assert data["reference"] == "2026-08"
    assert data["operation_id"] == DIRECT
    assert data["version_identity"] == state.plan.version.identity
    assert data["publication_identity"] == "p6-cpi-live:publication"
    assert data["durable"] is True
    assert data["decision_at"] == "2026-10-27T08:00:00Z"
    assert len(data["measurements"]) == 4
    assert_effects(state)


@pytest.mark.parametrize("change", ["credentials", "bindings", "config"])
def test_local_survives_unavailable_source_inputs(tmp_path, change):
    _app, state = fixture(tmp_path)
    host = state.host
    if change == "credentials":
        host = replace(
            host,
            bindings=tuple(
                replace(b, admission=CpiSourceAdmission()) for b in host.bindings
            ),
        )
    elif change == "bindings":
        host = replace(host, bindings=())
    else:
        host.packages.store.path.unlink()
    response = query(create_app(query_host=host), operation=None)
    assert response.status_code == 200
    assert response.json()["channel"] == "local"
    assert_effects(state)


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("miss", "local_reference_missing"),
        ("partial", "local_coverage_partial"),
        ("stale", "local_freshness_unsatisfied"),
    ],
)
def test_one_source_request_whole_result_and_no_persistence(tmp_path, scenario, reason):
    app, state = fixture(tmp_path, scenario)
    data = query(app).json()
    assert data["status"] == "OK"
    assert data["channel"] == "upstream"
    assert data["local_reason"] == reason
    assert data["durable"] is False
    assert data["version_identity"] is data["publication_identity"] is None
    assert data["obtained_at"] == "2026-10-27T08:00:01Z"
    assert data["provider_vintage"] == "UNKNOWN"
    assert len(data["measurements"]) == 4
    assert state.requests[0].decision_at == AT
    assert len(state.requests[0].members) == 4
    assert_effects(state, 1)


@pytest.mark.parametrize(
    ("operation", "reference", "classification"),
    [
        (None, "2026-08", "source_selection_required"),
        ("untrusted-secret-provider", "2026-08", "source_binding_not_admitted"),
        (DIRECT, "2026-09", "source_reference_not_admitted"),
        (RELEASE, "2026-08", "source_reference_not_admitted"),
    ],
)
def test_selection_and_reference_contracts(
    tmp_path, operation, reference, classification
):
    app, state = fixture(tmp_path, "miss")
    response = query(app, operation, reference)
    expected_status = 422 if classification == "source_binding_not_admitted" else 409
    assert response.status_code == expected_status
    assert response.json()["classification"] == classification
    assert "untrusted-secret" not in response.text
    assert_effects(state)


@pytest.mark.parametrize(
    ("operation", "reference"),
    [(DIRECT, "2026-08"), (RELEASE, "2026-09"), (HISTORY, "2025-06")],
)
def test_all_three_real_named_bindings(tmp_path, operation, reference):
    app, state = fixture(tmp_path, "miss")
    assert operation in {e.identity for e in directory(DirectoryQuery()).entries}
    response = query(app, operation, reference)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["operation_id"] == operation
    assert {m["reference_period"] for m in data["measurements"]} == {reference}
    assert state.requests[0].reference == reference
    assert_effects(state, 1)


@pytest.mark.parametrize(
    "entry",
    [
        e.identity
        for e in directory(DirectoryQuery()).entries
        if e.web_read != "CPI_REQUEST_QUALIFIED"
    ],
)
def test_non_cpi_bindings_never_execute(tmp_path, entry):
    app, state = fixture(tmp_path, "miss")
    response = query(app, entry)
    assert response.status_code == 422
    assert response.json()["status"] == "REJECTED"
    assert response.json()["classification"] == "source_binding_not_admitted"
    assert_effects(state)


@pytest.mark.parametrize(
    ("scenario", "classification", "status"),
    [
        ("storage_failure", "storage_unavailable", 503),
        ("scope_exit_failure", "storage_unavailable", 503),
        ("content_invalid", "cpi_payload_invalid_or_mismatched", 409),
        ("missing_content", "content_missing", 409),
        ("corrupt_content", "content_digest_mismatch", 409),
        ("withdrawn", "as_of_no_version", 409),
        ("unpublished", "as_of_no_version", 409),
        ("ambiguous", "as_of_ambiguous", 409),
        ("lineage_invalid", "lineage_missing_or_mismatched", 409),
        ("read_denied", "read_permission_denied", 403),
        ("read_unknown", "read_permission_unknown", 403),
        ("metadata_denied", "metadata_permission_denied", 403),
        ("metadata_unknown", "metadata_permission_unknown", 403),
        ("source_unknown", "source_qualification_unknown", 409),
        ("source_not_qualified", "source_not_qualified", 409),
        ("credential_missing", "credential_missing", 409),
        ("credential_unknown", "credential_unknown", 409),
        ("source_permission_unknown", "source_permission_unknown", 403),
        ("query_permission_denied", "query_permission_denied", 403),
        ("source_scope_denied", "source_scope_not_admitted", 403),
        ("config_failure", "config_storage_corrupt", 503),
        ("unconfigured", "query_host_not_configured", 503),
    ],
)
def test_failures_are_not_misses_or_source_permission(
    tmp_path, scenario, classification, status
):
    app, state = fixture(tmp_path, scenario)
    response = query(app)
    assert response.status_code == status
    assert response.json()["classification"] == classification
    assert not {"credential=", "sensitive", "secret-invalid"} & set(
        response.text.split()
    )
    assert_effects(state)


@pytest.mark.parametrize(
    "scenario",
    ["source_failure", "source_partial", "source_secret_error", "source_bad_time"],
)
def test_source_failure_no_fallback_or_sensitive_details(tmp_path, scenario):
    app, state = fixture(tmp_path, scenario)
    response = query(app)
    assert response.status_code in {409, 503}
    assert response.json()["status"] != "OK"
    assert set(response.json()) == {"status", "classification", "local_reason"}
    for secret in ("super-secret", "credential=", "sensitive", "RuntimeError"):
        assert secret not in response.text
    assert_effects(state, 1)


@pytest.mark.parametrize("base", ["stale", "partial"])
def test_source_failure_never_returns_old_local_sample(tmp_path, base):
    _app, state = fixture(tmp_path, base)

    def failed(_request):
        state.source += 1
        raise RuntimeError("secret")

    host = replace(
        state.host,
        bindings=tuple(replace(b, fetch=failed) for b in state.host.bindings),
    )
    response = query(create_app(query_host=host))
    assert response.status_code == 503
    assert response.json()["classification"] == "source_failed"
    assert "measurements" not in response.json()
    assert_effects(state, 1)


@pytest.mark.parametrize(
    "suffix",
    [
        "",
        "reference=2026-13",
        "reference=0001-01",
        "reference=2026-08&reference=2026-08",
        "reference=2026-08&mode=latest",
        "reference=2026-08&latest=true",
        "reference=2026-08&as_of=secret",
        "reference=2026-08&version=secret",
        "reference=2026-08&provider=secret",
        "reference=2026-08&extra=secret",
        "reference=2026-08&operation_id=x&operation_id=x",
        "reference=2026-08&operation_id=",
        "reference=2026-08&operation_id=" + "s" * 513,
    ],
)
def test_http_contract_is_strict_and_redacted(tmp_path, suffix):
    app, state = fixture(tmp_path, "miss")
    with TestClient(app) as client:
        response = client.get("/api/r5/cpi/query?" + suffix)
    assert response.status_code == 422
    assert response.json() == {
        "status": "REJECTED",
        "classification": "invalid_request",
        "local_reason": None,
    }
    assert response.headers["cache-control"] == "no-store"
    assert_effects(state)


def test_body_rejected_and_only_get_is_available(tmp_path):
    app, state = fixture(tmp_path)
    with TestClient(app) as client:
        response = client.request(
            "GET", "/api/r5/cpi/query?reference=2026-08", json={"secret": "value"}
        )
        assert response.status_code == 422
        assert "secret" not in response.text
        assert client.post("/api/r5/cpi/query?reference=2026-08").status_code == 405
    assert_effects(state)


def test_disabled_stops_new_source_and_missing_callback_is_not_qualification(tmp_path):
    _app, state = fixture(tmp_path, "source_disabled")
    response = query(
        create_app(query_host=state.host), reference="2026-09", operation=RELEASE
    )
    assert response.json()["classification"] == "source_disabled"
    assert_effects(state)


@pytest.mark.parametrize(
    "change", ["unconfigured", "duplicate", "mutated_contract", "unknown_freshness"]
)
def test_host_contract_fail_closed(tmp_path, change):
    _app, state = fixture(
        tmp_path, "local_hit" if change == "unknown_freshness" else "miss"
    )
    host = state.host
    if change == "unconfigured":
        host = replace(host, bindings=())
    elif change == "duplicate":
        host = replace(host, bindings=host.bindings + (host.bindings[0],))
    elif change == "mutated_contract":
        binding = host.bindings[0]
        host = replace(
            host,
            bindings=(
                replace(
                    binding,
                    binding=replace(binding.binding, compatibility_policy="untrusted"),
                ),
            ),
        )
    else:
        host = replace(host, freshness=lambda _view, _at: "unknown")
    response = query(create_app(query_host=host))
    assert response.status_code in (409, 503)
    assert_effects(state)


def test_fixed_aware_clock_once_and_historical_routes_remain_local(tmp_path):
    _app, state = fixture(tmp_path, "miss")
    calls = []

    def clock():
        calls.append(1)
        return AT

    host = replace(state.host, clock=clock)
    assert query(create_app(query_host=host)).status_code == 200
    assert len(calls) == 1
    app, local = fixture(tmp_path / "history")
    with TestClient(app) as client:
        for mode in ("as_of", "exact_version"):
            params = {"reference": "2026-08", "as_of": AT.isoformat(), "mode": mode}
            if mode == "exact_version":
                params["version_identity"] = local.plan.version.identity
            assert client.get("/api/r5/cpi/read", params=params).status_code == 200
    wire = json.loads(
        handle_request(
            local.ledger,
            json.dumps(
                {
                    "wire_revision": "mdf.consumer.read.v1",
                    "mode": "as_of",
                    "definition": {
                        "kind": "DatasetDefinition",
                        "identity": "mdf.macro.bls.cpi.headlines",
                        "revision": "p3-r1",
                    },
                    "as_of": AT.isoformat(),
                }
            ).encode(),
        )
    )
    assert wire["status"] == "ok"
    assert_effects(local)


def test_source_measurement_contract_and_evidence_are_reused(tmp_path):
    _app, state = fixture(tmp_path, "miss")
    original = state.host.bindings[0]

    def invalid(request):
        response = original.fetch(request)
        content = json.loads(response.content)
        content["data"]["measurements"][0]["unit"] = "index"
        return CpiSourceResponse(
            json.dumps(content).encode(), frozenset({Grade.ORIGINAL, Grade.PROJECTED})
        )

    host = replace(state.host, bindings=(replace(original, fetch=invalid),))
    assert (
        query(create_app(query_host=host)).json()["classification"]
        == "source_payload_invalid"
    )
    assert_effects(state, 1)


def test_native_composition_explicit_query_host_and_mutation_boundaries(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock

    from market_data_foundation.historical.pce_pg_binding import BINDING as PCE_BINDING
    from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
    from market_data_foundation.web.pce_r5_host import create_native_preview
    from tests.b3_dev_fixture import fixture as pg_fixture

    _app, state = fixture(tmp_path)
    pce = SimpleNamespace(journal=lambda: Mock(binding=PCE_BINDING), read_scope=Mock())
    _server, rig, _preview = pg_fixture(tmp_path / "compose")
    cpi = SimpleNamespace(journal=lambda: rig.journal, read_scope=lambda: state.scope)
    app = create_native_preview(
        pce, cpi, ui_directory=None, enabled=True, query_host=state.host
    )
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 12345)
    ) as client:
        response = client.get("/api/r5/cpi/query?reference=2026-08")
        assert response.status_code == 200, response.text
        assert response.json()["channel"] == "local"
        denied = client.get(
            "/api/r5/cpi/query?reference=2026-08",
            headers={"Origin": "http://evil.invalid"},
        )
        assert denied.status_code == 403
        assert denied.json() == {
            "status": "REJECTED",
            "classification": "operator_not_authorized",
            "local_reason": None,
        }
        assert denied.headers["cache-control"] == "no-store"
        for path in ("/api/r5/cpi/jobs", "/api/r5/cpi/plans", "/api/pce/jobs"):
            assert client.post(path, json={}).status_code == 403
    assert_effects(state)
    pce.read_scope.assert_not_called()


def test_query_has_no_ledger_mutation_port(tmp_path, monkeypatch):
    from market_data_foundation.canonical.repository import InMemoryLedger

    app, state = fixture(tmp_path, "miss")

    def forbidden(*_args, **_kwargs):
        pytest.fail("ordinary_query_attempted_canonical_mutation")

    for method in ("put", "put_content", "publish"):
        monkeypatch.setattr(InMemoryLedger, method, forbidden)
    assert query(app).status_code == 200
    assert_effects(state, 1)


@pytest.mark.parametrize(
    "clock", [lambda: AT.replace(tzinfo=None), lambda: "secret-clock"]
)
def test_invalid_clock_fails_closed(tmp_path, clock):
    _app, state = fixture(tmp_path, "miss")
    host = replace(state.host, clock=clock)
    response = query(create_app(query_host=host))
    assert response.status_code == 503
    assert response.json()["classification"] == "decision_clock_invalid"
    assert_effects(state)


def test_requested_second_month_uses_its_own_real_publication(tmp_path):
    app, state = fixture(tmp_path, "local_other_reference")
    response = query(app, reference="2026-09")
    assert response.status_code == 200
    data = response.json()
    assert data["version_identity"] == "d02-other-reference:version"
    assert data["source_ref"] is data["binding_ref"] is data["operation_id"] is None
    assert_effects(state)


def test_source_evidence_must_belong_to_selected_real_binding(tmp_path):
    _app, state = fixture(tmp_path, "miss")
    original = state.host.bindings[0]

    def invalid(request):
        response = original.fetch(request)
        return replace(response, evidence_grades=frozenset({Grade.OTHER}))

    host = replace(state.host, bindings=(replace(original, fetch=invalid),))
    response = query(create_app(query_host=host))
    assert response.json()["classification"] == "source_evidence_invalid"
    assert_effects(state, 1)
