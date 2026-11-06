"""Real isolated package router/store; no live source or runtime writes."""

from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from market_data_foundation.web.package_catalog import SaveCommand
from market_data_foundation.web.packages import create_package_app
from tests.d01_package_fixture import fixture_host
from tests.test_package_atomic_save import no_external_io  # noqa: F401

PATH = "/api/r5/packages/macro"
KEYS = {"BLS", "BEA", "Census", "FRED"}
PUBLIC = {
    "Federal Reserve Board",
    "New York Fed",
    "US Treasury",
    "Credit archive",
    "MDF",
}


def command(enabled, revision=None, sources=KEYS):
    return dict(
        expected_revision=revision,
        enabled_capability_ids=enabled,
        source_changes=[
            dict(source_id=s, credential_action="keep") for s in sorted(sources)
        ],
    )


def test_original_nine_source_command_is_rejected_without_side_effect_or_echo(tmp_path):
    host = fixture_host(tmp_path / "config")
    enabled = [c.capability_id for c in host.catalog.package("macro").capabilities]
    payload = command(enabled, sources=KEYS | PUBLIC)
    with pytest.raises(ValidationError) as invalid:
        SaveCommand.model_validate(payload)
    assert invalid.value.errors()[0]["type"] == "too_long"
    before = host.store.path.read_bytes()
    with TestClient(create_package_app(host)) as http:
        result = http.put(PATH, json=payload)
    assert result.status_code == 422
    assert result.json() == dict(
        status="REJECTED", classification="too_many_credential_changes", effect="NONE"
    )
    assert host.store.path.read_bytes() == before


def test_all_eleven_save_keeps_original_credentials_and_reopens_from_storage(tmp_path):
    host = fixture_host(tmp_path / "config")
    original = [
        c.capability_id
        for c in host.catalog.package("macro").capabilities
        if c.capability_id != "financial_daily"
    ]
    seed = command(original)
    seed["source_changes"] = [
        dict(
            source_id=s, credential_action="replace", credential_value="synthetic-" + s
        )
        for s in sorted(KEYS)
    ]
    with TestClient(create_package_app(host)) as http:
        assert http.put(PATH, json=seed).status_code == 200
        before = host.store.read(host.environment_id, "macro")
        secrets_before = {
            p.name: p.read_bytes() for p in (host.store.root / "credentials").iterdir()
        }
        host.credentials = Mock(side_effect=AssertionError("unnecessary_legacy_loader"))
        result = http.put(PATH, json=command(original + ["financial_daily"], "r1"))
        assert result.status_code == 200, result.text
        assert set(result.json()["enabled_capability_ids"]) == set(original) | {
            "financial_daily"
        }
        assert result.json()["required_sources"] == sorted(KEYS | PUBLIC)
        assert {s["source_id"] for s in result.json()["credential_states"]} == KEYS
        assert all(
            s["credential_state"] == "configured"
            for s in result.json()["credential_states"]
        )
        assert all("synthetic-" + s not in result.text for s in KEYS)
        assert "credential_refs" not in result.text
        assert http.get(PATH).json() == result.json()
        host.credentials.assert_not_called()
    after = host.store.read(host.environment_id, "macro")
    assert after.credential_refs == before.credential_refs
    assert secrets_before == {
        p.name: p.read_bytes() for p in (host.store.root / "credentials").iterdir()
    }
    reopened = fixture_host(host.store.root, initialize=False)
    with TestClient(create_package_app(reopened)) as http:
        assert http.get(PATH).json() == result.json()


@pytest.mark.parametrize("source", sorted(PUBLIC))
def test_public_and_local_credentials_and_verification_are_not_applicable(
    tmp_path, source
):
    host = fixture_host(tmp_path / "config")
    host.source_verifier = Mock(
        side_effect=AssertionError("public_auth_probe_forbidden")
    )
    before = host.store.path.read_bytes()
    with TestClient(create_package_app(host)) as http:
        response = http.put(PATH, json=command(["financial_daily"], sources={source}))
        assert response.status_code == 422
        assert response.json()["classification"] == "source_credentials_not_applicable"
        assert (
            http.post(
                PATH + f"/sources/{source}/verify", json={"expected_revision": "r1"}
            ).status_code
            == 422
        )
    assert host.store.path.read_bytes() == before
    host.source_verifier.assert_not_called()


def test_access_requirements_and_periodic_capability_are_explicit_not_authentication(
    tmp_path,
):
    host = fixture_host(tmp_path / "config", scenario="financial_manual")
    with TestClient(create_package_app(host)) as http:
        body = http.get(PATH).json()
    access = {s["source_id"]: s for s in body["source_access"]}
    assert set(access) == KEYS | PUBLIC
    assert all(access[s]["credential_requirement"] == "api_key" for s in KEYS)
    assert all(access[s]["credential_requirement"] == "none" for s in PUBLIC)
    assert access["MDF"]["kind"] == "local_derived"
    assert all(access[s]["kind"] == "external" for s in set(access) - {"MDF"})
    assert {s["source_id"] for s in body["source_verifications"]} == KEYS
    financial = next(
        c for c in body["capabilities"] if c["capability_id"] == "financial_daily"
    )
    assert len(financial["source_relations"]) == 6
    assert all(
        "周期采集" in r["release_calendar"] and "未接入" not in r["release_calendar"]
        for r in financial["source_relations"]
    )
    asset = next(
        a
        for a in body["assets"]
        if a["identity"] == financial["definition_ref"]["identity"]
    )
    entries = asset["revisions"][0]["source_entries"]
    assert len(entries) == 6
    assert all(
        "periodic maintenance" in e["source_contract"]["freshness_contract"]
        for e in entries
    )


def test_invalid_credential_value_has_safe_actionable_classification(tmp_path):
    host = fixture_host(tmp_path / "config")
    payload = command(["claims"])
    payload["source_changes"] = [
        dict(
            source_id="FRED",
            credential_action="replace",
            credential_value="synthetic-private\nvalue",
        )
    ]
    before = host.store.path.read_bytes()
    with TestClient(create_package_app(host)) as http:
        result = http.put(PATH, json=payload)
    assert result.status_code == 422
    assert result.json()["classification"] == "invalid_credential"
    assert "synthetic-private" not in result.text
    assert host.store.path.read_bytes() == before
