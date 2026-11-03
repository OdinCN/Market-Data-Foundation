"""Opt-in, one-exchange M3 candidate edge; no retry, activation or DB writes."""

from __future__ import annotations

import http.client
import json
import re
import time
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.census_retail_live import (
    HttpResult,
    _response_headers,
)
from market_data_foundation.historical.m3_capital_goods import (
    MAX_RESPONSE_BYTES,
    QUALIFICATION,
    SERIES,
    M3Request,
    check,
    parse_response,
)
from market_data_foundation.qualification import (
    Binding,
    Endpoint,
    Evidence,
    Fixture,
    MacroCapability,
    observe,
)
from market_data_foundation.source_request_policy import classify_source_exception

ENDPOINTS = frozenset(
    "https://api.census.gov/data/timeseries/eits/" + dataset
    for dataset in ("advm3", "m3")
)


class M3SourceError(RuntimeError):
    def __init__(self, classification, attempts=0, http_status=None):
        self.classification = classification
        self.attempts = attempts
        self.http_status = http_status
        super().__init__(classification)


class M3HttpsTransport:
    """Dedicated capability boundary; does not broaden the MARTS transport."""

    def get(self, endpoint, parameters, *, timeout, max_response_bytes):
        check(endpoint in ENDPOINTS, "endpoint_invalid")
        check(type(timeout) in (float, int) and 0 < timeout <= 20, "timeout_invalid")
        check(
            type(max_response_bytes) is int
            and 0 < max_response_bytes <= MAX_RESPONSE_BYTES,
            "size_limit_invalid",
        )
        parsed = urlsplit(endpoint)
        deadline = time.monotonic() + timeout
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout)
        try:
            connection.request(
                "GET",
                parsed.path + "?" + urlencode(parameters),
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            if connection.sock is not None:
                connection.sock.settimeout(max(0.001, deadline - time.monotonic()))
            response = connection.getresponse()
            headers = _response_headers(response.getheaders())
            if response.status != 200:
                return HttpResult(response.status, b"", headers)
            length = headers.get("content-length")
            check(length is None or int(length) <= max_response_bytes, "body_too_large")
            chunks, size = [], 0
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError
                if connection.sock is not None:
                    connection.sock.settimeout(remaining)
                block = response.read1(min(65536, max_response_bytes + 1 - size))
                if not block:
                    break
                chunks.append(block)
                size += len(block)
                check(size <= max_response_bytes, "body_too_large")
            body = b"".join(chunks)
            check(length is None or len(body) == int(length), "body_incomplete")
            return HttpResult(response.status, body, headers)
        finally:
            connection.close()


class M3CandidateSource:
    """Inject the product's effective credential loader and Census request control.

    A successful candidate parse remains unqualified. No key discovery, provider
    substitution, request loop or credential persistence lives at this edge.
    """

    def __init__(
        self,
        *,
        credential_loader,
        request_control,
        enabled=False,
        transport=None,
        clock=lambda: datetime.now(UTC),
    ):
        self._credential_loader = credential_loader
        self._control = request_control
        self._enabled = enabled
        self._transport = transport
        self._clock = clock

    def fetch(self, request: M3Request):
        check(self._enabled is True, "explicit_opt_in_required")
        check(type(request) is M3Request, "request_invalid")
        attempts, status = 0, None
        try:
            key = self._credential_loader()
            check(
                type(key) is str and re.fullmatch(r"[A-Za-z0-9]{16,128}", key),
                "credential_invalid",
            )
            check(self._control is not None, "request_control_required")
            transport = self._transport or M3HttpsTransport()
            with self._control.attempt() as admission:
                attempts = 1
                result = transport.get(
                    request.endpoint,
                    request.persisted()["parameters"] | {"key": key},
                    timeout=20,
                    max_response_bytes=MAX_RESPONSE_BYTES,
                )
                check(
                    type(result) is HttpResult
                    and type(result.status) is int
                    and 100 <= result.status <= 599,
                    "http_result_invalid",
                )
                status = result.status
                admission.record_response(status, result.headers)
            if status != 200:
                raise M3SourceError("census_m3_http_failure", attempts, status)
            check(
                result.headers.get("content-type", "").split(";", 1)[0].lower()
                == "application/json"
                and result.headers.get("content-encoding", "identity").lower()
                == "identity",
                "http_headers_invalid",
            )
            return parse_response(
                result.body, request, obtained_at=self._clock(), forbidden_secret=key
            )
        except M3SourceError:
            raise
        except Exception as error:
            if isinstance(error, ContractError):
                classification = "response_contract_invalid"
            else:
                classification, _ = classify_source_exception(error)
            raise M3SourceError(
                "census_m3_" + classification, attempts, status
            ) from None


def qualify_candidate(source, request, *, artifact_directory: Path, allow_live=False):
    """Use the existing macro capability/fixture/observation contract.

    The caller supplies a fresh directory under ignored artifacts. Observations
    carry exception types/classifications only; raw and receipt stay local.
    """
    check(allow_live is True, "qualification_opt_in_required")
    directory = Path(artifact_directory).resolve()
    root = (Path.cwd() / "artifacts").resolve()
    check(directory.is_relative_to(root) and directory != root, "artifact_path_invalid")
    directory.mkdir(parents=True, exist_ok=False)
    symbols = tuple(SERIES[(request.category_code, kind)] for kind in request.kinds)
    fixture = Fixture(
        "census-" + request.dataset + "-" + request.category_code + "-candidate",
        MacroCapability.TIMESERIES,
        symbols,
        date.fromisoformat(request.start_month + "-01"),
        date.fromisoformat(request.end_month + "-01"),
        2,
        {"qualification": QUALIFICATION, "stage": request.report_stage},
    )
    binding = Binding(
        "Census",
        "mdf.m3.capital-goods.candidate",
        "r1",
        MacroCapability.TIMESERIES,
        "live",
        Endpoint("api.census.gov", 443),
    )
    outcome = {}

    def execute(_):
        try:
            snapshot = source.fetch(request)
        except M3SourceError as error:
            outcome.update(
                source_classification=error.classification,
                transport_attempts=str(error.attempts),
                http_status=str(error.http_status),
            )
            raise
        (directory / "raw-response.bin").write_bytes(snapshot.raw)
        (directory / "request.json").write_text(
            json.dumps(request.persisted(), indent=2)
        )
        (directory / "capture.json").write_text(
            json.dumps(
                {
                    "obtained_at": snapshot.obtained_at.isoformat(),
                    "raw_sha256": snapshot.raw_sha256,
                    "version_basis": snapshot.version_basis,
                    "source_version_date": None,
                    "official_release_event": None,
                    "qualification": QUALIFICATION,
                    "binding_evidence": snapshot.binding_evidence,
                },
                indent=2,
            )
        )
        return Evidence(
            success=any(c.value is not None for c in snapshot.cells),
            returned_symbols=tuple(
                s
                for s in symbols
                if any(c.series == s and c.value is not None for c in snapshot.cells)
            ),
            row_count=sum(c.source_pointer is not None for c in snapshot.cells),
            complete=snapshot.complete,
            missing_fields=tuple(
                f"{c.series}:{c.reference_month}"
                for c in snapshot.cells
                if c.value is None
            ),
            units={s: "million_current_usd" for s in symbols},
            semantics={
                "qualification": QUALIFICATION,
                "version_basis": snapshot.version_basis,
                "event_attribution": "unconfirmed",
                "stage": request.report_stage,
                "binding_evidence": snapshot.binding_evidence,
            },
            endpoint=Endpoint("api.census.gov", 443),
        )

    record = observe(binding, fixture, execute, allow_live=allow_live)
    record["semantics"].update(outcome)
    (directory / "observation.json").write_text(json.dumps(record, indent=2))
    return record
