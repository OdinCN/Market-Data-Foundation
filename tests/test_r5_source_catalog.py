"""Host-run deterministic directory tests; no provider/PG/host capability."""

from contextlib import contextmanager
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from market_data_foundation.domain_contracts.kd import (
    KD_B043,
    KD_B043_CURRENT_RANK,
    KD_B044_DISPOSITION,
)
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.bls_cpi_direct import (
    BLS_CPI_DIRECT_BINDING_CANDIDATE,
    BLS_CPI_DIRECT_SOURCE,
)
from market_data_foundation.web import cpi_api, source_catalog
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.source_catalog import (
    DirectoryEntryDTO,
    DirectoryQuery,
    directory,
)
from tests.r5_fixture import fixture_app


def test_code_inventory_is_stable_bounded_and_never_opens_host_scope(monkeypatch):
    def forbidden(*_args, **_kwargs):
        pytest.fail("external_effect_forbidden")

    @contextmanager
    def forbidden_scope():
        forbidden()
        yield  # pragma: no cover

    import socket

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    with TestClient(create_app(forbidden_scope)) as client:
        first = client.get("/api/r5/sources")
        second = client.get("/api/r5/sources")
        assert first.status_code == 200
        assert first.content == second.content
        assert first.headers["cache-control"] == "no-store"
        body = first.json()
        assert body["evidence_mode"] == "CODE_CONTRACTS_ONLY"
        assert body["scope"] == "NAMED_R5_I2_ALLOWLIST"
        entries = body["entries"]
        assert len(entries) == 17 <= source_catalog.MAX_ENTRIES
        assert len({e["identity"] for e in entries}) == len(entries)

        def order(e):
            return (
                e["domain"],
                e["source_name"],
                e["component_identity"],
                e["identity"],
            )

        assert entries == sorted(entries, key=order)
        assert {e["domain"] for e in entries} == {"Macro", "TDX", "KD"}
        for entry in entries:
            assert entry["evidence_refs"]
            for field in (
                "source_qualification",
                "current_binding",
                "runtime_observation",
                "local_holding",
            ):
                assert entry[field] == "UNKNOWN"
            if entry["binding_contract"]:
                assert entry["binding_contract"]["silent_fallback"] is False
                assert (
                    entry["binding_contract"]["source"]["identity"]
                    == entry["source_contract"]["identity"]
                )
        assert not any(e["source_name"] == "QMT" for e in entries)
        for method in ("post", "put", "patch", "delete"):
            assert getattr(client, method)("/api/r5/sources").status_code == 405


def test_canonical_source_and_binding_candidates_are_preserved_without_activation():
    entries = directory(DirectoryQuery()).entries
    entry = next(
        e
        for e in entries
        if e.source_contract
        and e.source_contract.identity == BLS_CPI_DIRECT_SOURCE.identity
    )
    assert entry.identity == (
        f"{BLS_CPI_DIRECT_SOURCE.identity}@{BLS_CPI_DIRECT_SOURCE.contract_revision}"
    )
    assert entry.source_contract.revision == BLS_CPI_DIRECT_SOURCE.contract_revision
    assert entry.source_contract.projection_revision == (
        BLS_CPI_DIRECT_SOURCE.projection_revision
    )
    binding = entry.binding_contract
    assert binding.identity == BLS_CPI_DIRECT_BINDING_CANDIDATE.identity
    assert binding.revision == BLS_CPI_DIRECT_BINDING_CANDIDATE.revision
    assert binding.compatibility_policy == (
        BLS_CPI_DIRECT_BINDING_CANDIDATE.compatibility_policy
    )
    assert binding.limitations == list(BLS_CPI_DIRECT_BINDING_CANDIDATE.limitations)
    assert binding.mappings[0].definition == entry.asset
    assert (
        entry.asset.identity == entry.capability_identity == BLS_CPI_HEADLINES.identity
    )
    assert binding.mappings[0].durable_required is True
    assert entry.source_qualified_contract is True
    assert entry.source_qualification == entry.current_binding == "UNKNOWN"


def test_domain_contracts_do_not_masquerade_as_generic_services_or_descriptors():
    entries = directory(DirectoryQuery()).entries
    b043 = next(e for e in entries if e.operation == KD_B043.operation)
    assert b043.asset.identity == KD_B043_CURRENT_RANK.identity
    assert b043.code_delivery == "CONTRACT_ONLY"
    assert b043.source_contract is b043.binding_contract is None
    assert b043.web_read == "UNSUPPORTED"
    assert "kd_runtime_descriptor_required" in b043.reasons
    assert KD_B043.completeness in b043.reasons
    assert "sampled_current_state_history_only" in b043.reasons
    b044 = next(e for e in entries if e.operation == KD_B044_DISPOSITION.operation)
    assert b044.contract_disposition == KD_B044_DISPOSITION.status
    assert b044.asset is b044.source_contract is b044.binding_contract is None
    assert b044.web_read == "UNSUPPORTED"
    for entry in entries:
        if entry.asset and entry.asset.identity != BLS_CPI_HEADLINES.identity:
            assert entry.web_read != "CPI_REQUEST_QUALIFIED"
        if entry.domain == "TDX":
            assert entry.web_read == "DOMAIN_SPECIFIC_ONLY"
            assert "bounded" in entry.source_contract.completeness_contract
        if entry.source_name in {"ECB", "ESTAT"}:
            assert entry.source_qualified_contract is False
            assert "not_source_qualified" in entry.binding_contract.limitations
        if (
            entry.source_name == "BEA"
            and entry.asset.identity != "mdf.macro.bea.consumption-income"
        ):
            assert "source_mapping_not_independently_verified" in (
                entry.binding_contract.limitations
            )


@pytest.mark.parametrize("domain", ["Macro", "TDX", "KD"])
def test_exact_domain_filter_and_no_claim_of_complete_source_discovery(domain):
    with TestClient(create_app()) as client:
        entries = client.get("/api/r5/sources", params={"domain": domain}).json()[
            "entries"
        ]
        assert entries
        assert all(e["domain"] == domain for e in entries)


@pytest.mark.parametrize(
    "params",
    [
        {"domain": "QMT"},
        {"domain": "macro"},
        {"domain": ""},
        {"current_binding": "ACTIVE"},
        {"limit": "9999999"},
        [("domain", "KD"), ("domain", "Macro")],
        {"unexpected": "private-sensitive-value"},
    ],
)
def test_strict_bounded_redacted_queries(params):
    with TestClient(create_app()) as client:
        response = client.get("/api/r5/sources", params=params)
        assert response.status_code == 422
        assert response.json() == {
            "status": "NOT_AVAILABLE",
            "classification": "invalid_request",
        }
        assert "private-sensitive" not in response.text
        assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_qualification", "QUALIFIED"),
        ("current_binding", "ACTIVE"),
        ("runtime_observation", "ONLINE"),
        ("local_holding", "HELD"),
        ("code_delivery", True),
        ("unexpected", "value"),
    ],
)
def test_strict_dto_rejects_unproven_states_and_extra_fields(field, value):
    data = directory(DirectoryQuery()).entries[0].model_dump()
    data[field] = value
    with pytest.raises(ValidationError):
        DirectoryEntryDTO.model_validate(data)


@pytest.mark.parametrize("fault", ["exception", "overflow", "binding_mismatch"])
def test_directory_fault_discards_all_rows_and_raw_errors(monkeypatch, fault):
    if fault == "overflow":
        monkeypatch.setattr(source_catalog, "MAX_ENTRIES", 0)
    elif fault == "binding_mismatch":
        monkeypatch.setattr(
            source_catalog,
            "BLS_CPI_DIRECT_BINDING_CANDIDATE",
            replace(BLS_CPI_DIRECT_BINDING_CANDIDATE, source=BLS_CPI_HEADLINES.key),
        )
    else:

        def fail(_query):
            raise RuntimeError("private-sensitive-dsn")

        monkeypatch.setattr(cpi_api, "directory", fail)
    with TestClient(create_app()) as client:
        response = client.get("/api/r5/sources")
        assert response.status_code == 503
        assert response.json() == {
            "status": "UNKNOWN",
            "classification": "catalog_contract_invalid",
        }
        assert "private-sensitive" not in response.text
        assert "entries" not in response.json()


def test_directory_and_i1_assets_share_identity_but_not_qualification():
    params = {"reference": "2026-08", "as_of": "2026-10-06T00:00:10+00:00"}
    for scenario in ("available", "unconfigured", "missing_content"):
        with TestClient(fixture_app(scenario)) as client:
            entries = client.get("/api/r5/sources").json()["entries"]
            cpi = client.get("/api/r5/cpi", params=params).json()
            linked = [
                e
                for e in entries
                if e["asset"]
                and e["asset"]["identity"] == cpi["definition"]["identity"]
            ]
            assert linked
            assert all(
                e["asset"]["revision"] == cpi["definition"]["revision"] for e in linked
            )
            assert all(e["current_binding"] == "UNKNOWN" for e in linked)
            if scenario == "available":
                assert (
                    cpi["service"]["asset_identity"] == linked[0]["asset"]["identity"]
                )
            else:
                assert cpi["service"] is None


def test_directory_openapi_is_strict_get_only_and_bounded():
    schema = create_app().openapi()
    assert set(schema["paths"]["/api/r5/sources"]) == {"get"}
    for name in (
        "DirectoryDTO",
        "DirectoryEntryDTO",
        "SourceContractDTO",
        "BindingContractDTO",
        "MappingDTO",
        "ContractKeyDTO",
    ):
        schemas = schema["components"]["schemas"]
        key = name if name in schemas else name + "-Output"
        assert schemas[key]["additionalProperties"] is False
    assert (
        schema["components"]["schemas"]["DirectoryDTO"]["properties"]["entries"][
            "maxItems"
        ]
        == source_catalog.MAX_ENTRIES
    )
