"""Default-off exact-two acquisition; one durable budget directory per transaction.

No IO at import. The owner must keep the same directory through downstream
failures. A consumed slot without a complete receipt is terminal, never retried
by restarting the process. Trusted owner-only local disk is a prerequisite.
"""

import fcntl
import http.client
import json
import os
import re
import ssl
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from market_data_foundation.canonical.model import digest, instant, require
from market_data_foundation.source_request_policy import (
    classify_source_exception,
    request_source,
)

from . import sdmx_wire
from .sdmx_contract import MAX_ENTITY_BYTES, roles_for, surface_for
from .sdmx_official import (
    RetainedEntity,
    SdmxCandidate,
    validate_window,
)

STRUCTURE_MEDIA = "application/vnd.sdmx.structure+xml;version=2.1"
DATA_MEDIA = "application/vnd.sdmx.genericdata+xml;version=2.1"


def write_json(path: Path, document):
    """Atomic replacement with file and parent fsync on a trusted local disk."""
    temporary = path.with_suffix(".pending")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(document, stream, sort_keys=True, separators=(",", ":"))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def exclusive(root: Path):
    require("artifacts" in root.resolve().parts, "sdmx_artifact_root_required")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    require(not root.is_symlink(), "sdmx_artifact_root_invalid")
    descriptor = os.open(
        root / "executor.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)


@dataclass(frozen=True)
class SourcePlan:
    start: str
    end: str

    def __post_init__(self):
        validate_window(self.start, self.end)


@dataclass(frozen=True)
class Request:
    provider: str
    role: str
    url: str

    @property
    def accept(self):
        return DATA_MEDIA if self.role == "data" else STRUCTURE_MEDIA


@dataclass(frozen=True)
class Response:
    status_code: int
    body: bytes
    headers: tuple[tuple[str, str], ...] = ()


def validate_response(response, request):
    require(
        type(response.body) is bytes and 0 < len(response.body) <= MAX_ENTITY_BYTES,
        "sdmx_body_bound",
    )
    headers = {}
    for name, value in response.headers:
        name = name.lower()
        require(
            name not in headers and not any(c in value for c in "\r\n\x00"),
            "sdmx_headers_invalid",
        )
        headers[name] = value
    media = headers.get("content-type", "").lower().replace(" ", "")
    expected = request.accept
    require(
        media in {expected, expected + ";charset=utf-8"}, "sdmx_content_type_invalid"
    )
    require(
        headers.get("content-encoding", "identity").lower() == "identity",
        "sdmx_compression_not_admitted",
    )
    length = headers.get("content-length")
    transfer = headers.get("transfer-encoding")
    require(
        transfer in {None, "chunked"} and not (transfer and length is not None),
        "sdmx_framing_invalid",
    )
    if length is not None:
        require(
            length.isascii()
            and length.isdecimal()
            and len(length) <= 7
            and int(length) == len(response.body),
            "sdmx_length_invalid",
        )


class Acquisition:
    """Persistent seven-slot budget; parser failures reuse receipts, never Source.

    An injected exchange must make exactly one transport attempt per call. Tests
    use fakes; ExactHttpsSource is the only provided network implementation.
    """

    def __init__(self, root: Path, plan: SourcePlan):
        require(type(plan) is SourcePlan, "sdmx_source_plan_required")
        self.root, self.plan = root, plan

    def _document(self):
        path = self.root / "budget.json"
        spec = json.loads(json.dumps(asdict(self.plan)))
        if path.exists():
            document = json.loads(path.read_bytes())
            require(document["plan"] == spec, "sdmx_transaction_plan_mismatch")
            require(
                set(document["slots"])
                <= {f"{p}.{r}" for p in ("ECB", "ESTAT") for r in roles_for(p)},
                "sdmx_budget_invalid",
            )
            require(
                all(
                    type(slot["attempts"]) is int
                    and 0 <= slot["attempts"] <= 3
                    and slot["state"] in {"started", "retained"}
                    for slot in document["slots"].values()
                ),
                "sdmx_budget_invalid",
            )
            return document
        document = {"plan": spec, "slots": {}}
        write_json(path, document)
        return document

    def counts(self):
        with exclusive(self.root):
            slots = self._document()["slots"]
            return {
                "logical": len(slots),
                "attempts": sum(s["attempts"] for s in slots.values()),
                "retries": sum(max(0, s["attempts"] - 1) for s in slots.values()),
                "fallbacks": 0,
            }

    def acquire(
        self,
        provider,
        *,
        enabled=False,
        exchange=None,
        clock=lambda: datetime.now(UTC),
        sleep=time.sleep,
    ):
        require(enabled is True, "sdmx_source_opt_in_required")
        s = surface_for(provider)
        with exclusive(self.root):
            document = self._document()
            previous_obtained = None

            def read(role, url):
                nonlocal previous_obtained
                request = Request(provider, role, url)
                slot = f"{provider}.{role}"
                receipt = self.root / (slot + ".json")
                raw_path = self.root / (slot + ".bin")
                if receipt.exists():
                    item = json.loads(receipt.read_bytes())
                    raw = raw_path.read_bytes()
                    require(
                        item["request"] == asdict(request)
                        and item["sha256"] == digest(raw)
                        and type(item["size"]) is int
                        and item["size"] == len(raw)
                        and item["status"] == 200
                        and item["request_headers"]
                        == {"accept": request.accept, "accept-encoding": "identity"}
                        and type(item["attempts"]) is int
                        and 1 <= item["attempts"] <= 3,
                        "sdmx_retained_receipt_mismatch",
                    )
                    require(
                        slot in document["slots"]
                        and item["attempts"] == document["slots"][slot]["attempts"],
                        "sdmx_retained_budget_mismatch",
                    )
                else:
                    require(
                        slot not in document["slots"] and not raw_path.exists(),
                        "sdmx_consumed_slot_no_requery",
                    )
                    require(exchange is not None, "sdmx_exchange_required")
                    requested = clock()
                    instant(requested)
                    require(
                        previous_obtained is None or previous_obtained <= requested,
                        "sdmx_acquisition_time_invalid",
                    )
                    document["slots"][slot] = {
                        "attempts": 0,
                        "state": "started",
                        "request": asdict(request),
                        "requested_at": requested.isoformat(),
                        "observations": [],
                    }
                    write_json(self.root / "budget.json", document)

                    def save_attempt():
                        try:
                            write_json(self.root / "budget.json", document)
                        except Exception:
                            raise RuntimeError("sdmx_attempt_intent_failed") from None

                    def attempt():
                        entry = document["slots"][slot]
                        entry["attempts"] += 1
                        save_attempt()
                        try:
                            response = exchange(request)
                        except Exception as error:
                            classification, _ = _classify(error)
                            entry["observations"].append(
                                {
                                    "attempt": entry["attempts"],
                                    "classification": classification,
                                }
                            )
                            save_attempt()
                            raise
                        if response.status_code != 200:
                            entry["observations"].append(
                                {
                                    "attempt": entry["attempts"],
                                    "classification": "http_failure",
                                    "status": response.status_code,
                                }
                            )
                            save_attempt()
                        return response

                    # HTTP200 completes the retry boundary. Everything below it,
                    # including retention/header validation, can never requery.
                    response, attempts = request_source(
                        attempt,
                        status=lambda r: r.status_code,
                        classify=_classify,
                        sleep=sleep,
                    )
                    obtained = clock()
                    instant(obtained)
                    require(requested <= obtained, "sdmx_receipt_time_invalid")
                    raw = response.body
                    require(
                        type(raw) is bytes and len(raw) <= MAX_ENTITY_BYTES,
                        "sdmx_body_bound",
                    )
                    with raw_path.open("xb") as stream:
                        stream.write(raw)
                        stream.flush()
                        os.fsync(stream.fileno())
                    item = {
                        "request": asdict(request),
                        "requested_at": requested.isoformat(),
                        "obtained_at": obtained.isoformat(),
                        "attempts": attempts,
                        "sha256": digest(raw),
                        "size": len(raw),
                        "headers": response.headers,
                        "status": 200,
                        "request_headers": {
                            "accept": request.accept,
                            "accept-encoding": "identity",
                        },
                    }
                    write_json(receipt, item)
                    document["slots"][slot]["observations"].append(
                        {
                            "attempt": attempts,
                            "classification": "retained",
                            "status": 200,
                        }
                    )
                    document["slots"][slot]["state"] = "retained"
                    write_json(self.root / "budget.json", document)
                validate_response(
                    Response(200, raw, tuple(tuple(h) for h in item["headers"])),
                    request,
                )
                requested_at = datetime.fromisoformat(item["requested_at"])
                obtained_at = datetime.fromisoformat(item["obtained_at"])
                instant(requested_at)
                instant(obtained_at)
                require(
                    requested_at <= obtained_at
                    and (
                        previous_obtained is None or previous_obtained <= requested_at
                    ),
                    "sdmx_acquisition_time_invalid",
                )
                previous_obtained = obtained_at
                return RetainedEntity(
                    role,
                    url,
                    raw,
                    requested_at,
                    obtained_at,
                    item["attempts"],
                )

            flow = read(
                "flow", f"{s.base}/dataflow/{s.agency}/{s.flow}/latest?references=none"
            )
            dsd_ref = sdmx_wire.flow_reference(s, flow.raw)
            dsd = read(
                "dsd", f"{s.base}/datastructure/{'/'.join(dsd_ref)}?references=none"
            )
            sdmx_wire.dsd_structure(s, dsd_ref, dsd.raw)
            entities = (flow, dsd)
            # Check dimension identity and UNIT routing before the UNIT slot.
            # Full metadata validation below gates the data slot.
            unit_ref = sdmx_wire.unit_reference(s, dsd.raw)
            if provider == "ESTAT":
                unit = read(
                    "unit", f"{s.base}/codelist/{'/'.join(unit_ref)}?references=none"
                )
                entities += (unit,)
            sdmx_wire.metadata(s, entities)
            data = read(
                "data",
                f"{s.base}/data/{s.flow}/{s.key}?startPeriod={self.plan.start}&endPeriod={self.plan.end}",
            )
            candidate = SdmxCandidate(
                provider, self.plan.start, self.plan.end, (*entities, data)
            )
            sdmx_wire.validate_candidate(candidate)
            return candidate


def _classify(error):
    if isinstance(error, ssl.SSLCertVerificationError):
        return "tls_identity_invalid", False
    return classify_source_exception(error)


class ExactHttpsSource:
    """One bounded official HTTPS attempt, with no proxy/netrc/redirect support."""

    def __call__(self, request: Request):
        s = surface_for(request.provider)
        require(
            request.role in roles_for(request.provider)
            and request.url.startswith(s.base + "/"),
            "sdmx_url_not_admitted",
        )
        suffix = request.url[len(s.base) :]
        token = r"[A-Za-z0-9_@.-]{1,128}"
        patterns = {
            "flow": re.escape(f"/dataflow/{s.agency}/{s.flow}/latest?references=none"),
            "dsd": rf"/datastructure/{s.agency}/{token}/{token}\?references=none",
            "unit": rf"/codelist/ESTAT/UNIT/{token}\?references=none",
            "data": re.escape(f"/data/{s.flow}/{s.key}")
            + r"\?startPeriod=\d{4}-\d{2}-\d{2}&endPeriod=\d{4}-\d{2}-\d{2}",
        }
        require(re.fullmatch(patterns[request.role], suffix), "sdmx_url_not_admitted")
        parsed = urlsplit(request.url)
        connection = http.client.HTTPSConnection(parsed.hostname, timeout=10)
        try:
            connection.request(
                "GET",
                parsed.path + ("?" + parsed.query if parsed.query else ""),
                headers={
                    "Accept": request.accept,
                    "Accept-Encoding": "identity",
                    "User-Agent": "Market-Data-Foundation/0.1 T08",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                return Response(response.status, b"")
            # Retain complete bounded bytes even if media/framing later rejects.
            raw = response.read(MAX_ENTITY_BYTES + 1)
            if len(raw) > MAX_ENTITY_BYTES:
                raise http.client.HTTPException("sdmx_body_bound")
            headers = tuple(
                (k.lower(), v)
                for k, v in response.getheaders()
                if k.lower()
                in {
                    "content-type",
                    "content-length",
                    "transfer-encoding",
                    "content-encoding",
                }
            )
            return Response(200, raw, headers)
        finally:
            # Cleanup cannot revoke completed HTTP bytes or mask the original
            # request/read failure. Only transport failures above are retryable.
            try:
                connection.close()
            except OSError:
                pass
