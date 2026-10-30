"""Synthetic PCE API, credentials, runtime and worker isolation contracts."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI, Request

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesService,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.pce_release_schedule import PceReleaseScheduler
from market_data_foundation.web.cpi_timeseries_api import (
    admitted_job,
    admitted_strategy,
    timeseries_router,
)
from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.package_catalog import (
    CredentialState,
    PackageCatalog,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from market_data_foundation.web.pce_timeseries import PceTimeSeriesHost
from tests import test_readonly_host as old
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_pce_release_schedule import NOW, ics
from tests.test_ppi_timeseries_web import client

PREFIX = "/api/r5/pce/timeseries"


class Source:
    def __init__(self):
        self.calls = 0
        self.skip = set()

    def fetch(self, series, start, end):
        assert set(series) <= set(PCE_PROFILE.series_ids)
        self.calls += 1
        return (
            SourceBatch(
                tuple(
                    SourcePoint(s, m, Decimal(100 + int(m[-2:])), {}, ())
                    for s in series
                    for m in month_range(start, end)
                    if (s, m) not in self.skip
                ),
                b"synthetic-bea-index-fixture",
                {"datasetname": "NIPA", "TableName": "T20804", "Frequency": "M"},
                NOW,
                1,
            ),
        )


def command(action="initialize"):
    return {
        "request_id": str(uuid4()),
        "action": action,
        "start": "2026-09",
        "end": "2026-09",
    }


@pytest.fixture
def fixture(tmp_path):
    store = LocalPackageStore.initialize(tmp_path / "packages", PackageCatalog())
    store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["pce"]),
    )
    loaded = []

    def credential_loader(_environment, _package, source):
        loaded.append(source)
        assert source == "BEA"
        return "synthetic-bea-key-not-public"

    packages = PackageHost(
        store,
        "fixture",
        credentials=lambda *_: [
            CredentialState(source_id="BEA", credential_state="configured")
        ],
        credential_loader=credential_loader,
    )
    data, source = (
        InMemoryTimeSeriesStore(profile=PCE_PROFILE, clock=lambda: NOW),
        Source(),
    )

    def read():
        return CpiTimeSeriesService(data, source, profile=PCE_PROFILE)

    def bound(*, credential_loader):
        credential_loader()
        return read()

    host = PceTimeSeriesHost(
        packages=packages,
        read_service_factory=read,
        write_service_factory=bound,
        query_service_factory=bound,
        allow_collect=True,
        scheduler=PceReleaseScheduler(
            None,
            tmp_path / "pce" / "schedule.json",
            clock=lambda: NOW,
            history_start="2006-10",
            calendar_fetcher=None,
        ),
        job_directory=tmp_path / "pce" / "jobs",
        clock=lambda: NOW,
    )
    app = FastAPI()
    app.include_router(timeseries_router(host))
    return app, host, source, data, loaded


def test_help_is_bea_two_sa_indexes_and_not_a_key(fixture):
    _, host, _, _, loaded = fixture
    help = host.api_help()
    assert help["source_id"] == "BEA"
    assert help["series_ids"] == ["DPCERG", "DPCCRG"]
    assert help["series"]["DPCCRG"]["indicator"] == "core"
    assert help["series"]["DPCCRG"]["seasonal_adjustment"] == "SA"
    assert help["table_name"] == "T20804" and help["frequency"] == "M"
    assert help["last_revised_is_publication_time"] is False
    assert "BLS" not in json.dumps(help)
    assert "synthetic-bea-key" not in json.dumps(help)
    assert loaded == []


def test_two_series_job_and_ratio_readback(fixture):
    _, host, source, _, loaded = fixture
    job = host.submit(command())
    assert job["state"] == "complete"
    assert job["result"]["readback"]["expected_points"] == 2
    result = host.query(
        {
            "start": "2026-09",
            "end": "2026-09",
            "basket": "all",
            "adjustment": "all",
            "measure": "change_1m_ratio",
            "local_only": "true",
        }
    )
    assert len(result["points"]) == 2
    assert result["points"][0]["value"] == "0.0093"
    assert all(p["publication_event"] is None for p in result["points"])
    assert source.calls == 1 and set(loaded) == {"BEA"}
    assert host.executions()["executions"][0]["origin"] == "manual"


def test_source_shortage_completes_with_explicit_gap(fixture):
    _, host, source, _, _ = fixture
    source.skip = {("DPCCRG", "2026-09")}
    job = host.submit(command())
    assert job["state"] == "complete"
    assert job["classification"] is None
    assert job["result"]["completion"] == "source_coverage_partial"
    assert job["result"]["readback"]["refresh_coverage_complete"] is False


def test_status_without_worker_is_honest_and_240_months_editable(fixture):
    _, host, source, _, _ = fixture
    assert host.status()["source"]["source_id"] == "BEA"
    initial = host.status()["initialization"]
    assert initial["months"] == 240 and initial["editable"] is True
    assert initial["anchor_basis"] == "request_calendar_month_no_release_claim"
    assert initial["end"] == "2026-09"
    assert host.strategy()["worker"]["state"] == "not_started"
    assert host.strategy()["enabled"] is False
    assert host.journal.list() == [] and source.calls == 0
    assert not host.scheduler.state_path.exists()
    assert not host.auto_strategy.strategy_path.exists()


def test_published_event_requires_bea_month_evidence(fixture):
    _, host, _, _, _ = fixture
    host.submit(command())
    host.scheduler.import_calendar(ics(), NOW)
    result = host.query({"start": "2026-09", "end": "2026-09", "local_only": "true"})
    assert (
        result["points"][0]["publication_event"]["time"] == "2026-10-29T12:30:00+00:00"
    )
    assert result["metadata"]["publication_event"]["binds_current_revision"] is False
    assert host.status()["initialization"]["end"] == "2026-09"


def test_query_passthrough_is_ephemeral_history_stays_local(fixture):
    _, host, source, data, _ = fixture
    current = host.query({"start": "2026-09", "end": "2026-09"})
    assert current["points"] and source.calls == 1
    assert data.status()["observation_count"] == 0
    history = host.query(
        {
            "start": "2026-09",
            "end": "2026-09",
            "as_of": (NOW + timedelta(days=1)).isoformat(),
        }
    )
    assert history["points"] == [] and source.calls == 1
    with pytest.raises(WebError, match="pce_seasonal_adjustment_unsupported"):
        host.query({"start": "2026-09", "end": "2026-09", "adjustment": "nsa"})


def test_revoked_capability_never_uses_bls_or_fallback(fixture):
    _, host, source, _, loaded = fixture
    record = host.packages.store.read("fixture", "macro")
    host.packages.store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=["cpi"]),
    )
    with pytest.raises(WebError, match="pce_capability_disabled"):
        host.submit(command())
    assert source.calls == 0 and loaded == []


def test_full_asgi_surface_and_body_boundaries(fixture):
    app, _, _, _, _ = fixture
    with client(app) as http:
        for operation in (
            "status",
            "api-help",
            "jobs",
            "strategy",
            "schedule",
            "executions",
        ):
            assert http.get(PREFIX + "/" + operation).status_code == 200
        assert (
            http.get(
                PREFIX + "/query?start=2026-09&end=2026-09&local_only=true"
            ).status_code
            == 200
        )
        headers = {"Origin": CANDIDATE_ORIGIN}
        job = http.post(PREFIX + "/jobs", headers=headers, json=command())
        assert job.status_code == 202 and job.json()["state"] == "running"
        assert (
            http.put(
                PREFIX + "/strategy", headers=headers, json={"enabled": True}
            ).status_code
            == 200
        )
        assert http.get(PREFIX + "/strategy").json()["worker"]["state"] == "not_started"
        assert http.request("GET", PREFIX + "/status", content=b"{}").status_code == 400
        assert (
            http.get(
                PREFIX + "/query?start=2026-09&start=2026-09&end=2026-09"
            ).status_code
            == 400
        )
        assert (
            http.post(
                PREFIX + "/jobs",
                headers={"Origin": "http://evil.example"},
                json=command(),
            ).status_code
            == 403
        )
        assert "synthetic-bea-key" not in job.text


def test_http_handler_admits_only_exact_pce_writes(monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    from mdf_cpi_timeseries_web import TimeSeriesHandler

    handler = object.__new__(TimeSeriesHandler)
    for method, suffix, admitted in (
        ("POST", "jobs", admitted_job),
        ("PUT", "strategy", admitted_strategy),
    ):
        scope = {
            "type": "http",
            "method": method,
            "scheme": "http",
            "path": PREFIX + "/" + suffix,
            "query_string": b"",
            "client": ("192.168.10.25", 1234),
            "headers": [
                (b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode()),
                (b"origin", CANDIDATE_ORIGIN.encode()),
                (b"content-type", b"application/json"),
            ],
        }
        assert admitted(Request(scope), prefix=PREFIX)
        assert handler.request_admitted(Request(scope))
        assert not handler.request_admitted(
            Request(dict(scope, query_string=b"bypass=1"))
        )


@pytest.mark.parametrize(
    "instant, explicit, persisted, expected",
    [
        ("2026-10-15T12:00:00+00:00", None, None, "2006-10"),
        ("2026-10-31T15:59:59+00:00", None, None, "2006-10"),
        ("2026-10-31T16:00:00+00:00", None, None, "2006-11"),
        ("2026-11-01T00:00:00+00:00", None, None, "2006-11"),
        ("2026-10-31T16:00:00+00:00", "2001-03", None, "2001-03"),
        ("2026-10-31T16:00:00+00:00", None, "2004-07", "2004-07"),
        ("2026-10-31T16:00:00+00:00", "2001-03", "2004-07", "2004-07"),
    ],
)
def test_runtime_pce_history_baseline(
    tmp_path, monkeypatch, instant, explicit, persisted, expected
):
    from market_data_foundation.web.cpi_timeseries_host import TimeSeriesRuntime

    _, previous, connections, *_ = old.setup(
        tmp_path, monkeypatch, enabled_caps=("pce",)
    )
    now = datetime.fromisoformat(instant)
    schedule_path = tmp_path / "pce" / "schedule.json"
    if persisted is not None:
        schedule_path.parent.mkdir()
        schedule_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "asset": "pce",
                    "history_start": persisted,
                    "calendar": {"events": {}},
                    "targets": [],
                }
            )
        )
    runtime = TimeSeriesRuntime(
        previous.settings,
        connector=previous.connector,
        clock=lambda: now,
        schedule_path=tmp_path / "cpi" / "schedule.json",
        job_directory=tmp_path / "cpi" / "jobs",
        pce_enabled=True,
        pce_schedule_path=schedule_path,
        pce_job_directory=tmp_path / "pce" / "jobs",
        pce_history_start=explicit,
    )
    assert runtime.pce_timeseries.scheduler.history_start == expected
    assert runtime.timeseries.scheduler.history_start == "1999-01"
    assert connections == []
    if persisted is not None:
        assert json.loads(schedule_path.read_text())["history_start"] == persisted
    else:
        assert not schedule_path.exists()


def test_runtime_holding_uses_new_pce_store_without_legacy_data(tmp_path, monkeypatch):
    _, previous, connections, *_ = old.setup(
        tmp_path, monkeypatch, enabled_caps=("pce",)
    )
    source = Source()
    data = InMemoryTimeSeriesStore(profile=PCE_PROFILE, clock=lambda: NOW)

    def read():
        return CpiTimeSeriesService(data, source, profile=PCE_PROFILE)

    def bound(*, credential_loader):
        assert credential_loader() == "synthetic-bea-key"
        return read()

    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=lambda: NOW,
        cpi_access=old.ALLOW,
        pce_access=old.ALLOW,
        schedule_path=tmp_path / "cpi" / "schedule.json",
        job_directory=tmp_path / "cpi" / "jobs",
        pce_read_service_factory=read,
        pce_write_service_factory=bound,
        pce_query_service_factory=bound,
        pce_schedule_path=tmp_path / "pce" / "schedule.json",
        pce_job_directory=tmp_path / "pce" / "jobs",
        pce_allow_collect=True,
    )
    runtime = app.state.timeseries_runtime
    assert runtime.pce_timeseries is not None and runtime.ppi_timeseries is None
    assert connections == []
    legacy_loader = runtime.load_legacy_key
    runtime.load_legacy_key = Mock(return_value="synthetic-bea-key")
    assert runtime.pce_timeseries.source()["source_id"] == "BEA"
    assert runtime.load_legacy_key.call_args.args[-1] == "BEA"
    assert runtime.load_legacy_key.call_count == 1
    runtime.load_legacy_key = legacy_loader
    assert runtime.pce_timeseries.submit(command())["state"] == "complete"
    runtime.scopes["pce"] = Mock(side_effect=AssertionError("legacy_reader_forbidden"))
    with client(app) as http:
        view = http.get("/api/r5/packages/macro")
        assert view.status_code == 200
        definition = runtime.pce_timeseries.asset_metadata()["asset_id"]
        asset = next(a for a in view.json()["assets"] if a["identity"] == definition)
        assert asset["revisions"][0]["local_holding"] == "PRESENT"
        assert asset["revisions"][0]["local_read"]["allowed"] is True
        assert http.get(PREFIX + "/status").status_code == 200
    runtime.scopes["pce"].assert_not_called()


def test_pce_worker_requires_optin_and_never_ticks_cpi_or_ppi(tmp_path, monkeypatch):
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    import mdf_cpi_timeseries_worker as module

    parser = module.build_parser()
    args = [
        "--asset",
        "pce",
        "--config-directory",
        str(tmp_path),
        "--package-store-directory",
        str(tmp_path / "packages"),
        "--environment-id",
        "synthetic",
        "--pg-role",
        "synthetic",
        "--pce-schema-owner",
        "synthetic",
        "--pce-schema-oid",
        "1",
        "--schedule-path",
        str(tmp_path / "cpi" / "schedule.json"),
        "--job-directory",
        str(tmp_path / "cpi" / "jobs"),
        "--pce-schedule-path",
        str(tmp_path / "pce" / "schedule.json"),
        "--pce-job-directory",
        str(tmp_path / "pce" / "jobs"),
    ]
    with pytest.raises(ValueError, match="explicit_pce_worker_opt_in_required"):
        module.build_runtime(parser.parse_args(args))
    captured = []
    strategies = {name: Mock() for name in ("cpi", "ppi", "pce")}
    runtime = SimpleNamespace(
        timeseries=SimpleNamespace(auto_strategy=strategies["cpi"]),
        ppi_timeseries=SimpleNamespace(auto_strategy=strategies["ppi"]),
        pce_timeseries=SimpleNamespace(auto_strategy=strategies["pce"]),
    )
    monkeypatch.setattr(
        module, "TimeSeriesRuntime", lambda *a, **kw: captured.append(kw) or runtime
    )
    worker = Mock()
    monkeypatch.setattr(module, "run_worker", worker)
    assert module.main([*args, "--opt-in-pce-worker", "--allow-collect"]) == 0
    assert worker.call_args.args[0] is strategies["pce"]
    assert captured[0]["allow_collect"] is False
    assert captured[0]["ppi_allow_collect"] is False
    assert captured[0]["pce_allow_collect"] is True
    assert captured[0]["pce_enabled"] is True


def test_pce_replacement_failure_never_falls_back_to_legacy(fixture, monkeypatch):
    from market_data_foundation.web.package_catalog import PackageError

    _, host, _, _, loaded = fixture
    record = host.packages.store.read("fixture", "macro")
    host.packages.store.save(
        "fixture",
        "macro",
        SaveCommand.model_validate(
            {
                "expected_revision": record.revision,
                "enabled_capability_ids": ["pce"],
                "source_changes": [
                    {
                        "source_id": "BEA",
                        "credential_action": "replace",
                        "credential_value": "synthetic-replacement-bea-key",
                    }
                ],
            }
        ),
        allow_replace=True,
    )
    assert host.effective_credential() == "synthetic-replacement-bea-key"
    assert loaded == []
    monkeypatch.setattr(
        host.packages.store,
        "resolve_credential",
        Mock(side_effect=PackageError("credential_storage_unavailable")),
    )
    with pytest.raises(PackageError):
        host.effective_credential()
    assert loaded == []


def test_disabled_pce_timeseries_keeps_legacy_routes_and_explicit_503_detection(
    tmp_path, monkeypatch
):
    _, previous, _, *_ = old.setup(tmp_path, monkeypatch, enabled_caps=("pce",))
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=previous.clock,
        cpi_access=old.ALLOW,
        pce_access=old.ALLOW,
        schedule_path=tmp_path / "cpi" / "schedule.json",
        job_directory=tmp_path / "cpi" / "jobs",
    )
    assert app.state.timeseries_runtime.pce_timeseries is None
    with client(app) as http:
        response = http.get(PREFIX + "/status")
        assert response.status_code == 503
        assert response.json()["classification"] == "pce_timeseries_not_enabled"
        assert http.get("/api/pce/assets").status_code == 200


def test_manual_returned_row_lost_in_readback_fails(fixture):
    _, host, _, _, _ = fixture
    original = host.read_factory

    def read_with_loss():
        service = original()
        query = service.query_range

        def missing_row(*args, **kwargs):
            result = query(*args, **kwargs)
            result["points"] = [
                p
                for p in result["points"]
                if (p["series_id"], p["month"]) != ("DPCCRG", "2026-09")
            ]
            return result

        service.query_range = missing_row
        return service

    host.read_factory = read_with_loss
    job = host.submit(command())
    assert job["state"] == "failed"
    assert job["classification"] == "local_readback_incomplete"
    assert job["result"]["readback"]["verified"] is False


def test_manual_buffer_gap_does_not_change_requested_coverage(fixture):
    _, host, source, _, _ = fixture
    source.skip = {("DPCCRG", "2025-09")}
    job = host.submit(command())
    assert job["state"] == "complete"
    assert job["result"]["coverage_complete"] is True
    assert {"series_id": "DPCCRG", "month": "2025-09"} in job["result"][
        "source_missing"
    ]


def test_empty_continuous_store_preserves_legacy_holding_and_bea_binding(
    tmp_path, monkeypatch
):
    from market_data_foundation.web.cpi_timeseries_host import TimeSeriesRuntime

    _, previous, _, *_ = old.setup(tmp_path, monkeypatch, enabled_caps=("pce",))
    data = InMemoryTimeSeriesStore(profile=PCE_PROFILE, clock=lambda: NOW)
    runtime = TimeSeriesRuntime(
        previous.settings,
        connector=previous.connector,
        clock=lambda: NOW,
        cpi_access=old.ALLOW,
        pce_access=old.ALLOW,
        schedule_path=tmp_path / "cpi" / "schedule.json",
        job_directory=tmp_path / "cpi" / "jobs",
        pce_enabled=True,
        pce_read_service_factory=lambda: CpiTimeSeriesService(
            data, Source(), profile=PCE_PROFILE
        ),
        pce_schedule_path=tmp_path / "pce" / "schedule.json",
        pce_job_directory=tmp_path / "pce" / "jobs",
    )
    definition = next(
        c.definition_ref
        for c in runtime.packages.catalog.package("macro").capabilities
        if c.capability_id == "pce"
    )
    evidence = runtime._history("pce", definition, NOW)
    assert evidence.holding == "present"
    assert runtime.bea_request_control.provider == "BEA"
    assert (
        runtime.bea_request_control.state_path
        == previous.settings.config_directory / "bea-timeseries-request-control.json"
    )
    assert runtime.bea_request_control.state_path != runtime.request_control.state_path
    source = Mock()
    monkeypatch.setattr(
        "market_data_foundation.web.cpi_timeseries_host.build_source", source
    )
    monkeypatch.setattr(
        runtime, "pce_read_service", runtime.pce_timeseries.read_factory
    )
    runtime.pce_query_service(credential_loader=lambda: "synthetic-bea-key")
    assert source.call_args.kwargs["request_control"] is runtime.bea_request_control


def test_initialization_prefers_local_or_official_month_and_beijing_calendar(fixture):
    from market_data_foundation.web.cpi_timeseries_policy import initialization_preset

    _, host, _, _, _ = fixture
    host.clock = lambda: NOW.replace(day=31, hour=20)
    assert host.status()["initialization"]["end"] == "2026-10"
    host.submit(command())
    with host.service() as service:
        preset = initialization_preset(
            service, service.status(), {}, NOW, profile=PCE_PROFILE
        )
    assert preset["end"] == "2026-09"
    assert preset["anchor_basis"] == "confirmed_local_available"
