"""Isolated contracts for CPI strategy, current pass-through and worker wiring."""

import importlib
import json
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_foundation.historical import cpi_timeseries as core
from market_data_foundation.historical.cpi_timeseries_cli import (
    package_credential_loader,
    run_worker,
)
from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError
from market_data_foundation.web.cpi_timeseries_api import PREFIX, admitted_strategy
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_cpi_timeseries_web as base_web
from tests.test_cpi_timeseries_web import (
    NOW,
    client,
    no_network,  # noqa: F401
)
from tests.test_cpi_timeseries_web_host import setup

fixture = base_web.fixture
SID = core.SERIES_IDS[0]
STRATEGY_FIELDS = {
    "exists",
    "enabled",
    "history_start",
    "worker",
    "next_due",
    "policy",
    "next_release_at",
    "execution_state",
    "last_result",
}


def test_strategy_is_explicit_and_http_fields_are_fixed(fixture):
    host, store, source, closed, _ = fixture
    with client(host) as http:
        default = http.get(PREFIX + "/strategy")
        assert default.status_code == 200
        assert (
            STRATEGY_FIELDS
            <= set(default.json())
            <= STRATEGY_FIELDS | {"history_start_locked"}
        )
        assert default.json()["exists"] is False
        assert default.json()["enabled"] is False
        assert default.json()["worker"] == {
            "state": "not_started",
            "last_heartbeat_at": None,
        }
        assert not host.scheduler.state_path.parent.exists()
        response = http.put(
            PREFIX + "/strategy",
            json={"enabled": True, "history_start": "1999-01"},
            headers={"Origin": CANDIDATE_ORIGIN},
        )
        assert response.status_code == 200
        assert (
            STRATEGY_FIELDS
            <= set(response.json())
            <= STRATEGY_FIELDS | {"history_start_locked"}
        )
        assert response.json()["exists"] and response.json()["enabled"]
        assert response.json()["worker"]["last_heartbeat_at"] is None
        assert http.get(PREFIX + "/strategy").json() == response.json()
    assert source.calls == 0 and closed == []
    assert store.status()["observation_count"] == 0
    assert not host.scheduler.state_path.exists()


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"enabled": 1, "history_start": "1999-01"},
        {"enabled": False, "history_start": "1999-13"},
        {"enabled": False, "history_start": "1999-01", "extra": True},
        [],
    ],
)
def test_strategy_rejects_bad_contract_without_business_io(fixture, body):
    from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN

    host, _, source, closed, _ = fixture
    with client(host) as http:
        response = http.put(
            PREFIX + "/strategy", json=body, headers={"Origin": CANDIDATE_ORIGIN}
        )
    assert 400 <= response.status_code < 500
    assert source.calls == 0 and closed == []


def enable_query_factory(host):
    host.query_factory = lambda **_: host.read_factory()


@pytest.mark.parametrize("vintage", [None, "observed", "available"])
def test_current_fills_true_gaps_without_any_save(fixture, monkeypatch, vintage):
    host, store, source, _, _ = fixture
    enable_query_factory(host)

    def forbidden(*args, **kwargs):
        pytest.fail("ordinary query attempted persistence")

    monkeypatch.setattr(store, "save_batches", forbidden)
    params = {
        "start": "2000-01",
        "end": "2026-09",
        "query_mode": "current",
        "local_only": "false",
    }
    if vintage:
        params["vintage_mode"] = vintage
    result = host.query(params)
    assert result["complete"] and result["persisted"] is False
    assert result["query_mode"] == "current"
    assert source.calls > 0 and store.status()["observation_count"] == 0
    assert all(
        p["origin"] == "upstream"
        and p["availability"] == "unpersisted"
        and p["available_at"] is None
        and p["raw_locator"] is None
        for p in result["points"]
    )


@pytest.mark.parametrize("state", ["unknown", "withdrawn"])
def test_current_unknown_and_withdrawn_are_not_source_gaps(fixture, state):
    host, store, source, _, _ = fixture
    enable_query_factory(host)
    service = host.read_factory()
    service.init_range("2026-09", "2026-09", series_ids=(SID,))
    if state == "unknown":
        store._history[(SID, "2026-09")][-1]["available_at"] = None
    else:
        store._clock = lambda: NOW + timedelta(seconds=1)
        service.withdraw_range(
            "2026-09",
            "2026-09",
            series_ids=(SID,),
            evidence_url="https://www.bls.gov/errata/2026/cpi.htm",
        )
    calls = source.calls
    result = host.query(
        {
            "start": "2026-09",
            "end": "2026-09",
            "query_mode": "current",
            "local_only": "false",
            "vintage_mode": "observed",
        }
    )
    assert source.calls == calls
    assert result["points"] == []
    assert result["missing"][0]["reason"] == (
        "withdrawn" if state == "withdrawn" else "availability_evidence_insufficient"
    )


def test_current_confirmed_local_point_wins_over_upstream(fixture):
    host, store, source, _, _ = fixture
    enable_query_factory(host)
    service = host.read_factory()
    service.init_range("2026-09", "2026-09", series_ids=(SID,))
    # A missing comparison month must not overwrite the held numerator.
    del store._history[(SID, "2025-09")]
    result = host.query(
        {
            "start": "2026-09",
            "end": "2026-09",
            "query_mode": "current",
            "local_only": "false",
        }
    )
    assert result["points"][0]["origin"] == "local"
    assert result["points"][0]["available_at"] is not None
    assert source.calls == 2


@pytest.mark.parametrize(
    "extra",
    [
        {"as_of": NOW.isoformat()},
        {"query_mode": "history"},
        {"query_mode": "history", "as_of_basis": "available"},
        {"vintage_mode": "available"},
        {"as_of_basis": "available"},
    ],
)
def test_history_is_strictly_local_with_available_default(fixture, extra):
    host, _, source, _, _ = fixture
    result = host.query(
        {"start": "2026-09", "end": "2026-09", "local_only": "false", **extra}
    )
    assert result["points"] == [] and not result["complete"]
    assert result["provider_attempts"] == 0 and source.calls == 0
    assert result["as_of_basis"] == result["vintage_mode"] == "available"


def test_publication_event_never_claims_revision_time(fixture, monkeypatch):
    host, _, _, _, _ = fixture
    monkeypatch.setattr(
        host.scheduler,
        "status",
        lambda: {
            "targets": [
                {
                    "reference_month": "2026-09",
                    "release_at": "2026-10-01T12:30:00+00:00",
                    "reference_month_evidence": {
                        "source": "https://www.bls.gov/schedule/2026/10_sched.htm",
                        "sha256": "a" * 64,
                    },
                },
                {"reference_month": "2026-08", "release_at": "2026-09-01T00:00:00Z"},
            ]
        },
    )
    result = host.publication_metadata(
        {
            "points": [
                {"month": "2026-09", "revision": 7, "observed_at": NOW.isoformat()},
                {"month": "2026-08", "revision": 1},
            ]
        }
    )
    event = result["points"][0]["publication_event"]
    assert event["status"] == "scheduled"  # time passing is not proof of release
    assert event["reference_month"] == "2026-09" and event["evidence"]
    assert "revision" not in event and "observed_at" not in event
    assert result["points"][1]["publication_event"] is None
    assert result["metadata"]["publication_event"]["binds_current_revision"] is False


def test_source_failure_preserves_scheduler_facts(fixture):
    host, _, source, _, _ = fixture

    def fail(*args):
        raise CpiSourceError(
            "cpi_timeseries_http_429",
            1,
            http_status=429,
            retry_at=NOW + timedelta(minutes=10),
        )

    source.fetch = fail
    with pytest.raises(core.CpiTimeSeriesError) as raised:
        host.read_factory().update_range("2026-09", "2026-09")
    assert raised.value.http_status == 429
    assert raised.value.retry_at == NOW + timedelta(minutes=10)
    assert raised.value.attempts == raised.value.provider_attempts == 1


def test_runtime_and_worker_share_paths_without_starting_threads(tmp_path, monkeypatch):
    app, runtime, _, _, _, calls, source = setup(tmp_path, monkeypatch)
    assert calls == [] and source.calls == 0
    assert runtime.timeseries.scheduler.auto_mode is True
    assert (
        runtime.timeseries.auto_strategy.strategy_path
        == runtime.timeseries.scheduler.state_path.with_name("strategy.json")
    )
    assert runtime.request_control.state_path == core.DEFAULT_REQUEST_CONTROL_PATH
    assert not runtime.timeseries.scheduler.state_path.exists()
    assert runtime.timeseries.auto_strategy.get()["exists"] is False
    captured = []

    def stop(seconds):
        captured.append(seconds)
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(
            runtime.timeseries.auto_strategy, sleep=stop, emit_result=lambda _: None
        )
    view = runtime.timeseries.auto_strategy.get()
    assert captured == [5] and view["enabled"] is False and view["exists"] is False
    assert view["worker"]["state"] == "paused" and view["worker"]["last_heartbeat_at"]
    assert calls == [] and source.calls == 0


@pytest.mark.parametrize("explicit_ref", [False, True])
def test_runtime_source_and_write_use_same_governor_and_effective_key(
    tmp_path, monkeypatch, explicit_ref
):
    from market_data_foundation.web import cpi_timeseries_host as module
    from market_data_foundation.web.package_catalog import SaveCommand

    _, runtime, _, _, _, _, _ = setup(tmp_path, monkeypatch, allow_collect=True)
    (runtime.settings.config_directory / "secret.toml").write_text(
        '[bls]\napi_key="' + "a" * 32 + '"\n'
    )
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand.model_validate(
            {
                "expected_revision": record.revision,
                "enabled_capability_ids": ["cpi"],
                "source_changes": [
                    {
                        "source_id": "BLS",
                        "credential_action": "replace",
                        "credential_value": "a" * 32,
                    }
                ]
                if explicit_ref
                else [],
            }
        ),
        allow_replace=True,
    )
    captured = []

    def build(**kwargs):
        captured.append(kwargs)
        return SimpleNamespace(source=None, close=lambda: None)

    monkeypatch.setattr(module, "build_service", build)
    monkeypatch.setattr(module, "build_source", build)
    monkeypatch.setattr(
        runtime,
        "read_service",
        lambda: SimpleNamespace(source=None, close=lambda: None),
    )
    runtime.worker_service()
    runtime.query_service(credential_loader=runtime.timeseries.effective_credential)
    assert len(captured) == 2
    assert all(
        c["request_control"] is runtime.request_control
        and c["max_transport_attempts"] == 1
        and c["credential_loader"]() == "a" * 32
        for c in captured
    )
    assert (
        package_credential_loader(
            runtime.settings.package_store_directory,
            runtime.settings.environment_id,
            secret_path=runtime.settings.config_directory / "secret.toml",
        )()
        == "a" * 32
    )
    # Revocation is checked again, with no legacy key fallback.
    current = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(expected_revision=current.revision, enabled_capability_ids=[]),
    )
    with pytest.raises(Exception):
        captured[0]["credential_loader"]()


def test_bridge_explicitly_admits_strategy_and_bounds_long_requests(monkeypatch):
    from fastapi import Request

    from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("mdf_cpi_timeseries_web")
    handler = object.__new__(module.TimeSeriesHandler)
    request = Request(
        {
            "type": "http",
            "method": "PUT",
            "path": PREFIX + "/strategy",
            "query_string": b"",
            "scheme": "http",
            "client": ("192.168.10.25", 1234),
            "headers": [
                (b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode()),
                (b"origin", CANDIDATE_ORIGIN.encode()),
                (b"content-type", b"application/json"),
            ],
        }
    )
    assert admitted_strategy(request) and handler.request_admitted(request)
    assert module.TimeSeriesHandler.request_deadline_seconds == 180
    worker = importlib.import_module("mdf_cpi_timeseries_worker")
    assert (
        worker.TimeSeriesRuntime
        is module.create_timeseries_preview.__globals__["TimeSeriesRuntime"]
    )
    assert worker.build_parser().get_default("poll_seconds") == 5


def test_multiyear_current_uses_real_source_and_shared_governor_offline(
    fixture, tmp_path
):
    from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
    from market_data_foundation.historical.cpi_request_control import CpiRequestControl
    from market_data_foundation.historical.cpi_timeseries_source import BlsRangeSource

    host, store, _, _, _ = fixture
    clock = SimpleNamespace(at=NOW)
    requests = []

    class Transport:
        def post(self, endpoint, body, headers, **kwargs):
            request = json.loads(body)
            requests.append(request)
            result = {
                "status": "REQUEST_SUCCEEDED",
                "Results": {
                    "series": [
                        {
                            "seriesID": series,
                            "data": [
                                {
                                    "year": str(year),
                                    "period": f"M{month:02d}",
                                    "value": "100",
                                    "footnotes": [],
                                }
                                for year in range(
                                    int(request["startyear"]),
                                    int(request["endyear"]) + 1,
                                )
                                for month in range(1, 13)
                            ],
                        }
                        for series in request["seriesid"]
                    ]
                },
            }
            return HttpResult(200, json.dumps(result).encode(), {})

    def advance(seconds):
        clock.at += timedelta(seconds=seconds)

    control = CpiRequestControl(tmp_path / "governor.json", clock=lambda: clock.at)
    source = BlsRangeSource(
        transport=Transport(),
        credential_loader=lambda: "a" * 32,
        request_control=control,
        max_transport_attempts=1,
        clock=lambda: clock.at,
        sleep=advance,
    )

    def service():
        result = core.CpiTimeSeriesService(store, source)
        result.close = lambda: None
        return result

    host.read_factory = service
    enable_query_factory(host)
    result = host.query(
        {
            "start": "2000-01",
            "end": "2026-09",
            "query_mode": "current",
            "local_only": "false",
        }
    )
    assert result["complete"] and not result["persisted"]
    assert len(requests) == result["source_request_count"] == 2
    assert [(r["startyear"], r["endyear"]) for r in requests] == [
        ("1999", "2018"),
        ("2019", "2026"),
    ]
    assert len(json.loads(control.state_path.read_text())["requests"]) == 2
    assert store.status()["observation_count"] == 0


def test_observed_history_is_an_explicit_local_compatibility_mode(fixture):
    host, store, _, _, _ = fixture
    service = host.read_factory()
    service.init_range("2026-09", "2026-09", series_ids=(SID,))
    store._history[(SID, "2026-09")][-1]["available_at"] = None
    result = service.query_range(
        "2026-09",
        "2026-09",
        series_ids=(SID,),
        query_mode="history",
        as_of_basis="observed",
        as_of=NOW,
    )
    assert result["complete"] and result["provider_attempts"] == 0
    assert result["as_of_basis"] == "observed"


@pytest.mark.parametrize("has_key", [False, True])
def test_package_upstream_projection_is_independent_of_collection_permission(
    tmp_path, monkeypatch, has_key
):
    from market_data_foundation.web.package_catalog import SaveCommand
    from tests.test_cpi_timeseries_web_host import client as runtime_client

    app, runtime, _, _, _, _, _ = setup(tmp_path, monkeypatch, allow_collect=False)
    if not has_key:
        # This fixture really has no inherited BLS key; the runtime allows it.
        (runtime.settings.config_directory / "secret.toml").write_text(
            '[postgres]\npassword="synthetic-pg-password"\n'
            '[bea]\napi_key="synthetic-bea-key"\n'
        )
    if has_key:
        record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
        runtime.packages.store.save(
            runtime.settings.environment_id,
            "macro",
            SaveCommand.model_validate(
                {
                    "expected_revision": record.revision,
                    "enabled_capability_ids": ["cpi"],
                    "source_changes": [
                        {
                            "source_id": "BLS",
                            "credential_action": "replace",
                            "credential_value": "a" * 32,
                        }
                    ],
                }
            ),
            allow_replace=True,
        )
    with runtime.timeseries.service() as service:
        service.init_range("2026-09", "2026-09")
    with runtime_client(app) as http:
        package = http.get("/api/r5/packages/macro").json()
        asset = next(
            a
            for a in package["assets"]
            if a["identity"] == "mdf.macro.bls.cpi.headlines"
        )
        cpi = asset["revisions"][0]
        assert cpi["local_read"]["allowed"] is True
        assert cpi["upstream_read"]["allowed"] is has_key
        assert cpi["collect"]["allowed"] is False
        assert http.get(
            PREFIX + "/query?start=2026-09&end=2026-09&local_only=true"
        ).json()["complete"]
