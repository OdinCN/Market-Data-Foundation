"""Default-closed Employment edge; no imports or ordinary Reads perform IO.

Linux process isolation bounds DNS/TLS/headers/body together, including blocked
and slow-drip transports. The outer supervisor also bounds local processing and
artifact IO. Transient Source transport failures retry only within the shared
bounded logical-request policy; parsing and local effects remain outside it.
"""

from __future__ import annotations

import base64
import binascii
import ctypes
import html
import http.client
import json
import multiprocessing
import os
import re
import select
import signal
import struct
import subprocess
import threading
import time
import tomllib
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote_plus
from uuid import uuid4

from market_data_foundation.canonical.model import ContractError, instant
from market_data_foundation.historical.bls_employment import (
    CONTRACT_REVISION,
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    PARSER_REVISION,
    REFERENCES,
    SERIES_IDS,
    SOURCE_IDENTITY,
    BlsEmploymentRequest,
    EmploymentEvidence,
    build_plan,
    parse_response,
)
from market_data_foundation.historical.employment_candidate import (
    EmploymentRetainedCandidate,
)
from market_data_foundation.source_request_policy import (
    SOURCE_REQUEST_POLICY,
    SourceRequestFailed,
    classify_source_exception,
    request_source,
    retryable_http_status,
    valid_transport_attempts,
)

REVISION = "T03_EMPLOYMENT_SOURCE_OCCAM_R1"
IPC_BYTES = 1_300_000
SCREEN_WORK_UNITS = 16 * IPC_BYTES
SCREEN_VARIANTS = 4096
SCREEN_DEPTH = 8
PLAN_SHA256 = "81bcc3446959e9a18526e53b93d406f01982f18ee9ee3b5dc464f1ec70eefac6"
FIXTURE_ID = "t03-bls-employment-sa-2026-09-prior-two-months-qualification-r1"
CAPABILITY = "timeseries.data:CES-CPS-employment"
NETWORK_SECONDS = 20.0
SOURCE_REQUEST_SECONDS = (
    NETWORK_SECONDS * SOURCE_REQUEST_POLICY.max_transport_attempts
    + sum(SOURCE_REQUEST_POLICY.backoff_seconds)
    + 1.0
)
ATTEMPT_SECONDS = SOURCE_REQUEST_SECONDS + 10.0
REPOSITORY = Path(__file__).resolve().parents[3]
SECRET_PATH = Path("/srv/market-data-foundation/config/local/secret.toml")
ARTIFACT_ROOT = REPOSITORY / "artifacts/t03-source-qualification-r1"
_HEADERS = {
    "Accept": "application/json",
    "Accept-Encoding": "identity",
    "Content-Type": "application/json",
}
_SENSITIVE = re.compile(
    r"registration[\W_]*key|api[\W_]*key|credential|secret|token|password|authorization",
    re.IGNORECASE,
)


class Classification(StrEnum):
    RETAINED = "retained_unqualified"
    DISABLED = "opt_in_required"
    LIMITS = "source_limits_rejected"
    CREDENTIAL = "credential_invalid"
    TRANSPORT = "transport_failure_exhausted"
    TIMEOUT = "network_retry_exhausted"
    ATTEMPT_TIMEOUT = "attempt_deadline_exhausted"
    REDIRECT = "redirect_rejected"
    HTTP = "http_status_rejected"
    SIZE = "response_size_rejected"
    HEADERS = "response_headers_rejected"
    ECHO = "credential_echo_rejected"
    CONTRACT = "source_contract_rejected"
    TIME = "time_contract_rejected"
    ARTIFACT = "artifact_failure_receipt_unknown"


class EmploymentAcquisitionError(RuntimeError):
    """Only fixed classifications and safe status/counts cross the boundary."""

    def __init__(
        self,
        classification: Classification,
        attempts: int,
        http_status: int | None = None,
        attempt_id: str | None = None,
    ) -> None:
        if not _valid_error(classification, attempts, http_status):
            classification, attempts, http_status = Classification.TRANSPORT, 1, None
        if not _valid_attempt_id(attempt_id):
            attempt_id = None
        super().__init__(classification.value)
        self.classification = classification
        self.attempts = attempts
        self.http_status = http_status
        self.attempt_id = attempt_id
        self.writer_released = False

    def safe_observation(self) -> dict:
        code, attempts, status = _safe_error(self)
        attempt_id = self.attempt_id if _valid_attempt_id(self.attempt_id) else None
        return {
            "capability": CAPABILITY,
            "fixture_id": FIXTURE_ID,
            "revision": REVISION,
            "source_identity": SOURCE_IDENTITY,
            "source_qualification": "NOT_ESTABLISHED",
            "classification": code.value,
            "transport_attempts": attempts,
            "http_status": status,
            "attempt_id": attempt_id,
            "request_identity": BlsEmploymentRequest().identity,
            "pending_receipt": _pending_name(attempt_id) if attempt_id else None,
            "artifact_run": "run-" + attempt_id if attempt_id else None,
            "writer_released": self.writer_released is True,
            "writer_release_receipt": (
                _release_name(attempt_id) if attempt_id else None
            ),
            "transport_policy": "mdf_shared_source_request_policy",
            "max_transport_attempts": SOURCE_REQUEST_POLICY.max_transport_attempts,
            "retry_exhausted": (
                attempts == SOURCE_REQUEST_POLICY.max_transport_attempts
                and (
                    code in {Classification.TRANSPORT, Classification.TIMEOUT}
                    or (code is Classification.HTTP and retryable_http_status(status))
                )
            ),
            "exception": {
                "category": code.value,
                "module": __name__,
                "type": "EmploymentAcquisitionError",
            },
        }


def _valid_attempt_id(value):
    return value is None or (
        type(value) is str and re.fullmatch(r"[a-f0-9]{32}", value) is not None
    )


def _valid_error(code, attempts, status):
    return (
        type(code) is Classification
        and code is not Classification.RETAINED
        and type(attempts) is int
        and 0 <= attempts <= SOURCE_REQUEST_POLICY.max_transport_attempts
        and (status is None or (type(status) is int and 100 <= status <= 599))
    )


def _safe_error(error):
    if type(error) is EmploymentAcquisitionError:
        values = (error.classification, error.attempts, error.http_status)
        if _valid_error(*values):
            return values
    return Classification.TRANSPORT, 1, None


def _pending_name(attempt_id):
    return "attempt-" + attempt_id + ".pending.json"


def _release_name(attempt_id):
    return "attempt-" + attempt_id + ".writer-closed.json"


def _fail(code: Classification, attempts: int = 1, status: int | None = None):
    raise EmploymentAcquisitionError(code, attempts, status) from None


@dataclass(frozen=True)
class EmploymentSourceLimits:
    """Fixed Employment scope and effect limits, without execution grants."""

    revision: str = REVISION
    plan_sha256: str = PLAN_SHA256
    request_identity: str = field(
        default_factory=lambda: BlsEmploymentRequest().identity
    )
    source_identity: str = SOURCE_IDENTITY
    parser_revision: str = PARSER_REVISION
    contract_revision: str = CONTRACT_REVISION
    fixture_id: str = FIXTURE_ID
    provider_requests: int = 1
    response_bytes: int = MAX_RESPONSE_BYTES
    network_seconds: int = 20
    attempt_seconds: int = int(ATTEMPT_SECONDS)
    request_bytes: int = 4096
    pg_connections: int = 0
    pg_mutations: int = 0
    garage_requests: int = 0
    source_binding_activations: int = 0

    def validate(self) -> None:
        expected = EmploymentSourceLimits()
        numbers = (
            self.provider_requests,
            self.response_bytes,
            self.network_seconds,
            self.attempt_seconds,
            self.request_bytes,
            self.pg_connections,
            self.pg_mutations,
            self.garage_requests,
            self.source_binding_activations,
        )
        if self != expected or not all(type(value) is int for value in numbers):
            _fail(Classification.LIMITS, 0)


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes = field(repr=False)
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)
    obtained_at: datetime | None = None
    transport_attempts: int = 1


class EmploymentTransport(Protocol):
    def post(
        self,
        endpoint: str,
        body: bytes,
        headers: Mapping[str, str],
        *,
        deadline: float,
        monotonic: Callable[[], float],
    ) -> HttpResponse: ...


def _check_deadline(deadline: float, monotonic: Callable[[], float]) -> None:
    if monotonic() >= deadline:
        _fail(Classification.TIMEOUT)


class EmploymentHttpsTransport:
    """One HTTPS exchange; shared policy above this class owns retries."""

    def post(self, endpoint, body, headers, *, deadline, monotonic):
        if endpoint != ENDPOINT:
            _fail(Classification.LIMITS, 0)
        _check_deadline(deadline, monotonic)
        connection = http.client.HTTPSConnection(
            "api.bls.gov", timeout=max(0.001, deadline - monotonic())
        )
        try:
            connection.request("POST", "/publicAPI/v2/timeseries/data/", body, headers)
            response = connection.getresponse()
            pairs = response.getheaders()
            normalized = {}
            for name, value in pairs:
                name = name.lower()
                if name in normalized:
                    _fail(Classification.HEADERS)
                normalized[name] = value
            _validate_headers(response.status, normalized)
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            _check_deadline(deadline, monotonic)
            return HttpResponse(response.status, raw, normalized)
        finally:
            connection.close()


def _validate_headers(status: int, headers: Mapping[str, str]) -> None:
    if type(status) is not int or not 100 <= status <= 599:
        _fail(Classification.HEADERS)
    if 300 <= status < 400:
        _fail(Classification.REDIRECT, status=status)
    if status != 200:
        _fail(Classification.HTTP, status=status)
    if headers.get("content-encoding", "identity").lower() != "identity":
        _fail(Classification.HEADERS)
    if "transfer-encoding" in headers:
        # Fixed bounded endpoint contract: no ambiguous/chunked framing admission.
        _fail(Classification.HEADERS)
    length = headers.get("content-length")
    if length is not None:
        if re.fullmatch(r"[0-9]{1,9}", length) is None:
            _fail(Classification.HEADERS)
        if int(length) > MAX_RESPONSE_BYTES:
            _fail(Classification.SIZE)


def _screen(raw: bytes, key: str) -> None:
    """Reject originals rather than redact them, before parser/digest/report/IO.

    Decoded views are rejection probes only, never accepted replacement bytes.
    Scan hex independently and every Base64 alignment so surrounding prose,
    prefixes and '=' separators cannot hide an encoded value. Admission charges
    a cumulative work budget, including duplicate views, before queueing them.
    """
    try:
        if type(raw) is not bytes or b"\x00" in raw:
            _fail(Classification.ECHO)
        text = raw.decode("utf-8", errors="strict")
        if "\ufeff" in text or "\ufffe" in text:
            _fail(Classification.ECHO)
        pending = []
        seen = set()
        work = 0

        def enqueue(value, depth, strict):
            nonlocal work
            work += len(value)
            if work > SCREEN_WORK_UNITS or len(value) > IPC_BYTES:
                _fail(Classification.ECHO)
            identity = (value, strict)
            if identity in seen:
                return
            if depth > SCREEN_DEPTH or len(seen) >= SCREEN_VARIANTS:
                _fail(Classification.ECHO)
            seen.add(identity)
            pending.append((value, depth, strict))

        enqueue(text, 0, True)
        while pending:
            text, depth, strict = pending.pop()
            # NUL stripping here also catches encoded UTF16/32 echoes.
            probe = text.replace("\x00", "")
            if key.casefold() in probe.casefold() or _SENSITIVE.search(probe):
                _fail(Classification.ECHO)
            decoded = re.sub(
                r'\\(?:u[0-9a-fA-F]{4}|["\\/bfnrt])',
                lambda match: json.loads('"' + match[0] + '"'),
                text,
            )
            decoded = html.unescape(
                unquote_plus(decoded, errors="strict" if strict else "replace")
            )
            variants = [unicodedata.normalize("NFKC", text)]
            compact = re.sub(r"\s+", "", text)
            if compact != text:
                variants.append(compact)
            if decoded != text:
                variants.append(decoded)
            # Unknown escape syntax cannot establish safe original UTF8 JSON.
            if strict and re.search(r"\\(?:x|U)|\\u(?![0-9a-fA-F]{4})|%[uU]", text):
                _fail(Classification.ECHO)
            # '=' is a delimiter as well as padding. Decode unpadded runs;
            # keeping it in the alphabet used to swallow reply=<encoded>.
            for alphabet, phases in (
                (r"[0-9a-fA-F]{24,}", 2),
                (r"[A-Za-z0-9_+/-]{24,}", 4),
            ):
                for match in re.finditer(alphabet, text):
                    token = match[0]
                    # Charge scans/decoding even when no text view is produced.
                    work += len(token) * phases
                    if work > SCREEN_WORK_UNITS:
                        _fail(Classification.ECHO)
                    for phase in range(phases):
                        part = token[phase:]
                        if phases == 2:
                            content = bytes.fromhex(part[: len(part) // 2 * 2])
                        else:
                            # Discard only an incomplete trailing sextet in a
                            # probe. A suffix may be prose, not Base64 padding.
                            if len(part) % 4 == 1:
                                part = part[:-1]
                            try:
                                content = base64.b64decode(
                                    part.translate(str.maketrans("-_", "+/"))
                                    + "=" * (-len(part) % 4),
                                    validate=True,
                                )
                            except binascii.Error:
                                _fail(Classification.ECHO)
                        # Prefix/suffix bytes need not be valid UTF8. Ignoring
                        # them in this rejection-only view preserves embedded
                        # ASCII encodings, including NUL-separated UTF16/32.
                        try:
                            variant = content.decode("utf-8")
                        except UnicodeError:
                            # Random digest bytes can contain apparent escapes.
                            # A lossy binary probe must not assert text syntax.
                            enqueue(
                                content.decode("utf-8", errors="ignore"),
                                depth + 1,
                                False,
                            )
                        else:
                            enqueue(variant, depth + 1, strict)
                        # Arbitrary digest bytes are not text; text encodings are
                        # inspected without ever admitting them as original JSON.
                        for encoding in ("utf-16", "utf-32"):
                            try:
                                variant = content.decode(encoding)
                            except UnicodeError:
                                continue
                            enqueue(variant, depth + 1, strict)
            for variant in variants:
                enqueue(variant, depth + 1, strict)
    except UnicodeError, ValueError, RecursionError:
        _fail(Classification.ECHO)


def _json(value: object) -> bytes:
    return (
        json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + "\n"
    ).encode()


def _load_key(path: Path) -> str:
    try:
        key = tomllib.loads(path.read_text())["bls"]["api_key"]
        if type(key) is str and re.fullmatch(r"[0-9a-fA-F]{32}", key):
            return key
    except OSError, KeyError, TypeError, ValueError:
        pass
    _fail(Classification.CREDENTIAL, 0)


def _strict_json(raw):
    def pairs(items):
        result = dict(items)
        if len(result) != len(items):
            raise ValueError
        return result

    def invalid(_):
        raise ValueError

    return json.loads(
        raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=invalid
    )


def _keys(value, names):
    if type(value) is not dict or set(value) != set(names.split()):
        raise ValueError


def _timestamp(value):
    if type(value) is not str or not 20 <= len(value) <= 40:
        raise ValueError
    result = datetime.fromisoformat(value)
    instant(result)
    if result.isoformat() != value:
        raise ValueError
    return result


def _raw_text(value):
    if type(value) is not str or not 0 < len(value) <= MAX_RESPONSE_BYTES:
        raise ValueError
    raw = value.encode("utf-8")
    if len(raw) > MAX_RESPONSE_BYTES or "\x00" in value or "\ufeff" in value:
        raise ValueError
    return raw


def _result_wire(value, attempt_id):
    if type(value) is HttpResponse:
        if type(value.body) is not bytes or value.status != 200:
            raise ValueError
        return {
            "kind": "response",
            "body": value.body.decode("utf-8"),
            "obtained_at": value.obtained_at.isoformat() if value.obtained_at else None,
            "transport_attempts": value.transport_attempts,
        }
    if type(value) is EmploymentAcquisition and attempt_id is not None:
        evidence, plan = value.candidate.evidence, value.candidate.plan
        return {
            "kind": "acquisition",
            "body": evidence.raw_bytes.decode("utf-8"),
            "requested_at": evidence.requested_at.isoformat(),
            "obtained_at": evidence.obtained_at.isoformat(),
            "finished_at": plan.run.finished_at.isoformat(),
            "frozen_at": plan.version.frozen_at.isoformat(),
            "provider_data_attempts": evidence.provider_data_attempts,
        }
    raise ValueError


def _result_read(value, attempt_id, expected_kind):
    if type(value) is not dict or value.get("kind") != expected_kind:
        raise ValueError
    if expected_kind == "response":
        _keys(value, "kind body obtained_at transport_attempts")
        if not valid_transport_attempts(value["transport_attempts"]):
            raise ValueError
        return HttpResponse(
            200,
            _raw_text(value["body"]),
            {},
            _timestamp(value["obtained_at"])
            if value["obtained_at"] is not None
            else None,
            value["transport_attempts"],
        )
    _keys(
        value,
        "kind body requested_at obtained_at finished_at frozen_at "
        "provider_data_attempts",
    )
    if attempt_id is None:
        raise ValueError
    if not valid_transport_attempts(value["provider_data_attempts"]):
        raise ValueError
    evidence = EmploymentEvidence(
        _raw_text(value["body"]),
        BlsEmploymentRequest(),
        _timestamp(value["requested_at"]),
        _timestamp(value["obtained_at"]),
        "mdf-bls-employment-" + attempt_id,
        "mdf-artifact://t03-source-qualification-r1/run-" + attempt_id,
        value["provider_data_attempts"],
    )
    plan = build_plan(
        evidence,
        identity="t03-employment-" + attempt_id,
        finished_at=_timestamp(value["finished_at"]),
        frozen_at=_timestamp(value["frozen_at"]),
    )
    return EmploymentAcquisition(
        EmploymentRetainedCandidate(evidence, plan),
        EmploymentObservation(
            classification=Classification.RETAINED.value,
            simulated_attempts=0,
            provider_requests=1,
            attempt_id=attempt_id,
        ),
        ARTIFACT_ROOT / ("run-" + attempt_id),
        attempt_id,
    )


def _encode_envelope(operation, attempt_id, expected_kind):
    try:
        result = _result_wire(operation(), attempt_id)
        # Both sides validate the exact primitive shape, not arbitrary objects.
        _result_read(result, attempt_id, expected_kind)
        value = {"ok": True, "result": result}
    except BaseException as error:
        code, attempts, status = _safe_error(error)
        value = {
            "ok": False,
            "result": {"code": code.value, "attempts": attempts, "http_status": status},
        }
    payload = _json({"v": 1, "attempt_id": attempt_id, **value})
    if len(payload) > IPC_BYTES:
        raise ValueError
    return payload


def _decode_envelope(raw, attempt_id, expected_kind):
    if not 0 < len(raw) <= IPC_BYTES:
        raise ValueError
    value = _strict_json(raw)
    _keys(value, "v attempt_id ok result")
    if (
        type(value["v"]) is not int
        or value["v"] != 1
        or type(value["ok"]) is not bool
        or not _valid_attempt_id(value["attempt_id"])
        or value["attempt_id"] != attempt_id
    ):
        raise ValueError
    if value["ok"]:
        return _result_read(value["result"], attempt_id, expected_kind)
    error = value["result"]
    _keys(error, "code attempts http_status")
    if type(error["code"]) is not str or len(error["code"]) > 64:
        raise ValueError
    code = Classification(error["code"])
    if not _valid_error(code, error["attempts"], error["http_status"]):
        raise ValueError
    _fail(code, error["attempts"], error["http_status"])


def _child(
    channel, parent_pid, operation, attempt_id=None, expected_kind="response"
) -> None:
    # Parent death must also close the nested network worker's socket. This
    # avoids orphan acquisition if the outer 30s supervisor terminates its child.
    try:
        if (
            ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0
            or os.getppid() != parent_pid
        ):
            os._exit(1)
        payload = _encode_envelope(operation, attempt_id, expected_kind)
        frame = memoryview(struct.pack("!I", len(payload)) + payload)
        while frame:
            frame = frame[os.write(channel, frame) :]
    except BaseException:
        # In particular, a closed parent channel must not print a traceback.
        pass
    finally:
        os.close(channel)


def _supervise(
    operation,
    *,
    deadline,
    monotonic,
    timeout_code,
    attempt_counter=None,
    attempt_id=None,
    expected_kind="response",
):
    """Kill isolated IO at the absolute deadline, including slow IPC transfers.

    Linux single-threaded fork is the qualified host contract; no fallback.
    Only bounded UTF8 JSON with exact primitive schemas crosses the pipe.
    """
    if threading.active_count() != 1 or not os.sys.platform.startswith("linux"):
        _fail(Classification.LIMITS, 0)
    if not _valid_attempt_id(attempt_id) or expected_kind not in {
        "response",
        "acquisition",
    }:
        _fail(Classification.LIMITS, 0)
    failure = None
    result = None
    context = multiprocessing.get_context("fork")
    receive, send = os.pipe()
    process = context.Process(
        target=_child,
        args=(send, os.getpid(), operation, attempt_id, expected_kind),
        daemon=False,
    )
    try:
        process.start()
        os.close(send)
        send = None
        os.set_blocking(receive, False)
        buffer = bytearray()
        size = None
        while size is None or len(buffer) < size + 4:
            remaining = deadline - monotonic()
            if remaining <= 0 or not select.select([receive], [], [], remaining)[0]:
                _fail(
                    timeout_code,
                    attempt_counter.value if attempt_counter is not None else 1,
                )
            chunk = os.read(receive, 65536)
            if not chunk:
                _fail(
                    Classification.TRANSPORT,
                    attempt_counter.value if attempt_counter is not None else 1,
                )
            buffer.extend(chunk)
            if size is None and len(buffer) >= 4:
                size = struct.unpack("!I", buffer[:4])[0]
                if not 0 < size <= IPC_BYTES:
                    _fail(Classification.TRANSPORT)
        if monotonic() >= deadline:
            _fail(
                timeout_code,
                attempt_counter.value if attempt_counter is not None else 1,
            )
        if len(buffer) != size + 4:
            raise ValueError
        try:
            result = _decode_envelope(bytes(buffer[4:]), attempt_id, expected_kind)
            if monotonic() >= deadline:
                _fail(
                    timeout_code,
                    attempt_counter.value if attempt_counter is not None else 1,
                )
        except EmploymentAcquisitionError as error:
            if attempt_counter is not None:
                error.attempts = attempt_counter.value
            raise
    except EmploymentAcquisitionError as error:
        failure = error
        failure.attempt_id = attempt_id
        raise
    except BaseException:
        code = Classification.TRANSPORT
        # KeyboardInterrupt/cancel is also unknown, with no raw exception IPC.
        failure = EmploymentAcquisitionError(
            code,
            attempt_counter.value if attempt_counter is not None else 1,
            attempt_id=attempt_id,
        )
        raise failure from None
    finally:
        os.close(receive)
        if send is not None:
            os.close(send)
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            process.join(timeout=max(0.0, min(0.1, deadline - monotonic())))
            # A killed child may remain pending OS scheduling. Never wait
            # without a bound or start a successor; it has SIGKILL pending.
            if not process.is_alive():
                if failure is not None:
                    failure.writer_released = True
                if type(result) is EmploymentAcquisition:
                    result = replace(
                        result,
                        observation=replace(result.observation, writer_released=True),
                    )
                process.close()
    return result


@dataclass(frozen=True)
class EmploymentObservation:
    writer_released: bool = False
    attempt_id: str | None = None
    classification: str = "offline_pre_request_pass"
    capability: str = CAPABILITY
    fixture_id: str = FIXTURE_ID
    revision: str = REVISION
    plan_sha256: str = PLAN_SHA256
    parser_revision: str = PARSER_REVISION
    contract_revision: str = CONTRACT_REVISION
    request_identity: str = field(
        default_factory=lambda: BlsEmploymentRequest().identity
    )
    returned_series: tuple[str, ...] = SERIES_IDS
    reference_periods: tuple[str, ...] = REFERENCES
    source_identity: str = SOURCE_IDENTITY
    source_qualification: str = "NOT_ESTABLISHED"
    simulated_attempts: int = 1
    provider_requests: int = 0
    complete: bool = True
    missing_fields: tuple[str, ...] = ()


@dataclass(frozen=True)
class EmploymentAcquisition:
    candidate: EmploymentRetainedCandidate = field(repr=False)
    observation: EmploymentObservation
    artifact_directory: Path
    attempt_id: str | None = None


def _exchange(
    transport,
    key,
    monotonic,
    attempt_counter=None,
    clock=None,
    *,
    sleep=time.sleep,
):
    body = dict(BlsEmploymentRequest().persisted()["body"])
    body["registrationkey"] = key
    wire = json.dumps(body, sort_keys=True, separators=(",", ":")).encode()
    if len(wire) > 4096:
        _fail(Classification.CREDENTIAL, 0)

    def exchange_once():
        if attempt_counter is not None:
            attempt_counter.value += 1
        deadline = monotonic() + NETWORK_SECONDS
        try:
            response = transport.post(
                ENDPOINT,
                wire,
                dict(_HEADERS),
                deadline=deadline,
                monotonic=monotonic,
            )
            obtained_at = clock() if clock is not None else None
        except EmploymentAcquisitionError as error:
            if type(transport) is EmploymentHttpsTransport:
                code, attempts, status = _safe_error(error)
                raise EmploymentAcquisitionError(code, attempts, status) from None
            raise OSError("untrusted_transport_callback_failed") from None
        _check_deadline(deadline, monotonic)
        if type(response) is not HttpResponse or not isinstance(
            response.headers, Mapping
        ):
            _fail(Classification.HEADERS)
        normalized = {}
        for name, value in response.headers.items():
            if type(name) is not str or type(value) is not str:
                _fail(Classification.HEADERS)
            name = name.lower()
            if name in normalized:
                _fail(Classification.HEADERS)
            normalized[name] = value
        response = HttpResponse(
            response.status,
            response.body,
            normalized,
            obtained_at,
        )
        _validate_headers(response.status, response.headers)
        if type(response.body) is not bytes:
            _fail(Classification.SIZE)
        length = response.headers.get("content-length")
        if length is not None:
            declared_length = int(length)
            if len(response.body) < declared_length:
                raise http.client.IncompleteRead(
                    response.body,
                    declared_length - len(response.body),
                )
            if len(response.body) != declared_length:
                _fail(Classification.SIZE)
        if not 0 < len(response.body) <= MAX_RESPONSE_BYTES:
            _fail(Classification.SIZE)
        _screen(response.body, key)
        return response

    classified_status = [None]

    def classify(error):
        if type(error) is EmploymentAcquisitionError:
            code, _attempts, status = _safe_error(error)
            classified_status[0] = status
            if code is Classification.HTTP:
                return code.value, retryable_http_status(status)
            if code in {Classification.TRANSPORT, Classification.TIMEOUT}:
                return code.value, True
            return code.value, False
        classification, transient = classify_source_exception(error)
        if classification == "transport_failure":
            return Classification.TRANSPORT.value, transient
        if classification == "timeout":
            return Classification.TIMEOUT.value, transient
        return Classification.TRANSPORT.value, False

    try:
        response, attempts = request_source(
            exchange_once,
            status=lambda _response: 200,
            classify=classify,
            sleep=sleep,
        )
    except SourceRequestFailed as error:
        status = error.http_status
        if status is None:
            status = classified_status[0]
        if error.classification == Classification.HTTP.value:
            _fail(Classification.HTTP, error.attempts, status)
        if error.classification == Classification.TIMEOUT.value:
            _fail(Classification.TIMEOUT, error.attempts, status)
        try:
            code = Classification(error.classification)
        except ValueError:
            code = Classification.TRANSPORT
        _fail(code, error.attempts, status)

    if attempt_counter is not None:
        attempt_counter.value = attempts
    return replace(response, transport_attempts=attempts)


def qualify_offline(
    transport: EmploymentTransport,
    *,
    dummy_key: str,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> EmploymentObservation:
    """Source-named simulated pre-request fixture; never reads secrets or writes.

    Caller supplies only fake transport and dummy material. This is not a live
    execution API and does not create an external receipt or qualified Source.
    """
    if getattr(transport, "effect_domain", None) != "offline":
        _fail(Classification.DISABLED, 0)
    if (
        type(dummy_key) is not str
        or re.fullmatch(r"[0-9a-fA-F]{32}", dummy_key) is None
    ):
        _fail(Classification.CREDENTIAL, 0)
    response = _exchange(transport, dummy_key, monotonic, sleep=sleep)
    try:
        parse_response(response.body)
    except ContractError, RecursionError:
        _fail(Classification.CONTRACT)
    return EmploymentObservation()


def _open_artifact_root(descriptors):
    if ARTIFACT_ROOT.parent != REPOSITORY / "artifacts":
        _fail(Classification.ARTIFACT)
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", str(ARTIFACT_ROOT / "probe")],
        cwd=REPOSITORY,
        capture_output=True,
        check=False,
        timeout=2,
    )
    if ignored.returncode != 0:
        _fail(Classification.ARTIFACT)
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    parent_fd = os.open(REPOSITORY, flags)
    descriptors.append(parent_fd)
    for name in ("artifacts", ARTIFACT_ROOT.name):
        try:
            os.mkdir(name, mode=0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        parent_fd = os.open(name, flags, dir_fd=parent_fd)
        descriptors.append(parent_fd)
    return parent_fd


def _write_attempt_record(attempt_id, name, fields):
    """Immutable logical-request intent and release bookkeeping.

    Transport retries stay inside this single logical attempt_id. These records
    prevent history replacement; they are not retry permissions or denials.
    """
    descriptors = []
    try:
        parent_fd = _open_artifact_root(descriptors)
        fd = os.open(
            name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o400,
            dir_fd=parent_fd,
        )
        with os.fdopen(fd, "wb") as output:
            output.write(
                _json(
                    {
                        "attempt_id": attempt_id,
                        "request_identity": BlsEmploymentRequest().identity,
                        "artifact_run": "run-" + attempt_id,
                        "transport_policy": "mdf_shared_source_request_policy",
                        "max_transport_attempts": (
                            SOURCE_REQUEST_POLICY.max_transport_attempts
                        ),
                        **fields,
                    }
                )
            )
            output.flush()
            os.fsync(output.fileno())
        os.fsync(parent_fd)
    except OSError, subprocess.SubprocessError:
        _fail(Classification.ARTIFACT, 0)
    finally:
        for fd in reversed(descriptors):
            os.close(fd)


def _write_pending(attempt_id):
    _write_attempt_record(
        attempt_id,
        _pending_name(attempt_id),
        {
            "state": "LOGICAL_SOURCE_REQUEST_PENDING",
            "writer_release_receipt": _release_name(attempt_id),
        },
    )


def _write_release(attempt_id, code, attempts):
    _write_attempt_record(
        attempt_id,
        _release_name(attempt_id),
        {
            "state": "ARTIFACT_HANDLES_CLOSED_WORKER_REAP_PENDING",
            "classification": code.value,
            "transport_attempts": attempts,
            "source_qualification": "NOT_ESTABLISHED",
        },
    )


def _write_artifacts(
    files: dict[str, bytes],
    key: str,
    *,
    enabled: bool,
    limits: EmploymentSourceLimits,
    run_token: str,
) -> Path:
    if enabled is not True:
        _fail(Classification.DISABLED, 0)
    limits.validate()
    if re.fullmatch(r"[a-f0-9]{32}", run_token) is None or set(files) != {
        "raw-response.bin",
        "request.json",
        "source-envelope.json",
        "observation.json",
    }:
        _fail(Classification.ARTIFACT)
    for content in files.values():
        _screen(content, key)
    descriptors = []
    try:
        parent_fd = _open_artifact_root(descriptors)
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        run_name = "run-" + run_token
        os.mkdir(run_name, mode=0o700, dir_fd=parent_fd)
        directory_fd = os.open(run_name, flags, dir_fd=parent_fd)
        descriptors.append(directory_fd)
        for name in (
            "raw-response.bin",
            "request.json",
            "source-envelope.json",
            "observation.json",
        ):
            descriptor = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o400,
                dir_fd=directory_fd,
            )
            with os.fdopen(descriptor, "wb") as output:
                output.write(files[name])
                output.flush()
                os.fsync(output.fileno())
        os.fchmod(directory_fd, 0o500)
        os.fsync(directory_fd)
        os.fsync(parent_fd)
        return ARTIFACT_ROOT / run_name
    except OSError, subprocess.SubprocessError:
        _fail(Classification.ARTIFACT)
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _attempt_work(
    limits,
    transport,
    secret_path,
    clock,
    monotonic,
    attempt_counter=None,
    attempt_id=None,
    sleep=time.sleep,
):
    token = attempt_id or uuid4().hex
    if not _valid_attempt_id(token):
        _fail(Classification.LIMITS, 0)
    key = _load_key(secret_path)
    started = monotonic()
    requested_at = clock()
    try:
        instant(requested_at)
        response = _supervise(
            lambda: _exchange(
                transport,
                key,
                monotonic,
                attempt_counter,
                clock,
                sleep=sleep,
            ),
            deadline=started + SOURCE_REQUEST_SECONDS,
            monotonic=monotonic,
            timeout_code=Classification.TIMEOUT,
            attempt_counter=attempt_counter,
            attempt_id=token,
        )
        obtained_at = response.obtained_at
        instant(obtained_at)
        if requested_at > obtained_at:
            _fail(Classification.TIME)
        # Raw screening in the network worker precedes all parser digest calls.
        receipt = "mdf-artifact://t03-source-qualification-r1/run-" + token
        evidence = EmploymentEvidence(
            response.body,
            BlsEmploymentRequest(),
            requested_at,
            obtained_at,
            "mdf-bls-employment-" + token,
            receipt,
            response.transport_attempts,
        )
        finished_at, frozen_at = clock(), clock()
        plan = build_plan(
            evidence,
            identity="t03-employment-" + token,
            finished_at=finished_at,
            frozen_at=frozen_at,
        )
        candidate = EmploymentRetainedCandidate(evidence, plan)
        projection = parse_response(response.body)
        observation = EmploymentObservation(
            classification=Classification.RETAINED.value,
            attempt_id=token,
            simulated_attempts=0,
            provider_requests=1,
        )
        files = {
            "raw-response.bin": response.body,
            "request.json": _json(
                {
                    "request": evidence.request.persisted(),
                    "request_identity": evidence.request.identity,
                }
            ),
            "source-envelope.json": plan.content,
            "observation.json": _json(
                {
                    **observation.__dict__,
                    "limits": limits.__dict__,
                    "http_status": 200,
                    "provider_data_attempts": response.transport_attempts,
                    "requested_at": requested_at.isoformat(),
                    "obtained_at": obtained_at.isoformat(),
                    "finished_at": finished_at.isoformat(),
                    "frozen_at": frozen_at.isoformat(),
                    "source_known_at": obtained_at.isoformat(),
                    "publication_known_at": None,
                    "consumer_usable_at": None,
                    "acquisition_identity": evidence.acquisition_identity,
                    "controlled_external_receipt": receipt,
                    "raw_sha256": projection.raw_sha256,
                    "raw_size": projection.raw_size,
                    "attempt_id": token,
                    "pending_receipt": _pending_name(token),
                    "writer_release": "PENDING_PARENT_REAP_AND_INDEPENDENT_READBACK",
                    "transport_policy": "mdf_shared_source_request_policy",
                    "max_transport_attempts": (
                        SOURCE_REQUEST_POLICY.max_transport_attempts
                    ),
                    "receipt_state": (
                        "RETAINED_UNQUALIFIED_PENDING_INDEPENDENT_READBACK"
                    ),
                }
            ),
        }
        # Also screen the typed candidate carrier before reporting or persisting.
        _screen(plan.content, key)
        directory = _write_artifacts(
            files, key, enabled=True, limits=limits, run_token=token
        )
        return EmploymentAcquisition(candidate, observation, directory, token)
    except ContractError, ValueError, TypeError, RecursionError:
        _fail(Classification.CONTRACT)


def _attempt(
    limits,
    transport,
    secret_path,
    clock,
    monotonic,
    attempt_counter=None,
    attempt_id=None,
    record_pending=False,
    sleep=time.sleep,
):
    token = attempt_id or uuid4().hex
    if type(token) is not str or not _valid_attempt_id(token):
        _fail(Classification.LIMITS, 0)
    try:
        if record_pending:
            _write_pending(token)
        result = _attempt_work(
            limits,
            transport,
            secret_path,
            clock,
            monotonic,
            attempt_counter,
            token,
            sleep,
        )
    except BaseException as error:
        code, attempts, status = _safe_error(error)
        if attempt_counter is not None:
            attempts = attempt_counter.value
        if record_pending:
            try:
                _write_release(token, code, attempts)
            except BaseException:
                code, status = Classification.ARTIFACT, None
        raise EmploymentAcquisitionError(code, attempts, status, token) from None
    if record_pending:
        _write_release(
            token,
            Classification.RETAINED,
            attempt_counter.value if attempt_counter is not None else 1,
        )
    return result


def execute_live_candidate(
    *,
    enabled: bool = False,
    limits: EmploymentSourceLimits | None = None,
    transport: EmploymentTransport | None = None,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> EmploymentAcquisition:
    """Explicit one-request use of the existing MDF BLS Source capability.

    No extra grant, verifier or per-attempt approval is needed. A returned
    candidate still needs original-byte qualification before the existing
    retained materialization job can establish a Publication.
    Transient transport failures retry inside this one logical Source request.
    Parsing and artifact failures occur after a completed response and never
    requery BLS; no Provider fallback is implemented.
    """
    if enabled is not True:
        _fail(Classification.DISABLED, 0)
    if limits is None:
        limits = EmploymentSourceLimits()
    if type(limits) is not EmploymentSourceLimits:
        _fail(Classification.LIMITS, 0)
    limits.validate()
    started = monotonic()
    attempt_id = uuid4().hex
    attempt_counter = multiprocessing.get_context("fork").Value("b", 0, lock=False)
    return _supervise(
        lambda: _attempt(
            limits,
            transport if transport is not None else EmploymentHttpsTransport(),
            SECRET_PATH,
            clock,
            monotonic,
            attempt_counter,
            attempt_id,
            True,
            sleep,
        ),
        deadline=started + ATTEMPT_SECONDS,
        monotonic=monotonic,
        timeout_code=Classification.ATTEMPT_TIMEOUT,
        attempt_counter=attempt_counter,
        attempt_id=attempt_id,
        expected_kind="acquisition",
    )
