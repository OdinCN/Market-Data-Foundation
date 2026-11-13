"""Bounded single exchanges for registered public documents, never numeric APIs.

Source adapters keep their own admission, retry and parsing contracts. The child
only owns one verified HTTP exchange; no redirects/retries/business-state writes.
"""

import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import requests
from urllib3.exceptions import TimeoutError as StreamTimeout

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 DataResearchBot/1.0"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}
DOL_CLAIMS_PDF_TIMEOUT = 150
DOL_CLAIMS_PDF_READ_IDLE = 20


def _claims_pdf(url, route, accept):
    return (
        route == "dol"
        and url == "https://www.dol.gov/ui/data.pdf"
        and accept == "application/pdf"
    )


# Exact current document bindings, not an arbitrary URL/command execution port.
PATHS = {
    "bls_web": (
        {"www.bls.gov", "bls.gov"},
        r"/(?:schedule/news_release/(?:bls\.ics|cpi\.htm|ppi\.htm|empSit\.htm|jolts\.htm)|errata/)",
    ),
    "bea_calendar": (
        {"www.bea.gov"},
        r"/news/schedule/(?:full|ics/online-calendar-subscription\.ics)",
    ),
    "bea_material": (
        {"www.bea.gov"},
        r"/news/\d{4}/personal-income-and-outlays-[a-z0-9-]+",
    ),
    "retail_calendar": (
        {"www.census.gov"},
        r"/(?:retail/release_schedule\.html|economic-indicators/calendar-listview-\d{4}\.html)",
    ),
    "retail_material": ({"www.census.gov"}, r"/retail/sales\.html"),
    "m3_calendar": ({"www.census.gov"}, r"/manufacturing/m3/release_schedule\.html"),
    "m3_pdf": (
        {"www.census.gov"},
        r"/manufacturing/m3/(?:prel/pdf/s-i-o|adv/pdf/durgd)\.pdf",
    ),
    "dol": (
        {"oui.doleta.gov", "www.dol.gov"},
        r"/(?:unemploy/(?:archive|claims_arch)\.asp|ui/data\.pdf)",
    ),
    "board": ({"www.federalreserve.gov"}, r"/monetarypolicy/openmarket\.htm"),
    "treasury": (
        {"home.treasury.gov"},
        r"/resource-center/data-chart-center/interest-rates/pages/xml",
    ),
    "credit_archive": (
        {"raw.githubusercontent.com"},
        r"/PStoclet/BBA_TFG_CODE_PABLO_STOCLET/5f63f9ad367be0e9d28aac6a7b982500c23ffaa6/data/05_Credit_Spreads/(?:IG|HY)_OAS_spread_daily_1990_2024\.csv",
    ),
}


class PublicFetchError(RuntimeError):
    def __init__(self, classification, *, http_status=None, headers=None):
        super().__init__(classification)
        self.classification = classification
        self.request_may_be_in_flight = classification == "worker_cleanup_unknown"
        self.http_status = http_status
        self.headers = requests.structures.CaseInsensitiveDict(headers or {})


@dataclass(frozen=True)
class PublicResponse:
    status: int
    body: bytes
    headers: dict


def validate(url, route):
    try:
        if route not in PATHS or not isinstance(url, str) or len(url) > 2048:
            raise ValueError
        u = urlsplit(url)
        hosts, pattern = PATHS[route]
        if (
            u.scheme != "https"
            or u.hostname not in hosts
            or u.username is not None
            or u.password is not None
            or u.port not in {None, 443}
            or u.fragment
            or any(ord(c) < 33 or ord(c) == 127 or c in "\\%" for c in url)
            or not re.fullmatch(pattern, u.path)
        ):
            raise ValueError
        query = parse_qsl(u.query, strict_parsing=True)
        if len(dict(query)) != len(query):
            raise ValueError
        if route == "treasury":
            params = dict(query)
            if params.pop("data", None) not in {
                "daily_treasury_yield_curve",
                "daily_treasury_real_yield_curve",
            }:
                raise ValueError
            if len(params) != 1 or not any(
                key in params and re.fullmatch(pattern, params[key])
                for key, pattern in [
                    ("field_tdr_date_value", r"\d{4}"),
                    ("field_tdr_date_value_month", r"\d{6}"),
                ]
            ):
                raise ValueError
        elif route == "dol":
            if not (
                u.hostname == "www.dol.gov"
                and u.path == "/ui/data.pdf"
                and query in ([], [("os", "v0")])
                or u.hostname == "oui.doleta.gov"
                and u.path.startswith("/unemploy/")
                and not query
            ):
                raise ValueError
        elif query:
            raise ValueError
    except TypeError, ValueError:
        raise PublicFetchError("url_invalid") from None


def read_entity(response, max_bytes):
    """Retain exact entity bytes; cap wire and incremental decompression output."""
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding not in {"identity", "gzip", "deflate"}:
        raise PublicFetchError("content_encoding_rejected")
    decoder = (
        zlib.decompressobj(16 + zlib.MAX_WBITS)
        if encoding == "gzip"
        else zlib.decompressobj()
        if encoding == "deflate"
        else None
    )
    raw, wire = bytearray(), 0
    for block in response.raw.stream(16384, decode_content=False):
        wire += len(block)
        if wire > max_bytes:
            raise PublicFetchError("payload_too_large")
        raw.extend(
            decoder.decompress(block, max_bytes + 1 - len(raw)) if decoder else block
        )
        if len(raw) > max_bytes or decoder and decoder.unconsumed_tail:
            raise PublicFetchError("payload_too_large")
    if decoder and (not decoder.eof or decoder.unused_data):
        raise PublicFetchError("content_encoding_invalid")
    return bytes(raw)


def _write(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value))
    os.replace(temporary, path)


def _exchange(url, route, directory, max_bytes, timeout, accept):
    validate(url, route)
    if _claims_pdf(url, route, accept):
        read_idle = DOL_CLAIMS_PDF_READ_IDLE
    elif route == "treasury":
        read_idle = timeout
    else:
        read_idle = 5
    with requests.Session() as session:
        session.trust_env = False
        with session.get(
            url,
            headers={**HEADERS, "Accept": accept},
            timeout=(min(5, timeout), min(read_idle, timeout)),
            stream=True,
            verify=True,
            allow_redirects=False,
        ) as response:
            # Retain only protocol metadata. No cookies or arbitrary server text.
            headers = {
                k: v
                for k, v in response.headers.items()
                if k.lower()
                in {
                    "content-type",
                    "content-length",
                    "content-encoding",
                    "retry-after",
                    "location",
                    "etag",
                    "last-modified",
                    "date",
                }
            }
            head = dict(status=response.status_code, headers=headers)
            _write(directory / "head.json", head)
            raw = read_entity(response, max_bytes)
            (directory / "body").write_bytes(raw)
            return head


def _worker(url, route, directory, parent, max_bytes, timeout, accept):
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
        return 1
    os.umask(0o077)
    try:
        head = _exchange(url, route, directory, max_bytes, timeout, accept)
        result = dict(success=True, **head)
    except Exception as error:
        classification = (
            error.classification
            if isinstance(error, PublicFetchError)
            else "timeout"
            if isinstance(error, (requests.exceptions.Timeout, StreamTimeout))
            else "tls_failed"
            if isinstance(error, requests.exceptions.SSLError)
            else "content_encoding_invalid"
            if isinstance(error, zlib.error)
            else "connection_failed"
        )
        result = dict(success=False, classification=classification)
    _write(directory / "result.json", result)
    return 0


def fetch_public(
    url, route, *, max_bytes=2_000_000, timeout=30, accept="text/html", on_headers=None
):
    """Exactly one exchange. Caller admission must enclose this entire call.

    on_headers lets an existing ticket persist status/429 before body reads finish.
    Overall wall bound includes DNS/TLS/headers/body, plus <=1s killed-child cleanup.
    Only the registered current Claims PDF has the longer document deadline.
    """
    validate(url, route)
    max_timeout = DOL_CLAIMS_PDF_TIMEOUT if _claims_pdf(url, route, accept) else 30
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= 8 * 1024 * 1024
        or type(timeout) not in {float, int}
        or not 0 < timeout <= max_timeout
        or accept
        not in {
            "text/html",
            "text/calendar",
            "application/pdf",
            "application/xml",
            "text/csv",
        }
    ):
        raise PublicFetchError("request_limits_invalid")
    deadline = time.monotonic() + timeout
    with tempfile.TemporaryDirectory(prefix="mdf-public-document-") as name:
        directory = Path(name)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                __name__,
                url,
                route,
                name,
                str(os.getpid()),
                str(max_bytes),
                str(timeout),
                accept,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        head, notified = {}, False
        try:
            while True:
                if not notified and (directory / "head.json").exists():
                    if (directory / "head.json").stat().st_size > 16384:
                        raise PublicFetchError("response_headers_invalid")
                    head = json.loads((directory / "head.json").read_text())
                    if on_headers is not None:
                        on_headers(head["status"], head["headers"])
                    notified = True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise PublicFetchError(
                        "timeout",
                        http_status=head.get("status"),
                        headers=head.get("headers"),
                    )
                if process.poll() is not None:
                    # Read headers once even if a fast child exited before polling.
                    if not notified and (directory / "head.json").exists():
                        continue
                    break
                try:
                    process.wait(timeout=min(0.05, remaining))
                except subprocess.TimeoutExpired:
                    pass
            path = directory / "result.json"
            if (
                process.returncode != 0
                or not path.exists()
                or path.stat().st_size > 16384
            ):
                raise PublicFetchError(
                    "worker_outcome_unknown",
                    http_status=head.get("status"),
                    headers=head.get("headers"),
                )
            result = json.loads(path.read_text())
            if not result["success"]:
                raise PublicFetchError(
                    result["classification"],
                    http_status=head.get("status"),
                    headers=head.get("headers"),
                )
            with (directory / "body").open("rb") as stream:
                raw = stream.read(max_bytes + 1)
            if len(raw) > max_bytes:
                raise PublicFetchError("payload_too_large")
            return PublicResponse(
                result["status"],
                raw,
                requests.structures.CaseInsensitiveDict(result["headers"]),
            )
        except OSError, ValueError, KeyError:
            raise PublicFetchError("worker_outcome_unknown") from None
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                raise PublicFetchError("worker_cleanup_unknown") from None


def header_aware(function):
    """Optional transport port: record the admitted response before reading body."""

    def with_response_headers(request, callback):
        return function(request, on_headers=callback)

    function.with_response_headers = with_response_headers
    return function


def raise_io(error):
    """Keep existing source retry classification for actual transport failures."""
    if error.classification == "timeout":
        raise TimeoutError from None
    if error.classification == "tls_failed":
        import ssl

        raise ssl.SSLError("tls_failed") from None
    if error.request_may_be_in_flight:
        failure = ConnectionError("worker_cleanup_unknown")
        failure.request_may_be_in_flight = True
        raise failure from None
    if error.classification in {"connection_failed", "worker_outcome_unknown"}:
        raise ConnectionError from None
    raise error


if __name__ == "__main__":
    raise SystemExit(
        _worker(
            sys.argv[1],
            sys.argv[2],
            Path(sys.argv[3]),
            int(sys.argv[4]),
            int(sys.argv[5]),
            float(sys.argv[6]),
            sys.argv[7],
        )
    )
