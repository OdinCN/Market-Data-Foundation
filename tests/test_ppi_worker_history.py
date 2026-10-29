"""Offline worker selection and source-scoped historical verification."""

import copy
import importlib.util
import json
import socket
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from test_ppi_release_schedule import make


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("PPI worker/history tests must stay offline")

    monkeypatch.setattr(socket, "create_connection", forbidden)


def worker_module():
    path = Path(__file__).resolve().parents[1] / "scripts/mdf_cpi_timeseries_worker.py"
    spec = importlib.util.spec_from_file_location("ppi_worker_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def arguments(asset="ppi"):
    return [
        "--asset",
        asset,
        f"--opt-in-{asset}-worker",
        "--config-directory",
        "/tmp/config",
        "--package-store-directory",
        "/tmp/store",
        "--environment-id",
        "test",
        "--pg-role",
        "test",
        "--pce-schema-owner",
        "test",
        "--pce-schema-oid",
        "1",
        "--schedule-path",
        "/tmp/cpi/schedule.json",
        "--job-directory",
        "/tmp/cpi/jobs",
        "--ppi-schedule-path",
        "/tmp/ppi/schedule.json",
        "--ppi-job-directory",
        "/tmp/ppi/jobs",
        "--ppi-history-start",
        "2010-04",
        "--allow-collect",
    ]


@pytest.mark.parametrize("asset", ["ppi", "cpi"])
def test_worker_selects_only_asset_and_scopes_collect(monkeypatch, asset):
    module = worker_module()
    cpi, ppi = Mock(), Mock()
    runtime = SimpleNamespace(timeseries=cpi, ppi_timeseries=ppi)
    constructor = Mock(return_value=runtime)
    monkeypatch.setattr(module, "TimeSeriesRuntime", constructor)
    monkeypatch.setattr(module.sys, "version_info", (3, 14, 8))
    monkeypatch.setattr(module.os, "umask", lambda _: None)
    monkeypatch.setattr(module, "run_worker", lambda strategy, **kw: strategy.tick())
    assert module.main(arguments(asset)) == 0
    (ppi if asset == "ppi" else cpi).auto_strategy.tick.assert_called_once()
    (cpi if asset == "ppi" else ppi).auto_strategy.tick.assert_not_called()
    options = constructor.call_args.kwargs
    assert options["allow_collect"] is (asset == "cpi")
    assert options["ppi_allow_collect"] is (asset == "ppi")


@pytest.mark.parametrize("fault", ["optin", "history", "same", "relative"])
def test_worker_rejects_invalid_binding_before_construction(monkeypatch, fault):
    module = worker_module()
    args = module.build_parser().parse_args(arguments())
    if fault == "optin":
        args.opt_in_ppi_worker = False
        args.opt_in_cpi_worker = True
    elif fault == "history":
        args.ppi_history_start = None
    elif fault == "same":
        args.ppi_schedule_path = args.schedule_path
    else:
        args.ppi_job_directory = Path("relative")
    constructor = Mock()
    monkeypatch.setattr(module, "TimeSeriesRuntime", constructor)
    with pytest.raises(ValueError):
        module.build_runtime(args)
    constructor.assert_not_called()


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize(
    "fault",
    [
        None,
        "missing_field",
        "lost_write",
        "unconfirmed",
        "duplicate",
        "foreign",
        "empty_no_request",
        "bad_month",
        "outside_range",
        "not_persisted",
        "readback_loss",
        "mismatch",
    ],
)
def test_partial_history_requires_every_returned_key_confirmed(tmp_path, auto, fault):
    scheduler, service, clock = make(tmp_path, history_start="2026-01", auto_mode=auto)
    original = service.update_range(
        "2026-01", "2026-10", series_ids=scheduler.series_ids, include_buffer=False
    )
    result = copy.deepcopy(original)
    result["points"] = result["points"][-4:]
    result["source_returned_series_months"] = [
        {"series_id": p["series_id"], "month": p["month"]} for p in result["points"]
    ]
    result.update(refresh_complete=False, refresh_coverage_complete=False)
    actual = copy.deepcopy(result)
    if fault == "missing_field":
        result.pop("source_returned_series_months")
    elif fault == "lost_write":
        result["points"].pop()
    elif fault == "unconfirmed":
        actual["points"][0]["available_at"] = None
    elif fault == "duplicate":
        result["source_returned_series_months"] *= 2
    elif fault == "foreign":
        result["source_returned_series_months"][0]["series_id"] = "CPI"
    elif fault == "empty_no_request":
        result.update(source_returned_series_months=[], provider_requests=0)
    elif fault == "bad_month":
        result["source_returned_series_months"][0]["month"] = "2026-13"
    elif fault == "outside_range":
        result["source_returned_series_months"][0]["month"] = "2025-12"
    elif fault == "not_persisted":
        result["persisted"] = False
    elif fault == "readback_loss":
        actual["points"].pop()
    elif fault == "mismatch":
        actual["points"][0]["index_value"] = "999"
    service.update_range = lambda *a, **kw: result
    service.query_range = lambda *a, **kw: actual
    state = scheduler._read()
    stage = {"state": "pending", "attempts": 0}
    run = scheduler._auto_history_run if auto else scheduler._history_run
    action = run(state, stage, "2026-10", clock())
    if fault:
        assert action == "history_awaiting_provider"
        assert stage["state"] != "complete"
    else:
        assert action == "history_updated"
        assert stage["classification"] == "historical_source_gaps"
        assert stage["coverage_complete"] is False
        assert len(stage["source_missing"]) == 36


def test_history_confirmation_only_touches_returned_scope(tmp_path):
    scheduler, service, _ = make(tmp_path, history_start="2026-01")
    point = service.points("2026-10")[0]
    point["available_at"] = None
    result = {
        "points": [point],
        "persisted": True,
        "provider_requests": 1,
        "provider_attempts": 1,
        "source_returned_series_months": [
            {"series_id": point["series_id"], "month": point["month"]}
        ],
    }
    confirmed = copy.deepcopy(point)
    confirmed["available_at"] = "2026-11-13T13:30:00+00:00"
    service.query_range = lambda *a, **kw: {"points": [confirmed]}
    service.confirm_current_availability = Mock()
    scheduler._history_readback("2026-01", "2026-10", result, {})
    service.confirm_current_availability.assert_called_once_with(
        "2026-10", "2026-10", series_ids=(point["series_id"],)
    )


@pytest.mark.parametrize("auto", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_real_service_partial_response_and_source_null(tmp_path, auto, empty):
    from test_ppi_timeseries_core import fixture_service, raw_response

    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

    scheduler, _, clock = make(tmp_path, history_start="2026-01", auto_mode=auto)
    service, transport, _ = fixture_service()
    calls = []

    def post(endpoint, body, headers, **kwargs):
        request = json.loads(body)
        calls.append(request)
        return HttpResult(
            200,
            raw_response(
                request["seriesid"],
                [] if empty else ["2026-09", "2026-10"],
                {"2026-09": "-"},
            ),
            {},
        )

    transport.post = post
    scheduler.service = service
    stage = {"state": "pending", "attempts": 0}
    run = scheduler._auto_history_run if auto else scheduler._history_run
    assert run(scheduler._read(), stage, "2026-10", clock()) == "history_updated"
    assert calls
    assert stage["state"] == "complete"
    assert stage["classification"] == "historical_source_gaps"
    assert stage["coverage_complete"] is False
    available = service.query_range(
        "2026-01", "2026-10", allow_upstream=False, vintage_mode="available"
    )
    assert available["coverage_complete"] is False
    assert len(available["points"]) == (0 if empty else 4)
    assert len(available["source_unavailable"]) == (0 if empty else 4)
    assert all(p["available_at"] for p in available["source_unavailable"])
