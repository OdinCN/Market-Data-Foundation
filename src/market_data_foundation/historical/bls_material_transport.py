"""Ordinary requests for BLS archive materials, with a supervised total bound.

The disposable Python child owns DNS/TLS/headers/body and no business state.
requests' connect/read timeouts bound inactivity, not total wall time; the parent
also kills slow-drip IO at the overall deadline. No retry, proxy, netrc or email.
Numeric API, calendars and errata deliberately retain their existing transport.
"""

import ctypes
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import zlib
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urljoin

import requests
from urllib3.exceptions import TimeoutError as StreamTimeout

from market_data_foundation.historical.bls_web_transport import BlsWebError
from market_data_foundation.historical.calendar_resilience import retry_after
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
)
from market_data_foundation.historical.cpi_timeseries import (
    DEFAULT_REQUEST_CONTROL_PATH,
    CpiTimeSeriesError,
)
from market_data_foundation.historical.macro_release_material import official_url

MAX_BYTES = 2_000_000
TOTAL_SECONDS = 30
CONNECT_SECONDS = 5
READ_SECONDS = 5
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36 DataResearchBot/1.0"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _validate(url, family):
    if family not in {"cpi", "ppi", "employment", "jolts"}:
        raise BlsWebError("url_invalid")
    try:
        official_url(family, url)
    except CpiTimeSeriesError, ValueError:
        raise BlsWebError("url_invalid") from None


def _body(response):
    from market_data_foundation.historical.public_web_transport import (
        PublicFetchError,
        read_entity,
    )

    try:
        return read_entity(response, MAX_BYTES)
    except PublicFetchError as error:
        raise BlsWebError(error.classification) from None


def _download(url, family, facts, *, request_control=None):
    path = official_url(family, url).path
    with requests.Session() as session:
        session.trust_env = False  # Preserve direct official binding; no netrc.
        for hop in range(4):
            _validate(url, family)
            if official_url(family, url).path != path:
                raise BlsWebError("redirect_rejected")
            with (
                request_control.attempt()
                if request_control is not None
                else nullcontext()
            ) as attempt:
                facts["http_attempts"] += 1
                with session.get(
                    url,
                    headers=HEADERS,
                    timeout=(CONNECT_SECONDS, READ_SECONDS),
                    verify=True,
                    stream=True,
                    allow_redirects=False,
                ) as response:
                    facts.update(http_status=response.status_code)
                    if attempt is not None:
                        attempt.record_response(
                            response.status_code, dict(response.headers)
                        )
                    if response.status_code in {301, 302, 303, 307, 308}:
                        target = response.headers.get("Location")
                        if not target or hop == 3:
                            raise BlsWebError("redirect_rejected")
                        try:
                            target = urljoin(url, target)
                            _validate(target, family)
                            if official_url(family, target).path != path:
                                raise ValueError
                        except BlsWebError, ValueError:
                            raise BlsWebError("redirect_rejected") from None
                        url = target
                        continue
                    if response.status_code != 200:
                        raise BlsWebError(
                            f"http_{response.status_code}",
                            http_status=response.status_code,
                            retry_at=retry_after(response.headers.get("Retry-After")),
                        )
                    return _body(response)


def _worker(url, family, directory, parent, control_path=None):
    # Qualified Linux host: parent death cannot leave an orphan HTTP transfer.
    if ctypes.CDLL(None).prctl(1, signal.SIGKILL) != 0 or os.getppid() != parent:
        return 1
    os.umask(0o077)
    facts = dict(http_attempts=0)
    try:
        options = (
            {
                "request_control": CpiRequestControl(
                    control_path or DEFAULT_REQUEST_CONTROL_PATH
                )
            }
            if family == "jolts"
            else {}
        )
        raw = _download(url, family, facts, **options)
        (directory / "body").write_bytes(raw)
        facts["success"] = True
    except Exception as error:
        classification = (
            error.classification
            if isinstance(error, (BlsWebError, CpiRequestControlError))
            else "timeout"
            if isinstance(error, (requests.exceptions.Timeout, StreamTimeout))
            else "tls_failed"
            if isinstance(error, requests.exceptions.SSLError)
            else "content_encoding_invalid"
            if isinstance(error, zlib.error)
            else "transport_failed"
        )
        facts.update(success=False, classification=classification)
        if isinstance(error, CpiRequestControlError):
            facts["request_control_error"] = True
            facts["http_status"] = error.http_status
        retry = getattr(error, "retry_at", None)
        if retry is not None:
            facts["retry_at"] = retry.isoformat()
    (directory / "result.json").write_text(json.dumps(facts))
    return 0


def fetch_bls_material(url, family, *, observer=None):
    """Return byte-exact decoded HTTP entity, never a repaired parser input.

    At most four explicitly checked same-path hops, no retry. MaterialAcquirer
    retains its durable single-flight, three-attempt budget and cooldown. The
    optional observer exposes actual hop counts without changing legacy callers.
    JOLTS retains its existing shared admission path, per exchange including
    redirects. The child owns flock: kill/death releases it, retaining the
    counted in-flight marker for the control's conservative interrupted recovery.
    """
    _validate(url, family)
    deadline = time.monotonic() + TOTAL_SECONDS
    with tempfile.TemporaryDirectory(prefix="mdf-bls-material-") as temporary:
        directory = Path(temporary)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                __name__,
                url,
                family,
                temporary,
                str(os.getpid()),
                str(DEFAULT_REQUEST_CONTROL_PATH) if family == "jolts" else "",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            try:
                process.wait(timeout=max(0.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise BlsWebError("timeout") from None
            if time.monotonic() >= deadline:
                raise BlsWebError("timeout")
            result_path = directory / "result.json"
            if process.returncode != 0 or not result_path.exists():
                raise BlsWebError("transport_failed")
            if result_path.stat().st_size > 8192:
                raise BlsWebError("transport_failed")
            result = json.loads(result_path.read_text())
            if observer is not None:
                observer(dict(result))
            if not result.get("success"):
                from datetime import datetime

                error_type = (
                    CpiRequestControlError
                    if result.get("request_control_error")
                    else BlsWebError
                )
                raise error_type(
                    result["classification"],
                    http_status=result.get("http_status"),
                    retry_at=datetime.fromisoformat(result["retry_at"])
                    if result.get("retry_at")
                    else None,
                )
            with (directory / "body").open("rb") as body:
                raw = body.read(MAX_BYTES + 1)
            if not 0 < len(raw) <= MAX_BYTES:
                raise BlsWebError("payload_too_large")
            return raw
        except BlsWebError:
            raise
        except OSError, ValueError, KeyError:
            raise BlsWebError("transport_failed") from None
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                # No unbounded Popen.__exit__ wait; a killed process cannot
                # initiate another acquisition. The next attempt retains cooldown.
                raise BlsWebError("worker_cleanup_unknown") from None


if __name__ == "__main__":
    raise SystemExit(
        _worker(
            sys.argv[1],
            sys.argv[2],
            Path(sys.argv[3]),
            int(sys.argv[4]),
            sys.argv[5] or None,
        )
    )
