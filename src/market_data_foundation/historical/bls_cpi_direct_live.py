"""Exact-one BLS v2 execution edge for a separately authorized live run."""

from __future__ import annotations

import http.client
import json
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from market_data_foundation.canonical.model import (
    ContractError,
    instant,
    require,
)
from market_data_foundation.historical.bls_cpi_direct import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    BlsCpiRequest,
    DirectCpiLiveEvidence,
    DirectCpiLivePlan,
    build_future_live_plan,
    parse_response,
)

ARTIFACT_ROOT = Path("artifacts/p4-macro-bls-cpi-direct-live-r1")
DEFAULT_SECRET_PATH = Path("config/local/secret.toml")
TIMEOUT_SECONDS = 20.0
_KEY_SHAPE = re.compile(r"[0-9A-Fa-f]{32}")


class LiveExecutionError(RuntimeError):
    """Secret-safe terminal classification with an exact attempt count."""

    def __init__(self, classification: str, provider_data_attempts: int) -> None:
        super().__init__(classification)
        self.classification = classification
        self.provider_data_attempts = provider_data_attempts


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: Mapping[str, str]


class Transport(Protocol):
    def post(
        self,
        endpoint: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResult: ...


class ExactOneHttpsTransport:
    """One stdlib HTTPS exchange; redirects and retries are not implemented."""

    def post(
        self,
        endpoint: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResult:
        require(endpoint == ENDPOINT, "bls_live_endpoint_mismatch")
        connection = http.client.HTTPSConnection("api.bls.gov", timeout=timeout)
        try:
            connection.request(
                "POST", "/publicAPI/v2/timeseries/data/", body=body, headers=headers
            )
            response = connection.getresponse()
            length = response.getheader("Content-Length")
            if length is not None and int(length) > max_response_bytes:
                raise LiveExecutionError("bls_live_response_too_large", 1)
            raw = response.read(max_response_bytes + 1)
            if len(raw) > max_response_bytes:
                raise LiveExecutionError("bls_live_response_too_large", 1)
            return HttpResult(
                response.status,
                raw,
                {key.lower(): value for key, value in response.getheaders()},
            )
        finally:
            connection.close()


@dataclass(frozen=True)
class LiveExecutionCandidate:
    evidence: DirectCpiLiveEvidence
    plan: DirectCpiLivePlan
    artifact_directory: Path


def load_registration_key(path: Path = DEFAULT_SECRET_PATH) -> str:
    """Load only the BLS key needed on the wire; errors reveal no file content."""
    try:
        document = tomllib.loads(path.read_text())
        section = document["bls"]
        key = section["api_key"]
    except OSError, KeyError, TypeError, tomllib.TOMLDecodeError:
        raise LiveExecutionError("bls_live_credential_invalid", 0) from None
    if type(key) is not str or _KEY_SHAPE.fullmatch(key) is None:
        raise LiveExecutionError("bls_live_credential_invalid", 0)
    return key


def _wire_body(request: BlsCpiRequest, registration_key: str) -> bytes:
    persisted = request.persisted()
    body = dict(persisted["body"])
    body["registrationkey"] = registration_key
    return json.dumps(
        body, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()


def _safe_json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def _write_artifacts(
    root: Path,
    run_name: str,
    evidence: DirectCpiLiveEvidence,
    http_status: int,
    registration_key: str,
) -> Path:
    projection = parse_response(evidence.raw_bytes)
    directory = root / run_name
    files = {
        "raw-response.bin": evidence.raw_bytes,
        "request.json": _safe_json(
            {
                "request": evidence.request.persisted(),
                "request_identity": evidence.request_identity,
            }
        ),
        "observation.json": _safe_json(
            {
                "classification": "success",
                "http_status": http_status,
                "requested_at": evidence.requested_at.isoformat(),
                "obtained_at": evidence.obtained_at.isoformat(),
                "acquisition_identity": evidence.acquisition_identity,
                "provider_data_attempts": evidence.provider_data_attempts,
                "controlled_external_receipt": evidence.controlled_external_receipt,
                "raw_sha256": projection.raw_sha256,
                "raw_size": projection.raw_size,
            }
        ),
        "projection.json": _safe_json(
            {
                "data": projection.data,
                "parser_result": "accepted",
                "provider_messages": list(projection.provider_messages),
                "provider_response_time_ms": projection.response_time_ms,
            }
        ),
    }
    secret = registration_key.encode()
    if any(secret in content for content in files.values()):
        raise LiveExecutionError("bls_live_credential_persistence_forbidden", 1)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        for name, content in files.items():
            (directory / name).write_bytes(content)
    except OSError:
        raise LiveExecutionError("bls_live_artifact_write_failed", 1) from None
    return directory


def execute_live_candidate(
    *,
    secret_path: Path = DEFAULT_SECRET_PATH,
    artifact_root: Path = ARTIFACT_ROOT,
    transport: Transport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    identity_factory: Callable[[], object] = uuid4,
) -> LiveExecutionCandidate:
    """Execute once and construct live evidence/plan from observed facts.

    Calling this function is the provider-effect boundary and requires separate
    authorization. Credential validation completes before the transport call.
    """
    registration_key = load_registration_key(secret_path)
    request = BlsCpiRequest()
    token = str(identity_factory())
    require(bool(token), "bls_live_identity_invalid")
    acquisition_identity = "mdf-bls-cpi-direct-" + token
    run_name = "run-" + token
    receipt = "mdf-artifact://p4-macro-bls-cpi-direct-live-r1/" + run_name
    requested_at = clock()
    instant(requested_at)
    client = transport or ExactOneHttpsTransport()
    try:
        result = client.post(
            ENDPOINT,
            _wire_body(request, registration_key),
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
        raise LiveExecutionError("bls_live_transport_failure", 1) from None
    obtained_at = clock()
    instant(obtained_at)
    if 300 <= result.status < 400:
        raise LiveExecutionError("bls_live_redirect_rejected", 1)
    if result.status != 200:
        raise LiveExecutionError("bls_live_http_failure", 1)
    if len(result.body) > MAX_RESPONSE_BYTES:
        raise LiveExecutionError("bls_live_response_too_large", 1)
    if result.headers.get("content-encoding", "identity").lower() != "identity":
        raise LiveExecutionError("bls_live_content_encoding_rejected", 1)
    try:
        evidence = DirectCpiLiveEvidence(
            raw_bytes=result.body,
            request=request,
            request_identity=request.identity,
            requested_at=requested_at,
            obtained_at=obtained_at,
            acquisition_identity=acquisition_identity,
            provider_data_attempts=1,
            controlled_external_receipt=receipt,
        )
        finished_at = clock()
        frozen_at = clock()
        plan = build_future_live_plan(
            evidence,
            identity="p4-bls-cpi-direct-live-" + token,
            finished_at=finished_at,
            frozen_at=frozen_at,
        )
    except ContractError:
        raise LiveExecutionError("bls_live_response_contract_failed", 1) from None
    directory = _write_artifacts(
        artifact_root, run_name, evidence, result.status, registration_key
    )
    return LiveExecutionCandidate(evidence, plan, directory)
