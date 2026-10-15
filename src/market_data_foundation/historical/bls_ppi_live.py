"""Exact-one BLS PPI execution edge for explicit nonproduction opt-in."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from market_data_foundation.canonical.model import ContractError, instant, require
from market_data_foundation.historical.bls_cpi_direct_live import (
    ExactOneHttpsTransport,
    LiveExecutionError,
    Transport,
    load_registration_key,
)
from market_data_foundation.historical.bls_ppi_final_demand import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    BlsPpiRequest,
    PpiLiveEvidence,
    PpiLivePlan,
    build_live_plan,
    parse_response,
)

TIMEOUT_SECONDS = 20.0
DEFAULT_SECRET_PATH = Path("config/local/secret.toml")
ARTIFACT_ROOT = Path("artifacts/t01-macro-bls-ppi-vertical-r1")


@dataclass(frozen=True)
class PpiLiveCandidate:
    evidence: PpiLiveEvidence
    plan: PpiLivePlan
    artifact_directory: Path


def _safe_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def execute_live_candidate(
    *,
    secret_path: Path = DEFAULT_SECRET_PATH,
    artifact_root: Path = ARTIFACT_ROOT,
    transport: Transport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    identity_factory: Callable[[], object] = uuid4,
) -> PpiLiveCandidate:
    """Make one request, retain original bytes, and build the exact typed plan."""
    registration_key = load_registration_key(secret_path)
    request = BlsPpiRequest()
    token = str(identity_factory())
    require(bool(token), "bls_ppi_live_identity_invalid")
    run_name = "run-" + token
    receipt = "mdf-artifact://t01-macro-bls-ppi-vertical-r1/" + run_name
    body = dict(request.persisted()["body"])
    body["registrationkey"] = registration_key
    wire_body = json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    requested_at = clock()
    instant(requested_at)
    client = transport or ExactOneHttpsTransport()
    try:
        response = client.post(
            ENDPOINT,
            wire_body,
            {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Content-Type": "application/json",
            },
            timeout=TIMEOUT_SECONDS,
            max_response_bytes=MAX_RESPONSE_BYTES,
        )
    except LiveExecutionError:
        raise
    except Exception:
        raise LiveExecutionError("bls_ppi_live_transport_failure", 1) from None
    obtained_at = clock()
    instant(obtained_at)
    if 300 <= response.status < 400:
        raise LiveExecutionError("bls_ppi_live_redirect_rejected", 1)
    if response.status != 200:
        raise LiveExecutionError(
            "bls_ppi_live_http_failure", 1, http_status=response.status
        )
    if len(response.body) > MAX_RESPONSE_BYTES:
        raise LiveExecutionError("bls_ppi_live_response_too_large", 1)
    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise LiveExecutionError("bls_ppi_live_content_encoding_rejected", 1)
    try:
        evidence = PpiLiveEvidence(
            response.body,
            request,
            requested_at,
            obtained_at,
            "mdf-bls-ppi-final-demand-" + token,
            receipt,
        )
        finished_at = clock()
        frozen_at = clock()
        plan = build_live_plan(
            evidence,
            identity="t01-bls-ppi-final-demand-" + token,
            finished_at=finished_at,
            frozen_at=frozen_at,
        )
        projection = parse_response(response.body)
    except ContractError:
        raise LiveExecutionError("bls_ppi_live_response_contract_failed", 1) from None
    directory = artifact_root / run_name
    files = {
        "raw-response.bin": response.body,
        "request.json": _safe_json(
            {"request": request.persisted(), "request_identity": request.identity}
        ),
        "observation.json": _safe_json(
            {
                "classification": "success",
                "http_status": response.status,
                "requested_at": requested_at.isoformat(),
                "obtained_at": obtained_at.isoformat(),
                "acquisition_identity": evidence.acquisition_identity,
                "provider_data_attempts": 1,
                "controlled_external_receipt": receipt,
                "raw_sha256": projection.raw_sha256,
                "raw_size": projection.raw_size,
            }
        ),
    }
    secret = registration_key.encode()
    if any(secret in content for content in files.values()):
        raise LiveExecutionError("bls_ppi_live_credential_persistence_forbidden", 1)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            (directory / name).write_bytes(content)
    except OSError:
        raise LiveExecutionError("bls_ppi_live_artifact_write_failed", 1) from None
    return PpiLiveCandidate(evidence, plan, directory)
