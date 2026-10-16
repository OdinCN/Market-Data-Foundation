"""Default-off, source-only BEA edge. No storage, publication or qualification.

A supervised process holds the credential, HTTP request and unsanitized bodies.
The parent permits two sequential synthetic exchanges with cancellation timers
and receives only fixed classifications and a clean projection. Full lifecycle
deadlines remain unproved; current real transport admission is closed.
Private test transports are synthetic; receipts never qualify a source.
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

ADAPTER_REVISION = "t04-bea-pce-live-edge-execution-boundary-r1"
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


def _echo_scan(value: object, key: str) -> None:
    """Scan before hashes/IPC: structured JSON, URL encoding, base64 and hex.

    Only the exact BEA RequestParam UserID slot may hold authentication, and that
    envelope is removed in memory before this scan. Any other envelope is fatal.
    """
    forbidden = {
        key.casefold(),
        quote(key, safe="").casefold(),
        quote_plus(key, safe="").casefold(),
        key.encode().hex().casefold(),
        base64.b64encode(key.encode()).decode().casefold(),
        base64.urlsafe_b64encode(key.encode()).decode().casefold(),
    }
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


def _validate_table(table: str, results: dict) -> None:
    """Single-response admission before request two; full replay follows both.

    This deliberately checks the same frozen mapping as the retained parser;
    no fabricated counterpart or single-response bundle is ever constructed.
    """
    _check(
        type(results) is dict
        and set(results) == {"Data", "Notes", "UTCProductionTime"},
        "source_shape_rejected",
    )
    _check(
        _metadata_date(results["UTCProductionTime"], production=True),
        "source_shape_rejected",
    )
    notes = results["Notes"]
    _check(type(notes) is list and len(notes) == 2, "source_shape_rejected")
    mom = table == "T20807"
    # This is the frozen candidate shape, not a claim of actual BEA metadata.
    # Unreviewed prose/fields/rows cannot carry opaque bytes into the projection.
    table_note = (
        "Table 2.8.7. Monthly price changes from preceding month, seasonally adjusted."
        if mom
        else "Table 2.8.4. Monthly price indexes, 2017=100, seasonally adjusted."
    )
    for note in notes:
        _check(
            type(note) is dict
            and set(note) == {"NoteRef", "NoteText"}
            and all(type(v) is str for v in note.values())
            and (
                note["NoteRef"] == "1"
                and note["NoteText"] in {table_note, "FABRICATED " + table_note}
                or note["NoteRef"] == "LastRevised"
                and _metadata_date(note["NoteText"], production=False)
            ),
            "source_shape_rejected",
        )
    text = " ".join(n["NoteText"] for n in notes).casefold()
    revision = [n["NoteText"] for n in notes if n["NoteRef"] == "LastRevised"]
    _check(
        len(revision) == 1
        and bool(revision[0])
        and "seasonally adjusted" in text
        and "not seasonally adjusted" not in text
        and "monthly" in text
        and "price" in text
        and ("2.8.7" if mom else "2.8.4") in text
        and (
            ("preceding month" in text and "annual rate" not in text)
            if mom
            else "2017=100" in text.replace(" ", "")
        ),
        "source_shape_rejected",
    )
    rows = results["Data"]
    _check(type(rows) is list and 1 <= len(rows) <= 48, "source_shape_rejected")
    seen = set()
    for row in rows:
        _check(
            type(row) is dict
            and set(row)
            == {
                "TableName",
                "LineNumber",
                "SeriesCode",
                "LineDescription",
                "TimePeriod",
                "METRIC_NAME",
                "CL_UNIT",
                "UNIT_MULT",
                "DataValue",
            }
            and all(type(v) is str for v in row.values())
            and row["TableName"] == table,
            "source_shape_rejected",
        )
        line, period = row["LineNumber"], row["TimePeriod"]
        _check(
            type(line) is str
            and line in {"1", "25"}
            and type(period) is str
            and re.fullmatch(r"202[56]M(?:0[1-9]|1[0-2])", period) is not None
            and (line, period) not in seen,
            "source_shape_rejected",
        )
        seen.add((line, period))
        series = (
            ("DPCERGM" if line == "1" else "DPCCRGM")
            if mom
            else ("DPCERG" if line == "1" else "DPCCRG")
        )
        descriptions = (
            {
                "personal consumption expenditures",
                "personal consumption expenditures (pce)",
            }
            if line == "1"
            else {
                "pce excluding food and energy",
                "personal consumption expenditures excluding food and energy",
            }
        )
        _check(
            row["SeriesCode"] == series
            and row["LineDescription"].strip().casefold() in descriptions
            and row["METRIC_NAME"] == ("Percent Change" if mom else "Index")
            and row["CL_UNIT"] == ("Percent change" if mom else "Index Numbers")
            and row["UNIT_MULT"] == "0",
            "source_shape_rejected",
        )
        _check(
            re.fullmatch(r"-?[0-9]{1,9}(?:\.[0-9]{1,9})?", row["DataValue"])
            is not None,
            "source_shape_rejected",
        )
        pce._value(row, positive=not mom)
    required = {
        (line, period)
        for line in ("1", "25")
        for period in (("2026M07",) if mom else ("2026M07", "2025M07"))
    }
    _check(required <= seen, "source_shape_rejected")


def _metadata_date(value: object, *, production: bool) -> bool:
    if type(value) is not str:
        return False
    if value == ("FABRICATED UTC metadata" if production else "FABRICATED revision A"):
        return True
    pattern = (
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z"
        if production
        else r"[0-9]{4}-[0-9]{2}-[0-9]{2}"
    )
    if re.fullmatch(pattern, value) is None:
        return False
    try:
        datetime.fromisoformat(value)
        return True
    except ValueError:
        return False


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
        _check(seen == expected, "source_shape_rejected")
        _echo_scan(envelope["Results"], key)
        pce.reject_sensitive(envelope["Results"], key)
        _validate_table(table, envelope["Results"])
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
        _echo_scan(list(asdict(identity).values()), key)
        captures = []
        for table in pce.TABLES:
            pipe.send_bytes(b"ready")
            grant = pce.decode(pipe.recv_bytes(1024), max_bytes=1024)
            raw = transport.exchange(table, key, grant["deadline"])
            _clean_capture(table, raw, key)
            pipe.send_bytes(b"complete")
            completion = pce.decode(pipe.recv_bytes(1024), max_bytes=1024)
            captures.append(
                pce.ResponseCapture(
                    table,
                    raw,
                    datetime.fromisoformat(grant["requested"]),
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
        _echo_scan(pce.decode(projection)["responses"][0]["results"], key)
        _echo_scan(pce.decode(projection)["responses"][1]["results"], key)
        pce.reject_sensitive(pce.decode(projection), key)
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
    # Laboratory cancellation mechanism only. start/send/join, clocks and local
    # persistence have no qualified enclosing host deadline (B2 is blocked).
    if transport is None or isinstance(transport, _HttpsTransport):
        return AcquisitionResult("source_execution_boundary_blocked")
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
    """Source-only candidate; current authorization does not permit enabling.

    Real execution is unconditionally blocked: no qualified owner grant/review
    or full-lifecycle host deadline exists. Caller-supplied transports and loaders
    cannot establish offline trust. Use the fixed OfflineFixture boundary for
    offline execution; this API cannot issue real execution authority.
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
    return AcquisitionResult("source_execution_boundary_blocked")


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
    for table, response in zip(pce.TABLES, responses, strict=True):
        _validate_table(table, response["results"])
    parsed = pce.parse_bundle(projection)
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
