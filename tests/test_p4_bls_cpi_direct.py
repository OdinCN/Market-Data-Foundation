"""Deterministic direct-BLS CPI contract tests; no network or credentials."""

import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    ContractError,
    Grade,
    Key,
    Kind,
    RunKind,
    digest,
)
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import bls_cpi_direct as direct
from market_data_foundation.historical import bls_cpi_direct_live as live
from market_data_foundation.historical import cpi_retained_handoff as retained

BASE = datetime(2026, 10, 6, tzinfo=UTC)
EXACT_ROOT = Path("artifacts/macro-bls-cpi-retained-handoff-r1/capture-001")


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def catalog(series_id):
    seasonality, item = direct._CATALOG[series_id]
    return {
        "series_title": (
            f"{item} in U.S. city average, all urban consumers, {seasonality.lower()}"
        ),
        "series_id": series_id,
        "seasonality": seasonality,
        "survey_name": "Consumer Price Index for All Urban Consumers (CPI-U)",
        "survey_abbreviation": "CU",
        "measure_data_type": item,
        "area": "U.S. city average",
        "item": item,
    }


def response():
    values = {
        "CUSR0000SA0": {"1": "0.4", "12": "3.4"},
        "CUUR0000SA0": {"1": "0.3", "12": "3.4"},
        "CUSR0000SA0L1E": {"1": "0.3", "12": "2.4"},
        "CUUR0000SA0L1E": {"1": "0.3", "12": "2.4"},
    }
    series = []
    for series_id in direct.SERIES_IDS:
        rows = [
            {
                "year": "2026",
                "period": "M08",
                "periodName": "August",
                "latest": "true",
                "value": "334.131",
                "footnotes": [{}],
                "calculations": {
                    "net_changes": {},
                    "pct_changes": values[series_id],
                },
            },
            {
                "year": "2026",
                "period": "M07",
                "periodName": "July",
                "value": "332.813",
                "footnotes": [{}],
                "calculations": {
                    "net_changes": {},
                    "pct_changes": {"1": "0.1"},
                },
            },
            {
                "year": "2025",
                "period": "M10",
                "periodName": "October",
                "value": "-",
                "footnotes": [
                    {
                        "code": "X",
                        "text": (
                            "Data unavailable due to the 2025 lapse in appropriations"
                        ),
                    }
                ],
            },
            {
                "year": "2025",
                "period": "M08",
                "periodName": "August",
                "value": "323.291",
                "footnotes": [{}],
                "calculations": {
                    "net_changes": {},
                    "pct_changes": {"12": "2.9"},
                },
            },
        ]
        series.append(
            {"seriesID": series_id, "catalog": catalog(series_id), "data": rows}
        )
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 12,
        "message": ["provider message retained verbatim"],
        "Results": {"series": series},
    }


def september_response():
    values = {
        "CUSR0000SA0": {"1": "0.271828", "12": "9.999"},
        "CUUR0000SA0": {"1": "8.888", "12": "2.71828"},
        "CUSR0000SA0L1E": {"1": "0.314159", "12": "7.777"},
        "CUUR0000SA0L1E": {"1": "6.666", "12": "3.14159"},
    }
    series = []
    for series_id in direct.SERIES_IDS:
        rows = [
            {
                "year": "2026",
                "period": "M09",
                "periodName": "September",
                "latest": "true",
                "value": "999999.999",
                "footnotes": [{}],
                "calculations": {
                    "net_changes": {},
                    "pct_changes": values[series_id],
                },
            },
            {
                "year": "2026",
                "period": "M08",
                "periodName": "August",
                "value": "334.131",
                "footnotes": [{}],
            },
            {
                "year": "2025",
                "period": "M09",
                "periodName": "September",
                "value": "324.800",
                "footnotes": [{}],
            },
        ]
        series.append(
            {"seriesID": series_id, "catalog": catalog(series_id), "data": rows}
        )
    return {
        "status": "REQUEST_SUCCEEDED",
        "responseTime": 34,
        "message": ["synthetic September release response"],
        "Results": {"series": series},
    }


def replay_plan(raw=None):
    return direct.build_offline_replay_plan(
        raw or encode(response()),
        identity="direct-cpi-offline",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def live_evidence(raw=None, **changes):
    request = changes.pop("request", direct.BlsCpiRequest())
    values = {
        "raw_bytes": raw or encode(response()),
        "request": request,
        "request_identity": request.identity,
        "requested_at": BASE,
        "obtained_at": BASE + timedelta(seconds=1),
        "acquisition_identity": "mdf-direct-acquisition-new",
        "provider_data_attempts": 1,
        "controlled_external_receipt": "controlled-live-receipt",
    }
    values.update(changes)
    return direct.DirectCpiLiveEvidence(**values)


def test_exact_request_has_no_credential_persistence_surface():
    request = direct.BlsCpiRequest()
    persisted = request.persisted()
    assert persisted == {
        "endpoint": direct.ENDPOINT,
        "method": "POST",
        "body": {
            "seriesid": list(direct.SERIES_IDS),
            "startyear": "2025",
            "endyear": "2026",
            "catalog": True,
            "calculations": True,
        },
    }
    assert "registrationkey" not in json.dumps(persisted).lower()
    with pytest.raises(TypeError):
        direct.BlsCpiRequest(registrationkey="secret")


@pytest.mark.parametrize(
    "change,code",
    [
        ({"endpoint": "https://example.invalid"}, "endpoint"),
        ({"method": "GET"}, "method"),
        ({"series_ids": direct.SERIES_IDS[:-1]}, "series"),
        ({"startyear": "2024"}, "year"),
        ({"catalog": False}, "options"),
        ({"calculations": False}, "options"),
    ],
)
def test_request_drift_rejected(change, code):
    with pytest.raises(ContractError, match=code):
        replace(direct.BlsCpiRequest(), **change).persisted()


def test_parser_uses_provider_pct_changes_and_accepts_historical_unavailable_row():
    payload = response()
    projected = direct.parse_response(encode(payload))
    values = {item["name"]: item["value"] for item in projected.data["measurements"]}
    assert values == {
        "core_mom": "0.3",
        "core_yoy": "2.4",
        "headline_mom": "0.4",
        "headline_yoy": "3.4",
    }
    # Index levels are not inputs to the provider-returned percentage projection.
    for item in payload["Results"]["series"]:
        item["data"][0]["value"] = "999999.999"
    assert direct.parse_response(encode(payload)).data == projected.data


def test_september_parser_projects_exact_reference_and_provider_values():
    projection = direct.parse_response(
        encode(september_response()),
        reference=retained.SEPTEMBER_REFERENCE_SEMANTICS,
    )
    assert projection.data["reference_period"] == "2026-09"
    assert projection.data["availability"] == {
        "state": "available",
        "basis": "exact_provider_calculation_completeness",
    }
    measurements = {item["name"]: item for item in projection.data["measurements"]}
    assert {name: item["value"] for name, item in measurements.items()} == {
        "core_mom": "0.314159",
        "core_yoy": "3.14159",
        "headline_mom": "0.271828",
        "headline_yoy": "2.71828",
    }
    assert {
        item["comparison_period"]
        for item in measurements.values()
        if item["change_months"] == 1
    } == {"2026-08"}
    assert {
        item["comparison_period"]
        for item in measurements.values()
        if item["change_months"] == 12
    } == {"2025-09"}
    assert {item["reference_period"] for item in measurements.values()} == {"2026-09"}


@pytest.mark.parametrize(
    "mutation",
    [
        "missing_target",
        "wrong_target",
        "missing_mom_comparison",
        "missing_yoy_comparison",
        "wrong_mom_comparison",
        "wrong_yoy_comparison",
    ],
)
def test_september_parser_fails_closed_on_target_or_comparison_drift(mutation):
    payload = september_response()
    rows = payload["Results"]["series"][0]["data"]
    if mutation == "missing_target":
        rows.pop(0)
    elif mutation == "wrong_target":
        rows[0]["periodName"] = "August"
    elif mutation == "missing_mom_comparison":
        rows.pop(1)
    elif mutation == "missing_yoy_comparison":
        rows.pop(2)
    elif mutation == "wrong_mom_comparison":
        rows[1]["periodName"] = "July"
    else:
        rows[2]["periodName"] = "August"
    with pytest.raises(ContractError):
        direct.parse_response(
            encode(payload),
            reference=retained.SEPTEMBER_REFERENCE_SEMANTICS,
        )


def test_september_parser_classifies_partial_target_presence_distinctly():
    payload = september_response()
    payload["Results"]["series"][0]["data"].pop(0)
    with pytest.raises(ContractError, match="bls_cpi_reference_partial"):
        direct.parse_response(
            encode(payload),
            reference=retained.SEPTEMBER_REFERENCE_SEMANTICS,
        )


def test_provider_credential_echo_fails_closed():
    payload = response()
    payload["registrationKey"] = "must-not-persist"
    with pytest.raises(ContractError, match="credential_echo_forbidden"):
        direct.parse_response(encode(payload))


@pytest.mark.parametrize(
    "mutation,code",
    [
        ("status", "request_failed"),
        ("missing_series", "series_not_exact_four"),
        ("series_order", "series_identity"),
        ("catalog", "catalog_identity"),
        ("duplicate_period", "duplicate_period"),
        ("comparison", "source_pointer"),
        ("target_calculation", "target_calculation"),
        ("latest", "latest"),
        ("footnote", "footnotes"),
    ],
)
def test_response_contract_rejections(mutation, code):
    payload = response()
    series = payload["Results"]["series"]
    if mutation == "status":
        payload["status"] = "REQUEST_FAILED"
    elif mutation == "missing_series":
        series.pop()
    elif mutation == "series_order":
        series.reverse()
    elif mutation == "catalog":
        series[0]["catalog"]["seasonality"] = "Not Seasonally Adjusted"
    elif mutation == "duplicate_period":
        series[0]["data"].append(copy.deepcopy(series[0]["data"][0]))
    elif mutation == "comparison":
        series[0]["data"] = [
            row
            for row in series[0]["data"]
            if not (row["year"] == "2025" and row["period"] == "M08")
        ]
    elif mutation == "target_calculation":
        del series[0]["data"][0]["calculations"]
        del series[0]["data"][0]["latest"]
    elif mutation == "latest":
        series[0]["data"][1]["latest"] = "true"
    else:
        series[0]["data"][0]["footnotes"] = [{"code": "P"}]
    with pytest.raises(ContractError, match=code):
        direct.parse_response(encode(payload))


def test_offline_replay_is_reprocess_without_binding_or_acquisition():
    candidate = replay_plan()
    assert candidate.run.kind == RunKind.REPROCESS
    assert candidate.run.binding is None
    assert candidate.version.binding is None
    assert candidate.version.acquisitions == ()
    assert not hasattr(candidate, "acquisition")
    content = json.loads(candidate.content)
    assert content["mode"] == "offline_retained_replay"
    assert content["obtained_at"] is None
    assert content["provider_revision"] == {"state": "not_declared", "value": None}
    assert content["provider_released_at"] is content["provider_vintage"] is None
    assert content["source_pointers"]["core_yoy"] == {
        "series_id": "CUUR0000SA0L1E",
        "reference_period": "2026-08",
        "comparison_period": "2025-08",
        "calculation": "pct_changes/12",
    }
    assert "registrationkey" not in candidate.content.decode().lower()

    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    direct.persist_offline_replay(kernel, candidate)
    publication = direct.publish_offline_replay(kernel, candidate)
    assert publication.request == candidate.publication_request
    forged = replace(
        candidate,
        version=replace(
            candidate.version,
            acquisitions=(Key(Kind.ACQUISITION, "forged"),),
        ),
    )
    with pytest.raises(ContractError, match="offline_acquisition_forbidden"):
        direct.persist_offline_replay(kernel, forged)
    mapped = replace(candidate, version=replace(candidate.version, mapping="active"))
    with pytest.raises(ContractError, match="offline_binding_forbidden"):
        direct.persist_offline_replay(kernel, mapped)


def test_future_live_plan_requires_fresh_direct_identity_and_actual_times():
    with pytest.raises(ContractError, match="identity_not_fresh"):
        live_evidence(acquisition_identity=retained.ACQUISITION)
    evidence = live_evidence()
    candidate = direct.build_future_live_plan(
        evidence,
        identity="direct-cpi-live",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )
    assert candidate.run.kind == RunKind.COLLECT
    assert candidate.acquisition.identity == "mdf-direct-acquisition-new"
    assert candidate.acquisition.obtained_at == evidence.obtained_at
    assert candidate.acquisition.request_identity == evidence.request_identity
    assert candidate.acquisition.provider_data_attempts == 1
    assert candidate.acquisition.external_receipt == "controlled-live-receipt"
    assert candidate.acquisition.identity != retained.ACQUISITION
    assert "candidate_only_not_active" in candidate.binding_candidate.limitations
    content = json.loads(candidate.content)
    assert content["mode"] == "direct_live_provider_response"
    assert content["obtained_at"] == evidence.obtained_at.isoformat()


def test_august_default_contract_preserves_accepted_parser_and_plan_identity():
    raw = encode(response())
    assert direct.parse_response(raw) == direct.parse_response(
        raw,
        reference=retained.AUGUST_REFERENCE_SEMANTICS,
    )
    default = replay_plan(raw)
    explicit = direct.build_offline_replay_plan(
        raw,
        identity="direct-cpi-offline",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
        reference_contract=direct.AUGUST_REFERENCE_CONTRACT,
    )
    assert default == explicit
    assert direct.REFERENCE_PERIOD == "2026-08"
    assert direct.PARSER_REVISION == "mdf-bls-cpi-direct-v1"
    assert direct.RULE == "p4-macro-bls-cpi-direct-admission-r1"
    assert direct.COMPLETENESS == ("exact_four_cpi_provider_calculations_for_2026_08")
    assert direct.BLS_CPI_DIRECT_SOURCE.identity == (
        "mdf.source-candidate.bls-v2.cpi.direct"
    )
    assert direct.BLS_CPI_DIRECT_BINDING_CANDIDATE.identity == (
        "mdf.binding-candidate.bls-v2.cpi.direct"
    )
    assert default.version.finite_scope.members == tuple(
        f"2026-08:bls:cpi:{name}" for name in sorted(retained.MEASURES)
    )


def test_september_live_plan_metadata_agrees_and_candidate_is_inactive():
    evidence = live_evidence(
        encode(september_response()),
        acquisition_identity="mdf-september-release-candidate",
        controlled_external_receipt="controlled-september-release-receipt",
        reference_contract=direct.SEPTEMBER_REFERENCE_CONTRACT,
    )
    candidate = direct.build_future_live_plan(
        evidence,
        identity="bls-cpi-release-reference-2026-09",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
        release_causation=direct.ReleaseCausation(
            "CPI/2026-09/2026-10-14T12:30:00Z",
            "mdf-bls-cpi-release-2026-09",
            1,
            "2026-09",
            datetime(2026, 10, 14, 12, 30, tzinfo=UTC),
        ),
    )
    content = json.loads(candidate.content)
    assert candidate.source == direct.BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE
    assert candidate.binding_candidate == (
        direct.BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE
    )
    assert candidate.source.acquisition_mode == "release_capture"
    assert candidate.source.completeness_contract == direct.SEPTEMBER_COMPLETENESS
    assert candidate.acquisition.result_mode == "release_capture"
    assert candidate.acquisition.completeness == direct.SEPTEMBER_COMPLETENESS
    assert candidate.version.completeness == direct.SEPTEMBER_COMPLETENESS
    assert candidate.publication_request.required_completeness == (
        direct.SEPTEMBER_COMPLETENESS
    )
    assert candidate.version.finite_scope.members == tuple(
        f"2026-09:bls:cpi:{name}" for name in sorted(retained.MEASURES)
    )
    assert content["schema"] == direct.SEPTEMBER_RULE
    assert content["parser_revision"] == direct.SEPTEMBER_PARSER_REVISION
    assert content["data"]["reference_period"] == "2026-09"
    assert {
        pointer["reference_period"] for pointer in content["source_pointers"].values()
    } == {"2026-09"}
    assert {
        pointer["comparison_period"]
        for name, pointer in content["source_pointers"].items()
        if name.endswith("_mom")
    } == {"2026-08"}
    assert {
        pointer["comparison_period"]
        for name, pointer in content["source_pointers"].items()
        if name.endswith("_yoy")
    } == {"2025-09"}
    assert content["provider_revision"] == {"state": "not_declared", "value": None}
    assert content["provider_released_at"] is None
    assert content["provider_vintage"] is None
    assert "candidate_only_not_active" in candidate.binding_candidate.limitations
    assert candidate.binding_candidate.silent_fallback is False


def test_september_contract_cannot_reuse_august_plan_metadata():
    mismatched = replace(
        direct.SEPTEMBER_REFERENCE_CONTRACT,
        completeness=direct.COMPLETENESS,
        source_identity=direct.BLS_CPI_DIRECT_SOURCE.identity,
    )
    with pytest.raises(ContractError, match="reference_contract_not_admitted"):
        live_evidence(
            encode(september_response()),
            reference_contract=mismatched,
        )


def test_live_evidence_fails_closed_for_each_execution_fact():
    invalid = (
        ({"acquisition_identity": ""}, "identity_not_fresh"),
        (
            {
                "requested_at": BASE + timedelta(seconds=2),
                "obtained_at": BASE + timedelta(seconds=1),
            },
            "time_reversal",
        ),
        ({"provider_data_attempts": 0}, "provider_data_attempts_invalid"),
        ({"request_identity": "wrong"}, "request_identity_mismatch"),
        (
            {"request": replace(direct.BlsCpiRequest(), endpoint="https://invalid")},
            "endpoint_mismatch",
        ),
        ({"controlled_external_receipt": ""}, "live_receipt_required"),
        ({"direct_execution": False}, "direct_execution_evidence_required"),
        ({"evidence_mode": "offline_replay"}, "direct_execution_evidence_required"),
    )
    for changes, code in invalid:
        with pytest.raises(ContractError, match=code):
            live_evidence(**changes)


def test_live_builder_rejects_offline_plan_and_other_objects():
    for value in (replay_plan(), object()):
        with pytest.raises(ContractError, match="direct_live_evidence_required"):
            direct.build_future_live_plan(
                value,
                identity="direct-cpi-live",
                finished_at=BASE + timedelta(seconds=2),
                frozen_at=BASE + timedelta(seconds=3),
            )


@pytest.mark.skipif(
    not (EXACT_ROOT / "original.bin").exists(), reason="ignored retained bundle absent"
)
def test_exact_retained_raw_direct_projection_equivalence():
    raw = (EXACT_ROOT / "original.bin").read_bytes()
    retained_record = json.loads((EXACT_ROOT / "retained.json").read_bytes())[
        "retained"
    ]
    assert digest(encode(retained_record)) == retained.SEMANTIC_RECORD_SHA256
    projection = direct.parse_response(raw)
    assert projection.raw_sha256 == retained.RAW_SHA256
    assert projection.raw_size == retained.RAW_SIZE
    assert projection.data == retained_record["data"]
    candidate = replay_plan(raw)
    assert json.loads(candidate.content)["data"] == retained_record["data"]
    evidence = live_evidence(
        raw,
        acquisition_identity="fresh-direct-occurrence-with-identical-content",
        controlled_external_receipt="controlled-direct-execution-receipt",
    )
    live = direct.build_future_live_plan(
        evidence,
        identity="direct-cpi-live-identical-content",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )
    assert live.raw_material.digest == retained.RAW_SHA256
    assert live.acquisition.identity != retained.ACQUISITION
    assert live.acquisition.provider_data_attempts == 1


def test_postgres_allowlist_is_exact_transaction_namespace():
    schema = "mdf_p4_macro_bls_cpi_direct_admission_r1"
    assert validate_schema(schema) == schema
    for unsafe in (schema + "_extra", schema + ";drop"):
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            validate_schema(unsafe)


class FakeTransport:
    def __init__(self, result=None, failure=None):
        self.result = result
        self.failure = failure
        self.calls = []

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        self.calls.append(
            {
                "endpoint": endpoint,
                "body": body,
                "headers": headers,
                "timeout": timeout,
                "max_response_bytes": max_response_bytes,
            }
        )
        if self.failure is not None:
            raise self.failure
        return self.result


def _synthetic_secret(tmp_path, value="A" * 32):
    path = tmp_path / "secret.toml"
    path.write_text(f'[bls]\napi_key = "{value}"\n')
    return path


def _live_clocks():
    return iter(BASE + timedelta(seconds=offset) for offset in range(4))


def test_live_executor_exact_one_wire_shape_and_secret_safe_artifacts(tmp_path):
    credential = "A1" * 16
    transport = FakeTransport(
        live.HttpResult(200, encode(response()), {"content-encoding": "identity"})
    )
    clocks = _live_clocks()
    candidate = live.execute_live_candidate(
        secret_path=_synthetic_secret(tmp_path, credential),
        artifact_root=tmp_path / "artifacts",
        transport=transport,
        clock=lambda: next(clocks),
        identity_factory=lambda: "synthetic-001",
    )
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["endpoint"] == direct.ENDPOINT
    assert call["headers"] == {
        "Accept": "application/json",
        "Accept-Encoding": "identity",
        "Content-Type": "application/json",
    }
    assert call["timeout"] == live.TIMEOUT_SECONDS
    assert call["max_response_bytes"] == direct.MAX_RESPONSE_BYTES
    assert json.loads(call["body"]) == {
        "seriesid": list(direct.SERIES_IDS),
        "startyear": "2025",
        "endyear": "2026",
        "catalog": True,
        "calculations": True,
        "registrationkey": credential,
    }
    assert candidate.evidence.requested_at == BASE
    assert candidate.evidence.obtained_at == BASE + timedelta(seconds=1)
    assert candidate.evidence.provider_data_attempts == 1
    assert candidate.plan.run.started_at == BASE
    assert candidate.plan.run.finished_at == BASE + timedelta(seconds=2)
    assert candidate.plan.version.frozen_at == BASE + timedelta(seconds=3)
    assert candidate.plan.acquisition.identity == "mdf-bls-cpi-direct-synthetic-001"
    assert candidate.plan.raw_material.grade == Grade.ORIGINAL
    assert candidate.plan.raw_material.durable is False
    assert candidate.plan.raw_material.replayable is False
    assert candidate.plan.raw_material.controlled_ref.endswith("/raw-response.bin")
    assert candidate.plan.semantic_material.grade == Grade.PROJECTED
    assert candidate.plan.semantic_material.durable is True
    assert candidate.plan.semantic_material.replayable is True
    assert (
        candidate.plan.semantic_material.digest == candidate.plan.version.content_digest
    )
    assert candidate.plan.semantic_material.controlled_ref == (
        "mdf-content://" + candidate.plan.version.identity
    )
    assert candidate.artifact_directory.name == "run-synthetic-001"
    assert (candidate.artifact_directory / "raw-response.bin").read_bytes() == encode(
        response()
    )
    artifact_bytes = b"".join(
        path.read_bytes() for path in candidate.artifact_directory.iterdir()
    )
    assert credential.encode() not in artifact_bytes
    assert b"registrationkey" not in artifact_bytes.lower()
    observation = json.loads(
        (candidate.artifact_directory / "observation.json").read_bytes()
    )
    assert observation["provider_data_attempts"] == 1
    assert observation["raw_sha256"] == digest(encode(response()))


@pytest.mark.parametrize(
    "result,classification",
    [
        (
            live.HttpResult(302, b"redirect", {"location": "https://elsewhere"}),
            "redirect_rejected",
        ),
        (live.HttpResult(503, b"failure", {}), "http_failure"),
        (
            live.HttpResult(200, b"x" * (direct.MAX_RESPONSE_BYTES + 1), {}),
            "response_too_large",
        ),
        (live.HttpResult(200, b"not-json", {}), "response_contract_failed"),
    ],
)
def test_live_executor_failures_are_terminal_exact_one(
    tmp_path, result, classification
):
    transport = FakeTransport(result)
    clocks = _live_clocks()
    with pytest.raises(live.LiveExecutionError, match=classification) as caught:
        live.execute_live_candidate(
            secret_path=_synthetic_secret(tmp_path),
            artifact_root=tmp_path / "artifacts",
            transport=transport,
            clock=lambda: next(clocks),
            identity_factory=lambda: "failure-001",
        )
    assert caught.value.provider_data_attempts == 1
    assert len(transport.calls) == 1
    assert not (tmp_path / "artifacts").exists()


def test_live_executor_transport_failure_has_no_retry_or_fallback(tmp_path):
    transport = FakeTransport(failure=OSError("secret-bearing diagnostics omitted"))
    with pytest.raises(live.LiveExecutionError) as caught:
        live.execute_live_candidate(
            secret_path=_synthetic_secret(tmp_path),
            artifact_root=tmp_path / "artifacts",
            transport=transport,
            identity_factory=lambda: "transport-failure",
        )
    assert str(caught.value) == "bls_live_transport_failure"
    assert caught.value.provider_data_attempts == 1
    assert len(transport.calls) == 1


@pytest.mark.parametrize(
    "document",
    [
        "[postgres]\npassword='not-a-bls-key'\n",
        "[bls]\napi_key='short'\n",
        "[bls]\napi_key='GGGGGGGGGGGGGGGGGGGGGGGGGGGGGGGG'\n",
        "[bls]\napi_key=42\n",
        "not valid toml =",
    ],
)
def test_invalid_credential_fails_before_provider_execution(tmp_path, document):
    secret = tmp_path / "secret.toml"
    secret.write_text(document)
    transport = FakeTransport()
    with pytest.raises(live.LiveExecutionError, match="credential_invalid") as caught:
        live.execute_live_candidate(secret_path=secret, transport=transport)
    assert caught.value.provider_data_attempts == 0
    assert transport.calls == []


def test_live_plan_persists_and_publishes_through_kernel_without_activation():
    candidate = direct.build_future_live_plan(
        live_evidence(),
        identity="direct-cpi-live-persistence",
        finished_at=BASE + timedelta(seconds=2),
        frozen_at=BASE + timedelta(seconds=3),
    )
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=4))
    assert (
        direct.persist_live_plan_before_publication(kernel, candidate)
        == candidate.version
    )
    assert ledger.get(candidate.source.key) == candidate.source
    assert ledger.get(candidate.binding_candidate.key) == candidate.binding_candidate
    assert ledger.get(candidate.acquisition.key) == candidate.acquisition
    assert ledger.get_content(candidate.version.key) == candidate.content
    publication = direct.publish_live_plan(kernel, candidate)
    assert publication.request == candidate.publication_request
    assert "candidate_only_not_active" in candidate.binding_candidate.limitations


def test_live_postgres_allowlist_is_exact_transaction_namespace():
    schema = "mdf_p4_macro_bls_cpi_direct_live_r1"
    assert validate_schema(schema) == schema
    with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
        validate_schema(schema + "_extra")
