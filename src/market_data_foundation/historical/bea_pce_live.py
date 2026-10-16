"""Default-off, source-only BEA edge. No storage, publication or qualification.

A supervised process holds the credential, HTTP request and unsanitized bodies.
Explicit opt-in and an exact budget of two permit the fixed sequential requests.
The parent applies cancellation timers and receives only fixed classifications
and a clean projection. Test transports remain synthetic; receipts never qualify
a source. Timers are cancellation thresholds, not host lifecycle guarantees.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import html
import http.client
import multiprocessing
import re
import ssl
import time
import tomllib
import zlib
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import quote, quote_plus, unquote

from market_data_foundation.historical import bea_pce as pce

ADAPTER_REVISION = "t04-bea-pce-live-edge-real-shape-r2"
PLAN_SHA256 = "c5b7e35d0d6434b2d2d0d80223ea8ea083ac33dc77443bcce744bfab23965726"
CAPABILITY = "bea.nipa.pce.headline_core.monthly.source_projection"
FIXTURE = "bea-nipa-pce-2026-07-t20807-t20804-lines-1-25-v1"
REQUEST_SECONDS = 10.0
NETWORK_SECONDS = 20.0
ATTEMPT_SECONDS = 30.0
CHUNK = 16_384
OUTPUT_LIMIT = pce.MAX_RESPONSE_BYTES + 32_768
_FAILURES = frozenset(
    {
        "transport_unknown",
        "deadline_unknown",
        "http_status_rejected",
        "header_rejected",
        "partial_response_rejected",
        "size_rejected",
        "decode_rejected",
        "credential_echo_rejected",
        "source_shape_rejected",
        "credential_unavailable",
        "context_rejected",
        "worker_unknown",
    }
)
_FAILURE_FRAMES = {name: name.encode("ascii") for name in _FAILURES}


class EdgeRejected(Exception):
    """Only an allowlisted classification may cross the process boundary."""

    def __init__(self, classification: str):
        super().__init__(
            classification
            if type(classification) is str and classification in _FAILURES
            else "worker_unknown"
        )


def _failure_classification(exc: BaseException) -> str:
    # Never invoke exception overrides, stringify args, or forward their bytes.
    # Even a subclass of our typed exception is an untrusted exception type.
    if type(exc) is EdgeRejected:
        args = BaseException.args.__get__(exc)
        if type(args) is tuple and len(args) == 1 and type(args[0]) is str:
            for name in _FAILURES:
                if args[0] == name:
                    return name  # Return the local constant, never the argument.
    return "worker_unknown"


def _check(ok: bool, classification: str) -> None:
    if not ok:
        raise EdgeRejected(classification)


@dataclass(frozen=True)
class ExecutionIdentity:
    producer_execution_identity: str
    exact_code_git_sha: str
    code_manifest_sha256: str
    attempt_identity: str
    controlled_observation_id: str
    exact_plan_sha256: str = PLAN_SHA256

    def validate(self) -> None:
        for value in (
            self.producer_execution_identity,
            self.attempt_identity,
            self.controlled_observation_id,
        ):
            _check(
                type(value) is str
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value) is not None,
                "context_rejected",
            )
        for value, size in (
            (self.exact_code_git_sha, 40),
            (self.code_manifest_sha256, 64),
        ):
            _check(
                type(value) is str
                and re.fullmatch(r"[0-9a-f]{" + str(size) + "}", value) is not None,
                "context_rejected",
            )
        _check(self.exact_plan_sha256 == PLAN_SHA256, "context_rejected")


@dataclass(frozen=True)
class Observation:
    table: str
    requested_at_utc: str
    obtained_at_utc: str | None
    classification: str
    attempted: bool = True
    sent: str = "unknown"  # Never infer server receipt from local send intent.
    response_complete: bool = False


@dataclass(frozen=True)
class AcquisitionResult:
    classification: str
    observations: tuple[Observation, ...] = ()
    projection: bytes | None = field(default=None, repr=False)
    receipt: bytes | None = field(default=None, repr=False)
    source_qualified: bool = field(default=False, init=False)
    exact_original_retained: bool = field(default=False, init=False)


def _load_key() -> str:
    # Only called inside an explicitly enabled process, never during imports/QA.
    try:
        with Path("/srv/market-data-foundation/config/local/secret.toml").open(
            "rb"
        ) as f:
            value = tomllib.load(f)["bea"]["api_key"]
        _check(type(value) is str and len(value) >= 16, "credential_unavailable")
        return value
    except Exception:
        raise EdgeRejected("credential_unavailable") from None


def _credential_fragments(key: str) -> set[str]:
    standard = base64.b64encode(key.encode()).decode().casefold()
    urlsafe = base64.urlsafe_b64encode(key.encode()).decode().casefold()
    return {
        key.casefold(),
        quote(key, safe="").casefold(),
        quote_plus(key, safe="").casefold(),
        key.encode().hex().casefold(),
        standard,
        standard.rstrip("="),
        urlsafe,
        urlsafe.rstrip("="),
    }


def _identity_credential_scan(value: str, key: str) -> None:
    """Bounded credential decoding, without provider-payload metadata rules."""
    codecs = ("utf-8", "utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be")
    key_bytes = tuple(key.encode(codec) for codec in codecs)
    folded_key = key.casefold()
    pending = [(value, 0)]
    visited = set()
    work = 0

    def charge(size: int) -> None:
        nonlocal work
        work += size
        _check(work <= 262_144, "credential_echo_rejected")

    while pending:
        candidate, depth = pending.pop()
        if candidate in visited:
            continue
        _check(depth <= 8 and len(visited) < 128, "credential_echo_rejected")
        charge(len(candidate))
        visited.add(candidate)
        _check(folded_key not in candidate.casefold(), "credential_echo_rejected")
        for decoded in (unquote(candidate), html.unescape(candidate)):
            if decoded != candidate:
                pending.append((decoded, depth + 1))
        blobs = []
        for token in re.findall(r"[A-Za-z0-9+/_-]{16,}={0,2}", candidate):
            # Inspect each alignment so an embedded encoding still decodes when
            # adjacent identity characters also belong to the base64 alphabet.
            for offset in range(4):
                encoded = token[offset:].rstrip("=")
                # A trailing partial quartet can belong to an identity suffix.
                if len(encoded) % 4 == 1:
                    encoded = encoded[:-1]
                charge(len(encoded))
                try:
                    blobs.append(
                        base64.b64decode(
                            encoded + "=" * (-len(encoded) % 4),
                            altchars=b"-_",
                            validate=True,
                        )
                    )
                except ValueError, UnicodeError, binascii.Error:
                    pass
        for token in re.findall(r"[0-9a-fA-F]{16,}", candidate):
            for offset in range(2):
                encoded = token[offset:]
                encoded = encoded[: len(encoded) // 2 * 2]
                charge(len(encoded))
                blobs.append(bytes.fromhex(encoded))
        for blob in blobs:
            charge(len(blob))
            _check(
                not any(encoded_key in blob for encoded_key in key_bytes),
                "credential_echo_rejected",
            )
            # Arbitrary binary data (including NUL and invalid UTF-8) is allowed.
            # Successful text decodings permit nested encodings and casefolding.
            for codec in codecs:
                charge(len(blob))
                try:
                    pending.append((blob.decode(codec), depth + 1))
                except UnicodeError:
                    pass


def _echo_scan(value: object, key: str) -> None:
    """Scan before hashes/IPC: structured JSON, URL encoding, base64 and hex.

    Only the exact BEA RequestParam UserID slot may hold authentication, and that
    envelope is removed in memory before this scan. Any other envelope is fatal.
    """
    forbidden = _credential_fragments(key)
    if isinstance(value, dict):
        for name, item in value.items():
            _check(
                isinstance(name, str)
                and name.casefold()
                not in {"request", "requestparam", "parametername", "parametervalue"},
                "credential_echo_rejected",
            )
            _echo_scan(name, key)
            _echo_scan(item, key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _echo_scan(item, key)
    elif isinstance(value, str):
        pending = [(value, 0)]
        visited = set()
        scanned = 0
        # Finite decoding schema; exhaustion rejects rather than accepting an
        # incompletely inspected string. The parent also bounds processing time.
        while pending:
            candidate, depth = pending.pop()
            if candidate in visited:
                continue
            scanned += len(candidate)
            _check(
                depth <= 8 and len(visited) < 128 and scanned <= 4_000_000,
                "credential_echo_rejected",
            )
            visited.add(candidate)
            folded = candidate.casefold()
            _check(
                "\x00" not in candidate
                and not any(fragment in folded for fragment in forbidden)
                and not re.search(
                    r"userid|api.?key|credential|secret|token|https?://|[?&]\w+="
                    r"|\\[ux][0-9a-f]",
                    folded,
                ),
                "credential_echo_rejected",
            )
            decoded = unquote(candidate)
            if decoded != candidate:
                pending.append((decoded, depth + 1))
            decoded = html.unescape(candidate)
            if decoded != candidate:
                pending.append((decoded, depth + 1))
            blobs = []
            # Decode whole embedded blobs, including prefixed query envelopes;
            # searching only base64(key) misses alignment-dependent encodings.
            for blob in re.findall(r"[A-Za-z0-9+/_-]{16,}={0,2}", candidate):
                try:
                    blobs.append(
                        base64.b64decode(
                            blob + "=" * (-len(blob) % 4), altchars=b"-_", validate=True
                        )
                    )
                except ValueError, UnicodeError, binascii.Error:
                    pass
            for blob in re.findall(r"(?:[0-9a-fA-F]{2}){8,}", candidate):
                try:
                    blobs.append(bytes.fromhex(blob))
                except ValueError, UnicodeError:
                    pass
            for blob in blobs:
                # NUL-interleaved encodings must not be treated as harmless
                # UTF-8 strings. Reject them even when no credential is found.
                _check(b"\x00" not in blob, "credential_echo_rejected")
                for codec in (
                    "utf-8",
                    "utf-16",
                    "utf-16-le",
                    "utf-16-be",
                    "utf-32",
                    "utf-32-le",
                    "utf-32-be",
                ):
                    try:
                        pending.append((blob.decode(codec), depth + 1))
                    except UnicodeError:
                        pass


def _retained_credential_scan(value: object, key: str) -> None:
    """Reject encoded credentials only in bytes/metadata that cross the boundary."""
    if isinstance(value, dict):
        for name, item in value.items():
            _retained_credential_scan(name, key)
            _retained_credential_scan(item, key)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _retained_credential_scan(item, key)
    elif isinstance(value, str):
        _identity_credential_scan(value, key)


def _clean_capture(table: str, raw: bytes, key: str) -> bytes:
    """Validate the entire envelope before allowing the next exchange."""
    try:
        # The admitted JSON transport is UTF-8, never JSON's implicit UTF-16/32
        # autodetection. Escaped NULs are rejected by the structured scan below.
        _check(type(raw) is bytes and b"\x00" not in raw, "decode_rejected")
        raw.decode("utf-8")
        document = pce.decode(raw)
        _check(set(document) == {"BEAAPI"}, "source_shape_rejected")
        envelope = document["BEAAPI"]
        _check(set(envelope) == {"Request", "Results"}, "source_shape_rejected")
        request = envelope["Request"]
        _check(set(request) == {"RequestParam"}, "source_shape_rejected")
        parameters = request["RequestParam"]
        _check(type(parameters) is list, "source_shape_rejected")
        expected = {k.casefold(): v for k, v in pce.request_parameters(table).items()}
        expected["userid"] = key
        seen = {}
        for row in parameters:
            _check(
                type(row) is dict
                and set(row) == {"ParameterName", "ParameterValue"}
                and type(row["ParameterName"]) is str
                and type(row["ParameterValue"]) is str,
                "source_shape_rejected",
            )
            name = row["ParameterName"].casefold()
            _check(name not in seen, "source_shape_rejected")
            seen[name] = row["ParameterValue"]
        _check(set(seen) == set(expected), "source_shape_rejected")
        for name, expected_value in expected.items():
            actual = seen[name]
            if name == "userid":
                _check(actual == expected_value, "source_shape_rejected")
            else:
                # BEA Request is an echo, not an execution authority boundary.
                # The API may canonicalize enum-like parameter value casing.
                _check(
                    actual.casefold() == expected_value.casefold(),
                    "source_shape_rejected",
                )
        _echo_scan(envelope["Results"], key)
        pce.reject_sensitive(envelope["Results"], key)
        projected = pce.project_provider_results(table, envelope["Results"])
        _retained_credential_scan(projected, key)
        # No digest or persistence of provider response or authenticated Request.
        return raw
    except EdgeRejected:
        raise
    except Exception:
        raise EdgeRejected("source_shape_rejected") from None


def _read_stream(
    response, *, monotonic=time.monotonic, deadline: float, key: str | None = None
) -> bytes:
    """Bound wire and decoded data independently, including gzip expansion."""
    _check(response.status == 200, "http_status_rejected")
    headers = response.getheaders()
    _check(len(headers) <= 64, "header_rejected")
    if key is not None:
        _echo_scan(headers, key)
    mapped = {}
    for name, value in headers:
        _check(
            type(name) is str
            and type(value) is str
            and len(name) + len(value) <= 8192
            and re.fullmatch(r"[A-Za-z0-9-]+", name) is not None
            and not any(ord(c) < 32 or ord(c) == 127 for c in value),
            "header_rejected",
        )
        name = name.casefold()
        if name in {
            "content-type",
            "content-length",
            "content-encoding",
            "location",
            "transfer-encoding",
        }:
            _check(name not in mapped, "header_rejected")
            mapped[name] = value
    _check(
        "location" not in mapped
        and "transfer-encoding" not in mapped
        and mapped.get("content-type", "").lower()
        in {
            "application/json",
            "application/json; charset=utf-8",
        },
        "header_rejected",
    )
    length = mapped.get("content-length", "")
    _check(bool(re.fullmatch(r"[0-9]{1,10}", length)), "header_rejected")
    length = int(length)
    _check(0 < length <= pce.MAX_RESPONSE_BYTES, "size_rejected")
    encoding = mapped.get("content-encoding", "identity").lower()
    _check(encoding in {"identity", "gzip"}, "header_rejected")
    decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if encoding == "gzip" else None
    output = bytearray()
    wire = 0
    while True:
        _check(monotonic() < deadline, "deadline_unknown")
        # read1 avoids an unbounded read; the supervising parent also kills DNS,
        # TLS, header parsing or a blocked socket/slow drip at the absolute limit.
        chunk = response.read1(min(CHUNK, pce.MAX_RESPONSE_BYTES + 1 - wire))
        _check(monotonic() < deadline, "deadline_unknown")
        if not chunk:
            break
        wire += len(chunk)
        _check(wire <= pce.MAX_RESPONSE_BYTES, "size_rejected")
        try:
            decoded = (
                decoder.decompress(chunk, pce.MAX_RESPONSE_BYTES + 1 - len(output))
                if decoder
                else chunk
            )
        except zlib.error:
            raise EdgeRejected("decode_rejected") from None
        output.extend(decoded)
        _check(len(output) <= pce.MAX_RESPONSE_BYTES, "size_rejected")
        if decoder:
            _check(not decoder.unconsumed_tail, "size_rejected")
    _check(wire == length, "partial_response_rejected")
    if decoder:
        _check(decoder.eof and not decoder.unused_data, "decode_rejected")
    _check(bool(output), "decode_rejected")
    return bytes(output)


class _HttpsTransport:
    def exchange(self, table: str, key: str, deadline: float) -> bytes:
        connection = None
        try:
            # These limits are process-local, never shared with other products.
            http.client._MAXLINE = 8192
            http.client._MAXHEADERS = 64
            parameters = {**pce.request_parameters(table), "UserID": key}
            query = "&".join(
                quote(k) + "=" + quote(v, safe="") for k, v in parameters.items()
            )
            connection = http.client.HTTPSConnection(
                "apps.bea.gov",
                timeout=max(0.001, deadline - time.monotonic()),
                context=ssl.create_default_context(),
            )
            connection.request(
                "GET",
                "/api/data/?" + query,
                headers={"Accept": "application/json", "Accept-Encoding": "identity"},
            )
            return _read_stream(connection.getresponse(), deadline=deadline, key=key)
        except EdgeRejected:
            raise
        except Exception:
            raise EdgeRejected("transport_unknown") from None
        finally:
            if connection is not None:
                connection.close()


def _worker(pipe, buffer, size, identity, transport, loader):
    """No raw exceptions, authenticated URLs or response bytes enter the pipe."""
    try:
        key = loader()
        _check(type(key) is str and len(key) >= 16, "credential_unavailable")
        identity.validate()
        for value in asdict(identity).values():
            _identity_credential_scan(value, key)
        captures = []
        for table in pce.TABLES:
            pipe.send_bytes(b"ready")
            request = pce.decode(pipe.recv_bytes(1024), max_bytes=1024)
            raw = transport.exchange(table, key, request["deadline"])
            _clean_capture(table, raw, key)
            pipe.send_bytes(b"complete")
            completion = pce.decode(pipe.recv_bytes(1024), max_bytes=1024)
            captures.append(
                pce.ResponseCapture(
                    table,
                    raw,
                    datetime.fromisoformat(request["requested"]),
                    datetime.fromisoformat(completion["obtained"]),
                )
            )
        projection = pce.retain_sanitized_bundle(
            tuple(captures),
            credential=key,
            provenance="retained_unqualified"
            if type(transport) is _HttpsTransport
            else "synthetic",
        )
        projected_document = pce.decode(projection)
        for response in projected_document["responses"]:
            _echo_scan(response["results"], key)
            _retained_credential_scan(response["results"], key)
        pce.reject_sensitive(projected_document, key)
        _check(len(projection) <= pce.MAX_RESPONSE_BYTES, "size_rejected")
        # Only sanitized bytes are hashed, and only after complete residual scans.
        receipt = {
            **asdict(identity),
            "capability": CAPABILITY,
            "fixture": FIXTURE,
            "source_adapter_revision": ADAPTER_REVISION,
            "endpoint": pce.ENDPOINT,
            "requests": [
                {
                    "parameters": pce.request_parameters(c.table),
                    "requested_at_utc": c.requested_at.isoformat(),
                    "obtained_at_utc": c.obtained_at.isoformat(),
                    "classification": "response_complete",
                }
                for c in captures
            ],
            "sanitizer_revision": pce.SANITIZER_REVISION,
            "source_projection_schema": pce.BUNDLE_SCHEMA,
            "source_projection_size": len(projection),
            "source_projection_sha256": hashlib.sha256(projection).hexdigest(),
            "exact_original_retained": False,
            "source_qualified": False,
            "trust": "independent_source_readback_required",
            "execution_class": "owner_local_candidate"
            if type(transport) is _HttpsTransport
            else "synthetic",
        }
        # Endpoint is fixed nonsecret. All caller-controlled receipt fields were
        # scanned above; the authenticated Request envelope is never serialized.
        receipt_bytes = pce.encode(receipt)
        _check(len(receipt_bytes) <= 32_768, "size_rejected")
        buffer[: len(projection)] = projection
        buffer[len(projection) : len(projection) + len(receipt_bytes)] = receipt_bytes
        size[0], size[1] = len(projection), len(receipt_bytes)
        pipe.send_bytes(b"candidate")
    except EdgeRejected as exc:
        pipe.send_bytes(_FAILURE_FRAMES[_failure_classification(exc)])
    except BaseException:
        pipe.send_bytes(b"source_shape_rejected")
    finally:
        pipe.close()


def _utc() -> str:
    return datetime.now(UTC).isoformat()


def _supervise(
    identity,
    transport,
    loader,
    *,
    request_seconds=REQUEST_SECONDS,
    network_seconds=NETWORK_SECONDS,
    attempt_seconds=ATTEMPT_SECONDS,
    before_send=None,
    after_response=None,
):
    # Best-effort cancellation includes worker loading and sanitization. Host
    # start/send/join and scheduling are not subject to a hard lifecycle bound.
    start = time.monotonic()
    observations = []
    context = multiprocessing.get_context("spawn")
    # Only bounded byte copies happen in the parent; all JSON parsing and
    # sanitation remain inside the cancellable worker, including final receipt.
    buffer = context.RawArray("B", OUTPUT_LIMIT)
    size = context.RawArray("i", 2)
    parent, child = context.Pipe()
    process = context.Process(
        target=_worker, args=(child, buffer, size, identity, transport, loader)
    )
    network_used = 0.0
    active = None

    def receive(deadline):
        remaining = deadline - time.monotonic()
        _check(remaining > 0 and parent.poll(remaining), "deadline_unknown")
        message = parent.recv_bytes(128).decode("ascii")
        _check(time.monotonic() < deadline, "deadline_unknown")
        if message in _FAILURES:
            raise EdgeRejected(message)
        return message

    try:
        end = start + attempt_seconds
        process.start()
        child.close()
        for table in pce.TABLES:
            _check(receive(end) == "ready", "worker_unknown")
            began = time.monotonic()
            deadline = min(
                end, began + request_seconds, began + network_seconds - network_used
            )
            _check(deadline > began, "deadline_unknown")
            requested = _utc()
            _check(
                not observations or requested >= observations[-1].obtained_at_utc,
                "source_shape_rejected",
            )
            active = (table, requested)
            # Pre-send intent is recorded before the worker is allowed to send.
            if before_send is not None:
                before_send(table, requested)
            _check(time.monotonic() < deadline, "deadline_unknown")
            parent.send_bytes(
                pce.encode({"deadline": deadline, "requested": requested})
            )
            _check(receive(deadline) == "complete", "worker_unknown")
            obtained = _utc()
            _check(
                datetime.fromisoformat(obtained) >= datetime.fromisoformat(requested),
                "source_shape_rejected",
            )
            network_used += time.monotonic() - began
            observations.append(
                Observation(
                    table,
                    requested,
                    obtained,
                    "response_complete",
                    sent="acknowledged",
                    response_complete=True,
                )
            )
            active = None
            if after_response is not None:
                after_response(observations[-1])
            parent.send_bytes(pce.encode({"obtained": obtained}))
        _check(receive(end) == "candidate", "worker_unknown")
        _check(
            0 < size[0] <= pce.MAX_RESPONSE_BYTES and 0 < size[1] <= 32_768,
            "worker_unknown",
        )
        projection = bytes(buffer[: size[0]])
        receipt = bytes(buffer[size[0] : size[0] + size[1]])
        _check(time.monotonic() < end, "deadline_unknown")
        return AcquisitionResult(
            "candidate_pending_independent_readback",
            tuple(observations),
            projection,
            receipt,
        )
    except Exception as exc:
        classification = _failure_classification(exc)
        if active:
            observations.append(Observation(*active, None, classification))
        return AcquisitionResult(classification, tuple(observations))
    finally:
        if process.pid is not None and process.is_alive():
            process.kill()
        if process.pid is not None:
            process.join()
        parent.close()
        child.close()
        buffer[:] = bytes(len(buffer))
        process.close()


def acquire(
    identity: ExecutionIdentity,
    *,
    enabled: bool = False,
    provider_budget: int = 0,
    transport=None,
    credential_loader=None,
) -> AcquisitionResult:
    """Explicitly acquire the fixed PCE pair using the standing BEA capability.

    The default pair is real HTTPS and the local BEA key loader. Paired transport
    and loader overrides are test seams; their output never qualifies a Source.
    No external grant, reviewer verifier or host hard-deadline proof is required.
    """
    if enabled is not True or type(provider_budget) is not int or provider_budget != 2:
        return AcquisitionResult("disabled_budget_zero")
    if type(identity) is not ExecutionIdentity:
        return AcquisitionResult("context_rejected")
    try:
        identity.validate()
    except Exception:
        return AcquisitionResult("context_rejected")
    if (transport is None) != (credential_loader is None):
        return AcquisitionResult("context_rejected")
    return _supervise(
        identity,
        _HttpsTransport() if transport is None else transport,
        _load_key if credential_loader is None else credential_loader,
    )


def inspect_candidate(
    projection: bytes, receipt: bytes, *, expected: ExecutionIdentity
) -> dict:
    """Independent offline replay precheck, never a source qualification verdict.

    An independent reviewer must additionally verify the controlled owner-local
    observation and actual source identity. Arbitrary receipt strings are invalid.
    """
    expected.validate()
    document = pce.decode(receipt, max_bytes=32_768)
    pce.reject_sensitive(document)
    _check(
        set(document)
        == set(asdict(expected))
        | {
            "capability",
            "fixture",
            "source_adapter_revision",
            "endpoint",
            "requests",
            "sanitizer_revision",
            "source_projection_schema",
            "source_projection_size",
            "source_projection_sha256",
            "exact_original_retained",
            "source_qualified",
            "trust",
            "execution_class",
        },
        "source_shape_rejected",
    )
    responses = pce.decode(projection)["responses"]
    _check(type(responses) is list and len(responses) == 2, "source_shape_rejected")
    try:
        parsed = pce.parse_bundle(projection)
    except pce.ContractError:
        raise EdgeRejected("source_shape_rejected") from None
    for name, value in asdict(expected).items():
        _check(document.get(name) == value, "context_rejected")
    _check(
        document.get("source_projection_size") == len(projection)
        and document.get("source_projection_sha256")
        == hashlib.sha256(projection).hexdigest()
        and document.get("capability") == CAPABILITY
        and document.get("fixture") == FIXTURE
        and document.get("endpoint") == pce.ENDPOINT
        and document.get("source_adapter_revision") == ADAPTER_REVISION
        and document.get("source_projection_schema") == pce.BUNDLE_SCHEMA
        and document.get("sanitizer_revision") == pce.SANITIZER_REVISION
        and document.get("source_qualified") is False
        and document.get("exact_original_retained") is False
        and document.get("trust") == "independent_source_readback_required"
        and document.get("execution_class") in {"synthetic", "owner_local_candidate"},
        "source_shape_rejected",
    )
    _check(
        document.get("requests")
        == [
            {
                "parameters": r["request"],
                "requested_at_utc": r["requested_at"],
                "obtained_at_utc": r["obtained_at"],
                "classification": "response_complete",
            }
            for r in responses
        ],
        "source_shape_rejected",
    )
    return {
        "replay": parsed,
        "source_qualified": False,
        "independent_source_readback_required": True,
    }
