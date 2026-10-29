"""Synthetic-only checks for Web composition, admission and durable CPI jobs."""

import json
import socket
from datetime import UTC, datetime
from decimal import Decimal
from threading import Event
from uuid import uuid4

import psycopg
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from market_data_foundation.historical.bls_cpi_direct import SERIES_IDS
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
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
from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_timeseries_api import (
    PREFIX,
    TimeSeriesHost,
    admitted_job,
    timeseries_router,
)
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.package_catalog import (
    CredentialState,
    PackageCatalog,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN

NOW = datetime(2026, 10, 29, tzinfo=UTC)


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("external_io_forbidden")

    for name in ("connect", "connect_ex", "bind", "listen"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)


class Source:
    def __init__(self):
        self.calls = 0
        self.skip = set()
        self.nulls = set()

    def fetch(self, series, start, end):
        self.calls += 1
        points = tuple(
            SourcePoint(s, m, None if (s, m) in self.nulls else Decimal("100"), {}, ())
            for s in series
            for m in month_range(start, end)
            if (s, m) not in self.skip
        )
        return (
            SourceBatch(
                points, b"synthetic-cpi-response", {"seriesid": list(series)}, NOW, 1
            ),
        )


@pytest.fixture
def fixture(tmp_path):
    store = LocalPackageStore.initialize(tmp_path / "packages", PackageCatalog())
    store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["cpi"]),
    )
    packages = PackageHost(
        store,
        "fixture",
        credentials=lambda *_: [
            CredentialState(source_id="BLS", credential_state="configured")
        ],
        credential_loader=lambda *_: "synthetic-legacy-key",
    )
    source = Source()
    data = InMemoryTimeSeriesStore(clock=lambda: NOW)
    closed, credentials = [], []

    def service():
        result = CpiTimeSeriesService(data, source)
        result.close = lambda: closed.append(True)
        return result

    def write_service(*, credential_loader):
        credentials.append(credential_loader())
        return service()

    host = TimeSeriesHost(
        packages=packages,
        read_service_factory=service,
        write_service_factory=write_service,
        allow_collect=True,
        scheduler=CpiReleaseScheduler(
            None, tmp_path / "absent" / "schedule.json", clock=lambda: NOW
        ),
        job_directory=tmp_path / "jobs",
        clock=lambda: NOW,
    )
    return host, data, source, closed, credentials


def command(action="init", **extra):
    return {
        "request_id": str(uuid4()),
        "action": action,
        "start": "2026-09",
        "end": "2026-09",
        **extra,
    }


def client(host):
    app = FastAPI()
    app.include_router(timeseries_router(host))
    return TestClient(app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234))


def test_gets_are_lazy_local_and_leave_schedule_and_jobs_absent(fixture):
    host, store, source, closed, _ = fixture
    assert closed == []
    with client(host) as http:
        status = http.get(PREFIX + "/status")
        assert status.status_code == 200
        assert status.json()["web_capabilities"]["collect"] is True
        assert status.json()["collection_scope"]["max_months"] is None
        result = http.get(PREFIX + "/query?start=2026-09&end=2026-09")
        assert result.status_code == 200
        assert result.json()["points"] == []
        assert result.json()["complete"] is False
        assert http.get(PREFIX + "/schedule").json()["worker"]["enabled"] is False
        assert http.get(PREFIX + "/jobs").json() == {"jobs": []}
    assert source.calls == 0 and len(closed) == 2
    assert not host.scheduler.state_path.parent.exists()
    assert not host.journal.directory.exists()
    assert store.status()["observation_count"] == 0


def test_persisted_job_replay_conflict_and_independent_readback(fixture, monkeypatch):
    host, store, source, closed, credentials = fixture
    finished = Event()
    original_submit = host.submit

    def submit(*args, **kwargs):
        try:
            return original_submit(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(host, "submit", submit)
    request = command()
    with client(host) as http:
        response = http.post(
            PREFIX + "/jobs", json=request, headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert response.status_code == 202
        assert response.json()["state"] == "running"
        assert finished.wait(10), "collection_not_finished"
        job = http.get(PREFIX + "/jobs").json()["jobs"][0]
        assert job["state"] == "complete"
        assert job["result"]["readback"]["verified"] is True
        assert job["result"]["readback"]["expected_points"] == 4
        assert (
            http.post(
                PREFIX + "/jobs", json=request, headers={"Origin": CANDIDATE_ORIGIN}
            ).json()
            == job
        )
        conflict = http.post(
            PREFIX + "/jobs",
            json=dict(request, end="2026-10"),
            headers={"Origin": CANDIDATE_ORIGIN},
        )
        assert conflict.status_code == 409
        assert http.get(PREFIX + "/jobs").json()["jobs"] == [job]
    assert source.calls == 1 and len(closed) == 2
    assert credentials == ["synthetic-legacy-key"]
    assert store.status()["revision_count"] == 0
    assert "synthetic-legacy-key" not in json.dumps(job)


def test_package_replacement_is_effective_key_and_disable_is_real(fixture):
    host, _, source, _, credentials = fixture
    record = host.packages.store.read("fixture", "macro")
    host.packages.store.save(
        "fixture",
        "macro",
        SaveCommand.model_validate(
            {
                "expected_revision": record.revision,
                "enabled_capability_ids": ["cpi"],
                "source_changes": [
                    {
                        "source_id": "BLS",
                        "credential_action": "replace",
                        "credential_value": "synthetic-new-package-key",
                    }
                ],
            }
        ),
        allow_replace=True,
    )
    assert host.submit(command())["state"] == "complete"
    assert credentials == ["synthetic-new-package-key"]
    record = host.packages.store.read("fixture", "macro")
    host.packages.store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=[]),
    )
    with pytest.raises(WebError, match="cpi_capability_disabled"):
        host.submit(command())
    assert source.calls == 1
    assert host.query({"start": "2026-09", "end": "2026-09"})["complete"]


def test_same_value_update_creates_no_revision(fixture):
    host, store, _, _, _ = fixture
    host.submit(command())
    before = store.status()
    result = host.submit(command("update"))
    assert result["state"] == "complete"
    assert result["result"]["saved_points"] == 0
    assert result["result"]["saved_revisions"] == 0
    assert store.status()["revision_count"] == before["revision_count"]


@pytest.mark.parametrize("null", [False, True])
def test_missing_range_differs_from_explicit_source_null(fixture, null):
    host, _, source, _, _ = fixture
    point = (SERIES_IDS[0], "2026-09")
    (source.nulls if null else source.skip).add(point)
    result = host.submit(command())
    assert result["state"] == ("complete" if null else "failed")
    assert result["result"]["coverage_complete"] is null
    assert result["result"]["readback"]["source_unavailable_count"] == int(null)
    assert len(result["result"]["readback"]["missing"]) == int(not null)


def test_successful_write_cannot_complete_without_fresh_local_readback(fixture):
    host, _, _, closed, _ = fixture
    empty = InMemoryTimeSeriesStore()
    host.read_factory = lambda: CpiTimeSeriesService(empty, Source())
    result = host.submit(command())
    assert result["state"] == "failed"
    assert result["classification"] == "local_readback_incomplete"
    assert result["result"]["readback"]["verified"] is False
    assert len(closed) == 1


def test_interrupted_job_is_unknown_and_never_replayed(fixture):
    host, _, source, _, _ = fixture
    request = command()
    with host.journal.execution():
        host.journal.save(
            dict(
                request,
                job_id=request["request_id"],
                state="running",
                created_at=NOW.isoformat(),
                started_at=NOW.isoformat(),
                finished_at=None,
                classification=None,
                result=None,
            )
        )
    raw_before = next(host.journal.directory.glob("*.json")).read_bytes()
    assert host.journal.list()[0]["state"] == "unknown"
    assert host.submit(request)["state"] == "unknown"
    assert next(host.journal.directory.glob("*.json")).read_bytes() == raw_before
    assert source.calls == 0


def test_readback_exception_closes_services_and_records_unknown(fixture):
    host, _, _, closed, _ = fixture

    def fail():
        raise RuntimeError("private-error-synthetic-secret")

    host.read_factory = fail
    result = host.submit(command())
    assert result["state"] == "unknown"
    assert result["classification"] == "internal_error"
    assert "private-error" not in json.dumps(result)
    assert len(closed) == 1


@pytest.mark.parametrize(
    "query",
    [
        "start=2026-09&end=2026-09&local_only=false",
        "start=2026-09&end=2026-09&start=2026-08",
        "start=2026-09&end=2026-09&endpoint=https://invalid.example",
        "start=invalid&end=2026-09",
    ],
)
def test_invalid_queries_never_open_service(fixture, query):
    host, _, source, closed, _ = fixture
    with client(host) as http:
        assert http.get(PREFIX + "/query?" + query).status_code == 400
    assert source.calls == 0 and closed == []


@pytest.mark.parametrize(
    "change",
    [
        {"client": ("10.2.3.4", 123)},
        {"method": "PUT"},
        {"path": "/api/cpi/init"},
        {"query_string": b"start=2026-09"},
        {"extra_headers": [(b"x-forwarded-for", b"192.168.10.25")]},
        {"extra_headers": [(b"origin", b"http://evil.example")]},
        {"extra_headers": [(b"host", b"192.168.10.20:18761")]},
        {"extra_headers": [(b"transfer-encoding", b"chunked")]},
    ],
)
def test_same_origin_lan_admission_rejects_spoofing(change):
    headers = [
        (b"host", b"192.168.10.20:18761"),
        (b"origin", CANDIDATE_ORIGIN.encode()),
        (b"content-type", b"application/json"),
    ]
    extra = change.pop("extra_headers", [])
    scope = dict(
        type="http",
        method="POST",
        path=PREFIX + "/jobs",
        scheme="http",
        query_string=b"",
        client=("192.168.10.25", 123),
        headers=headers + extra,
    )
    scope.update(change)
    assert admitted_job(Request(scope)) is False


def test_valid_but_not_authorized_job_has_no_journal_side_effect(fixture):
    host, _, source, closed, _ = fixture
    host.write_factory = None
    with client(host) as http:
        response = http.post(
            PREFIX + "/jobs", json=command(), headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert response.status_code == 403
        assert (
            response.json()["classification"]
            == "cpi_package_credential_binding_unavailable"
        )
    assert source.calls == 0 and closed == []
    assert not host.journal.directory.exists()


def test_default_legacy_app_has_no_new_timeseries_routes():
    paths = create_app().openapi()["paths"]
    assert not any(path.startswith(PREFIX) for path in paths)


def test_update_omission_cannot_hide_behind_existing_local_coverage(fixture):
    host, _, source, _, _ = fixture
    assert host.submit(command())["state"] == "complete"
    source.skip.add((SERIES_IDS[0], "2026-09"))
    result = host.submit(command("update"))
    assert result["state"] == "failed"
    assert result["classification"] == "source_range_incomplete"
    assert result["result"]["coverage_complete"] is False
    readback = result["result"]["readback"]
    assert readback["local_coverage_complete"] is True
    assert readback["source_missing"] == [
        {"series_id": SERIES_IDS[0], "month": "2026-09"}
    ]


def test_journal_recovers_old_running_before_another_job(fixture):
    host, _, source, _, _ = fixture
    request = command()
    with host.journal.execution():
        host.journal.save(
            dict(
                request,
                job_id=request["request_id"],
                state="running",
                created_at=NOW.isoformat(),
                started_at=NOW.isoformat(),
                finished_at=None,
                classification=None,
                result=None,
            )
        )
    with host.journal.execution():
        assert host.journal.read(request["request_id"])["state"] == "unknown"
    assert host.submit(request)["state"] == "unknown"
    assert source.calls == 0


def test_busy_writer_is_bounded_and_does_not_create_second_job(fixture):
    host, _, source, _, _ = fixture
    with host.journal.execution():
        with pytest.raises(WebError, match="cpi_jobs_busy"):
            host.submit(command())
    assert list(host.journal.directory.glob("*.json")) == []
    assert source.calls == 0


def test_replay_survives_host_limit_or_source_revocation(fixture):
    host, _, source, _, _ = fixture
    request = command(start="2026-08")
    job = host.submit(request)
    host.max_collect_months = 1
    host.allow_collect = False
    assert host.submit(request) == job
    assert source.calls == 1


def test_explicit_operational_limit_accepts_boundary_and_rejects_excess(fixture):
    host, _, source, _, _ = fixture
    host.max_collect_months = 24
    assert host.status()["collection_scope"]["max_months"] == 24
    assert host.submit(command(start="2024-10"))["state"] == "complete"
    with pytest.raises(WebError, match="collection_range_exceeds_host_limit"):
        host.submit(command(start="2024-09"))
    assert source.calls == 1
    assert len(host.journal.list()) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"schema": "arbitrary"},
        {"start": "1900-12"},
        {"start": "1901-01", "end": "2101-01"},
        {"action": "schedule/due"},
        {"request_id": "../invalid"},
    ],
)
def test_invalid_jobs_have_no_execution_or_journal_effect(fixture, extra):
    host, _, source, closed, _ = fixture
    with client(host) as http:
        result = http.post(
            PREFIX + "/jobs",
            json=command(**extra),
            headers={"Origin": CANDIDATE_ORIGIN},
        )
    assert result.status_code == 400
    assert source.calls == 0 and closed == []
    assert not host.journal.directory.exists()
