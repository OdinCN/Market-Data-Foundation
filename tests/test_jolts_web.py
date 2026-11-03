"""Offline real HTTP serialization and manual job contract for JOLTS."""

from datetime import timedelta
from uuid import uuid4

import pytest

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_timeseries_cli import execute
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.jolts_timeseries import JoltsTimeSeriesService
from market_data_foundation.web.jolts_runtime import create_jolts_app
from market_data_foundation.web.package_catalog import (
    CredentialState,
    PackageCatalog,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_jolts_timeseries import OPENINGS, OPENINGS_RATE, Source
from tests.test_ppi_timeseries_web import client

PREFIX = "/api/r5/jolts/timeseries"


def command(action="initialize", **options):
    return dict(
        request_id=str(uuid4()),
        action=action,
        start="2026-07",
        end="2026-08",
        **options,
    )


@pytest.fixture
def setup(tmp_path):
    catalog = PackageCatalog()
    config = LocalPackageStore.initialize(tmp_path / "packages", catalog)
    config.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["jolts"]),
    )
    packages = PackageHost(
        config,
        "fixture",
        credentials=lambda *_: [
            CredentialState(source_id="BLS", credential_state="configured")
        ],
        credential_loader=lambda *_: "synthetic-only",
    )
    source = Source()
    data = InMemoryTimeSeriesStore(profile=JOLTS_PROFILE, clock=lambda: source.at)

    def read():
        return JoltsTimeSeriesService(data, source)

    def bound(*, credential_loader):
        credential_loader()
        return read()

    app = create_jolts_app(
        packages=packages,
        runtime_path=tmp_path / "unused-runtime.toml",
        secret_path=tmp_path / "unused-secret.toml",
        state_directory=tmp_path / "state",
        allow_read=True,
        allow_collect=True,
        allow_source=True,
        read_factory=read,
        write_factory=bound,
        query_factory=bound,
        clock=lambda: source.at,
    )
    return app, app.state.jolts_host, data, source, tmp_path


def test_get_local_defaults_and_no_state_writes(setup):
    app, _, data, source, root = setup
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    with client(app) as http:
        reply = http.get(PREFIX + "/query?start=2026-08&end=2026-08")
        assert reply.status_code == 200, reply.text
        assert reply.json()["series_ids"] == list(JOLTS_PROFILE.series_ids)
        assert not reply.json()["points"]
        assert http.get(PREFIX + "/status").json()["periodic_enabled"] is False
        help_doc = http.get(PREFIX + "/api-help").json()
        assert help_doc["query_defaults"]["local_only"] is True
        assert help_doc["query_persists"] is False
        assert http.get(PREFIX + "/jobs").json() == {"jobs": []}
        assert http.get(PREFIX + "/executions").json() == {"executions": []}
    assert sorted(p.relative_to(root) for p in root.rglob("*")) == before
    assert not source.calls and data.status()["observation_count"] == 0


def test_temporary_http_fill_precision_projection_history_and_bad_filters(setup):
    app, _, data, source, _ = setup
    source.overrides[OPENINGS, "2026-08"] = "120.123456789123456789"
    with client(app) as http:
        reply = http.get(
            PREFIX + "/query",
            params={
                "start": "2026-08",
                "end": "2026-08",
                "series": OPENINGS,
                "local_only": "false",
                "columns": "value,change_1m_level",
            },
        )
        assert reply.status_code == 200, reply.text
        point = reply.json()["points"][0]
        assert point["value"] == "120.123456789123456789"
        assert point["change_1m_level"] == "20.123456789123456789"
        assert "change_1m_ratio" not in point
        assert point["available_at"] is None and point["raw_locator"] is None
        history = http.get(
            PREFIX + "/query",
            params={
                "start": "2026-08",
                "end": "2026-08",
                "local_only": "false",
                "query_mode": "history",
                "as_of_basis": "available",
            },
        ).json()
        assert history["points"] == [] and history["provider_requests"] == 0
        for suffix in (
            "&basket=headline",
            "&series=CES0000000001",
            "&measure=mom",
            "&columns=index_value",
            "&vintage_mode=official",
        ):
            assert (
                http.get(
                    PREFIX + "/query?start=2026-08&end=2026-08" + suffix
                ).status_code
                == 400
            )
    assert len(source.calls) == 1 and data.status()["observation_count"] == 0


def test_manual_jobs_real_readback_replay_and_changed_only(setup, monkeypatch):
    app, host, data, source, _ = setup
    from threading import Event

    completed = Event()
    original = host.submit

    def submit(*args, **kwargs):
        try:
            return original(*args, **kwargs)
        finally:
            completed.set()

    monkeypatch.setattr(host, "submit", submit)
    job_command = command(series=OPENINGS + "," + OPENINGS_RATE)
    with client(app) as http:
        reply = http.post(
            PREFIX + "/jobs", json=job_command, headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert reply.status_code in {200, 202}, reply.text
        assert completed.wait(2)
        job = host.journal.read(job_command["request_id"])
        assert job["state"] == "complete", job
        assert job["result"]["readback"]["verified"] is True
        assert job["asset"] == "jolts"
        calls = len(source.calls)
        assert host.submit(job_command) == job
        assert len(source.calls) == calls
        source.at += timedelta(minutes=1)
        updated = host.submit(command("update", series=job_command["series"]))
        assert updated["state"] == "complete", updated
        assert updated["result"]["saved_revisions"] == 0
        assert len(source.calls) == calls + 1
        points = http.get(PREFIX + "/query?start=2026-08&end=2026-08").json()["points"]
        assert {p["series_id"] for p in points} == {OPENINGS, OPENINGS_RATE}
        assert all(p["available_at"] is not None for p in points)
        assert (
            http.put(
                PREFIX + "/strategy",
                json={"enabled": True},
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 200
        )
    assert data.status()["observation_count"] == 28


def test_history_available_cutoff_inclusive_and_same_comparison(setup):
    _, host, _, source, _ = setup
    host.submit(command())
    cutoff = source.at
    source.at += timedelta(minutes=1)
    source.overrides[OPENINGS, "2026-07"] = "80"
    source.overrides[OPENINGS_RATE, "2026-07"] = "4.1"
    assert host.submit(command("update"))["state"] == "complete"
    old = host.query(
        {
            "start": "2026-08",
            "end": "2026-08",
            "as_of": cutoff.isoformat(),
            "local_only": "false",
        }
    )
    points = {p["series_id"]: p for p in old["points"]}
    assert points[OPENINGS]["change_1m_level"] == "0"
    assert points[OPENINGS_RATE]["change_1m_percentage_points"] == "0.0"
    before = host.query(
        {
            "start": "2026-08",
            "end": "2026-08",
            "as_of": (cutoff - timedelta(microseconds=1)).isoformat(),
        }
    )
    assert before["points"] == []
    assert old["provider_requests"] == 0


def test_permissions_fail_closed_and_cli_default_is_local(setup):
    _, host, data, source, _ = setup
    service = JoltsTimeSeriesService(data, source)
    assert (
        execute(service, "query", {"start": "2026-08", "end": "2026-08"})["points"]
        == []
    )
    host.query_allowed = False
    with pytest.raises(ValueError, match="local_read_not_authorized"):
        host.query({"start": "2026-08", "end": "2026-08"})
    with pytest.raises(ValueError, match="local_read_not_authorized"):
        host.submit(command())
    assert not source.calls


def test_native_value_mismatch_fails_manual_verification(setup):
    from copy import deepcopy

    from market_data_foundation.web.cpi_timeseries_jobs import verify_readback

    _, host, _, _, _ = setup
    with host.service(
        write=True, credential_loader=lambda: "synthetic-only"
    ) as service:
        written = service.update_range("2026-07", "2026-08")
    readback = host.query(
        {
            "start": "2025-07",
            "end": "2026-08",
            "as_of_basis": "available",
            "query_mode": "history",
        }
    )
    verified, _ = verify_readback(
        written, readback, command("update"), profile=JOLTS_PROFILE
    )
    assert verified
    wrong = deepcopy(readback)
    next(p for p in wrong["points"] if p["month"] == "2026-08")["value"] = "9999"
    verified, _ = verify_readback(
        written, wrong, command("update"), profile=JOLTS_PROFILE
    )
    assert verified is False


def test_jolts_catalog_does_not_enable_source_or_other_capabilities(setup):
    _, host, _, source, _ = setup
    row = host.packages.catalog.entries["macro", "jolts"][0]
    assert row.source_name == "BLS"
    assert row.source_qualified_contract is False
    assert row.binding_contract.silent_fallback is False
    assert "CES" not in row.source_contract.freshness_contract
    assert host.packages.store.read("fixture", "macro").enabled_capability_ids == [
        "jolts"
    ]
    assert not source.calls


def test_default_240_month_suggestion_uses_product_timezone(setup):
    from datetime import datetime

    _, host, _, source, _ = setup
    source.at = datetime.fromisoformat("2026-10-31T17:00:00+00:00")
    preset = host.status()["initialization"]
    assert preset["end"] == "2026-10" and preset["start"] == "2006-11"
    assert preset["anchor_basis"] == "request_calendar_month_no_release_claim"
    assert not source.calls
