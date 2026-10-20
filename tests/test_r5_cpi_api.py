"""Deterministic ASGI→canonical CPI reads, classification and zero-effect proofs."""

import json
import socket
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from market_data_foundation.canonical.consumer_read import ReadFailure
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.model import Grade
from market_data_foundation.canonical.operator_asof import OperatorQueryBudgetError
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.web.cpi_api import Failure, create_app
from tests.r5_fixture import AT, BASE, ReadOnlySnapshot, fixture_app, fixture_ledger

PARAMS = {"reference": "2026-08", "as_of": AT}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("network_or_listener_forbidden")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "bind", forbidden)
    monkeypatch.setattr(socket.socket, "listen", forbidden)


def read(client, **changes):
    return client.get("/api/r5/cpi/read", params={**PARAMS, "mode": "as_of", **changes})


def test_real_domain_reads_identity_and_no_mutation(monkeypatch):
    ledger, plan, publication = fixture_ledger()
    before = ledger.snapshot()
    scope_calls = []

    @contextmanager
    def scope():
        scope_calls.append(1)
        yield ReadOnlySnapshot(ledger)

    app = create_app(scope, evidence_mode="OFFLINE_FIXTURE")
    with TestClient(app) as client:
        # Builders have completed. All subsequent reads must be effect-free.
        def forbidden(*_args, **_kwargs):
            pytest.fail("canonical_write_forbidden")

        for name in ("put", "put_content", "publish"):
            monkeypatch.setattr(type(ledger), name, forbidden)
        for _ in range(2):
            catalog = client.get("/api/r5/cpi", params=PARAMS).json()
            assert catalog["status"] == "OK"
            assert catalog["classification"] is None
            assert catalog["definition"]["identity"] == BLS_CPI_HEADLINES.identity
            assert catalog["asset"]["identity"] == catalog["service"]["asset_identity"]
            assert catalog["asset"]["local_holding"] == "VERIFIED_FOR_REQUEST"
            assert catalog["asset"]["qualification"] is None
            assert catalog["asset"]["versions"][0]["identity"] == plan.version.identity
            assert catalog["asset"]["versions"][0]["publications"][0]["identity"] == (
                publication.key.identity
            )
            assert catalog["service"]["version_identity"] == plan.version.identity
            assert catalog["definition"]["binding_activation"] == "UNKNOWN"
            assert catalog["definition"]["source_health"] == "UNKNOWN"
            assert catalog["evidence_mode"] == "OFFLINE_FIXTURE"
        for options in (
            {},
            {"mode": "exact_version", "version_identity": plan.version.identity},
        ):
            response = read(client, **options)
            assert response.status_code == 200
            data = response.json()
            assert data["version_identity"] == plan.version.identity
            assert data["publication_identity"] == publication.key.identity
            assert len(data["measurements"]) == 4
            assert {m["value"] for m in data["measurements"]} == {
                "0.3",
                "0.4",
                "2.4",
                "3.4",
            }
            assert data["content_mode"] == "whole_version"
            assert data["content_sha256"] == plan.version.content_digest
            assert "controlled-receipt" not in response.text
            assert "controlled_ref" not in response.text
            assert "base64" not in response.text
        original_wire = json.loads(
            handle_request(
                ledger,
                json.dumps(
                    {
                        "wire_revision": "mdf.consumer.read.v1",
                        "mode": "as_of",
                        "definition": {
                            "kind": "DatasetDefinition",
                            "identity": BLS_CPI_HEADLINES.identity,
                            "revision": BLS_CPI_HEADLINES.revision,
                        },
                        "as_of": AT,
                    }
                ).encode(),
            )
        )
        assert original_wire["status"] == "ok"
    assert ledger.snapshot() == before
    assert len(scope_calls) == 4


@pytest.mark.parametrize(
    ("scenario", "failure"),
    [
        ("unconfigured", "capability_not_configured"),
        ("empty", "as_of_no_version"),
        ("missing_content", "content_missing"),
        ("corrupt_content", "content_digest_mismatch"),
        ("withdrawn", "version_withdrawn"),
        ("ambiguous", "as_of_ambiguous"),
        ("partial", "coverage_partial_or_mismatched"),
    ],
)
def test_fail_closed_real_reader(scenario, failure):
    with TestClient(fixture_app(scenario)) as client:
        response = read(client)
        # As-of has no eligible candidate; exact names the terminal state.
        if scenario == "withdrawn":
            assert response.json()["classification"] == "as_of_no_version"
            response = read(
                client, mode="exact_version", version_identity="p6-cpi-live:version"
            )
        assert response.status_code in (409, 503)
        assert response.json()["classification"] == failure
        assert set(response.json()) == {"status", "classification"}
        catalog = client.get("/api/r5/cpi", params=PARAMS).json()
        assert catalog["service"] is None
        assert catalog["status"] != "OK"
        if scenario != "unconfigured":
            assert catalog["status"] == "NOT_AVAILABLE"
            assert catalog["asset"]["local_holding"] == "UNKNOWN"
            assert catalog["asset"]["qualification"] == catalog["classification"]
            assert len(catalog["asset"]["versions"]) == (
                0 if scenario == "empty" else 2 if scenario == "ambiguous" else 1
            )
            for version in catalog["asset"]["versions"]:
                assert version["publications"]


@pytest.mark.parametrize("scenario", ["unpublished", "quality", "publication_invalid"])
def test_domain_qualification_remains_classified(scenario):
    ledger, plan, publication = fixture_ledger()
    params = PARAMS
    if scenario == "unpublished":
        params = {**PARAMS, "as_of": (BASE + timedelta(seconds=3)).isoformat()}
        failure = "as_of_no_version"
    else:
        publication = (
            replace(
                publication,
                request=replace(
                    publication.request, claimed_grades=frozenset({Grade.PROJECTED})
                ),
            )
            if scenario == "quality"
            else replace(
                publication, usable_at=publication.decision_at - timedelta(seconds=1)
            )
        )
        # Rebuild stored fixture metadata; no mutation occurs during requests.
        from market_data_foundation.canonical.codec import dumps

        ledger._records[(publication.key.kind.value, publication.key.identity, "")] = (
            dumps(publication)
        )
        failure = (
            "quality_not_admitted"
            if scenario == "quality"
            else "publication_state_invalid"
        )
    before = ledger.snapshot()

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(ledger)

    with TestClient(create_app(scope)) as client:
        catalog = client.get("/api/r5/cpi", params=params).json()
        assert catalog["status"] == "NOT_AVAILABLE"
        assert catalog["classification"] == failure
        assert catalog["service"] is None
        if scenario == "publication_invalid":
            assert catalog["asset"] is None
        else:
            assert catalog["asset"]["local_holding"] == "UNKNOWN"
            assert catalog["asset"]["qualification"] == failure
            version = catalog["asset"]["versions"][0]
            assert version["identity"] == plan.version.identity
            assert version["publications"][0]["identity"] == publication.key.identity
        for options in (
            {"mode": "as_of"},
            {"mode": "exact_version", "version_identity": plan.version.identity},
        ):
            response = read(client, **params, **options)
            assert response.status_code == 409
            assert response.json() == {
                "status": "NOT_AVAILABLE",
                "classification": (
                    "unpublished_or_unavailable_as_of"
                    if scenario == "unpublished" and options["mode"] == "exact_version"
                    else failure
                ),
            }
    assert ledger.snapshot() == before


def test_asof_clocks_and_reference_and_exact_miss():
    with TestClient(fixture_app()) as client:
        cases = [
            ({"reference": "2026-09"}, "reference_stale_or_mismatched"),
            ({"reference": "2026-10"}, "reference_not_admitted"),
            (
                {"mode": "exact_version", "version_identity": "absent"},
                "version_not_found",
            ),
            ({"as_of": (BASE + timedelta(seconds=3)).isoformat()}, "as_of_no_version"),
            (
                {
                    "mode": "exact_version",
                    "version_identity": "p6-cpi-live:version",
                    "as_of": (BASE + timedelta(seconds=3)).isoformat(),
                },
                "unpublished_or_unavailable_as_of",
            ),
        ]
        for changes, classification in cases:
            assert read(client, **changes).json()["classification"] == classification


def test_ambiguity_exact_read_still_operates_without_fake_service():
    with TestClient(fixture_app("ambiguous")) as client:
        catalog = client.get("/api/r5/cpi", params=PARAMS).json()
        assert catalog["asset"] and catalog["service"] is None
        response = read(
            client, mode="exact_version", version_identity="p6-cpi-live:version"
        )
        assert response.status_code == 200


@pytest.mark.parametrize(
    "changes",
    [
        {"reference": "2026-13"},
        {"as_of": "2026-10-06T00:00:10"},
        {"as_of": "private-sensitive-invalid-value"},
        {"mode": "latest"},
        {"version_identity": "unadmitted"},
        {"mode": "exact_version"},
        {"unexpected": "private-sensitive-invalid-value"},
        {"version_identity": "x" * 257},
    ],
)
def test_strict_query_and_redacted_validation(changes):
    with TestClient(fixture_app()) as client:
        response = read(client, **changes)
        assert response.status_code == 422
        assert response.json() == {
            "status": "NOT_AVAILABLE",
            "classification": "invalid_request",
        }
        assert "private-sensitive" not in response.text


def test_no_mutation_routes_or_network_entry():
    with TestClient(create_app()) as client:
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/r5/cpi").status_code == 405
        assert client.get("/api/r5/jobs").status_code == 404
        assert client.get("/api/r5/cpi", params=PARAMS).json()["asset"] is None
        assert read(client).json()["status"] == "UNKNOWN"


def test_bounded_metadata_fails_with_no_partial_catalog():
    with TestClient(fixture_app("over_budget")) as client:
        data = client.get("/api/r5/cpi", params=PARAMS).json()
        assert data["classification"] == "operator_query_budget_exceeded"
        assert data["status"] == "UNKNOWN"
        assert data["asset"] is data["service"] is None


@pytest.mark.parametrize(
    ("method", "budget_failure"),
    [
        ("get_content", False),
        ("get", False),
        ("versions_for_definition", False),
        ("publications_for_version", False),
        ("versions_for_definition", True),
    ],
)
def test_reader_operational_failure_discards_successful_metadata(
    monkeypatch, method, budget_failure
):
    ledger, plan, publication = fixture_ledger()
    before = ledger.snapshot()
    faults = []

    def fail(*_args):
        faults.append(method)
        if budget_failure:
            raise OperatorQueryBudgetError()
        raise OSError("private-sensitive-dsn")

    def forbidden(*_args, **_kwargs):
        pytest.fail("canonical_write_forbidden")

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(ledger)

    # Operator metadata uses its separate operator_reads() port and succeeds;
    # only the subsequent consumer read reaches these faulting methods.
    monkeypatch.setattr(ReadOnlySnapshot, method, fail)
    for name in ("put", "put_content", "publish"):
        monkeypatch.setattr(type(ledger), name, forbidden)
    failure = (
        "operator_query_budget_exceeded" if budget_failure else "storage_unavailable"
    )
    requests = [
        ("/api/r5/cpi", PARAMS),
        ("/api/r5/cpi/read", {**PARAMS, "mode": "as_of"}),
    ]
    if method != "versions_for_definition":
        requests.append(
            (
                "/api/r5/cpi/read",
                {
                    **PARAMS,
                    "mode": "exact_version",
                    "version_identity": plan.version.identity,
                },
            )
        )
    with TestClient(create_app(scope)) as client:
        for path, params in requests:
            response = client.get(path, params=params)
            data = response.json()
            assert response.status_code == (200 if path == "/api/r5/cpi" else 503)
            assert data["status"] == "UNKNOWN"
            assert data["classification"] == failure
            if path == "/api/r5/cpi":
                assert data["asset"] is data["service"] is None
            else:
                assert set(data) == {"status", "classification"}
            assert plan.version.identity not in response.text
            assert publication.key.identity not in response.text
            assert "private-sensitive" not in response.text
            assert response.headers["cache-control"] == "no-store"
    assert len(faults) == len(requests)
    assert ledger.snapshot() == before


@pytest.mark.parametrize("exit_failure", [False, True])
@pytest.mark.parametrize("scenario", ["available", "missing_content"])
def test_storage_and_scope_exit_error_never_expose_raw_messages(
    monkeypatch, exit_failure, scenario
):
    ledger, plan, publication = fixture_ledger(scenario)
    before = ledger.snapshot()

    def forbidden(*_args, **_kwargs):
        pytest.fail("canonical_write_forbidden")

    for name in ("put", "put_content", "publish"):
        monkeypatch.setattr(type(ledger), name, forbidden)

    @contextmanager
    def scope():
        if not exit_failure:
            raise RuntimeError("private-sensitive-dsn")
        try:
            yield ReadOnlySnapshot(ledger)
        finally:
            raise RuntimeError("private-sensitive-dsn")

    with TestClient(create_app(scope)) as client:
        for path, params in (
            ("/api/r5/cpi", PARAMS),
            ("/api/r5/cpi/read", {**PARAMS, "mode": "as_of"}),
            (
                "/api/r5/cpi/read",
                {
                    **PARAMS,
                    "mode": "exact_version",
                    "version_identity": plan.version.identity,
                },
            ),
        ):
            response = client.get(path, params=params)
            assert response.status_code == (200 if path == "/api/r5/cpi" else 503)
            assert response.json()["status"] == "UNKNOWN"
            assert response.json()["classification"] == "storage_unavailable"
            assert "private-sensitive" not in response.text
            assert plan.version.identity not in response.text
            assert publication.key.identity not in response.text
            if path == "/api/r5/cpi":
                assert response.json()["asset"] is response.json()["service"] is None
            else:
                assert set(response.json()) == {"status", "classification"}
    assert ledger.snapshot() == before


def test_openapi_contract_fresh_and_all_failures_represented():
    from pathlib import Path

    assert json.loads(Path("web/openapi.json").read_text()) == create_app().openapi()
    assert {item.value for item in ReadFailure} <= {item.value for item in Failure}
    schema = create_app().openapi()
    for name in ("CatalogDTO", "ReadDTO", "ErrorDTO", "MeasurementDTO"):
        assert schema["components"]["schemas"][name]["additionalProperties"] is False
    for path in ("/api/r5/cpi", "/api/r5/cpi/read"):
        assert set(schema["paths"][path]) == {"get"}
        assert schema["paths"][path]["get"]["responses"]["422"]["content"][
            "application/json"
        ]["schema"]["$ref"].endswith("ErrorDTO")


def test_duplicate_query_rejected_and_current_evidence_not_cached():
    with TestClient(fixture_app()) as client:
        response = client.get(
            "/api/r5/cpi",
            params=[
                *PARAMS.items(),
                ("reference", "2026-09"),
            ],
        )
        assert response.status_code == 422
        assert response.json()["classification"] == "invalid_request"
        assert (
            client.get("/api/r5/cpi", params=PARAMS).headers["cache-control"]
            == "no-store"
        )
        assert read(client).headers["cache-control"] == "no-store"


def test_optional_static_ui_and_api_share_inert_asgi_app(tmp_path):
    (tmp_path / "index.html").write_text('<div id="root">offline-test-ui</div>')
    with TestClient(create_app(ui_directory=tmp_path)) as client:
        assert client.get("/").text == '<div id="root">offline-test-ui</div>'
        assert client.get("/api/r5/cpi", params=PARAMS).json()["status"] == "UNKNOWN"
        assert client.get("/api/r5/jobs").status_code == 404
