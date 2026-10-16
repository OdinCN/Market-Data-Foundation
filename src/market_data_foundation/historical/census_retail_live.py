"""Default-off Census MARTS GET edge; one logical read, shared Source retry."""

from __future__ import annotations

import http.client
import json
import re
import time
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlencode
from uuid import uuid4

from market_data_foundation.canonical.model import instant, require
from market_data_foundation.historical.census_retail import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    CensusRetailRequest,
    RetailEvidence,
    RetailPlan,
    build_live_plan,
)
from market_data_foundation.historical.macro_lifecycle import _meter
from market_data_foundation.source_request_policy import (
    SourceRequestFailed,
    request_source,
)

TIMEOUT_SECONDS = 20.0
DEFAULT_SECRET_PATH = Path("config/local/secret.toml")
ARTIFACT_ROOT = Path("artifacts/t05-macro-census-retail-r1")


@dataclass(frozen=True)
class HttpResult:
    status: int
    body: bytes
    headers: Mapping[str, str]


class Transport(Protocol):
    def get(
        self, endpoint, parameters, *, timeout, max_response_bytes
    ) -> HttpResult: ...


class RetailLiveError(RuntimeError):
    def __init__(self, classification: str, attempts: int):
        self.classification = classification
        self.provider_data_attempts = attempts
        super().__init__(classification)


class ExactCensusTransport:
    """One HTTPS GET; no redirects, fallback, key logging or local persistence."""

    def get(self, endpoint, parameters, *, timeout, max_response_bytes) -> HttpResult:
        require(endpoint == ENDPOINT, "census_retail_endpoint_mismatch")
        connection = http.client.HTTPSConnection("api.census.gov", timeout=timeout)
        try:
            connection.request(
                "GET",
                "/data/timeseries/eits/marts?" + urlencode(parameters),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            pairs = [(key.lower(), value) for key, value in response.getheaders()]
            require(
                len(dict(pairs)) == len(pairs), "census_retail_duplicate_http_header"
            )
            headers = dict(pairs)
            if response.status != 200:
                return HttpResult(response.status, b"", headers)
            length = headers.get("content-length")
            require(
                length is None or re.fullmatch(r"[0-9]+", length) is not None,
                "census_retail_content_length_invalid",
            )
            require(
                length is None or int(length) <= max_response_bytes,
                "census_retail_response_too_large",
            )
            body = response.read(max_response_bytes + 1)
            require(len(body) <= max_response_bytes, "census_retail_response_too_large")
            if length is not None and len(body) != int(length):
                raise http.client.IncompleteRead(body)
            return HttpResult(response.status, body, headers)
        finally:
            connection.close()


class MeteredRetailTransport:
    """Journal one intent and each wire attempt, using shared metering semantics."""

    def __init__(self, delegate, before_request, after_request):
        self.delegate = delegate
        self.before_request = before_request
        self.after_request = after_request
        self.attempts = 0

    def get(self, endpoint, parameters, *, timeout, max_response_bytes):
        if self.attempts == 0:
            _meter(self.before_request)
        self.attempts += 1
        try:
            result = self.delegate.get(
                endpoint,
                parameters,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
        except Exception:
            _meter(self.after_request, False, self.attempts)
            raise
        _meter(self.after_request, True, self.attempts)
        return result


def load_api_key(path: Path) -> str:
    """Standing capability; called only after explicit enablement, never on import."""
    try:
        with path.open("rb") as stream:
            value = tomllib.load(stream)["census"]["api_key"]
        require(
            type(value) is str and re.fullmatch(r"[A-Za-z0-9]{16,128}", value),
            "census_retail_credential_invalid",
        )
        return value
    except Exception:
        raise RetailLiveError("census_retail_credential_unavailable", 0) from None


@dataclass(frozen=True)
class RetailCandidate:
    evidence: RetailEvidence
    plan: RetailPlan
    artifact_directory: Path


def execute_live_candidate(
    *,
    enabled: bool = False,
    secret_path: Path = DEFAULT_SECRET_PATH,
    artifact_root: Path = ARTIFACT_ROOT,
    transport: Transport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    identity_factory: Callable[[], object] = uuid4,
    sleep: Callable[[float], None] = time.sleep,
) -> RetailCandidate:
    require(enabled is True, "census_retail_explicit_opt_in_required")
    request = CensusRetailRequest()
    token = str(identity_factory())
    require(
        re.fullmatch(r"[a-zA-Z0-9-]{1,80}", token) is not None,
        "census_retail_identity_invalid",
    )
    key = load_api_key(secret_path)
    parameters = request.persisted()["parameters"] | {"key": key}
    requested_at = clock()
    instant(requested_at)
    client = transport or ExactCensusTransport()

    def status(response):
        require(
            type(response) is HttpResult and type(response.status) is int,
            "census_retail_http_response_invalid",
        )
        return response.status

    try:
        response, attempts = request_source(
            lambda: client.get(
                ENDPOINT,
                parameters,
                timeout=TIMEOUT_SECONDS,
                max_response_bytes=MAX_RESPONSE_BYTES,
            ),
            status=status,
            sleep=sleep,
        )
    except SourceRequestFailed as error:
        raise RetailLiveError(error.classification, error.attempts) from None
    # Every remaining operation is outside Source retry, including headers and IO.
    try:
        obtained_at = clock()
        instant(obtained_at)
        require(
            type(response.body) is bytes and len(response.body) <= MAX_RESPONSE_BYTES,
            "census_retail_body_invalid",
        )
        require(
            response.headers.get("content-encoding", "identity").lower() == "identity"
            and response.headers.get("content-type", "").split(";", 1)[0].lower()
            == "application/json",
            "census_retail_http_headers_invalid",
        )
        require(
            key.encode() not in response.body, "census_retail_credential_echo_forbidden"
        )
        receipt = "mdf-artifact://t05-macro-census-retail-r1/run-" + token
        evidence = RetailEvidence(
            response.body,
            request,
            requested_at,
            obtained_at,
            "mdf-census-retail-" + token,
            receipt,
            attempts,
        )
        plan = build_live_plan(
            evidence,
            identity="t05-census-retail-" + token,
            finished_at=clock(),
            frozen_at=clock(),
        )
    except Exception:
        raise RetailLiveError(
            "census_retail_response_contract_failed", attempts
        ) from None
    directory = artifact_root / ("run-" + token)
    try:
        directory.mkdir(parents=True, exist_ok=False)
        (directory / "raw-response.bin").write_bytes(response.body)
        (directory / "request.json").write_text(
            json.dumps(request.persisted(), sort_keys=True)
        )
        (directory / "observation.json").write_text(
            json.dumps(
                {
                    "classification": "success",
                    "provider_data_attempts": attempts,
                    "requested_at": requested_at.isoformat(),
                    "obtained_at": obtained_at.isoformat(),
                    "raw_sha256": plan.raw_material.digest,
                    "raw_size": len(response.body),
                },
                sort_keys=True,
            )
        )
    except OSError:
        raise RetailLiveError("census_retail_artifact_write_failed", attempts) from None
    return RetailCandidate(evidence, plan, directory)
