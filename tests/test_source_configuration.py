"""Saved-source behavior with isolated synthetic credentials and real Host gates."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from market_data_foundation.historical.claims_source import ClaimsResponse
from market_data_foundation.web.package_catalog import (
    PackageError,
    SaveCommand,
    VerificationFacts,
)
from market_data_foundation.web.packages import create_package_app
from market_data_foundation.web.source_verification import response_facts
from tests.d01_package_fixture import fixture_host
from tests.test_claims_web import HEADERS, client, setup
from tests.test_package_atomic_save import no_external_io  # noqa: F401

PATH = "/api/r5/packages/macro"
VERIFY = PATH + "/sources/FRED/verify"
FIRST, SECOND = "synthetic-fred-first", "synthetic-fred-second"


def save(http, revision, value=FIRST):
    return http.put(
        PATH,
        json={
            "expected_revision": revision,
            "enabled_capability_ids": ["claims"],
            "source_changes": [
                {
                    "source_id": "FRED",
                    "credential_action": "replace",
                    "credential_value": value,
                }
            ],
        },
        headers=HEADERS,
    )


def observation(response):
    assert response.status_code == 200, response.text
    assert FIRST not in response.text and SECOND not in response.text
    assert "credential_refs" not in response.text
    return next(
        v for v in response.json()["source_verifications"] if v["source_id"] == "FRED"
    )


def test_fred_saved_replacement_is_consumed_by_actual_host_and_verifier(
    tmp_path, monkeypatch
):
    app, host, store, _, calls = setup(tmp_path, monkeypatch, package_config_save=True)
    received = []

    class Transport:
        def __init__(self, value):
            received.append(value)

        def __call__(self, request):
            assert request.operation == "metadata"
            return ClaimsResponse(200, b'{"seriess":[{"id":"ICSA"}]}', {})

    monkeypatch.setattr(
        "market_data_foundation.web.source_verification.ClaimsHttpsTransport", Transport
    )
    with client(app) as http:
        revision = http.get(PATH).json()["config_revision"]
        saved = save(http, revision)
        assert saved.status_code == 200
        assert host.effective_credential() == FIRST
        assert received == [] and calls == []
        result = http.post(
            VERIFY,
            json={"expected_revision": saved.json()["config_revision"]},
            headers=HEADERS,
        )
        assert observation(result)["authentication"] == "confirmed"
        assert received == [FIRST]
        assert observation(http.get(PATH))["checked_at"]
        assert (
            observation(
                http.post(
                    VERIFY,
                    json={"expected_revision": saved.json()["config_revision"]},
                    headers=HEADERS,
                )
            )["request_status"]
            == "succeeded"
        )
        assert received == [FIRST]  # Server bounds repeated clicks.
        newer = save(http, saved.json()["config_revision"], SECOND)
        assert observation(newer)["reason"] == "configuration_changed"
        assert host.effective_credential() == SECOND
        conflict = save(http, saved.json()["config_revision"], FIRST)
        assert conflict.status_code == 409
        assert host.effective_credential() == SECOND and received == [FIRST]
        monkeypatch.setattr(
            host.packages.store,
            "_stage_credential",
            Mock(side_effect=PackageError("credential_prepare_failed")),
        )
        assert save(http, newer.json()["config_revision"], FIRST).status_code == 503
        assert host.effective_credential() == SECOND
        assert observation(http.get(PATH))["request_status"] == "not_run"
        assert store.rows == [] and calls == []


def test_verification_competing_save_and_legacy_change_never_publish_old_success(
    tmp_path,
):
    host = fixture_host(tmp_path / "config")
    host.credential_loader = Mock(return_value=FIRST)
    entered, release = Event(), Event()

    def verifier(*args):
        entered.set()
        assert release.wait(5)
        return VerificationFacts(
            request_status="succeeded",
            authentication="confirmed",
            reason="request_succeeded",
        )

    host.source_verifier = verifier
    with TestClient(create_package_app(host)) as http:
        assert save(http, None).status_code == 200
        with ThreadPoolExecutor() as pool:
            pending = pool.submit(http.post, VERIFY, json={"expected_revision": "r1"})
            assert entered.wait(5)
            assert (
                http.post(VERIFY, json={"expected_revision": "r1"}).status_code == 409
            )
            assert save(http, "r1", SECOND).status_code == 200
            release.set()
            assert (
                pending.result().json()["classification"]
                == "verification_configuration_changed"
            )
        assert observation(http.get(PATH))["request_status"] == "not_run"
        host.source_verifier = lambda *args: VerificationFacts(
            request_status="succeeded", reason="request_succeeded"
        )
        # An effective-loader change also invalidates a result without a CAS change.
        host.store.save(
            host.environment_id,
            "macro",
            SaveCommand(
                expected_revision="r2",
                enabled_capability_ids=["claims"],
                source_changes=[],
            ),
        )
        current = host.store.read(host.environment_id, "macro")
        host.store.resolve_credential = Mock(return_value=FIRST)
        assert (
            observation(
                http.post(VERIFY, json={"expected_revision": current.revision})
            )["request_status"]
            == "succeeded"
        )
        host.store.resolve_credential = Mock(return_value=SECOND)
        assert observation(http.get(PATH))["reason"] == "configuration_changed"


@pytest.mark.parametrize(
    "source,status,body,reason,authentication",
    [
        ("FRED", 403, b"private provider message", "http_forbidden", "unknown"),
        ("FRED", 429, b"", "rate_limited", "unknown"),
        (
            "FRED",
            200,
            b'{"error_code":400,"error_message":"secret"}',
            "business_error",
            "unknown",
        ),
        ("BLS", 200, b'{"status":"REQUEST_FAILED"}', "business_error", "unknown"),
        (
            "BEA",
            200,
            b'{"BEAAPI":{"Results":{"Error":{"APIErrorCode":"4"}}}}',
            "business_error",
            "unknown",
        ),
        (
            "Census",
            200,
            b'[["cell_value","time"],["1","2025-01"]]',
            "request_succeeded",
            "unknown",
        ),
        (
            "BLS",
            200,
            b'{"status":"REQUEST_SUCCEEDED","Results":{"series":[{"seriesID":"CUUR0000SA0","data":[1]}]}}',
            "request_succeeded",
            "unknown",
        ),
        ("FRED", 200, b'{"seriess":[{"id":"ICSA"}]}', "request_succeeded", "confirmed"),
        ("FRED", 200, b"<html>blocked</html>", "response_invalid", "unknown"),
    ],
)
def test_response_classification_is_separate_from_authentication(
    source, status, body, reason, authentication
):
    facts = response_facts(source, status, body)
    assert facts.reason == reason and facts.authentication == authentication
    assert "secret" not in facts.model_dump_json()


@pytest.mark.parametrize(
    "headers",
    [{}, {"Origin": "http://evil.example", "Content-Type": "application/json"}],
)
def test_actual_preview_rejects_cross_origin_verification(
    tmp_path, monkeypatch, headers
):
    app, host, *_ = setup(tmp_path, monkeypatch, package_config_save=True)
    verifier = Mock()
    host.packages.source_verifier = verifier
    with client(app) as http:
        assert http.post(
            VERIFY, json={"expected_revision": "r1"}, headers=headers
        ).status_code in {403, 405}
    verifier.assert_not_called()


def test_nine_canonical_relationships_include_disabled_claims(tmp_path):
    host = fixture_host(tmp_path / "config")
    with TestClient(create_package_app(host)) as http:
        body = http.get(PATH).json()
    assert len(body["capabilities"]) == 9
    assert body["enabled_capability_ids"] == []
    assert all(c["title"].startswith("美国") for c in body["capabilities"])
    claims = next(c for c in body["capabilities"] if c["capability_id"] == "claims")
    relation = claims["source_relations"][0]
    assert relation["source_id"] == "FRED"
    assert relation["original_institution"] == "DOL/ETA"
    assert (
        "DOL" in relation["release_calendar"] and "未装配" in relation["backup_status"]
    )
    assert "分段开发" not in json.dumps(body, ensure_ascii=False)


@pytest.mark.parametrize(
    "error,reason",
    [
        (TimeoutError("synthetic private diagnostic"), "timeout"),
        (OSError("synthetic private diagnostic"), "transport_failure"),
    ],
)
def test_failed_probe_does_not_block_retained_local_reads(
    tmp_path, monkeypatch, error, reason
):
    from decimal import Decimal

    from market_data_foundation.historical.claims_timeseries import ClaimsObservation
    from tests.test_claims_web import AT, PREFIX

    app, host, store, _, calls = setup(tmp_path, monkeypatch, package_config_save=True)
    store.rows.append(
        ClaimsObservation("ICSA", "2026-10-03", Decimal("100"), AT, AT, 1, {})
    )

    class Transport:
        def __init__(self, value):
            assert value == FIRST

        def __call__(self, request):
            raise error

    monkeypatch.setattr(
        "market_data_foundation.web.source_verification.ClaimsHttpsTransport", Transport
    )
    with client(app) as http:
        saved = save(http, http.get(PATH).json()["config_revision"])
        result = http.post(
            VERIFY,
            json={"expected_revision": saved.json()["config_revision"]},
            headers=HEADERS,
        )
        assert observation(result)["reason"] == reason
        assert "synthetic private diagnostic" not in result.text
        result = http.get(
            PREFIX + "/query?start=2026-10-01&end=2026-10-31&local_only=true"
        )
        assert result.status_code == 200 and result.json()["local_only"]
        assert any(row["value"] == "100" for row in result.json()["rows"])
        assert calls == []


@pytest.mark.parametrize("source", ["BEA", "Census"])
def test_verifier_reuses_transport_safety_and_discards_bea_request_echo(
    tmp_path, monkeypatch, source
):
    from contextlib import contextmanager
    from types import SimpleNamespace

    from market_data_foundation.canonical.model import ContractError
    from market_data_foundation.web import source_verification as module

    @contextmanager
    def attempt():
        yield lambda *args: None

    control = SimpleNamespace(attempt=attempt)
    host = SimpleNamespace(
        source_id=source,
        source=lambda record: {},
        upstream_access=lambda state: {"allowed": True},
    )
    runtime = SimpleNamespace(
        request_control=control,
        bea_request_control=control,
        retail_request_control=control,
        packages=SimpleNamespace(timeseries_hosts={"test": host}),
    )

    class Transport:
        def post(self, *args, **kwargs):
            return ClaimsResponse(
                200,
                json.dumps(
                    {
                        "BEAAPI": {
                            "Request": {"RequestParam": [{"ParameterValue": FIRST}]},
                            "Results": {"Dataset": [{"DatasetName": "NIPA"}]},
                        }
                    }
                ).encode(),
                {},
            )

        def get(self, *args, **kwargs):
            raise ContractError("census_retail_duplicate_http_header")

    monkeypatch.setattr(module, "BeaHttpsTransport", Transport)
    monkeypatch.setattr(module, "ExactCensusTransport", Transport)
    module.bind_source_verifier(runtime, None)
    result = runtime.packages.source_verifier(source, FIRST, None)
    assert result.reason == (
        "request_succeeded" if source == "BEA" else "protocol_failure"
    )
    assert result.authentication == ("confirmed" if source == "BEA" else "unknown")
    assert FIRST not in result.model_dump_json()


@pytest.mark.parametrize(
    "status,body,reason,authentication",
    [
        (
            200,
            b'{"BEAAPI":{"Results":{"Dataset":[{"DatasetName":"NIPA"}]}}}',
            "request_succeeded",
            "confirmed",
        ),
        (
            200,
            b'{"BEAAPI":{"Results":{"Error":{"APIErrorCode":"1"}}}}',
            "business_error",
            "rejected",
        ),
        (200, b'{"BEAAPI":{"Error":{"APIErrorCode":7}}}', "rate_limited", "unknown"),
        (
            200,
            b'{"BEAAPI":{"Results":{"Dataset":[{}]}}}',
            "response_invalid",
            "unknown",
        ),
        (400, b'{"BEAAPI":{"Error":{"APIErrorCode":1}}}', "http_failure", "unknown"),
        (403, b"", "http_forbidden", "unknown"),
        (500, b"", "http_failure", "unknown"),
    ],
)
def test_bea_official_authentication_evidence_is_bounded(
    status, body, reason, authentication
):
    result = response_facts("BEA", status, body)
    assert result.reason == reason and result.authentication == authentication
