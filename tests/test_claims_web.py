"""Actual Claims router/runtime with deterministic stores and source wire responses."""

import importlib.util
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from market_data_foundation.historical.claims_collection import ClaimsCollectionService
from market_data_foundation.historical.claims_contract import SERIES_IDS, weeks
from market_data_foundation.historical.claims_source import (
    ClaimsFredSource,
    ClaimsResponse,
)
from market_data_foundation.historical.claims_timeseries import ClaimsObservation
from market_data_foundation.web.claims_host import PREFIX
from market_data_foundation.web.claims_runtime import ClaimsOptions
from market_data_foundation.web.cpi_timeseries_host import create_timeseries_preview
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_readonly_host as old
from tests.test_claims_source import Control

AT = datetime(2026, 11, 2, 18, tzinfo=UTC)
HEADERS = dict(Origin=CANDIDATE_ORIGIN, **{"Content-Type": "application/json"})


class Store:
    def __init__(self, clock):
        self.clock, self.rows, self.depth = clock, [], 0

    @contextmanager
    def read_snapshot(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1

    def read_range(self, series, start, end, *args):
        return [
            r
            for r in self.rows
            if r.series_id in series and start <= r.week_ending <= end
        ]

    def save_batches(self, batches):
        assert not self.depth
        n = 0
        for b in batches:
            for p in b.points:
                if any(
                    r.series_id == p.series_id and r.week_ending == p.week_ending
                    for r in self.rows
                ):
                    continue
                self.rows.append(
                    ClaimsObservation(
                        p.series_id,
                        p.week_ending,
                        p.value,
                        b.observed_at,
                        b.observed_at,
                        1,
                        p.provider_extra,
                        batch_id="synthetic",
                    )
                )
                n += 1
        return dict(saved_points=n, saved_revisions=0)

    def status(self):
        return dict(
            observation_count=len(self.rows),
            revision_count=0,
            batch_count=0,
            latest_weeks={
                s: max(
                    (r.week_ending for r in self.rows if r.series_id == s), default=None
                )
                for s in SERIES_IDS
            },
        )

    def close(self):
        pass


def setup(tmp_path, monkeypatch, **overrides):
    from market_data_foundation.historical.claims_release import ClaimsReleaseRecords

    _, previous, *_ = old.setup(tmp_path, monkeypatch, enabled_caps=("claims",))
    with (previous.settings.config_directory / "secret.toml").open("a") as f:
        f.write('\n[fred]\napi_key="synthetic-fred-key"\n')
    monkeypatch.setattr(ClaimsReleaseRecords, "_rows", lambda *args: [])
    clock, calls = [AT], []
    store = Store(lambda: clock[0])

    def transport(request):
        assert not store.depth
        calls.append(request)
        p = request.parameters
        ws = weeks(p["observation_start"], p["observation_end"])
        return ClaimsResponse(
            200,
            json.dumps(
                dict(
                    count=len(ws),
                    offset=0,
                    units="lin",
                    output_type=1,
                    sort_order="asc",
                    observation_start=p["observation_start"],
                    observation_end=p["observation_end"],
                    observations=[
                        dict(
                            date=w,
                            value="100",
                            realtime_start="2026-11-02",
                            realtime_end="2026-11-02",
                        )
                        for w in ws
                    ],
                )
            ).encode(),
        )

    def read():
        return ClaimsCollectionService(store, clock=lambda: clock[0])

    def write(*, credential_loader):
        assert credential_loader() == "synthetic-fred-key"
        return ClaimsCollectionService(
            store,
            ClaimsFredSource(
                transport=transport, request_control=Control(), clock=lambda: clock[0]
            ),
            clock=lambda: clock[0],
        )

    def material():
        raise ValueError("synthetic private diagnostic")

    config = dict(
        material_fetcher=material,
        schedule_path=tmp_path / "claims-schedule.json",
        job_directory=tmp_path / "claims-jobs",
        read_factory=read,
        write_factory=write,
        query_factory=write,
        allow_read=True,
        allow_collect=True,
        allow_source=True,
    )
    config.update(overrides)
    app = create_timeseries_preview(
        previous.settings,
        enabled=True,
        connector=previous.connector,
        clock=lambda: clock[0],
        schedule_path=tmp_path / "cpi-schedule.json",
        job_directory=tmp_path / "cpi-jobs",
        claims_options=ClaimsOptions(**config),
    )
    return app, app.state.timeseries_runtime.claims_timeseries, store, clock, calls


def client(app):
    return TestClient(app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234))


def command():
    return dict(
        request_id=str(uuid4()), action="init", start="2026-10-01", end="2026-10-31"
    )


def test_all_gets_are_local_and_do_not_create_state_or_liveness(tmp_path, monkeypatch):
    app, host, store, _, calls = setup(tmp_path, monkeypatch)
    with client(app) as http:
        for op in (
            "status",
            "strategy",
            "schedule",
            "executions",
            "jobs",
            "api-help",
            "releases",
        ):
            r = http.get(PREFIX + "/" + op)
            assert r.status_code == 200, r.text
        result = http.get(PREFIX + "/query?start=2026-10-01&end=2026-10-31").json()
        assert result["local_only"] and result["as_of_basis"] == "available"
        assert len(result["rows"]) == 30
        assert host.strategy()["worker"]["state"] == "not_started"
    assert calls == [] and not store.rows
    assert (
        not host.scheduler.state_path.exists() and not host.journal.directory.exists()
    )


@pytest.mark.parametrize(
    "suffix",
    [
        "start=2026-10&end=2026-10-31",
        "start=2026-10-31&end=2026-10-01",
        "start=2026-10-01&end=2026-10-31&extra=1",
        "start=2026-10-01&end=2026-10-31&local_only=0",
        "start=2026-10-01&end=2026-10-31&series=ICSA,ICSA",
        "start=2026-10-01&end=2026-10-31&series=FAKE",
        "start=2026-10-01&end=2026-10-31&start=2026-10-02",
        "start=2026-10-01&end=2026-10-31&query_mode=history",
        "start=2026-10-01&end=2026-10-31&as_of=2026-11-02",
        "start=2026-10-01&end=2026-10-31&as_of_basis=official",
    ],
)
def test_strict_parameters(tmp_path, monkeypatch, suffix):
    app, _, store, _, calls = setup(tmp_path, monkeypatch)
    with client(app) as http:
        assert http.get(PREFIX + "/query?" + suffix).status_code == 400
    assert calls == [] and not store.rows


def test_current_gap_fill_never_persists_and_history_is_zero_source(
    tmp_path, monkeypatch
):
    app, _, store, clock, calls = setup(tmp_path, monkeypatch)
    with client(app) as http:
        r = http.get(PREFIX + "/query?start=2026-10-01&end=2026-10-31&local_only=false")
        assert r.status_code == 200, r.text
        assert r.json()["coverage"]["available"] == 30 and len(calls) == 6
        assert not store.rows
        history = http.get(
            PREFIX + "/query",
            params=dict(
                start="2026-10-01",
                end="2026-10-31",
                local_only="false",
                as_of=clock[0].isoformat(),
            ),
        )
        assert history.status_code == 200 and history.json()["local_only"]
        assert len(calls) == 6


def test_async_receipt_durable_idempotency_material_failure_and_strategy(
    tmp_path, monkeypatch
):
    app, host, store, _, calls = setup(tmp_path, monkeypatch)
    entered, release, finished = Event(), Event(), Event()
    original = host.capture_material
    from market_data_foundation.web.cpi_timeseries_jobs import JobJournal

    original_save = JobJournal.save

    def save(journal, record):
        original_save(journal, record)
        if record["state"] != "running":
            finished.set()

    monkeypatch.setattr(JobJournal, "save", save)

    def capture(*args):
        entered.set()
        release.wait(5)
        return original(*args)

    host.capture_material = capture
    cmd = command()
    with client(app) as http:
        response = http.post(PREFIX + "/jobs", json=cmd, headers=HEADERS)
        assert response.status_code == 202 and response.json()["state"] == "running"
        assert entered.wait(2)
        assert http.get(PREFIX + "/jobs").json()["jobs"][0]["state"] == "running"
        release.set()
        assert finished.wait(2)
        job = http.get(PREFIX + "/jobs").json()["jobs"][0]
        assert job["state"] == "complete", job
        assert job["result"]["readback"]["verified"]
        assert job["result"]["material"]["status"] == "unavailable"
        assert "rows" not in job["result"]
        help_receipt = http.get(PREFIX + "/api-help").json()["collection"]["receipt"]
        assert set(help_receipt["fields"]) == set(job["result"])
        assert help_receipt["gap_limit_per_series"] == 16
        executions = http.get(PREFIX + "/executions").json()["executions"]
        assert executions[0]["result"] == job["result"]
        query = http.get(PREFIX + "/query?start=2026-10-01&end=2026-10-31").json()
        assert len(query["rows"]) == 30
        assert len(calls) == 6 and len(store.rows) == 54
        assert http.post(PREFIX + "/jobs", json=cmd, headers=HEADERS).status_code == 200
        assert len(calls) == 6
        assert (
            http.post(
                PREFIX + "/jobs", json=dict(cmd, action="update"), headers=HEADERS
            ).status_code
            == 409
        )
        strategy = http.put(
            PREFIX + "/strategy", json=dict(enabled=True), headers=HEADERS
        )
        assert strategy.status_code == 200, strategy.text
        assert strategy.json()["worker"]["state"] == "not_started"
        assert (
            http.put(
                PREFIX + "/strategy", json=dict(enabled=False), headers=HEADERS
            ).json()["enabled"]
            is False
        )


def test_permission_and_json_boundaries_and_openapi(tmp_path, monkeypatch):
    app, host, store, _, calls = setup(
        tmp_path, monkeypatch, allow_collect=False, allow_source=False
    )
    with client(app) as http:
        assert (
            http.post(PREFIX + "/jobs", json=command(), headers=HEADERS).status_code
            == 403
        )
        assert (
            http.put(
                PREFIX + "/strategy", json=dict(enabled=True), headers=HEADERS
            ).status_code
            == 403
        )
        assert (
            http.get(
                PREFIX + "/query?start=2026-10-01&end=2026-10-31&local_only=false"
            ).status_code
            == 403
        )
        assert (
            http.post(
                PREFIX + "/jobs",
                content='{"action":"init","action":"update"}',
                headers=HEADERS,
            ).status_code
            == 400
        )
        assert http.get(PREFIX + "/status?x=1").status_code == 400
        assert (
            http.get(
                PREFIX + "/status", headers={"Origin": "https://invalid.example"}
            ).status_code
            == 403
        )
    schema = app.openapi()["paths"][PREFIX + "/jobs"]["post"]["requestBody"]["content"][
        "application/json"
    ]["schema"]
    assert schema["additionalProperties"] is False
    assert schema["properties"]["start"]["format"] == "date"
    assert not calls and not store.rows


def test_http_history_uses_one_cutoff_for_all_derived_operands(tmp_path, monkeypatch):
    app, _, store, _, calls = setup(tmp_path, monkeypatch)
    from tests.test_claims_timeseries import T1, T2, row

    store.rows = [
        row("2026-09-19", "100"),
        row("2026-09-26", "120"),
        row("2026-09-19", "200", revision=2, observed=T1, available=T2),
    ]
    with client(app) as http:
        for cutoff, value in ((T2 - timedelta(microseconds=1), "20"), (T2, "-80")):
            r = http.get(
                PREFIX + "/query",
                params=dict(
                    start="2026-09-26",
                    end="2026-09-26",
                    series="ICSA",
                    as_of=cutoff.isoformat(),
                ),
            )
            assert r.status_code == 200, r.text
            assert r.json()["rows"][0]["weekly_change"]["value"] == value
    assert not calls


def test_unified_worker_requires_opt_in_and_independent_claims_paths():
    spec = importlib.util.spec_from_file_location(
        "claims_worker", Path("scripts/mdf_cpi_timeseries_worker.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    args = module.build_parser().parse_args(
        [
            "--asset",
            "claims",
            "--config-directory",
            "/tmp/config",
            "--package-store-directory",
            "/tmp/packages",
            "--environment-id",
            "synthetic",
            "--pg-role",
            "reader",
            "--pce-schema-owner",
            "owner",
            "--pce-schema-oid",
            "1",
            "--schedule-path",
            "/tmp/cpi-schedule",
            "--job-directory",
            "/tmp/cpi-jobs",
        ]
    )
    with pytest.raises(ValueError, match="explicit_claims_worker_opt_in_required"):
        module.build_runtime(args)
    args.opt_in_claims_worker = True
    with pytest.raises(ValueError, match="claims_independent_paths_required"):
        module.build_runtime(args)


def test_durable_interrupted_job_is_unknown_and_not_replayed(tmp_path, monkeypatch):
    app, host, store, _, calls = setup(tmp_path, monkeypatch)
    cmd = command()
    host.journal.directory.mkdir()
    host.journal.save(
        dict(
            cmd,
            job_id=cmd["request_id"],
            state="running",
            asset="claims",
            created_at=AT.isoformat(),
            result=None,
            classification=None,
        )
    )
    before = (
        host.journal.path
        if hasattr(host.journal, "path")
        else host.journal.directory / (cmd["request_id"] + ".json")
    )
    raw = before.read_bytes()
    with client(app) as http:
        assert http.get(PREFIX + "/jobs").json()["jobs"][0]["state"] == "unknown"
        assert before.read_bytes() == raw  # GET does not repair the journal
        replay = http.post(PREFIX + "/jobs", json=cmd, headers=HEADERS)
        assert replay.status_code == 200 and replay.json()["state"] == "unknown"
    assert not calls and not store.rows


def test_current_snapshot_material_old_report_and_failure_are_separate(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.calendar_resilience import AcquiredCalendar
    from market_data_foundation.historical.claims_calendar import CURRENT_REPORT_URL
    from market_data_foundation.historical.claims_release import (
        ClaimsReleaseRecords,
        parse_release,
    )
    from tests.test_claims_release import pages

    app, host, store, _, calls = setup(tmp_path, monkeypatch)
    raw = "\f".join(pages()).encode()
    acquired_at = host.clock()
    host.material_fetcher = lambda: AcquiredCalendar(raw, acquired_at)
    imported = []

    def write_material(records, raw, url, *, obtained_at):
        assert url == CURRENT_REPORT_URL
        assert type(raw) is bytes
        assert obtained_at == acquired_at
        doc = dict(parse_release(raw, url), evidence_id="synthetic")
        imported.append(doc)
        return doc

    monkeypatch.setattr(ClaimsReleaseRecords, "import_material", write_material)
    monkeypatch.setattr(
        ClaimsReleaseRecords, "read", lambda *args, **kwargs: dict(events=imported)
    )
    with host.service() as service:
        old = host.capture_material(service, "dol-claims-2026-11-05")
        assert old["status"] == "source_not_updated" and not imported
        actual = host.capture_material(service, "dol-claims-2026-10-29")
        assert actual["status"] == "available" and actual["readback_verified"]
        assert len(actual["actual_weeks"]) == 6
        assert actual["actual_weeks"]["ICSA"] == "2026-10-24"
        assert actual["actual_weeks"]["CCSA"] == "2026-10-17"
    assert imported[0]["evidence_kind"] == "official_current_report_snapshot"
    assert not calls and not store.rows


def test_runtime_paths_and_help_match_openapi(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="claims_independent_paths_required"):
        setup(tmp_path, monkeypatch, job_directory=tmp_path / "cpi-jobs")
    # Use a fresh fixture root because setup initializes its trusted directory.
    other = tmp_path / "other"
    other.mkdir()
    app, host, *_ = setup(other, monkeypatch)
    params = app.openapi()["paths"][PREFIX + "/query"]["get"]["parameters"]
    assert set(host.api_help()["parameters"]) == {p["name"] for p in params}
    assert (
        "available default"
        in next(p for p in params if p["name"] == "as_of_basis")["description"]
    )
    assert host.asset_metadata()["asset_id"] == "mdf.macro.dol.claims"


def test_historical_calendar_cutoff_survives_refresh_and_does_not_leak_moves(
    tmp_path, monkeypatch
):
    from tests.test_claims_schedule import html

    app, host, _, clock, calls = setup(tmp_path, monkeypatch)
    host.scheduler._accept(host.scheduler._read(), html(), clock[0])
    acquired = clock[0]
    clock[0] += timedelta(days=1)
    changed = html().replace(
        b"<tr><td>Wednesday, November 25, 2026</td><td>8:30 AM EST</td></tr>", b""
    )
    host.scheduler._accept(host.scheduler._read(), changed, clock[0])
    clock[0] += timedelta(days=1)
    host.scheduler._accept(host.scheduler._read(), changed, clock[0])
    with client(app) as http:

        def events(cutoff):
            response = http.get(
                PREFIX + "/query",
                params=dict(
                    start="2026-10-01", end="2026-10-31", as_of=cutoff.isoformat()
                ),
            )
            assert response.status_code == 200, response.text
            return {e["event_id"] for e in response.json()["publication_events"]}

        assert not events(acquired - timedelta(microseconds=1))
        before = events(acquired)
        assert "dol-claims-2026-11-25" in before
        assert "dol-claims-2026-11-26" not in before
        after = events(clock[0])
        assert "dol-claims-2026-11-26" in after
        assert "dol-claims-2026-11-25" not in after
    assert not calls
