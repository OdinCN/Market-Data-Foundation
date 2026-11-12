"""The 18766 composition uses existing exact gates and the same durable jobs."""

import importlib
import io
from email.message import Message
from pathlib import Path
from threading import Event
from types import SimpleNamespace

import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from market_data_foundation.web.cpi_timeseries_api import PREFIX
from market_data_foundation.web.package_catalog import SaveCommand
from market_data_foundation.web.pce_dev_host import UNIFIED_DEV_ORIGIN
from tests.test_cpi_timeseries_web import command, no_network  # noqa: F401
from tests.test_cpi_timeseries_web_host import setup

ORIGIN = UNIFIED_DEV_ORIGIN


def handler_for(monkeypatch, app, origin=ORIGIN):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("mdf_cpi_timeseries_web")
    handler = object.__new__(module.TimeSeriesHandler)
    handler.origin = origin
    handler.listener = ("192.168.10.20", int(origin.rsplit(":", 1)[1]))
    handler.server = SimpleNamespace(app=app)
    return handler


def request(path, method="POST", *, origin=ORIGIN, peer="192.168.10.25", extra=()):
    return Request(
        dict(
            type="http",
            method=method,
            path=path,
            query_string=b"",
            scheme="http",
            client=(peer, 1234),
            headers=[
                (b"host", origin.removeprefix("http://").encode()),
                (b"origin", origin.encode()),
                (b"content-type", b"application/json"),
                *extra,
            ],
        )
    )


MUTATIONS = [
    *(
        (f"/api/r5/{asset}/timeseries/jobs", "POST")
        for asset in (
            "cpi",
            "ppi",
            "pce",
            "consumption",
            "employment",
            "retail",
            "gdp",
            "claims",
            "jolts",
            "m3",
            "financial",
        )
    ),
    *(
        (f"/api/r5/{asset}/timeseries/strategy", "PUT")
        for asset in (
            "cpi",
            "ppi",
            "pce",
            "consumption",
            "employment",
            "retail",
            "gdp",
            "claims",
            "jolts",
            "m3",
        )
    ),
    *(
        (f"/api/r5/{asset}/timeseries/worker", "POST")
        for asset in (
            "cpi",
            "ppi",
            "pce",
            "consumption",
            "employment",
            "retail",
            "gdp",
            "claims",
            "jolts",
            "m3",
        )
    ),
    ("/api/r5/financial/timeseries/strategy", "POST"),
    *(
        (f"/api/r5/{asset}/timeseries/jobs/resume", "POST")
        for asset in (
            "retail",
            "m3",
            "financial",
        )
    ),
    ("/api/r5/packages/macro", "PUT"),
    ("/api/r5/packages/macro/sources/BLS/verify", "POST"),
]


@pytest.mark.parametrize("path,method", MUTATIONS)
def test_socket_gate_requires_explicit_composition_and_real_authority(
    tmp_path, monkeypatch, path, method
):
    app, *_ = setup(
        tmp_path,
        monkeypatch,
        dev_origin=ORIGIN,
        allow_unified_dev_macro_operations=True,
    )
    handler = handler_for(monkeypatch, app)
    scope = request(path, method)
    assert handler.request_admitted(scope)
    assert scope.headers["origin"] == ORIGIN  # No Origin rewriting.
    app.state.mdf_unified_dev_macro_operations = False
    assert not handler.request_admitted(scope)
    app.state.mdf_unified_dev_macro_operations = True
    for peer, extra in [
        ("203.0.113.4", ()),
        ("192.168.10.25", ((b"forwarded", b"host=x"),)),
        ("192.168.10.25", ((b"origin", ORIGIN.encode()),)),
        ("192.168.10.25", ((b"authorization", b"test"),)),
        ("192.168.10.25", ((b"sec-fetch-site", b"cross-site"),)),
        ("192.168.10.25", ((b"transfer-encoding", b"chunked"),)),
    ]:
        probe = request(path, method, peer=peer, extra=extra)
        assert not handler.request_admitted(probe)
    for change in [
        dict(query_string=b"x=1"),
        dict(path=path + "/"),
        dict(method="DELETE"),
        dict(method="PATCH"),
    ]:
        assert not handler.request_admitted(Request(dict(scope.scope, **change)))
    other = "http://192.168.10.20:18767"
    app.state.mdf_dev_read_origin = other
    assert not handler_for(monkeypatch, app, other).request_admitted(
        request(path, method, origin=other)
    )


@pytest.mark.parametrize(
    "origin",
    [
        "http://192.168.10.20:18761",
        "http://192.168.10.20:18767",
        "http://public.example:18766",
    ],
)
def test_opt_in_cannot_select_other_listener(tmp_path, monkeypatch, origin):
    with pytest.raises(ValueError, match="unified_dev_macro_operations_require_18766"):
        setup(
            tmp_path,
            monkeypatch,
            dev_origin=origin,
            allow_unified_dev_macro_operations=True,
        )


def test_18766_http_dispatch_persists_real_isolated_job_and_retains_capability_gate(
    tmp_path, monkeypatch
):
    app, runtime, _, _, _, _, source = setup(
        tmp_path,
        monkeypatch,
        dev_origin=ORIGIN,
        allow_unified_dev_macro_operations=True,
        allow_collect=True,
    )
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(
            expected_revision=record.revision,
            enabled_capability_ids=["cpi"],
            source_changes=[
                dict(
                    source_id="BLS",
                    credential_action="replace",
                    credential_value="a" * 32,
                )
            ],
        ),
        allow_replace=True,
    )
    module = importlib.import_module("market_data_foundation.web.cpi_timeseries_host")
    monkeypatch.setattr(
        module, "build_service", lambda **_: runtime.timeseries.read_factory()
    )
    finished = Event()
    original = runtime.timeseries.submit

    def submit(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.timeseries, "submit", submit)
    import json

    submitted = command()
    body = json.dumps(submitted).encode()
    handler = handler_for(monkeypatch, app)
    handler.path, handler.command = PREFIX + "/jobs", "POST"
    handler.client_address = ("192.168.10.25", 1234)
    handler.headers = Message()
    for key, value in request(handler.path).scope["headers"]:
        handler.headers[key.decode()] = value.decode()
    handler.headers["Content-Length"] = str(len(body))
    handler.rfile = io.BytesIO(body)
    captured = []
    handler.respond = lambda status, body=b"", *args: captured.append((status, body))
    handler.dispatch()  # Actual socket adapter -> ASGI -> original durable host.
    assert captured[0][0] in {200, 202}, captured
    assert finished.wait(10)
    with TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234)) as http:
        jobs = http.get(PREFIX + "/jobs").json()["jobs"]
        assert len(jobs) == 1 and jobs[0]["state"] == "complete"
        assert jobs[0]["request_id"] == submitted["request_id"]
        runtime.timeseries.allow_collect = False
        denied = http.post(
            PREFIX + "/jobs",
            json={**command(), "request_id": "d9968de0-ec49-41a6-8dda-e03ad626060e"},
            headers={"Origin": ORIGIN},
        )
        assert denied.status_code == 403
        for path in [
            "/api/pce/jobs",
            PREFIX + "/schedule/due",
            "/api/r5/unknown/timeseries/jobs",
        ]:
            assert (
                http.post(path, json={}, headers={"Origin": ORIGIN}).status_code == 405
            )
    assert source.calls == 1
    assert runtime.timeseries.journal.directory.is_relative_to(tmp_path)


def test_financial_write_and_resume_use_the_same_isolated_durable_host(
    tmp_path, monkeypatch
):
    from market_data_foundation.web import financial_daily_api as api
    from market_data_foundation.web import financial_daily_jobs as jobs_module
    from market_data_foundation.web.financial_daily_api import FinancialOptions
    from tests.test_financial_daily_jobs import (
        END,
        START,
        Raw,
        payload,
    )
    from tests.test_financial_daily_jobs import (
        command as financial_command,
    )

    calls = []

    class Source:
        request_count = 0

        def fetch(self, sid, *_args, **_kwargs):
            calls.append(sid)
            self.request_count += 1
            return payload(sid)

    store = SimpleNamespace(
        raw_store=Raw(),
        close=lambda: None,
        record_attempt=lambda *a, **k: None,
        save=lambda batch: dict(batch_id=batch.batch_id, saved_points=3),
    )
    hosts = []
    finished = Event()
    original_host = api.FinancialJobs

    def host(directory):
        result = original_host(
            directory,
            service_factory=lambda **_: SimpleNamespace(source=Source(), store=store),
        )
        submit = result.submit

        def finish(*args, **kwargs):
            try:
                return submit(*args, **kwargs)
            finally:
                finished.set()

        result.submit = finish
        hosts.append(result)
        return result

    monkeypatch.setattr(api, "FinancialJobs", host)
    monkeypatch.setattr(jobs_module, "coverage", lambda *a, **k: dict(series=[]))
    monkeypatch.setattr(
        jobs_module,
        "plan",
        lambda _: [
            dict(
                series_id="DGS2", start=START, end=END, source="origin", state="pending"
            )
        ],
    )
    app, *_ = setup(
        tmp_path,
        monkeypatch,
        dev_origin=ORIGIN,
        allow_unified_dev_macro_operations=True,
        financial_options=FinancialOptions(tmp_path / "financial-jobs", True, True),
    )
    assert len(hosts) == 1
    submitted = financial_command()
    with TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234)) as http:
        created = http.post(
            api.PREFIX + "/jobs", json=submitted, headers={"Origin": ORIGIN}
        )
        assert created.status_code == 200
        assert created.json()["job"]["request_id"] == submitted["request_id"]
        assert finished.wait(10)
        # Inspect through the original route/journal, without another executor.
        assert (
            http.get(api.PREFIX + "/jobs").json()["jobs"][0]["request_id"]
            == submitted["request_id"]
        )
        resumed = http.post(
            api.PREFIX + "/jobs/resume",
            json={"request_id": submitted["request_id"]},
            headers={"Origin": ORIGIN},
        )
        assert resumed.status_code == 200
        assert resumed.json()["job"]["request_id"] == submitted["request_id"]
        assert (
            http.get(api.PREFIX + "/jobs/" + submitted["request_id"]).status_code == 200
        )
    assert calls == ["DGS2"]
    assert hosts[0].journal.directory.is_relative_to(tmp_path)


def test_opt_in_does_not_grant_capabilities_package_admin_or_public_writes(
    tmp_path, monkeypatch
):
    from market_data_foundation.web.financial_daily_api import FinancialOptions

    monkeypatch.setenv("MDF_DEV_READONLY_ORIGIN", "http://review.example:18761")
    app, runtime, *_ = setup(
        tmp_path,
        monkeypatch,
        dev_origin=ORIGIN,
        allow_unified_dev_macro_operations=True,
        financial_options=FinancialOptions(tmp_path / "financial", True, False),
    )
    with TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234)) as http:
        for path in [PREFIX + "/jobs", "/api/r5/financial/timeseries/jobs"]:
            denied = http.post(path, json=command(), headers={"Origin": ORIGIN})
            assert denied.status_code == 403
            denied = http.post(
                path,
                json=command(),
                headers={
                    "Host": "review.example:18761",
                    "Origin": "http://review.example:18761",
                },
            )
            assert denied.status_code in {403, 405}
        # The opt-in is independent of package configuration authorization.
        assert not runtime.settings.package_config_save
        assert (
            http.put(
                "/api/r5/packages/macro", json={}, headers={"Origin": ORIGIN}
            ).status_code
            == 405
        )
    assert not runtime.timeseries.journal.directory.exists()
    assert not (tmp_path / "financial").exists()
