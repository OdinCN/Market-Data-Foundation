"""Explicit public DEV reads never confer collection or configuration writes."""

import pytest
from fastapi import Request

from market_data_foundation.web.cpi_timeseries_api import (
    admitted_job,
    admitted_strategy,
)
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, ORIGIN, admitted
from market_data_foundation.web.readonly_host import admitted_package_save

PUBLIC = "http://odin.pub:18761"


def request(
    method="GET", *, host="odin.pub:18761", peer="203.0.113.8", path="/pce", headers=()
):
    return Request(
        {
            "type": "http",
            "method": method,
            "path": path,
            "scheme": "http",
            "query_string": b"",
            "client": (peer, 12345),
            "headers": [(b"host", host.encode()), *headers],
        }
    )


@pytest.fixture(autouse=True)
def origin_disabled(monkeypatch):
    monkeypatch.delenv("MDF_DEV_READONLY_ORIGIN", raising=False)


def test_default_remains_lan_only():
    assert not admitted(request(), origin=CANDIDATE_ORIGIN)
    assert admitted(
        request(host="192.168.10.20:18761", peer="192.168.10.25"),
        origin=CANDIDATE_ORIGIN,
    )


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("peer", ["203.0.113.8", "192.168.10.25", "2001:db8::1"])
def test_configured_public_origin_reads(monkeypatch, method, peer):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", PUBLIC)
    assert admitted(request(method, peer=peer), origin=CANDIDATE_ORIGIN)
    assert admitted(
        request(method, peer=peer, headers=[(b"origin", PUBLIC.encode())]),
        origin=CANDIDATE_ORIGIN,
    )
    assert not admitted(request(method, peer=peer), origin=ORIGIN)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "*",
        "https://odin.pub:18761",
        "http://odin.pub",
        "http://odin.pub:18760",
        "http://u:p@odin.pub:18761",
        "http://odin.pub:18761/path",
        "http://odin.pub:18761?x=1",
        "http://odin.pub:18761#x",
        "http://odin.pub:bad",
    ],
)
def test_invalid_configuration_fails_closed(monkeypatch, value):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", value)
    assert not admitted(request(), origin=CANDIDATE_ORIGIN)


@pytest.mark.parametrize(
    "headers",
    [
        [(b"origin", CANDIDATE_ORIGIN.encode())],
        [(b"origin", b"http://evil.example")],
        [(b"origin", PUBLIC.encode()), (b"origin", PUBLIC.encode())],
        [(b"host", b"odin.pub:18761")],
        [(b"forwarded", b"for=127.0.0.1")],
        [(b"x-forwarded-for", b"127.0.0.1")],
        [(b"authorization", b"Bearer test")],
        [(b"x-user", b"operator")],
        [(b"sec-fetch-site", b"cross-site")],
    ],
)
def test_public_origin_keeps_header_guards(monkeypatch, headers):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", PUBLIC)
    assert not admitted(request(headers=headers), origin=CANDIDATE_ORIGIN)


@pytest.mark.parametrize(
    "host", ["other.example:18761", "odin.pub:18760", "192.168.10.20:18761"]
)
def test_public_peer_cannot_use_other_hosts(monkeypatch, host):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", PUBLIC)
    assert not admitted(request(host=host), origin=CANDIDATE_ORIGIN)


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS"])
def test_public_methods_remain_readonly(monkeypatch, method):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", PUBLIC)
    assert not admitted(request(method), origin=CANDIDATE_ORIGIN)


@pytest.mark.parametrize("origin", [PUBLIC, CANDIDATE_ORIGIN])
@pytest.mark.parametrize(
    "family", ["cpi", "ppi", "pce", "employment", "retail", "consumption"]
)
def test_write_adapters_cannot_reinterpret_public_get(monkeypatch, origin, family):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", PUBLIC)
    headers = [(b"origin", origin.encode()), (b"content-type", b"application/json")]
    prefix = f"/api/r5/{family}/timeseries"
    assert not admitted_job(
        request("POST", path=prefix + "/jobs", headers=headers), prefix=prefix
    )
    assert not admitted_strategy(
        request("PUT", path=prefix + "/strategy", headers=headers), prefix=prefix
    )
    assert not admitted_package_save(
        request("PUT", path="/api/r5/packages/macro", headers=headers)
    )


def test_existing_lan_writes_stay_available(monkeypatch):
    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", PUBLIC)
    headers = [
        (b"origin", CANDIDATE_ORIGIN.encode()),
        (b"content-type", b"application/json"),
    ]
    options = dict(host="192.168.10.20:18761", peer="192.168.10.25", headers=headers)
    assert admitted_job(request("POST", path="/api/r5/cpi/timeseries/jobs", **options))
    assert admitted_strategy(
        request("PUT", path="/api/r5/cpi/timeseries/strategy", **options)
    )
    assert admitted_package_save(
        request("PUT", path="/api/r5/packages/macro", **options)
    )
