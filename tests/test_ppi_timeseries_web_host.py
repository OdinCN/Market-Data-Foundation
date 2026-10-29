"""DEV host binds PPI separately and keeps unconfigured PPI lazy."""

from unittest.mock import Mock

import pytest
from fastapi import Request

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_readonly_host as old
from tests.test_cpi_timeseries_web import NOW, command, no_network  # noqa: F401
from tests.test_ppi_timeseries_web import Source, client


def setup(tmp_path, monkeypatch, *, configure=True, **options):
    _, previous, connections, _, _, _ = old.setup(
        tmp_path, monkeypatch, enabled_caps=("cpi", "ppi")
    )
    sources, stores = {}, {}
    factories = {}
    for profile in (CPI_PROFILE, PPI_PROFILE):
        source = Source(profile)
        store = InMemoryTimeSeriesStore(profile=profile, clock=lambda: NOW)

        def read(store=store, source=source, profile=profile):
            return CpiTimeSeriesService(store, source, profile=profile)

        def bound(*, credential_loader, read=read):
            credential_loader()
            return read()

        sources[profile.key], stores[profile.key] = source, store
        factories[profile.key] = (read, bound)
    if configure:
        options = {
            "ppi_read_service_factory": factories["ppi"][0],
            "ppi_write_service_factory": factories["ppi"][1],
            "ppi_query_service_factory": factories["ppi"][1],
            "ppi_allow_collect": True,
            "ppi_schedule_path": tmp_path / "ppi" / "schedule.json",
            "ppi_job_directory": tmp_path / "ppi" / "jobs",
            "ppi_history_start": PPI_PROFILE.earliest_month or "2020-01",
            "ppi_access": old.ALLOW,
            **options,
        }
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=previous.clock,
        cpi_access=old.ALLOW,
        pce_access=old.ALLOW,
        schedule_path=tmp_path / "cpi" / "schedule.json",
        job_directory=tmp_path / "cpi" / "jobs",
        read_service_factory=factories["cpi"][0],
        **options,
    )
    return app, app.state.timeseries_runtime, sources, stores, connections


def test_runtime_initialization_has_no_storage_or_provider_side_effect(
    tmp_path, monkeypatch
):
    _, runtime, sources, stores, connections = setup(tmp_path, monkeypatch)
    assert runtime.ppi_timeseries.scheduler.profile is PPI_PROFILE
    assert runtime.ppi_timeseries.scheduler.service.profile is PPI_PROFILE
    assert runtime.ppi_timeseries.profile is PPI_PROFILE
    assert runtime.timeseries.profile is CPI_PROFILE
    assert sources["cpi"].calls == sources["ppi"].calls == 0
    assert stores["ppi"].status()["observation_count"] == 0
    assert connections == []
    assert not runtime.ppi_timeseries.scheduler.state_path.exists()
    assert not runtime.ppi_timeseries.journal.directory.exists()


def test_runtime_ppi_holding_and_projection_use_ppi_store(tmp_path, monkeypatch):
    _, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    ppi = runtime.ppi_timeseries
    assert ppi.submit(command())["state"] == "complete"
    history = runtime.history(runtime.settings.environment_id, "macro", None)
    definitions = {
        c.capability_id: c.definition_ref
        for c in runtime.packages.catalog.package("macro").capabilities
    }
    evidence = {row.definition_ref.identity: row for row in history.history}
    assert evidence[definitions["ppi"].identity].holding == "present"
    assert evidence[definitions["cpi"].identity].holding == "absent"
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/r5/packages/macro",
            "scheme": "http",
            "query_string": b"",
            "client": ("192.168.10.25", 1234),
            "headers": [(b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode())],
        }
    )
    view = runtime.packages.view(request, "macro")
    assets = {a.identity: a for a in view.assets}
    ppi_view = assets[definitions["ppi"].identity].revisions[0]
    assert ppi_view.local_holding == "PRESENT"
    assert ppi_view.local_read.allowed is True
    assert ppi_view.collect.allowed is True


def test_unconfigured_ppi_default_is_lazy(tmp_path, monkeypatch):
    _, runtime, sources, _, connections = setup(tmp_path, monkeypatch, configure=False)
    assert runtime.ppi_timeseries is None
    assert runtime.accesses["ppi"] == CpiQueryAccess()
    assert sources["ppi"].calls == 0 and connections == []
    assert set(runtime.packages.timeseries_hosts) == {"cpi"}


def test_runtime_rejects_shared_business_paths(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="ppi_independent_paths_required"):
        setup(tmp_path, monkeypatch, ppi_job_directory=tmp_path / "cpi" / "jobs")


def test_default_ppi_read_factory_binds_separate_schema_without_source(
    tmp_path, monkeypatch
):
    _, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    connection = Mock(autocommit=True)
    runtime.connector = Mock(return_value=connection)
    service = runtime.ppi_read_service()
    assert service.profile is PPI_PROFILE and service.store.profile is PPI_PROFILE
    assert service.store.raw_store is None
    assert service.store.connection is connection
    assert runtime.connector.call_args.kwargs["options"].endswith(
        "default_transaction_read_only=on"
    )
    service.close()
    connection.close.assert_called_once()


def test_asgi_runtime_ppi_and_cpi_share_host_but_not_holding(tmp_path, monkeypatch):
    app, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    runtime.ppi_timeseries.submit(command())
    with client(app) as http:
        ppi = http.get("/api/r5/ppi/timeseries/query?start=2026-09&end=2026-09")
        cpi = http.get("/api/r5/cpi/timeseries/query?start=2026-09&end=2026-09")
        assert ppi.status_code == cpi.status_code == 200
        assert ppi.json()["points"] and cpi.json()["points"] == []
        assert (
            http.get("/api/r5/ppi/timeseries/status").json()["schema"]
            == PPI_PROFILE.schema
        )
        assert http.get("/api/r5/packages/macro").status_code == 200
        assert (
            http.request(
                "GET", "/api/r5/ppi/timeseries/status", content=b"{}"
            ).status_code
            == 400
        )


def test_runtime_factories_share_budget_and_bind_capability_profiles(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace

    from market_data_foundation.web import cpi_timeseries_host as module
    from market_data_foundation.web.package_catalog import SaveCommand

    _, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    captured = []

    def build(**options):
        captured.append(options)
        return SimpleNamespace(
            profile=options["profile"], source=None, close=lambda: None
        )

    monkeypatch.setattr(module, "build_source", build)
    monkeypatch.setattr(module, "build_service", build)
    monkeypatch.setattr(
        runtime,
        "read_service",
        lambda **_: SimpleNamespace(source=None, close=lambda: None),
    )
    runtime.query_service(credential_loader=runtime.timeseries.effective_credential)
    runtime.ppi_query_service(
        credential_loader=runtime.ppi_timeseries.effective_credential
    )
    runtime.ppi_write_service(
        credential_loader=runtime.ppi_timeseries.effective_credential
    )
    assert [entry["profile"] for entry in captured] == [
        CPI_PROFILE,
        PPI_PROFILE,
        PPI_PROFILE,
    ]
    assert all(
        entry["request_control"] is runtime.request_control for entry in captured
    )
    assert all(entry["max_transport_attempts"] == 1 for entry in captured)
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=["cpi"]),
    )
    with pytest.raises(Exception, match="ppi_capability_disabled"):
        captured[1]["credential_loader"]()
    assert captured[0]["credential_loader"]() == "synthetic-bls-key"


def test_handler_enforces_ppi_write_boundaries(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from mdf_cpi_timeseries_web import TimeSeriesHandler

    handler = object.__new__(TimeSeriesHandler)
    for method, suffix in (("POST", "jobs"), ("PUT", "strategy")):
        scope = {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": "/api/r5/ppi/timeseries/" + suffix,
            "query_string": b"",
            "client": ("192.168.10.25", 1234),
            "headers": [
                (b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode()),
                (b"origin", CANDIDATE_ORIGIN.encode()),
                (b"content-type", b"application/json"),
            ],
        }
        assert handler.request_admitted(Request(scope))
        for changes in (
            {"path": "/api/r5/ppi/timeseries/other"},
            {"query_string": b"bypass=1"},
            {
                "headers": [
                    (k, b"http://evil.example" if k == b"origin" else v)
                    for k, v in scope["headers"]
                ]
            },
            {
                "headers": [
                    (k, b"text/plain" if k == b"content-type" else v)
                    for k, v in scope["headers"]
                ]
            },
            {"headers": [*scope["headers"], (b"transfer-encoding", b"chunked")]},
        ):
            assert not handler.request_admitted(Request(dict(scope, **changes)))
