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
    Key,
    Kind,
    RunKind,
    digest,
)
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import bls_cpi_direct as direct
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
