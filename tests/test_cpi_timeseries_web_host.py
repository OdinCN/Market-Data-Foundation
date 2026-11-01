"""Synthetic host integration; existing PCE wire fixtures stay read-only."""

from contextlib import contextmanager
from threading import Event
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_store import (
    SERIES_IDS,
    InMemoryTimeSeriesStore,
)
from market_data_foundation.web.cpi_query import CpiQueryAccess
from market_data_foundation.web.cpi_timeseries_api import PREFIX
from market_data_foundation.web.cpi_timeseries_host import (
    LocalOnlySource,
    create_timeseries_preview,
)
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_readonly_host as old
from tests.test_cpi_timeseries_web import NOW, Source, command, no_network  # noqa: F401


def setup(tmp_path, monkeypatch, **options):
    _, previous, connections, ledgers, _, pce_plan = old.setup(
        tmp_path, monkeypatch, enabled_caps=("cpi",)
    )
    data, source = InMemoryTimeSeriesStore(clock=lambda: NOW), Source()
    calls = []

    def service():
        calls.append("open")
        result = CpiTimeSeriesService(data, source)
        result.close = lambda: calls.append("close")
        return result

    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        cpi_access=options.pop("cpi_access", old.ALLOW),
        pce_access=old.ALLOW,
        clock=previous.clock,
        schedule_path=tmp_path / "absent" / "schedule.json",
        job_directory=tmp_path / "jobs",
        allow_collect=options.pop("allow_collect", False),
        read_service_factory=service,
        **options,
    )
    return (
        app,
        app.state.timeseries_runtime,
        connections,
        ledgers,
        pce_plan,
        calls,
        source,
    )


def client(app):
    return TestClient(app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234))


def test_runtime_preserves_optional_operational_limit(tmp_path, monkeypatch):
    app, runtime, _, _, _, _, _ = setup(tmp_path, monkeypatch, max_collect_months=36)
    assert runtime.timeseries.max_collect_months == 36
    with client(app) as http:
        assert (
            http.get(PREFIX + "/status").json()["collection_scope"]["max_months"] == 36
        )


@pytest.mark.parametrize("limit", [True, 0, -1, 2401, 24.0, "24"])
def test_runtime_rejects_invalid_operational_limit(tmp_path, monkeypatch, limit):
    with pytest.raises(ValueError, match="invalid_collection_limit"):
        setup(tmp_path, monkeypatch, max_collect_months=limit)


def test_new_host_keeps_pce_and_never_projects_timeseries_as_old_cpi(
    tmp_path, monkeypatch
):
    app, runtime, connections, ledgers, pce_plan, calls, source = setup(
        tmp_path, monkeypatch
    )
    assert calls == [] and connections == []
    with runtime.timeseries.service() as service:
        service.init_range("2026-09", "2026-09")
    calls.clear()

    @contextmanager
    def forbidden_old_cpi():
        raise AssertionError("legacy_cpi_scope_must_not_be_used")
        yield

    runtime.scopes["cpi"] = forbidden_old_cpi
    with client(app) as http:
        status = http.get(PREFIX + "/status")
        assert status.status_code == 200
        assert status.json()["schema"] == "mdf_cpi_timeseries_dev_r1"
        assert status.json()["collection_scope"]["max_months"] is None
        assert status.json()["web_capabilities"] == {
            "query": True,
            "collect": False,
            "reason": "collection_not_enabled",
        }
        result = http.get(
            PREFIX + "/query?start=2026-09&end=2026-09&basket=all&adjustment=all"
        ).json()
        assert result["complete"] and len(result["points"]) == len(SERIES_IDS)
        assert "version_identity" not in result and "publication_identity" not in result
        package = http.get("/api/r5/packages/macro")
        assert package.status_code == 200
        assets = {a["identity"]: a for a in package.json()["assets"]}
        cpi = assets["mdf.macro.bls.cpi.headlines"]["revisions"][0]
        assert cpi["local_read"] == {
            "allowed": True,
            "reasons": ["cpi_timeseries_local_coverage"],
        }
        assert cpi["collect"]["allowed"] is False
        pce = assets["mdf.macro.bea.pce.headline-core"]["revisions"][0]
        assert pce["local_read"]["allowed"] is True
        response = http.get(
            "/api/pce/read",
            params={
                "reference": "2026-07",
                "as_of": runtime.clock().isoformat(),
                "version": pce_plan.version.identity,
            },
        )
        assert response.status_code == 200
        assert response.json()["version_identity"] == pce_plan.version.identity
        assert http.get("/api/pce/catalog").json()["write_eligible"] is False
        denied = http.post(
            PREFIX + "/jobs", json=command(), headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert denied.status_code == 403
        for path in (
            "/api/pce/jobs",
            "/api/r5/cpi/jobs/backfill",
            "/api/cpi/init",
            PREFIX + "/schedule/due",
        ):
            assert (
                http.post(
                    path, json={}, headers={"Origin": CANDIDATE_ORIGIN}
                ).status_code
                == 405
            )
    assert source.calls == 1
    assert calls.count("open") == calls.count("close")
    assert {c.selected_schema for c in connections} == {"mdf_macro_bea_pce_vertical_r1"}
    assert all(c.closed for c in connections)
    assert ledgers[0].snapshot() == ledgers[1] and ledgers[2].snapshot() == ledgers[3]
    assert not runtime.timeseries.journal.directory.exists()


def test_unknown_read_permission_is_not_promoted_by_new_host(tmp_path, monkeypatch):
    app, _, connections, _, _, calls, _ = setup(
        tmp_path, monkeypatch, cpi_access=CpiQueryAccess()
    )
    with client(app) as http:
        assert http.get(PREFIX + "/status").status_code == 403
        assert http.get(PREFIX + "/query?start=2026-09&end=2026-09").status_code == 403
    assert connections == [] and calls == []


def test_default_read_factory_uses_new_store_and_separate_readonly_connection(
    tmp_path, monkeypatch
):
    _, runtime, _, _, _, _, _ = setup(tmp_path, monkeypatch)
    connection = Mock(autocommit=True)
    connector = Mock(return_value=connection)
    runtime.connector = connector
    service = runtime.read_service()
    assert service.store.raw_store is None
    assert isinstance(service.source, LocalOnlySource)
    connection.execute.assert_called_once_with("SET TIME ZONE 'UTC'")
    assert connector.call_args.kwargs["options"].endswith(
        "default_transaction_read_only=on"
    )
    assert service.store.connection is connection
    service.close()
    connection.close.assert_called_once()


def test_get_body_is_rejected_and_schedule_does_not_create_files(tmp_path, monkeypatch):
    app, runtime, _, _, _, calls, source = setup(tmp_path, monkeypatch)
    with client(app) as http:
        assert (
            http.request(
                "GET", PREFIX + "/query?start=2026-09&end=2026-09", content=b"{}"
            ).status_code
            == 400
        )
        assert http.get(PREFIX + "/schedule").status_code == 200
    assert calls == [] and source.calls == 0
    assert not runtime.timeseries.scheduler.state_path.parent.exists()


def test_enabled_host_passes_package_loader_to_same_core_factory(tmp_path, monkeypatch):
    from market_data_foundation.web import cpi_timeseries_host as module

    app, runtime, _, _, _, _, source = setup(tmp_path, monkeypatch, allow_collect=True)
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    from market_data_foundation.web.package_catalog import SaveCommand

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
    calls = []

    def build(**options):
        assert (
            options["runtime_path"]
            == runtime.settings.config_directory / "runtime.toml"
        )
        assert (
            options["secret_path"] == runtime.settings.config_directory / "secret.toml"
        )
        assert options["registered"] is True
        assert options["credential_loader"]() == "a" * 32
        calls.append("build")
        return runtime.timeseries.read_factory()

    monkeypatch.setattr(module, "build_service", build)
    finished = Event()
    original_submit = runtime.timeseries.submit

    def submit(*args, **kwargs):
        try:
            return original_submit(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(runtime.timeseries, "submit", submit)
    with client(app) as http:
        assert (
            http.get(PREFIX + "/status").json()["web_capabilities"]["collect"] is True
        )
        assert calls == []
        job = http.post(
            PREFIX + "/jobs", json=command(), headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert job.status_code == 202
        assert job.json()["state"] == "running"
        assert finished.wait(10), "collection_not_finished"
        jobs = http.get(PREFIX + "/jobs").json()["jobs"]
        assert len(jobs) == 1 and jobs[0]["state"] == "complete"
    assert calls == ["build"] and source.calls == 1
