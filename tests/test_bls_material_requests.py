"""Offline material-only requests, limits and no-email contract."""

import gzip
import json
import os
import subprocess
import sys
import time
import zlib
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests
from urllib3.exceptions import ReadTimeoutError

from market_data_foundation.historical import bls_material_transport as transport
from market_data_foundation.historical import macro_release_acquisition as acquisition
from market_data_foundation.historical.bls_web_transport import BlsWebError
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
)

URL = "https://www.bls.gov/news.release/archives/cpi_11102026.htm"


@pytest.fixture
def wire(monkeypatch):
    scenarios, sessions, responses = [], [], []

    class Response:
        def __init__(self, value):
            self.value = value
            self.status_code = value.get("status", 200)
            self.headers = value.get("headers", {})
            self.raw = self
            self.closed = False
            self.blocks_read = 0
            responses.append(self)

        def stream(self, size, *, decode_content):
            assert size == 16384 and decode_content is False
            for block in self.value.get("blocks", [b"retained original"]):
                self.blocks_read += 1
                if isinstance(block, Exception):
                    raise block
                yield block

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    class Session:
        def __init__(self):
            self.closed, self.calls = False, []
            sessions.append(self)

        def get(self, url, **options):
            self.calls.append((url, options))
            value = scenarios.pop(0)
            if isinstance(value, Exception):
                raise value
            return Response(value)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.closed = True

    monkeypatch.setattr(transport.requests, "Session", Session)
    return scenarios, sessions, responses


def download():
    facts = dict(http_attempts=0)
    return transport._download(URL, "cpi", facts), facts


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_verified_requests_full_body_has_browser_headers_no_personal_data(
    wire, encoding
):
    raw = b"<html>" + b"original bytes\n" * 3000 + b"</html>"
    encoded = (
        gzip.compress(raw)
        if encoding == "gzip"
        else zlib.compress(raw)
        if encoding == "deflate"
        else raw
    )
    wire[0].append(
        dict(
            headers={"Content-Encoding": encoding},
            blocks=[encoded[i : i + 8192] for i in range(0, len(encoded), 8192)],
        )
    )
    body, facts = download()
    assert body == raw and facts == dict(http_attempts=1, http_status=200)
    session = wire[1][0]
    assert session.trust_env is False and session.closed and wire[2][0].closed
    url, options = session.calls[0]
    assert url == URL and options["verify"] is True
    assert options["stream"] is True and options["allow_redirects"] is False
    assert options["timeout"] == (5, 5)
    assert set(options["headers"]) == {
        "User-Agent",
        "Accept",
        "Accept-Language",
    }
    assert "Chrome/156.0.0.0" in options["headers"]["User-Agent"]
    assert options["headers"]["User-Agent"].endswith("DataResearchBot/1.0")
    assert not any("@" in v for v in options["headers"].values())


@pytest.mark.parametrize("status", [403, 429, 503])
def test_error_status_no_body_read_or_retry_closes_and_preserves_retry_after(
    wire, status
):
    wire[0].append(dict(status=status, headers={"Retry-After": "3600"}))
    with pytest.raises(BlsWebError, match=f"http_{status}") as caught:
        download()
    assert caught.value.http_status == status and caught.value.retry_at is not None
    assert len(wire[1][0].calls) == 1 and wire[2][0].blocks_read == 0
    assert wire[1][0].closed and wire[2][0].closed


@pytest.mark.parametrize(
    "target",
    [
        "http://www.bls.gov/news.release/archives/cpi_11102026.htm",
        "https://evil.example/",
        "https://www.bls.gov/news.release/archives/cpi_10142026.htm",
        "https://www.bls.gov/news.release/archives/cpi_11102026.htm?key=private",
        None,
    ],
)
def test_redirect_must_remain_same_official_archive_before_next_request(wire, target):
    wire[0].append(dict(status=302, headers={"Location": target}))
    with pytest.raises(BlsWebError, match="redirect_rejected"):
        download()
    assert len(wire[1][0].calls) == 1 and wire[2][0].closed


def test_relative_same_path_redirect_is_bounded(wire):
    wire[0].extend(
        [
            dict(
                status=302,
                headers={"Location": "/news.release/archives/cpi_11102026.htm"},
            )
        ]
        * 4
    )
    with pytest.raises(BlsWebError, match="redirect_rejected"):
        download()
    assert len(wire[1][0].calls) == 4 and all(r.closed for r in wire[2])


@pytest.mark.parametrize("encoding", ["identity", "gzip", "deflate"])
def test_wire_and_decompressed_limits_stop_before_full_read(
    wire, monkeypatch, encoding
):
    monkeypatch.setattr(transport, "MAX_BYTES", 100)
    raw = b"x" * 10000
    first = (
        gzip.compress(raw)
        if encoding == "gzip"
        else zlib.compress(raw)
        if encoding == "deflate"
        else raw[:101]
    )
    wire[0].append(
        dict(headers={"Content-Encoding": encoding}, blocks=[first, b"never read"])
    )
    with pytest.raises(BlsWebError, match="payload_too_large"):
        download()
    assert wire[2][0].blocks_read == 1 and wire[2][0].closed


@pytest.mark.parametrize(
    "encoding,raw",
    [("br", b"unknown"), ("gzip", b"truncated"), ("deflate", b"truncated")],
)
def test_invalid_or_unrequested_encoding_is_not_returned_as_original(
    wire, encoding, raw
):
    wire[0].append(dict(headers={"Content-Encoding": encoding}, blocks=[raw]))
    with pytest.raises((BlsWebError, zlib.error)):
        download()
    assert wire[2][0].closed


@pytest.mark.parametrize(
    "error",
    [
        requests.exceptions.ConnectTimeout("private"),
        requests.exceptions.ReadTimeout("private"),
        requests.exceptions.ConnectionError("private"),
        requests.exceptions.SSLError("private"),
    ],
)
def test_connection_failure_closes_session_without_retry(wire, error):
    wire[0].append(error)
    with pytest.raises(type(error)):
        download()
    assert wire[1][0].closed and len(wire[1][0].calls) == 1


def test_stream_read_timeout_closes_response(wire):
    wire[0].append(dict(blocks=[b"prefix", ReadTimeoutError(None, None, "private")]))
    with pytest.raises(ReadTimeoutError):
        download()
    assert wire[1][0].closed and wire[2][0].closed


def test_absolute_deadline_kills_and_reaps_slow_drip_child_in_resource_group(
    monkeypatch,
):
    real = subprocess.Popen
    children = []

    def slow(*args, **kwargs):
        p = real([sys.executable, "-c", "import time; time.sleep(10)"], **kwargs)
        children.append(p)
        return p

    monkeypatch.setattr(transport.subprocess, "Popen", slow)
    monkeypatch.setattr(transport, "TOTAL_SECONDS", 0.2)
    start = time.monotonic()
    with pytest.raises(BlsWebError, match="timeout"):
        transport.fetch_bls_material(URL, "cpi")
    assert time.monotonic() - start < 2 and children[0].poll() is not None


@pytest.mark.parametrize(
    "url",
    [
        URL.replace("https:", "http:"),
        URL.replace("www.bls.gov", "api.bls.gov"),
        URL + "?x=1",
        URL.replace("www.bls.gov", "user:pass@www.bls.gov"),
    ],
)
def test_unofficial_url_rejected_before_child(monkeypatch, url):
    monkeypatch.setattr(
        transport.subprocess,
        "Popen",
        lambda *a, **k: pytest.fail("no process for invalid URL"),
    )
    with pytest.raises(ValueError):
        transport.fetch_bls_material(url, "cpi")


@pytest.mark.parametrize(
    "family,path",
    [("cpi", "cpi"), ("ppi", "ppi"), ("employment", "empsit"), ("jolts", "jolts")],
)
def test_normal_product_acquisition_uses_material_transport_for_all_bls_families(
    monkeypatch, family, path
):
    calls = []
    monkeypatch.setattr(
        acquisition,
        "fetch_bls_material",
        lambda *args: calls.append(args) or b"original",
    )
    url = f"https://www.bls.gov/news.release/archives/{path}_11102026.htm"
    assert acquisition.fetch_official_material(url, family) == b"original"
    assert calls == [(url, family)]


@pytest.mark.parametrize(
    "exception,classification",
    [
        (requests.exceptions.ReadTimeout("private"), "timeout"),
        (requests.exceptions.SSLError("private"), "tls_failed"),
        (requests.exceptions.ConnectionError("private"), "transport_failed"),
        (ReadTimeoutError(None, None, "private"), "timeout"),
    ],
)
def test_worker_exception_envelope_is_classified_without_raw_message(
    tmp_path, monkeypatch, exception, classification
):
    monkeypatch.setattr(
        transport.ctypes, "CDLL", lambda *a: SimpleNamespace(prctl=lambda *a: 0)
    )
    monkeypatch.setattr(
        transport, "_download", lambda *a: (_ for _ in ()).throw(exception)
    )
    assert transport._worker(URL, "cpi", tmp_path, os.getppid()) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["classification"] == classification and "private" not in json.dumps(
        result
    )


@pytest.mark.parametrize("block", ["cooldown", "budget", "in_flight"])
def test_jolts_shared_control_denies_before_any_http(wire, tmp_path, block):
    from contextlib import nullcontext
    from datetime import UTC, datetime

    at = datetime(2026, 11, 10, tzinfo=UTC)
    path = tmp_path / "shared.json"
    control = CpiRequestControl(path, clock=lambda: at, min_interval_seconds=0)
    if block == "cooldown":
        with control.attempt() as attempt:
            attempt.record_response(429, {"Retry-After": "3600"})
    elif block == "budget":
        for _ in range(400):
            with control.attempt() as attempt:
                attempt.record_response(200, {})
    url = URL.replace("cpi_", "jolts_")
    with control.attempt() if block == "in_flight" else nullcontext():
        before = path.read_bytes()
        facts = dict(http_attempts=0)
        with pytest.raises(CpiRequestControlError) as caught:
            transport._download(url, "jolts", facts, request_control=control)
        assert (
            caught.value.classification
            == {
                "cooldown": "cpi_timeseries_request_cooldown",
                "budget": "cpi_timeseries_request_budget_exhausted",
                "in_flight": "cpi_timeseries_request_in_flight",
            }[block]
        )
        assert facts["http_attempts"] == 0 and not wire[1][0].calls
        assert path.read_bytes() == before


def test_jolts_redirects_each_count_and_429_prevents_next_exchange(wire, tmp_path):
    from datetime import UTC, datetime

    path = tmp_path / "shared.json"
    control = CpiRequestControl(
        path, clock=lambda: datetime(2026, 11, 10, tzinfo=UTC), min_interval_seconds=0
    )
    url = URL.replace("cpi_", "jolts_")
    wire[0].extend(
        [
            dict(status=302, headers={"Location": url}),
            dict(status=429, headers={"Retry-After": "3600"}),
        ]
    )
    facts = dict(http_attempts=0)
    with pytest.raises(BlsWebError, match="http_429"):
        transport._download(url, "jolts", facts, request_control=control)
    state = json.loads(path.read_text())
    assert len(state["requests"]) == facts["http_attempts"] == 2
    assert state["in_flight_at"] is None and state["consecutive_429"] == 1
    with pytest.raises(CpiRequestControlError, match="request_cooldown"):
        transport._download(url, "jolts", facts, request_control=control)
    assert len(wire[1][0].calls) == 2 and not wire[1][1].calls
    assert all(r.closed for r in wire[2])


@pytest.fixture
def local_jolts_worker(monkeypatch, wire, tmp_path):
    path = tmp_path / "shared.json"
    monkeypatch.setattr(transport, "DEFAULT_REQUEST_CONTROL_PATH", path)
    monkeypatch.setattr(
        transport.ctypes, "CDLL", lambda *a: SimpleNamespace(prctl=lambda *a: 0)
    )
    real = subprocess.Popen
    args_seen = []

    # Run real worker, replacing only its HTTP socket; parent reads real envelope.
    def local_worker(args, **options):
        args_seen.append(args)
        assert args[-1] == str(path)
        assert (
            transport._worker(
                args[3],
                args[4],
                Path(args[5]),
                os.getppid(),
                args[7],
            )
            == 0
        )
        return real([sys.executable, "-c", "pass"], **options)

    monkeypatch.setattr(transport.subprocess, "Popen", local_worker)
    return path, args_seen


def test_jolts_worker_composition_uses_shared_default_only_for_jolts(
    wire, local_jolts_worker
):
    path, args_seen = local_jolts_worker
    wire[0].append(dict(status=429, headers={"Retry-After": "3600"}))
    url = URL.replace("cpi_", "jolts_")
    with pytest.raises(BlsWebError, match="http_429"):
        transport.fetch_bls_material(url, "jolts")
    before = path.read_bytes()
    with pytest.raises(
        CpiRequestControlError,
        match="request_cooldown",
    ):
        transport.fetch_bls_material(url, "jolts")
    assert len(args_seen) == 2 and path.read_bytes() == before
    assert len(json.loads(before)["requests"]) == 1
    assert not wire[1][1].calls
    # No global admission added to CPI/PPI/employment material paths.
    assert CpiRequestControl(path).lock_path.exists()


def test_killed_jolts_transfer_releases_lock_retains_count_and_interrupt_guard(
    monkeypatch, tmp_path
):
    path = tmp_path / "shared.json"
    ready = tmp_path / "body-started"
    monkeypatch.setattr(transport, "DEFAULT_REQUEST_CONTROL_PATH", path)
    monkeypatch.setattr(transport, "TOTAL_SECONDS", 1.2)
    real = subprocess.Popen
    children = []
    script = f"""
import os, time
from pathlib import Path
from market_data_foundation.historical import bls_material_transport as t
class Response:
    status_code = 200
    headers = {{}}
    def __enter__(self): return self
    def __exit__(self, *args): pass
    @property
    def raw(self): return self
    def stream(self, *args, **kwargs):
        Path({str(ready)!r}).write_text("started")
        time.sleep(60)
        yield b"never returned"
class Session:
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def get(self, *args, **kwargs): return Response()
t.requests.Session = Session
t._worker(
    {URL.replace("cpi_", "jolts_")!r}, "jolts",
    Path({str(tmp_path)!r}), os.getppid(), {str(path)!r}
)
"""

    def slow(args, **options):
        child = real([sys.executable, "-c", script], **options)
        children.append(child)
        return child

    monkeypatch.setattr(transport.subprocess, "Popen", slow)
    observed = []
    with pytest.raises(BlsWebError, match="timeout"):
        transport.fetch_bls_material(
            URL.replace("cpi_", "jolts_"), "jolts", observer=observed.append
        )
    assert ready.exists() and children[0].poll() is not None and not observed
    state = json.loads(path.read_text())
    assert len(state["requests"]) == 1 and state["in_flight_at"] is not None
    assert not (tmp_path / "result.json").exists()
    with pytest.raises(CpiRequestControlError, match="request_interrupted"):
        with CpiRequestControl(path).attempt():
            pytest.fail("not admitted after interrupted exchange")
    state = json.loads(path.read_text())
    assert len(state["requests"]) == 1 and state["in_flight_at"] is None
    assert state["recovery_until"] is not None


@pytest.mark.parametrize(
    "family,stem", [("cpi", "cpi"), ("ppi", "ppi"), ("employment", "empsit")]
)
def test_other_bls_material_workers_do_not_expand_shared_admission(
    family, stem, monkeypatch, wire, tmp_path
):
    monkeypatch.setattr(
        transport.ctypes, "CDLL", lambda *a: SimpleNamespace(prctl=lambda *a: 0)
    )
    monkeypatch.setattr(
        transport,
        "CpiRequestControl",
        lambda *a: pytest.fail("no new global accounting for other families"),
    )
    wire[0].append({})
    url = URL.replace("cpi_", stem + "_")
    assert transport._worker(url, family, tmp_path, os.getppid()) == 0
    result = json.loads((tmp_path / "result.json").read_text())
    assert result["success"] and result["http_attempts"] == 1
    assert (tmp_path / "body").read_bytes() == b"retained original"
