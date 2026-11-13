"""Offline public-document boundaries and real admitted/child lifecycle seams."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from market_data_foundation.historical import public_web_transport as web
from market_data_foundation.historical.bls_document_page import fetch_bls_page
from market_data_foundation.historical.bls_web_transport import BlsWebError
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    CpiRequestControlError,
)
from tests.test_bls_material_requests import wire as request_wire  # noqa: F401

ICS = "https://www.bls.gov/schedule/news_release/bls.ics"


@pytest.fixture
def inline_worker(monkeypatch, request_wire):  # noqa: F811
    """Replace only process startup/socket; execute real worker and parent envelope."""
    real = subprocess.Popen
    monkeypatch.setattr(
        web.ctypes, "CDLL", lambda *_: SimpleNamespace(prctl=lambda *_: 0)
    )
    starts = []

    def start(args, **options):
        starts.append(args)
        mode = os.umask(0o077)
        try:
            assert (
                web._worker(
                    args[3],
                    args[4],
                    Path(args[5]),
                    os.getppid(),
                    int(args[7]),
                    float(args[8]),
                    args[9],
                )
                == 0
            )
        finally:
            os.umask(mode)
        return real([sys.executable, "-c", "pass"], **options)

    monkeypatch.setattr(web.subprocess, "Popen", start)
    return request_wire, starts


def test_complete_verified_entity_no_private_headers_or_automatic_redirect(
    inline_worker,
):
    wire, starts = inline_worker
    raw = b"BEGIN:VCALENDAR\nEND:VCALENDAR"
    wire[0].append(dict(blocks=[raw], headers={"retry-after": "900"}))
    seen = []
    response = web.fetch_public(
        ICS, "bls_web", accept="text/calendar", on_headers=lambda *v: seen.append(v)
    )
    assert response.body == raw and response.status == 200 and len(starts) == 1
    assert response.headers.get("Retry-After") == "900" and len(seen) == 1
    call = wire[1][0].calls[0]
    assert call[0] == ICS
    assert call[1]["verify"] is True and call[1]["allow_redirects"] is False
    assert call[1]["timeout"] == (5, 5)
    assert call[1]["headers"]["Accept"] == "text/calendar"
    assert call[1]["headers"]["User-Agent"].endswith("DataResearchBot/1.0")
    assert not any("@" in v for v in call[1]["headers"].values())
    assert not {"Accept-Encoding", "Connection"} & set(call[1]["headers"])
    assert wire[1][0].closed and wire[2][0].closed


def test_treasury_waits_for_xml_within_the_existing_deadline(inline_worker):
    wire, _ = inline_worker
    wire[0].append(dict(blocks=[b"<feed/>"]))
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        "pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=202611"
    )
    assert web.fetch_public(url, "treasury", timeout=30).status == 200
    assert wire[1][0].calls[0][1]["timeout"] == (5, 30)


@pytest.mark.parametrize(
    "url,route",
    [
        ("http://www.bea.gov/news/schedule/full", "bea_calendar"),
        ("https://evil.example/news/schedule/full", "bea_calendar"),
        ("https://user:password@www.bea.gov/news/schedule/full", "bea_calendar"),
        ("https://www.bea.gov/news/schedule/full?key=private", "bea_calendar"),
        ("https://api.stlouisfed.org/fred/series", "credit_archive"),
        (
            "https://markets.newyorkfed.org/api/rates/secured/sofr/search.json",
            "treasury",
        ),
        (
            "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value=2026&data=other",
            "treasury",
        ),
        (
            "https://raw.githubusercontent.com/PStoclet/BBA_TFG_CODE_PABLO_STOCLET/main/data/05_Credit_Spreads/IG_OAS_spread_daily_1990_2024.csv",
            "credit_archive",
        ),
    ],
)
def test_reject_unregistered_endpoints_and_numeric_apis_before_child(
    monkeypatch, url, route
):
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no process/network")
    )
    with pytest.raises(web.PublicFetchError, match="url_invalid"):
        web.fetch_public(url, route)


@pytest.mark.parametrize("status", [301, 302, 403, 429, 503])
def test_exact_error_entity_status_and_retry_metadata_do_not_become_data(
    inline_worker, status
):
    wire, starts = inline_worker
    raw = b"<html>error document</html>"
    wire[0].append(
        dict(
            status=status,
            blocks=[raw],
            headers={"Retry-After": "1200", "Location": "https://evil.example/"},
        )
    )
    reply = web.fetch_public(ICS, "bls_web")
    assert reply.status == status and reply.body == raw
    assert reply.headers["Retry-After"] == "1200" and len(starts) == 1
    assert len(wire[1][0].calls) == 1  # redirect not followed inside one exchange


@pytest.mark.parametrize(
    "error,classification",
    [
        (requests.exceptions.ReadTimeout("private"), "timeout"),
        (requests.exceptions.SSLError("private"), "tls_failed"),
        (requests.exceptions.ConnectionError("private"), "connection_failed"),
    ],
)
def test_errors_classified_and_resources_closed_without_raw_exception(
    inline_worker, error, classification
):
    wire, _ = inline_worker
    wire[0].append(error)
    with pytest.raises(web.PublicFetchError) as caught:
        web.fetch_public(ICS, "bls_web")
    assert caught.value.classification == classification
    assert "private" not in str(caught.value) and wire[1][0].closed


def test_child_deadline_covers_dns_headers_and_body_and_reaps(monkeypatch):
    real = subprocess.Popen
    children = []

    def start(*_a, **options):
        child = real([sys.executable, "-c", "import time; time.sleep(10)"], **options)
        children.append(child)
        return child

    monkeypatch.setattr(web.subprocess, "Popen", start)
    at = time.monotonic()
    with pytest.raises(web.PublicFetchError, match="timeout"):
        web.fetch_public(ICS, "bls_web", timeout=0.15)
    assert time.monotonic() - at < 2 and children[0].poll() is not None


def test_header_429_saved_before_slow_body_timeout_shared_count_not_released(
    tmp_path, monkeypatch
):
    real = subprocess.Popen
    children = []
    script = """
import os,time,sys
from pathlib import Path
from market_data_foundation.historical import public_web_transport as w
class Response:
    status_code=429
    headers={"Retry-After":"3600"}
    def __enter__(self):return self
    def __exit__(self,*_):pass
    @property
    def raw(self):return self
    def stream(self,*_a,**_k):
        time.sleep(10)
        yield b"never returned"
class Session:
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def get(self,*_a,**_k):return Response()
w.requests.Session=Session
w._worker(sys.argv[1],"bls_web",Path(sys.argv[2]),os.getppid(),100,.7,"text/calendar")
"""

    def start(args, **options):
        child = real([sys.executable, "-c", script, args[3], args[5]], **options)
        children.append(child)
        return child

    monkeypatch.setattr(web.subprocess, "Popen", start)
    control = CpiRequestControl(tmp_path / "shared.json", min_interval_seconds=0)
    with pytest.raises(web.PublicFetchError, match="timeout") as caught:
        with control.attempt() as ticket:
            web.fetch_public(
                ICS, "bls_web", timeout=0.7, on_headers=ticket.record_response
            )
    state = json.loads(control.state_path.read_text())
    assert (
        caught.value.http_status == 429
        and caught.value.headers["Retry-After"] == "3600"
    )
    assert len(state["requests"]) == 1 and state["consecutive_429"] == 1
    assert state["cooldown_until"] and state["in_flight_at"] is None
    assert children[0].poll() is not None
    with pytest.raises(CpiRequestControlError, match="request_cooldown"):
        with control.attempt():
            pytest.fail("cooldown must survive timeout")


@pytest.mark.parametrize("block", ["cooldown", "in_flight"])
def test_bls_page_shared_admission_denies_before_process(
    inline_worker, tmp_path, block
):
    wire, starts = inline_worker
    control = CpiRequestControl(tmp_path / "shared.json", min_interval_seconds=0)
    from contextlib import nullcontext

    if block == "cooldown":
        with control.attempt() as ticket:
            ticket.record_response(429, {"Retry-After": "3600"})
    with control.attempt() if block == "in_flight" else nullcontext():
        with pytest.raises(CpiRequestControlError):
            fetch_bls_page(ICS, request_control=control)
    assert not starts and not wire[1]


def test_bls_redirect_counts_each_exchange_and_rejects_external_before_request(
    inline_worker, tmp_path
):
    wire, starts = inline_worker
    control = CpiRequestControl(tmp_path / "shared.json", min_interval_seconds=0)
    wire[0].extend(
        [dict(status=302, headers={"Location": ICS}), dict(blocks=[b"BEGIN:VCALENDAR"])]
    )
    assert fetch_bls_page(ICS, request_control=control) == b"BEGIN:VCALENDAR"
    assert (
        len(starts) == len(json.loads(control.state_path.read_text())["requests"]) == 2
    )
    wire[0].append(dict(status=302, headers={"Location": "https://evil.example/"}))
    with pytest.raises(BlsWebError, match="redirect_rejected"):
        fetch_bls_page(ICS, request_control=control)
    assert len(starts) == 3


def test_dol_real_source_shared_429_exactly_once_and_does_not_retry(
    inline_worker, tmp_path
):
    from market_data_foundation.historical.claims_calendar import (
        CURRENT_REPORT_URL,
        PRIMARY_URL,
    )
    from market_data_foundation.historical.claims_contract import ClaimsError
    from market_data_foundation.historical.claims_material import material_fetcher

    wire, starts = inline_worker
    wire[0].append(dict(status=429, headers={"Retry-After": "3600"}, blocks=[b"busy"]))
    path = tmp_path / "dol.json"
    fetch = material_fetcher(tmp_path / "raw", path, live_opt_in=True)
    with pytest.raises(ClaimsError) as caught:
        fetch(PRIMARY_URL)
    assert caught.value.http_status == 429
    state = json.loads(path.read_text())
    assert len(starts) == len(state["requests"]) == state["consecutive_429"] == 1
    with pytest.raises(ClaimsError):
        fetch(CURRENT_REPORT_URL)
    assert len(starts) == 1


def test_dol_current_exact_query_free_route_retains_entity(inline_worker, tmp_path):
    from market_data_foundation.historical.claims_calendar import CURRENT_REPORT_URL
    from market_data_foundation.historical.claims_material import material_fetcher

    wire, starts = inline_worker
    raw = b"%PDF-1.7 synthetic exact entity\r\n"
    wire[0].append(dict(blocks=[raw], headers={"Content-Type": "application/pdf"}))
    fetch = material_fetcher(tmp_path / "raw", tmp_path / "control", live_opt_in=True)
    assert CURRENT_REPORT_URL == "https://www.dol.gov/ui/data.pdf"
    result = fetch(CURRENT_REPORT_URL)
    assert bytes(result) == raw and result.http_attempts == 1
    assert len(starts) == len(wire[1][0].calls) == 1
    url, options = wire[1][0].calls[0]
    assert url == CURRENT_REPORT_URL
    assert options["timeout"] == (5, 20)
    assert options["verify"] is True and options["allow_redirects"] is False
    assert next((tmp_path / "raw").glob("*.bin")).read_bytes() == raw


@pytest.mark.parametrize("suffix", ["?arbitrary=1", "?os=v1", "?os=v0&x=1", "/other"])
def test_dol_current_rejects_arbitrary_route_before_network(monkeypatch, suffix):
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no process/network")
    )
    with pytest.raises(web.PublicFetchError, match="url_invalid"):
        web.fetch_public("https://www.dol.gov/ui/data.pdf" + suffix, "dol")


def test_normal_api_transport_admission_protocol_stays_one_unchanged_call():
    from market_data_foundation.source_request_policy import admitted_exchange

    request = object()
    response = SimpleNamespace(status=200, headers={"X-API": "unchanged"})
    calls = []

    def transport(value):
        calls.append(value)
        return response

    ticket = SimpleNamespace(record_response=lambda *v: calls.append(v) or "retry")
    assert admitted_exchange(transport, request, ticket) == (response, "retry")
    assert calls == [request, (200, response.headers)]


@pytest.mark.parametrize("family", ["cpi", "ppi", "employment", "jolts"])
@pytest.mark.parametrize("reference", [False, True])
def test_real_default_bls_calendar_wiring_uses_requests_and_fixed_path(
    inline_worker, tmp_path, monkeypatch, family, reference
):
    from market_data_foundation.historical import jolts_release_schedule as jolts
    from market_data_foundation.historical.bls_timeseries_profile import (
        EMPLOYMENT_PROFILE,
    )
    from market_data_foundation.historical.cpi_release_schedule import (
        CpiReleaseScheduler,
    )
    from market_data_foundation.historical.ppi_release_schedule import (
        PpiReleaseScheduler,
    )

    if family == "jolts":
        monkeypatch.setattr(
            jolts, "DEFAULT_REQUEST_CONTROL_PATH", tmp_path / "shared.json"
        )
        scheduler = jolts.JoltsReleaseScheduler(tmp_path / "schedule.json")
    elif family == "ppi":
        scheduler = PpiReleaseScheduler(
            None, tmp_path / "schedule.json", history_start="2020-01"
        )
    else:
        options = {"profile": EMPLOYMENT_PROFILE} if family == "employment" else {}
        scheduler = CpiReleaseScheduler(
            None, tmp_path / "schedule.json", history_start="2020-01", **options
        )
    transport, starts = inline_worker
    transport[0].append(dict(blocks=[b"offline-original"]))
    fetch = scheduler.reference_fetcher if reference else scheduler.calendar_fetcher
    assert fetch() == b"offline-original"
    call = transport[1][0].calls[0]
    name = "empSit" if family == "employment" else family
    path = (name + ".htm") if reference else "bls.ics"
    assert call[0] == "https://www.bls.gov/schedule/news_release/" + path
    assert call[1]["verify"] is True and call[1]["allow_redirects"] is False
    assert call[1]["headers"]["Accept"] == (
        "text/html" if reference else "text/calendar"
    )
    assert len(starts) == 1


def test_ordinary_requests_verified_tls_has_no_legacy_advertisement_requirement():
    import ssl

    from urllib3.util.ssl_ import create_urllib3_context

    context = create_urllib3_context(cert_reqs=ssl.CERT_REQUIRED)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert context.minimum_version >= ssl.TLSVersion.TLSv1_2
    assert not any(
        cipher["name"] in {"NULL-MD5", "DES-CBC3-SHA", "RC4-MD5"}
        for cipher in context.get_ciphers()
    )


def test_nyfed_numeric_json_keeps_original_wire_and_no_public_child(
    inline_worker, monkeypatch
):
    from market_data_foundation.historical import financial_daily_origin as origin

    sent = []

    class Connection:
        def __init__(self, host, *, timeout):
            assert host == "markets.newyorkfed.org" and timeout == 30

        def request(self, *args, **kwargs):
            sent.append((args, kwargs))

        def getresponse(self):
            return self

        status = 200

        def getheader(self, name, default=None):
            return default

        def getheaders(self):
            return [("Content-Type", "application/json")]

        def read(self, limit):
            assert limit == 2_000_001
            return b'{"refRates":[]}'

        def close(self):
            pass

    monkeypatch.setattr(origin.http.client, "HTTPSConnection", Connection)
    url = "https://markets.newyorkfed.org/api/rates/unsecured/effr/search.json"
    response = origin.direct_transport(
        SimpleNamespace(
            url=url, parameters={"startDate": "2026-11-02", "endDate": "2026-11-03"}
        )
    )
    assert response.body == b'{"refRates":[]}'
    assert sent == [
        (
            (
                "GET",
                "/api/rates/unsecured/effr/search.json?startDate=2026-11-02&endDate=2026-11-03",
            ),
            {
                "headers": {
                    "Accept-Encoding": "identity",
                    "User-Agent": "MDF-DEV financial-conditions research",
                }
            },
        )
    ]
    assert not inline_worker[1]


@pytest.mark.parametrize("format", ["html", "ics", "pdf", "xml", "csv"])
def test_http200_denial_page_is_rejected_by_actual_format_parser(format):
    from market_data_foundation.historical.claims_calendar import CURRENT_REPORT_URL
    from market_data_foundation.historical.claims_contract import ClaimsError
    from market_data_foundation.historical.claims_release import parse_release
    from market_data_foundation.historical.cpi_release_schedule import (
        CpiScheduleError,
        parse_official_ics,
    )
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
    from market_data_foundation.historical.financial_daily import FinancialError
    from market_data_foundation.historical.financial_daily_credit import archive_points
    from market_data_foundation.historical.financial_daily_origin import points
    from market_data_foundation.historical.macro_release_material import parse_material

    raw = b"<html><h1>Access Denied</h1><p>Request not served</p></html>"
    error_types = (CpiTimeSeriesError, CpiScheduleError, ClaimsError, FinancialError)
    with pytest.raises(error_types):
        if format == "html":
            parse_material(
                raw,
                "https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026",
                "pce",
            )
        elif format == "ics":
            parse_official_ics(raw)
        elif format == "pdf":
            parse_release(raw, CURRENT_REPORT_URL)
        elif format == "xml":
            points(raw, "DGS10", "2010-01-04", "2010-01-05")
        else:
            archive_points(raw, "BAMLC0A0CM", "2026-03-01", "2026-03-17")


def test_unknown_child_cleanup_retains_prebooked_marker_and_recovery_guard(tmp_path):
    from datetime import UTC, datetime, timedelta

    at = [datetime(2026, 11, 11, tzinfo=UTC)]
    control = CpiRequestControl(
        tmp_path / "shared.json", clock=lambda: at[0], min_interval_seconds=0
    )
    with pytest.raises(web.PublicFetchError):
        with control.attempt():
            raise web.PublicFetchError("worker_cleanup_unknown")
    before = json.loads(control.state_path.read_text())
    assert len(before["requests"]) == 1 and before["in_flight_at"] is not None
    with pytest.raises(CpiRequestControlError, match="request_interrupted"):
        with control.attempt():
            pytest.fail("do not release an unknown exchange")
    recovered = json.loads(control.state_path.read_text())
    assert len(recovered["requests"]) == 1 and recovered["recovery_until"]
    at[0] += timedelta(seconds=31)
    with control.attempt():
        pass
    assert len(json.loads(control.state_path.read_text())["requests"]) == 2


@pytest.mark.parametrize("kind", ["dol", "origin", "archive", "m3", "retail"])
def test_adapter_unknown_cleanup_keeps_same_shared_ledger(kind, tmp_path, monkeypatch):
    from market_data_foundation.historical import claims_material as dol
    from market_data_foundation.historical import financial_daily_credit as credit
    from market_data_foundation.historical import financial_daily_origin as origin
    from market_data_foundation.historical import m3_release_acquisition as m3
    from market_data_foundation.historical import retail_release_scheduler as retail
    from market_data_foundation.source_request_policy import admitted_exchange

    def unknown(*_a, **_k):
        raise web.PublicFetchError("worker_cleanup_unknown")

    for module in [dol, origin, credit, m3, retail]:
        monkeypatch.setattr(module, "fetch_public", unknown)
    control = CpiRequestControl(tmp_path / "shared.json", min_interval_seconds=0)
    with pytest.raises(Exception) as caught:
        if kind == "m3":
            m3.fetch_m3_material(
                "https://www.census.gov/manufacturing/m3/prel/pdf/s-i-o.pdf", control
            )
        elif kind == "retail":
            retail.CensusControlledTransport(
                retail.CensusCalendarTransport(), control
            ).get()
        else:
            if kind == "dol":
                transport = dol.public_transport
                request = SimpleNamespace(url=dol.PRIMARY_URL)
            elif kind == "origin":
                transport = origin.direct_transport
                request = SimpleNamespace(url=origin.BOARD, parameters={})
            else:
                transport = credit.archive_transport
                request = SimpleNamespace(
                    url=credit.ARCHIVE_ROOT + next(iter(credit.FILES.values())),
                    parameters={},
                )
            with control.attempt() as ticket:
                admitted_exchange(transport, request, ticket)
    assert caught.value.request_may_be_in_flight
    state = json.loads(control.state_path.read_text())
    assert len(state["requests"]) == 1 and state["in_flight_at"] is not None


@pytest.mark.parametrize(
    "kind,route,timeout,accept,limit",
    [
        ("bea_ics", "bea_calendar", 15, "text/calendar", 2_000_000),
        ("bea_html", "bea_calendar", 15, "text/html", 2_000_000),
        ("pio", "bea_material", 20, "text/html", 2_000_000),
        ("retail_article", "retail_material", 20, "text/html", 2_000_000),
        ("retail_current", "retail_calendar", 20, "text/html", 2_000_000),
        ("retail_annual", "retail_calendar", 20, "text/html", 2_000_000),
        ("m3_calendar", "m3_calendar", 20, "text/html", 2_000_000),
        ("m3_pdf", "m3_pdf", 10, "application/pdf", 2_000_000),
        ("dol_html", "dol", 12, "text/html", 8 * 1024 * 1024),
        ("dol_pdf", "dol", 150, "application/pdf", 8 * 1024 * 1024),
        ("board", "board", 30, "text/html", 2_000_000),
        ("treasury", "treasury", 30, "application/xml", 2_000_000),
        ("archive", "credit_archive", 30, "text/csv", 2_000_000),
    ],
)
def test_actual_document_adapters_preserve_limits_formats_and_request_identity(
    inline_worker, tmp_path, kind, route, timeout, accept, limit
):
    from market_data_foundation.historical import claims_material as dol
    from market_data_foundation.historical import financial_daily_credit as credit
    from market_data_foundation.historical import financial_daily_origin as origin
    from market_data_foundation.historical import (
        m3_calendar,
    )
    from market_data_foundation.historical import (
        m3_release_acquisition as m3,
    )
    from market_data_foundation.historical import macro_release_acquisition as material
    from market_data_foundation.historical import pce_release_schedule as bea
    from market_data_foundation.historical import retail_release_scheduler as retail

    wire, starts = inline_worker
    raw = b"original entity byte-for-byte"
    wire[0].append(dict(blocks=[raw]))
    control = CpiRequestControl(tmp_path / "shared.json", min_interval_seconds=0)
    if kind == "bea_ics":
        reply = bea.fetch_official_calendar()
    elif kind == "bea_html":
        reply = bea.fetch_official_html()
    elif kind == "pio":
        reply = material.fetch_official_material(
            "https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026",
            "pce",
        )
    elif kind == "retail_article":
        reply = material.fetch_official_material(
            "https://www.census.gov/retail/sales.html", "retail"
        )
    elif kind == "retail_current":
        reply = retail.calendar_fetcher(control)()
    elif kind == "retail_annual":
        reply = retail.calendar_fetcher(control)(2026)
    elif kind == "m3_calendar":
        reply = m3_calendar.fetch_calendar(control)
    elif kind == "m3_pdf":
        reply = m3.fetch_m3_material(
            "https://www.census.gov/manufacturing/m3/prel/pdf/s-i-o.pdf", control
        )
    elif kind.startswith("dol"):
        url = dol.CURRENT_REPORT_URL if kind == "dol_pdf" else dol.PRIMARY_URL
        reply = dol.public_transport(SimpleNamespace(url=url)).body
    elif kind == "board":
        reply = origin.direct_transport(
            SimpleNamespace(url=origin.BOARD, parameters={})
        ).body
    elif kind == "treasury":
        reply = origin.direct_transport(
            SimpleNamespace(
                url=origin.TREASURY,
                parameters={
                    "data": "daily_treasury_yield_curve",
                    "field_tdr_date_value_month": "202610",
                },
            )
        ).body
    else:
        reply = credit.archive_transport(
            SimpleNamespace(
                url=credit.ARCHIVE_ROOT + next(iter(credit.FILES.values())),
                parameters={},
            )
        ).body
    assert reply == raw and len(starts) == 1
    args = starts[0]
    assert (
        args[4] == route
        and int(args[7]) == limit
        and float(args[8]) == timeout
        and args[9] == accept
    )
    assert wire[1][0].calls[0][1]["verify"] is True


@pytest.mark.parametrize("route", ["bea", "bls"])
def test_unknown_download_outcome_does_not_trigger_calendar_fallback(
    monkeypatch, route
):
    from market_data_foundation.historical import bls_document_page as bls
    from market_data_foundation.historical import pce_release_schedule as bea
    from market_data_foundation.historical.calendar_resilience import may_fallback
    from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError

    def unknown(*_a, **_k):
        raise web.PublicFetchError("worker_cleanup_unknown")

    monkeypatch.setattr(bea if route == "bea" else bls, "fetch_public", unknown)
    with pytest.raises((CpiScheduleError, BlsWebError)) as caught:
        bea.fetch_official_calendar() if route == "bea" else bls.fetch_bls_page(ICS)
    assert "request_outcome_unknown" in caught.value.classification
    assert not may_fallback(caught.value)


@pytest.mark.parametrize("elapsed", [58.572, 70.653])
def test_claims_pdf_slow_complete_transfer_preserves_full_entity_and_admission(
    inline_worker, tmp_path, monkeypatch, elapsed
):
    from market_data_foundation.historical.claims_calendar import CURRENT_REPORT_URL
    from market_data_foundation.historical.claims_material import material_fetcher

    wire, starts = inline_worker
    raw = b"%PDF-1.7 synthetic full original\r\n" * 1000
    wire[0].append(dict(blocks=[raw[:9000], raw[9000:]]))
    clock = [1000.0]
    original = web.read_entity

    def delayed(response, max_bytes):
        body = original(response, max_bytes)
        clock[0] += elapsed
        return body

    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(web, "read_entity", delayed)
    control = tmp_path / "control.json"
    fetch = material_fetcher(tmp_path / "raw", control, live_opt_in=True)
    result = fetch(CURRENT_REPORT_URL)
    assert bytes(result) == raw and result.http_attempts == 1
    assert len(starts) == len(json.loads(control.read_text())["requests"]) == 1
    assert json.loads(control.read_text())["in_flight_at"] is None
    assert wire[1][0].calls[0][1]["timeout"] == (5, 20)


@pytest.mark.parametrize(
    "url,route,accept",
    [
        (ICS, "bls_web", "text/calendar"),
        ("https://oui.doleta.gov/unemploy/archive.asp", "dol", "text/html"),
        ("https://oui.doleta.gov/unemploy/claims_arch.asp", "dol", "text/html"),
        ("https://www.dol.gov/ui/data.pdf?os=v0", "dol", "application/pdf"),
        ("https://www.dol.gov/ui/data.pdf", "dol", "text/html"),
        (
            "https://www.census.gov/manufacturing/m3/prel/pdf/s-i-o.pdf",
            "m3_pdf",
            "application/pdf",
        ),
        ("https://www.bea.gov/news/schedule/full", "bea_calendar", "text/html"),
    ],
)
@pytest.mark.parametrize("timeout", [30.01, 120, 150])
def test_long_timeout_rejected_for_every_other_registered_document(
    monkeypatch, url, route, accept, timeout
):
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no process/network")
    )
    with pytest.raises(web.PublicFetchError, match="request_limits_invalid"):
        web.fetch_public(url, route, accept=accept, timeout=timeout)


@pytest.mark.parametrize("timeout", [150.01, float("inf"), float("nan"), True])
def test_claims_pdf_rejects_unbounded_or_excessive_deadline(monkeypatch, timeout):
    monkeypatch.setattr(
        web.subprocess, "Popen", lambda *_a, **_k: pytest.fail("no process/network")
    )
    with pytest.raises(web.PublicFetchError, match="request_limits_invalid"):
        web.fetch_public(
            "https://www.dol.gov/ui/data.pdf",
            "dol",
            accept="application/pdf",
            timeout=timeout,
        )


@pytest.mark.parametrize(
    "url,route,accept,timeout",
    [
        (ICS, "bls_web", "text/calendar", 30),
        ("https://www.dol.gov/ui/data.pdf", "dol", "application/pdf", 150),
    ],
)
def test_slow_child_cannot_extend_document_absolute_deadline(
    monkeypatch, url, route, accept, timeout
):
    clock, children = [1000.0], []

    class SlowChild:
        killed = False
        returncode = None

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            if self.killed:
                self.returncode = -9
                return self.returncode
            clock[0] += timeout
            raise subprocess.TimeoutExpired("synthetic slow document", timeout)

        def kill(self):
            self.killed = True

    def start(*_a, **_k):
        child = SlowChild()
        children.append(child)
        return child

    monkeypatch.setattr(web.subprocess, "Popen", start)
    monkeypatch.setattr(web.time, "monotonic", lambda: clock[0])
    with pytest.raises(web.PublicFetchError, match="timeout"):
        web.fetch_public(url, route, accept=accept, timeout=timeout)
    assert clock[0] - 1000 == pytest.approx(timeout)
    assert len(children) == 1 and children[0].killed
    assert children[0].poll() == -9


def test_claims_pdf_long_transfer_keeps_redirect_rejection(inline_worker):
    from market_data_foundation.historical.claims_contract import ClaimsError
    from market_data_foundation.historical.claims_material import public_transport

    wire, starts = inline_worker
    wire[0].append(dict(status=302, headers={"Location": ICS}))
    with pytest.raises(ClaimsError, match="claims_dol_redirect_rejected"):
        public_transport(SimpleNamespace(url="https://www.dol.gov/ui/data.pdf"))
    assert len(starts) == len(wire[1][0].calls) == 1
    assert wire[1][0].calls[0][1]["allow_redirects"] is False
