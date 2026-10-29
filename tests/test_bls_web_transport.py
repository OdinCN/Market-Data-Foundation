"""Offline transport seams: no socket/DNS/HTTP traffic in this module."""

import ssl
from unittest.mock import Mock

import pytest

from market_data_foundation.historical import bls_web_transport as web
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    fetch_official_calendar,
    fetch_reference_calendar,
)
from market_data_foundation.historical.cpi_revision_signals import (
    CpiSignalError,
    fetch_official_errata,
)

ICS = "https://www.bls.gov/schedule/news_release/bls.ics"


@pytest.fixture
def wire(monkeypatch):
    scenarios, connections = [], []

    class Connection:
        def __init__(self, host, port, *, timeout, context):
            self.options = scenarios.pop(0) if scenarios else {}
            self.host, self.port = host, port
            self.context, self.timeout = context, timeout
            self.events = []
            self.sock = self
            connections.append(self)

        def connect(self):
            self.events.append("connect")
            if error := self.options.get("error"):
                raise error

        def version(self):
            self.events.append("version")
            return self.options.get("tls", "TLSv1.3")

        def request(self, method, path, *, headers):
            self.events.append("request")
            self.request_args = method, path, headers

        def getresponse(self):
            self.events.append("response")
            return self

        @property
        def status(self):
            return self.options.get("status", 200)

        def getheader(self, name):
            assert name == "Location"
            return self.options.get("location")

        def read(self, limit):
            self.events.append("read")
            self.read_limit = limit
            return self.options.get("raw", b"BEGIN:VCALENDAR")[:limit]

        def close(self):
            self.events.append("close")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr(web, "HTTPSConnection", Connection)
    return scenarios, connections


@pytest.mark.parametrize("tls", ["TLSv1.2", "TLSv1.1", None])
def test_non_tls13_closes_before_any_http_request(wire, tls):
    scenarios, connections = wire
    scenarios.append({"tls": tls})
    with pytest.raises(web.BlsWebError, match="tls_version_rejected"):
        web.fetch_bls_page(ICS)
    assert connections[0].events == ["connect", "version", "close"]


def test_tls13_uses_same_local_verified_context_on_redirect_and_ics(wire, monkeypatch):
    scenarios, connections = wire
    scenarios.extend([{"status": 302, "location": ICS}, {}])
    context = ssl.create_default_context()
    default_minimum = context.minimum_version
    trust_store = context.cert_store_stats()
    ciphers = Mock(wraps=context.set_ciphers)
    alpn = Mock(wraps=context.set_alpn_protocols)
    monkeypatch.setattr(context, "set_ciphers", ciphers)
    monkeypatch.setattr(context, "set_alpn_protocols", alpn)
    factory = Mock(return_value=context)
    monkeypatch.setattr(web.ssl, "create_default_context", factory)
    assert (
        web.fetch_bls_page(
            "https://bls.gov:443/schedule/news_release/bls.ics",
            headers={"Accept": "text/calendar"},
        )
        == b"BEGIN:VCALENDAR"
    )
    factory.assert_called_once_with()
    ciphers.assert_called_once_with("DEFAULT:@SECLEVEL=2")
    alpn.assert_called_once_with(["http/1.1"])
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.check_hostname is True
    assert context.security_level == 2
    assert context.minimum_version == default_minimum
    assert context.cert_store_stats() == trust_store
    # Hashed default CA directories may load certificates lazily on handshake.
    assert all(c.context is context and c.timeout == 20 for c in connections)
    for connection in connections:
        assert connection.events[:4] == ["connect", "version", "request", "response"]
        assert connection.events[-1] == "close"
        assert connection.request_args == (
            "GET",
            "/schedule/news_release/bls.ics",
            {
                "Accept": "text/calendar",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bls.gov/errata/",
        "https://api.bls.gov/errata/",
        "https://www.bls.gov.evil.example/",
        "https://user:password@www.bls.gov/",
        "https://www.bls.gov:444/",
        "https://www.bls.gov\\@evil.example/",
        "https://www.bls.gov/\npath",
        "https://www.bls.gov/%0a",
    ],
)
def test_nonofficial_url_rejected_before_connect(wire, url):
    with pytest.raises(web.BlsWebError, match="url_invalid"):
        web.fetch_bls_page(url)
    assert wire[1] == []


@pytest.mark.parametrize(
    "location",
    [
        "https://evil.example/",
        "http://www.bls.gov/",
        "https://api.bls.gov/",
        "https://user:password@www.bls.gov/",
        "https://www.bls.gov:444/",
        None,
    ],
)
def test_redirect_rejected_before_opening_target(wire, location):
    wire[0].append({"status": 302, "location": location})
    with pytest.raises(web.BlsWebError, match="redirect_rejected"):
        web.fetch_bls_page(ICS)
    assert len(wire[1]) == 1
    assert "read" not in wire[1][0].events
    assert wire[1][0].events[-1] == "close"


def test_same_host_relative_redirect_rechecks_tls_and_uses_bounded_hops(wire):
    wire[0].extend([{"status": 302, "location": "/errata/"}, {"tls": "TLSv1.2"}])
    with pytest.raises(web.BlsWebError, match="tls_version_rejected"):
        web.fetch_bls_page(ICS)
    assert wire[1][1].events == ["connect", "version", "close"]
    wire[0].extend([{"status": 302, "location": "/errata/"}] * 4)
    with pytest.raises(web.BlsWebError, match="redirect_rejected"):
        web.fetch_bls_page(ICS)
    assert len(wire[1]) == 6  # Two earlier connections plus four bounded hops.


@pytest.mark.parametrize(
    ("error", "classification"),
    [
        (ssl.SSLCertVerificationError("private"), "certificate_verification_failed"),
        (ssl.SSLError("private"), "tls_failed"),
        (TimeoutError("private"), "timeout"),
        (OSError("private"), "transport_failed"),
    ],
)
def test_handshake_failure_is_classified_without_request_or_raw_message(
    wire, error, classification
):
    wire[0].append({"error": error})
    with pytest.raises(web.BlsWebError, match=classification) as raised:
        web.fetch_bls_page(ICS)
    assert "private" not in str(raised.value)
    assert wire[1][0].events == ["connect", "close"]


@pytest.mark.parametrize(
    ("fetch", "error_type", "prefix"),
    [
        (fetch_official_calendar, CpiScheduleError, "calendar"),
        (fetch_reference_calendar, CpiScheduleError, "calendar"),
        (fetch_official_errata, CpiSignalError, "signal"),
    ],
)
def test_existing_adapters_keep_classifications_and_byte_limits(
    wire, fetch, error_type, prefix
):
    for options, suffix in [
        ({"status": 403}, "http_403"),
        ({"error": TimeoutError("private")}, "timeout"),
        ({"tls": "TLSv1.2"}, "tls_version_rejected"),
        (
            {"raw": b"x" * (web.MAX_BYTES + 1)},
            "payload_too_large" if prefix == "calendar" else "payload_invalid",
        ),
    ]:
        wire[0].append(options)
        with pytest.raises(error_type, match=prefix + "_" + suffix):
            fetch()
    assert wire[1][-1].read_limit == web.MAX_BYTES + 1
    wire[0].append({"raw": b"x" * web.MAX_BYTES})
    assert len(fetch()) == web.MAX_BYTES


def test_calendar_redirect_still_requires_original_path_and_errata_checks_domain(wire):
    wire[0].append({"status": 302, "location": "/errata/"})
    with pytest.raises(CpiScheduleError, match="calendar_redirect_rejected"):
        fetch_official_calendar()
    wire[0].append({"status": 302, "location": "https://evil.example/"})
    with pytest.raises(CpiSignalError, match="signal_redirect_rejected"):
        fetch_official_errata()
    assert len(wire[1]) == 2


def test_empty_errata_payload_remains_invalid(wire):
    wire[0].append({"raw": b""})
    with pytest.raises(CpiSignalError, match="signal_payload_invalid"):
        fetch_official_errata()


def test_ics_is_default_and_html_reference_has_separate_fixed_path(wire):
    wire[0].extend([{}, {}])
    fetch_official_calendar()
    fetch_reference_calendar()
    first, second = wire[1]
    assert first.request_args[1] == "/schedule/news_release/bls.ics"
    assert first.request_args[2]["Accept"] == "text/calendar"
    assert second.request_args[1] == "/schedule/news_release/cpi.htm"
    assert second.request_args[2]["Accept"] == "text/html"
    wire[0].append({"status": 302, "location": "/schedule/news_release/cpi.htm"})
    with pytest.raises(CpiScheduleError, match="calendar_redirect_rejected"):
        fetch_official_calendar()
