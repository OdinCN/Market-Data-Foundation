"""Actual host/launcher composition stays offline and capability scoped."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE as P,
)
from market_data_foundation.historical.consumption_cli import main
from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError
from market_data_foundation.historical.macro_release_material import parse_material
from market_data_foundation.web.cpi_timeseries_host import TimeSeriesRuntime
from tests import test_readonly_host as old
from tests.consumption_fixtures import NOW, make_host
from tests.test_ppi_worker_history import arguments, worker_module


def test_launcher_runs_only_consumption_and_permissions(monkeypatch, tmp_path):
    module = worker_module()
    host = Mock()
    host.auto_strategy.strategy_path = tmp_path / "auto-strategy.json"
    constructor = Mock(return_value=SimpleNamespace(consumption_timeseries=host))
    worker = Mock()
    monkeypatch.setattr(module, "TimeSeriesRuntime", constructor)
    monkeypatch.setattr(module.sys, "version_info", (3, 14, 8))
    monkeypatch.setattr(module.os, "umask", lambda _: None)
    monkeypatch.setattr(module, "run_worker", worker)
    argv = arguments("consumption") + [
        "--consumption-schedule-path",
        "/tmp/consumption/calendar.json",
        "--consumption-job-directory",
        "/tmp/consumption/jobs",
        "--bea-request-control-path",
        "/tmp/bea/request-control.json",
    ]
    assert module.main(argv) == 0
    worker.assert_called_once_with(
        host.auto_strategy, poll_seconds=5, sleep=module.worker_sleep
    )
    options = constructor.call_args.kwargs
    assert options["consumption_enabled"] and options["consumption_allow_collect"]
    assert not any(
        options[k]
        for k in (
            "allow_collect",
            "pce_allow_collect",
            "ppi_allow_collect",
            "retail_allow_collect",
            "employment_allow_collect",
        )
    )
    assert not options["pce_enabled"]
    argv.remove("--opt-in-consumption-worker")
    assert module.main(argv) == 2


def test_actual_runtime_constructs_without_network_or_pg_and_separate_state(
    tmp_path, monkeypatch
):
    _, previous, connections, *_ = old.setup(
        tmp_path, monkeypatch, enabled_caps=("consumption",)
    )
    runtime = TimeSeriesRuntime(
        previous.settings,
        connector=previous.connector,
        clock=lambda: NOW,
        schedule_path=tmp_path / "cpi/calendar.json",
        job_directory=tmp_path / "cpi/jobs",
        consumption_enabled=True,
        consumption_schedule_path=tmp_path / "consumption/calendar.json",
        consumption_job_directory=tmp_path / "consumption/jobs",
        consumption_access=old.ALLOW,
        pce_request_control_path=tmp_path / "bea/request-control.json",
    )
    host = runtime.consumption_timeseries
    assert host.profile is P and host.scheduler.history_start == "2006-11"
    assert host.auto_strategy.scheduler is host.scheduler
    assert (
        host.auto_strategy.strategy_path
        != runtime.timeseries.auto_strategy.strategy_path
    )
    assert runtime.pce_timeseries is None and connections == []
    assert not host.scheduler.state_path.exists()
    assert not host.auto_strategy.strategy_path.exists()
    assert (
        host.material_state_path == tmp_path / "bea/bea-pio-material-acquisition.json"
    )


def test_cli_native_query_status_and_schema_are_real_dispatch(tmp_path, capsys):
    _, host, source, _ = make_host(tmp_path)

    def factory(**_):
        return host.read_factory()

    for command in ("query", "status"):
        argv = [
            command,
            "--runtime",
            "/tmp/runtime.toml",
            "--secret",
            "/tmp/secret.toml",
        ]
        if command == "query":
            argv += ["--start", "2026-09", "--end", "2026-09", "--local-only"]
        assert main(argv, service_factory=factory) == 0
        doc = json.loads(capsys.readouterr().out)
        if command == "query":
            assert len(doc["points"]) == 7 and doc["measure"] == "native"
    assert main(["schema-sql"]) == 0
    assert P.schema in capsys.readouterr().out and not source.calls


@pytest.mark.parametrize("transient,status", [(True, 500), (True, 429), (False, 403)])
def test_current_query_safe_partial_preserves_local_rows(tmp_path, transient, status):
    _, host, source, store = make_host(tmp_path)
    before = store.status()

    def fail(*_):
        raise CpiSourceError(
            "pce_timeseries_http_failure",
            3,
            http_status=status,
            transient=transient,
            request_count=1,
        )

    source.fetch = fail
    if not transient:
        with pytest.raises(Exception, match="http_failure"):
            host.query(dict(start="2026-09", end="2027-02", series="DPCERC"))
    else:
        result = host.query(dict(start="2026-09", end="2027-02", series="DPCERC"))
        assert (
            result["status"] == "PARTIAL" and result["points"][0]["value"] == "22452780"
        )
        assert result["provider_attempts"] == 3 and result["provider_requests"] == 1
        assert result["source_failure"]["http_status"] == status
    assert store.status() == before


def test_retained_official_html_has_asset_scoped_reported_rates():
    from pathlib import Path

    for ref in ("202608", "202609"):
        raw = Path(f"tests/fixtures/macro_release/bea-pio-{ref}.html").read_bytes()
        doc = parse_material(
            raw,
            "https://www.bea.gov/news/2026/personal-income-and-outlays-"
            + ("august" if ref.endswith("08") else "september")
            + "-2026",
            "consumption",
        )
        assert doc["parser_revision"] == "official-consumption-income-v1"
        assert {"DPCERC", "DPCERX", "A067RC", "A067RX"} <= {
            c["series_id"] for c in doc["claims"]
        }
        assert not any(c["series_id"] in {"DPCERG", "DPCCRG"} for c in doc["claims"])


def test_cli_import_and_source_history_do_not_call_provider(tmp_path, capsys):
    _, host, source, _ = make_host(tmp_path)

    def factory(**_):
        return host.read_factory()

    base = ["--runtime", "/tmp/runtime.toml", "--secret", "/tmp/secret.toml"]
    assert (
        main(
            [
                "import-material",
                *base,
                "--file",
                "tests/fixtures/macro_release/bea-consumption-synthetic.html",
                "--source-url",
                "https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026",
                "--material-format",
                "official_selected_projection",
            ],
            service_factory=factory,
        )
        == 0
    )
    evidence = json.loads(capsys.readouterr().out)
    assert evidence["evidence_id"]
    assert (
        main(
            [
                "releases",
                *base,
                "--read-mode",
                "source_history",
                "--reference-month",
                "2026-09",
            ],
            service_factory=factory,
        )
        == 0
    )
    read = json.loads(capsys.readouterr().out)
    assert read["events"][0]["event_id"] == evidence["event_id"]
    assert not source.calls


def test_failed_manual_job_persists_request_attempt_counts(tmp_path):
    _, host, source, _ = make_host(tmp_path, preload=False)
    from uuid import uuid4

    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError

    def fail(*_, **kw):
        raise CpiTimeSeriesError(
            "pce_timeseries_http_failure", 4, provider_requests=2, http_status=503
        )

    service = host.write_factory

    def write(**kw):
        result = service(**kw)
        result.update_range = fail
        return result

    host.write_factory = write
    command = dict(
        request_id=str(uuid4()), action="initialize", start="2026-09", end="2026-09"
    )
    job = host.submit(command)
    assert job["state"] == "unknown"
    assert job["result"] == {"provider_requests": 2, "provider_attempts": 4}
    assert host.journal.read(command["request_id"]) == job
