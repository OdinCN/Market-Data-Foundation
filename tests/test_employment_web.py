"""Employment public chain with synthetic source, calendar and in-memory storage."""

from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from market_data_foundation.historical.bls_timeseries_profile import EMPLOYMENT_PROFILE
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.employment_release_schedule import (
    CALENDAR_URL,
    parse_official_history,
    parse_official_ics,
)
from market_data_foundation.historical.employment_timeseries import (
    EmploymentTimeSeriesService,
)
from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_readonly_host as old
from tests.test_cpi_release_schedule_ics import event, html, ics
from tests.test_cpi_timeseries_web import command, no_network  # noqa: F401
from tests.test_employment_timeseries import NOW, Source
from tests.test_ppi_timeseries_web import client

REFERENCE = ("October 2026", "Nov. 6, 2026", "08:30 AM")


def setup(tmp_path, monkeypatch, *, enabled=True, allowed=True, **options):
    _, previous, connections, _, _, _ = old.setup(
        tmp_path, monkeypatch, enabled_caps=("cpi", "employment")
    )
    source = Source()
    data = InMemoryTimeSeriesStore(profile=EMPLOYMENT_PROFILE, clock=lambda: source.at)

    def read():
        return EmploymentTimeSeriesService(data, source)

    def bound(*, credential_loader):
        credential_loader()
        return read()

    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=lambda: source.at,
        schedule_path=tmp_path / "cpi" / "plan.json",
        job_directory=tmp_path / "cpi" / "jobs",
        employment_enabled=enabled,
        employment_access=old.ALLOW if allowed else old.CpiQueryAccess(),
        employment_read_service_factory=read,
        employment_query_service_factory=bound,
        employment_write_service_factory=bound,
        employment_allow_collect=True,
        employment_schedule_path=tmp_path / "employment" / "plan.json",
        employment_job_directory=tmp_path / "employment" / "jobs",
        employment_history_start="2026-08",
        employment_calendar_fetcher=options.pop(
            "employment_calendar_fetcher",
            lambda: ics(
                event(
                    "employment-oct", "20261106T083000", summary="Employment Situation"
                )
            ),
        ),
        employment_reference_fetcher=options.pop(
            "employment_reference_fetcher", lambda: html(REFERENCE)
        ),
        **options,
    )
    return app, app.state.timeseries_runtime, source, data, connections


def test_host_explicit_enable_permission_and_snapshot_isolation(tmp_path, monkeypatch):
    app, runtime, source, _, connections = setup(tmp_path, monkeypatch, enabled=False)
    assert runtime.employment_timeseries is None
    assert "employment" not in runtime.packages.timeseries_hosts
    with client(app) as http:
        reply = http.get("/api/r5/employment/timeseries/status")
        assert reply.status_code == 503
        view = http.get("/api/r5/packages/macro").json()
        employment = next(
            a
            for a in view["assets"]
            if a["identity"] == "mdf.macro.bls.employment.ces-cps"
        )
        assert not employment["revisions"][0]["collect"]["allowed"]
        assert not employment["revisions"][0]["local_read"]["allowed"]
    assert not source.calls and not connections


def test_query_series_history_and_collection_readback(tmp_path, monkeypatch):
    app, runtime, source, data, _ = setup(tmp_path, monkeypatch)
    host = runtime.employment_timeseries
    with client(app) as http:
        prefix = host.prefix
        status = http.get(prefix + "/status").json()
        assert status["initialization"]["end"] == "2026-09"
        assert (
            status["initialization"]["anchor_basis"]
            == "request_calendar_month_no_release_claim"
        )
        assert status["host_binding"] == "employment-continuous-dev-r1"
        result = http.get(
            prefix + "/query?start=2026-08&end=2026-09&series=LNS14000000"
        ).json()
        assert result["series_ids"] == ["LNS14000000"]
        assert result["points"][0]["value_unit"] == "percent"
        assert "index_value" not in result["points"][0]
        assert data.status()["observation_count"] == 0
        calls = len(source.calls)
        history = http.get(
            prefix + "/query?start=2026-08&end=2026-09&series=LNS14000000"
            "&query_mode=history&as_of_basis=available&local_only=false"
        ).json()
        assert history["points"] == [] and history["provider_requests"] == 0
        assert len(source.calls) == calls
        assert (
            http.get(
                prefix + "/query?start=2026-08&end=2026-09&series=WPSFD4"
            ).status_code
            == 400
        )
        assert (
            http.get(
                prefix + "/query?start=2026-08&end=2026-09&basket=headline"
            ).status_code
            == 400
        )
        job = host.submit(command(action="initialize", start="2026-08", end="2026-09"))
        assert job["state"] == "complete", job
        assert job["result"]["readback"]["verified"] is True
        assert host.executions()["executions"][0]["id"] == job["job_id"]
        assert runtime.timeseries.executions()["executions"] == []
        package = http.get("/api/r5/packages/macro").json()
        asset = next(
            a for a in package["assets"] if a["identity"] == status["asset_id"]
        )
        assert asset["revisions"][0]["local_read"]["allowed"] is True
        assert asset["revisions"][0]["collect"]["allowed"] is True
        help_doc = http.get(prefix + "/api-help").json()
        assert "provider_calculations" in help_doc["fields"]
        assert len(help_doc["examples"]) == 2


def test_denied_query_permission_blocks_writes(tmp_path, monkeypatch):
    app, runtime, source, _, _ = setup(tmp_path, monkeypatch, allowed=False)
    host = runtime.employment_timeseries
    with client(app) as http:
        assert (
            http.get(host.prefix + "/query?start=2026-09&end=2026-09").status_code
            == 403
        )
    with pytest.raises(WebError):
        host.submit(command())
    assert not source.calls


def test_http_help_matches_defaults_units_and_official_nulls(tmp_path, monkeypatch):
    app, runtime, source, _, _ = setup(tmp_path, monkeypatch)
    source.overrides[(EMPLOYMENT_PROFILE.series_ids[1], "2025-10")] = None
    prefix = runtime.employment_timeseries.prefix
    with client(app) as http:
        help_doc = http.get(prefix + "/api-help").json()
        assert help_doc["query_defaults"]["local_only"] is False
        assert help_doc["query_defaults"]["as_of_basis"] == "observed"
        assert help_doc["query_defaults"]["history_as_of_basis"] == "available"
        assert "raw_sha256/batch_id" not in help_doc["fields"]
        current = http.get(prefix + "/query?start=2025-10&end=2025-10").json()
        assert current["query_mode"] == "current"
        assert current["as_of_basis"] == current["vintage_mode"] == "observed"
        assert current["coverage_complete"] is True and current["complete"] is False
        assert current["persisted"] is False
        assert current["provider_requests"] == 1
        assert {p["value_unit"] for p in current["points"]} == {
            "thousand_jobs",
            "USD/hour",
            "percent",
            "hours",
        }
        assert current["missing"] == [
            {
                "series_id": "LNS14000000",
                "month": "2025-10",
                "reason": "source_unavailable",
            }
        ]
        null_point = current["source_unavailable"][0]
        assert null_point["value"] is None and null_point["value_unit"] == "percent"
        for point in [*current["points"], null_point]:
            assert "index_value" not in point and "batch_id" not in point
        assert not {"measure", "unit", "measure_complete"} & current.keys()
        invalid = http.get(prefix + "/query?start=2025-10&end=2025-10&measure=yoy")
        assert invalid.status_code == 400
        assert invalid.json()["classification"] == "employment_series_filter_required"
        calls = len(source.calls)
        history = http.get(
            prefix + "/query?start=2025-10&end=2025-10&query_mode=history"
        ).json()
        assert history["as_of_basis"] == history["vintage_mode"] == "available"
        assert history["provider_requests"] == 0 and len(source.calls) == calls
        # Availability is returned evidence, not a query filter with a default.
        assert (
            http.get(
                prefix + "/query?start=2025-10&end=2025-10&availability=available"
            ).status_code
            == 400
        )


def test_calendar_filters_references_retiming_and_cancellation(tmp_path, monkeypatch):
    _, runtime, source, _, _ = setup(tmp_path, monkeypatch)
    scheduler = runtime.employment_timeseries.scheduler
    assert scheduler.calendar_url == CALENDAR_URL
    mixed = ics(
        event(summary="Consumer Price Index"),
        event("emp", "20261106T083000", summary="Employment Situation"),
    )
    assert [row["uid"] for row in parse_official_ics(mixed)] == ["emp"]
    assert (
        parse_official_history(
            b"<table><tr><td>November 06, 2026</td><td>08:30 AM</td>"
            b"<td>Employment Situation for October 2026</td></tr></table>"
        )[0]["reference_month"]
        == "2026-10"
    )
    scheduler.reference_fetcher = None
    snapshot = scheduler.import_calendar(mixed, source.at)
    assert snapshot["targets"] == []  # DTSTART alone never establishes a month.
    scheduler.import_calendar(html(REFERENCE), source.at)
    snapshot = scheduler.import_calendar(mixed, source.at)
    assert snapshot["targets"][0]["reference_month"] == "2026-10"
    assert snapshot["targets"][0]["release_at"] == "2026-11-06T13:30:00+00:00"
    source.at += timedelta(seconds=1)
    changed = ics(
        event("emp", "20261109T083000", sequence=2, summary="Employment Situation")
    )
    snapshot = scheduler.import_calendar(changed, source.at)
    assert snapshot["targets"][0]["release_at"] == "2026-11-09T13:30:00+00:00"
    source.at += timedelta(seconds=1)
    snapshot = scheduler.import_calendar(
        ics(
            event(
                "emp", at=None, sequence=3, summary=None, extra="STATUS:CANCELLED\r\n"
            )
        ),
        source.at,
    )
    assert snapshot["targets"][0]["state"] == "calendar_removed"
    assert runtime.timeseries.scheduler.status()["targets"] == []
    assert not source.calls


def test_real_worker_service_captures_current_then_rechecks_previous_months(
    tmp_path, monkeypatch
):
    _, runtime, source, data, _ = setup(tmp_path, monkeypatch)
    host = runtime.employment_timeseries
    assert host.scheduler.service.profile is EMPLOYMENT_PROFILE
    assert host.submit(command(start="2026-08", end="2026-09"))["state"] == "complete"
    source.calls.clear()
    source.overrides[(EMPLOYMENT_PROFILE.series_ids[0], "2026-09")] = Decimal("160000")
    host.scheduler.refresh_calendar()
    host.update_strategy({"enabled": True})
    source.at = NOW.replace(month=11, day=6, hour=13, minute=30)
    result = host.auto_strategy.tick()
    assert source.calls, result
    assert source.calls[0][1:] == ("2026-10", "2026-10")
    assert host.scheduler.status()["targets"][0]["target_available"] is True
    source.at += timedelta(seconds=7206)
    host.auto_strategy.tick()
    assert any(
        start <= "2026-09" and end == "2026-10" for _, start, end in source.calls
    )
    assert data.status()["observation_count"] >= 9
    previous = host.query(
        {
            "start": "2026-09",
            "end": "2026-09",
            "series": EMPLOYMENT_PROFILE.series_ids[0],
            "local_only": "true",
        }
    )["points"][0]
    assert previous["value"] == "160000" and previous["revision"] == 2
    assert runtime.timeseries.scheduler.status()["targets"] == []
    assert host.auto_strategy.strategy_path.name == "employment-strategy.json"


def test_shared_budget_and_schema_in_runtime_factories(tmp_path, monkeypatch):
    from market_data_foundation.web import cpi_timeseries_host as module

    _, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    captured = []

    def build(**options):
        captured.append(options)
        return SimpleNamespace(source=None, close=lambda: None)

    monkeypatch.setattr(module, "build_source", build)
    monkeypatch.setattr(module, "build_service", build)
    monkeypatch.setattr(
        runtime,
        "read_service",
        lambda **_: SimpleNamespace(source=None, close=lambda: None),
    )
    runtime.employment_query_service(credential_loader=lambda: "synthetic")
    runtime.employment_write_service(credential_loader=lambda: "synthetic")
    assert all(
        c["profile"] is EMPLOYMENT_PROFILE
        and c["request_control"] is runtime.request_control
        and c["max_transport_attempts"] == 1
        for c in captured
    )
    connection = Mock()
    runtime.connector = Mock(return_value=connection)
    service = module.TimeSeriesRuntime.read_service(runtime, profile=EMPLOYMENT_PROFILE)
    assert service.store.profile.schema == "mdf_employment_timeseries_dev_r1"
    service.close()


def test_worker_cli_uses_actual_employment_runtime_assembly(tmp_path, monkeypatch):
    from tests.test_ppi_worker_history import arguments, worker_module

    module = worker_module()
    _, previous, connections, _, _, _ = old.setup(
        tmp_path, monkeypatch, enabled_caps=("cpi", "employment")
    )
    settings = previous.settings
    args = module.build_parser().parse_args(
        arguments("employment")
        + [
            "--config-directory",
            str(settings.config_directory),
            "--package-store-directory",
            str(settings.package_store_directory),
            "--environment-id",
            settings.environment_id,
            "--pg-role",
            settings.pce_identity.role,
            "--pce-schema-owner",
            settings.pce_identity.schema_owner,
            "--pce-schema-oid",
            str(settings.pce_identity.schema_oid),
            "--employment-schedule-path",
            str(tmp_path / "employment" / "plan.json"),
            "--employment-job-directory",
            str(tmp_path / "employment" / "jobs"),
        ]
    )
    runtime = module.build_runtime(args)
    host = runtime.employment_timeseries
    assert isinstance(runtime, module.TimeSeriesRuntime)
    assert host.scheduler.service.profile is EMPLOYMENT_PROFILE
    assert host.scheduler.calendar_url == CALENDAR_URL
    assert host.scheduler.state_path == args.employment_schedule_path
    assert host.read_factory.__self__ is runtime
    assert host.write_factory.__name__ == "employment_write_service"
    assert host.query_factory.__name__ == "employment_query_service"
    assert runtime.ppi_timeseries is runtime.pce_timeseries is None
    assert not runtime.timeseries.allow_collect and host.allow_collect
    assert host.auto_strategy.tick()["action"] == "paused"
    assert connections == []


@pytest.mark.parametrize("path", ["same", "nested"])
def test_employment_rejects_shared_plan_or_job_paths(tmp_path, monkeypatch, path):
    _, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="employment_independent_paths_required"):
        create_timeseries_preview(
            runtime.settings,
            enabled=True,
            connector=runtime.connector,
            schedule_path=tmp_path / "cpi" / "plan.json",
            job_directory=tmp_path / "cpi" / "jobs",
            employment_enabled=True,
            employment_schedule_path=tmp_path / "employment" / "plan.json",
            employment_job_directory=tmp_path / "cpi" / "jobs"
            if path == "same"
            else tmp_path / "cpi" / "jobs" / "employment",
        )


def test_enabled_host_still_requires_its_own_package_capability(tmp_path, monkeypatch):
    from market_data_foundation.web.package_catalog import SaveCommand

    _, runtime, source, _, _ = setup(tmp_path, monkeypatch)
    host = runtime.employment_timeseries
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=["cpi"]),
    )
    assert host.capabilities(host.source())["collect"] is False
    with pytest.raises(WebError, match="employment_capability_disabled"):
        host.submit(command())
    with pytest.raises(WebError, match="employment_capability_disabled"):
        host.query({"start": "2026-09", "end": "2026-09"})
    assert source.calls == []


def test_display_history_is_offline_and_never_creates_jobs(tmp_path, monkeypatch):
    _, runtime, source, _, _ = setup(tmp_path, monkeypatch)
    scheduler = runtime.employment_timeseries.scheduler
    source.at = NOW.replace(month=11, day=7)
    snapshot = scheduler.import_display_history(
        b"<table><tr><td>Friday, November 06, 2026</td><td>08:30 AM</td>"
        b"<td>Employment Situation for October 2026</td></tr></table>",
        source.at,
        source="https://www.bls.gov/schedule/2026/11_sched_list.htm",
        start="2026-10",
        end="2026-10",
    )
    assert snapshot["targets"] == []
    assert snapshot["calendar_publications"][0]["reference_month"] == "2026-10"
    assert runtime.timeseries.scheduler.status()["calendar_publications"] == []
    assert not source.calls


@pytest.mark.parametrize("selection", [None, "LNS11300000,CES0500000002"])
def test_public_jobs_five_defaults_and_subset_readback(
    tmp_path, monkeypatch, selection
):
    from threading import Event

    app, runtime, source, _, _ = setup(tmp_path, monkeypatch)
    host = runtime.employment_timeseries
    payload = command(start="2026-09", end="2026-09")
    if selection:
        payload["series"] = selection
    with client(app) as http:
        accepted = http.post(
            host.prefix + "/jobs", json=payload, headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert accepted.status_code in {200, 202}, accepted.json()
        for _ in range(100):
            result = host.journal.read(payload["request_id"])
            if result["state"] != "running":
                break
            Event().wait(0.01)
        assert result["state"] == "complete", result
        expected = (
            tuple(selection.split(",")) if selection else EMPLOYMENT_PROFILE.series_ids
        )
        assert source.calls[0][0] == expected
        assert result["result"]["readback"]["expected_points"] == len(expected)
        assert result["result"]["readback"]["verified"]
        assert (
            http.post(
                host.prefix + "/jobs",
                json=payload,
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 200
        )
        changed = dict(payload, series="LNS14000000")
        assert (
            http.post(
                host.prefix + "/jobs",
                json=changed,
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 409
        )
        invalid = dict(payload, series="CIVPART")
        assert (
            http.post(
                host.prefix + "/jobs",
                json=invalid,
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 400
        )
        help_doc = http.get(host.prefix + "/api-help").json()
        assert len(help_doc["series_ids"]) == 5
        assert help_doc["series"]["LNS11300000"]["fred_alias"] == "CIVPART"
        assert help_doc["series"]["CES0500000002"]["value_unit"] == "hours"
