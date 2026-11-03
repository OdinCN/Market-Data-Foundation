"""Offline fixtures only: JOLTS calendar, evidence, scheduler and real job execution."""

from datetime import timedelta

import pytest

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.bls_web_transport import BlsWebError
from market_data_foundation.historical.calendar_publications import publication_events
from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.jolts_release_schedule import (
    CALENDAR_URL,
    JoltsReleaseScheduler,
    annual_window,
    parse_official_ics,
)
from market_data_foundation.historical.macro_release_acquisition import select_material
from market_data_foundation.historical.macro_release_material import parse_material
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from tests.test_cpi_release_schedule_ics import event, html, ics
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_jolts_timeseries import AT, OPENINGS_RATE, parse_response, response
from tests.test_jolts_web import PREFIX
from tests.test_jolts_web import setup as setup
from tests.test_macro_release_chain import Raw
from tests.test_ppi_timeseries_web import client

TITLE = "Job Openings and Labor Turnover Survey"
REF = ("September 2026", "Nov. 3, 2026", "10:00 AM")
URL = "https://www.bls.gov/news.release/archives/jolts_11032026.htm"
RAW = b"""<h1>JOB OPENINGS AND LABOR TURNOVER - September 2026</h1>
<p>Transmission of material in this release is embargoed until
10:00 a.m. (ET) Tuesday, November 3, 2026.</p>
<p>Seasonally adjusted. The job openings rate was unchanged at 4.3 percent.</p>"""


def scheduler(tmp_path, clock=lambda: AT, **options):
    options.setdefault(
        "calendar_fetcher",
        lambda: ics(event("jolts", "20261103T100000", summary=TITLE)),
    )
    options.setdefault("reference_fetcher", lambda: html(REF))
    return JoltsReleaseScheduler(tmp_path / "calendar.json", clock=clock, **options)


def test_national_family_and_no_month_guess():
    raw = ics(
        event("jolts", summary=TITLE),
        event("state", summary="State Job Openings and Labor Turnover"),
        event("employment", summary="Employment Situation"),
    )
    parsed = parse_official_ics(raw)
    assert [r["uid"] for r in parsed] == ["jolts"]
    assert "reference_month" not in parsed[0]
    assert parsed[0]["release_at"].endswith("13:30:00+00:00")


def test_calendar_cache_boundary_and_available_cutoff(tmp_path):
    now = [AT]
    sch = scheduler(tmp_path, clock=lambda: now[0])
    result = sch.import_calendar(html(REF), AT)
    assert result["calendar_status"] == "fresh"
    args = (result, "https://www.bls.gov/")
    assert publication_events(*args, as_of=AT - timedelta(microseconds=1)) == {}
    assert (
        publication_events(*args, as_of=AT, as_of_basis="available")["2026-09"][
            "status"
        ]
        == "scheduled"
    )
    # Old obtained evidence imported now must not become available in the past.
    now[0] += timedelta(hours=1)
    result = sch.import_calendar(html(REF), AT)
    assert (
        publication_events(
            result, "https://www.bls.gov/", as_of=AT, as_of_basis="available"
        )
        == {}
    )
    assert publication_events(
        result, "https://www.bls.gov/", as_of=AT, as_of_basis="observed"
    )
    now[0] = AT + timedelta(hours=24) - timedelta(microseconds=1)
    assert sch.status()["calendar_status"] == "fresh"
    now[0] += timedelta(microseconds=1)
    assert sch.status()["calendar_status"] == "stale"


def test_calendar_change_cancel_conflict(tmp_path):
    sch = scheduler(tmp_path)
    sch.import_calendar(html(REF), AT)
    sch.import_calendar(ics(event("jolts", "20261103T100000", summary=TITLE)), AT)
    moved = sch.import_calendar(
        ics(event("jolts", "20261104T100000", 2, summary=TITLE)), AT
    )
    assert moved["targets"][0]["release_at"] == "2026-11-04T15:00:00+00:00"
    cancelled = sch.import_calendar(
        ics(event("jolts", None, 3, summary=TITLE, extra="STATUS:CANCELLED\r\n")), AT
    )
    assert cancelled["targets"][0]["state"] == "calendar_removed"
    with pytest.raises(CpiScheduleError, match="calendar_ics_revision_conflict"):
        parse_official_ics(
            ics(
                event("same", "20261103T100000", summary=TITLE),
                event("same", "20261104T100000", summary=TITLE),
            )
        )


def test_same_agency_fallback_failure_diagnostics(tmp_path):
    calls = []

    def primary():
        calls.append("ics")
        raise BlsWebError("http_403", http_status=403)

    sch = scheduler(
        tmp_path, calendar_fetcher=primary, fallback_fetcher=lambda: html(REF)
    )
    result = sch.refresh_calendar()
    assert calls == ["ics"]
    assert result["calendar"]["source"] == CALENDAR_URL
    assert result["calendar"]["refresh_attempts"][0]["classification"] == "http_403"


def test_material_identity_partial_and_no_rounded_level_claims():
    doc = parse_material(RAW, URL, "jolts")
    assert doc["reference_month"] == "2026-09"
    assert doc["actual_at"] is None and doc["surprise"] is None
    assert doc["claims"] == []
    assert len(doc["missing_scopes"]) == 8
    assert "jolts_narrative_numeric_scope_not_verified" in doc["missing"]
    for raw, url in (
        (RAW.replace(b"TURNOVER -", b"TURNOVER STATE -"), URL),
        (RAW, URL.replace("11032026", "11042026")),
        (RAW, URL.replace("jolts_", "empsit_")),
    ):
        with pytest.raises(CpiTimeSeriesError):
            parse_material(raw, url, "jolts")


@pytest.mark.parametrize(
    "narrative",
    [
        b"Not seasonally adjusted. The job openings rate was at 4.3 percent.",
        b"Seasonally adjusted. The job openings rate was at 4.3 percent in August.",
        b"Seasonally adjusted. The job openings rate was at 4.3 percent "
        b"in September 2025.",
        b"Seasonally adjusted. In manufacturing, the job openings rate "
        b"was at 4.3 percent.",
        b"Seasonally adjusted. In the Northeast, the job openings rate "
        b"was at 4.3 percent.",
        b"Seasonally adjusted. <h2>State estimates</h2>"
        b"The job openings rate was at 4.3 percent.",
        b"Seasonally adjusted. <table><tr><td>The job openings rate "
        b"was at 4.3 percent.</td></tr></table>",
        b"Seasonally adjusted. <h2>Revisions</h2>"
        b"The job openings rate was at 4.3 percent.",
    ],
)
def test_material_unverified_numeric_scopes_remain_scope_only(narrative):
    header = RAW[: RAW.index(b"<p>Seasonally adjusted.")]
    doc = parse_material(header + b"<p>" + narrative + b"</p>", URL, "jolts")
    assert doc["reference_month"] == "2026-09"
    assert doc["event_id"] == "bls:jolts:2026-11-03"
    assert doc["claims"] == []
    assert len(doc["missing_scopes"]) == 8
    assert {scope["adjustment"] for scope in doc["missing_scopes"]} == {"sa"}
    assert {scope["reference_month"] for scope in doc["missing_scopes"]} == {"2026-09"}


def test_release_records_cutoff_and_read_get_no_effects(setup):
    app, host, data, source, root = setup
    data.raw_store = Raw(JOLTS_PROFILE)
    repo = ReleaseRecords(data)
    repo.prepare()
    saved = repo.import_material(RAW, URL, material_format="original_http_body")
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with client(app) as http:
        event = http.get(PREFIX + "/releases").json()["events"][0]
        assert event["claims"] == []
        assert len(event["missing_scopes"]) == 8
        assert event["state"] == "unknown"
        assert (
            "jolts_narrative_numeric_scope_not_verified"
            in event["material_deficiencies"][0]["missing"]
        )
        assert (
            http.get(
                PREFIX + "/releases",
                params={"as_of": (source.at - timedelta(microseconds=1)).isoformat()},
            ).json()["events"]
            == []
        )
        for path in ("/schedule", "/strategy", "/status", "/api-help", "/executions"):
            assert http.get(PREFIX + path).status_code == 200
    assert saved["obtained_at"] == saved["available_at"] == source.at.isoformat()
    assert not source.calls
    assert before == {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}


def test_material_selection_requires_explicit_due_reference(tmp_path):
    sch = scheduler(tmp_path)
    sch.import_calendar(html(REF), AT)
    assert select_material("jolts", sch.status(), "2026-09", "2026-09", AT) is None
    selection = select_material(
        "jolts", sch.status(), "2026-09", "2026-09", AT + timedelta(hours=16)
    )
    assert selection["source_url"] == URL
    assert (
        select_material(
            "jolts", sch.status(), "2026-08", "2026-08", AT + timedelta(hours=16)
        )
        is None
    )


def test_real_job_worker_annual_readback_dedup_and_restore(setup):
    _, host, _, source, _ = setup
    host.scheduler.calendar_fetcher = lambda: ics()
    host.scheduler.reference_fetcher = lambda: html(REF)
    host.scheduler.fallback_fetcher = lambda: html(REF)
    host.scheduler.maintenance_only = True
    assert host.auto_strategy.tick()["action"] == "paused"
    assert not source.calls
    host.update_strategy({"enabled": True})
    first = host.auto_strategy.tick()
    assert first["action"] == "annual_maintenance"
    assert first["execution"]["start"] == "2021-01"
    assert first["execution"]["end"] == "2025-12"
    assert first["execution"]["state"] == "complete", first["execution"]
    assert first["execution"]["result"]["readback"]["verified"]
    assert source.calls[-1][1:] == ("2020-01", "2025-12")
    assert len(source.calls) == 1
    assert host.auto_strategy.tick()["action"] == "not_due"
    assert len(source.calls) == 1
    assert host.executions()["executions"][0]["origin"] == "automatic"
    host.update_strategy({"enabled": False})
    assert host.auto_strategy.tick()["action"] == "paused"


def test_worker_retry_and_no_calendar_invented_event(tmp_path):
    now = [AT]
    sch = scheduler(tmp_path, clock=lambda: now[0], calendar_fetcher=lambda: ics())
    sch.maintenance_only = True
    jobs = []

    def submit(command):
        jobs.append(command)
        return dict(
            state="unknown" if len(jobs) == 1 else "complete",
            classification="source_timeout",
            result={},
            finished_at=now[0].isoformat(),
        )

    sch.submit = submit
    first = sch.due_check()
    assert first["execution"]["state"] == "unknown"
    assert first["targets"] == []
    assert sch.due_check()["action"] == "not_due"
    now[0] += timedelta(seconds=300)
    second = sch.due_check()
    assert second["execution"]["state"] == "complete"
    assert jobs[0]["request_id"] != jobs[1]["request_id"]
    assert annual_window(2027) == ("2022-01", "2026-12")


@pytest.mark.parametrize("target_value", ["0", "4.3"])
def test_real_host_release_waits_for_target_then_accepts_zero(setup, target_value):
    from dataclasses import replace

    _, host, _, source, _ = setup
    source.at = AT + timedelta(hours=15)
    host.scheduler.calendar_fetcher = lambda: ics(
        event("jolts", "20261103T100000", summary=TITLE)
    )
    host.scheduler.reference_fetcher = lambda: html(REF)
    original = source.fetch
    mode = ["missing"]

    def fetch(series, start, end):
        batches = original(series, start, end)
        if mode[0] == "missing":
            from market_data_foundation.historical.cpi_timeseries import month_range

            raw = response(series, month_range(start, end)[:-1], source.overrides)
            return tuple(
                replace(batch, points=parse_response(raw, series, start, end), raw=raw)
                for batch in batches
            )
        return batches

    source.fetch = fetch
    first = host.scheduler.due_check()
    assert first["execution"]["state"] == "awaiting_provider"
    assert first["execution"]["classification"] == "target_month_not_available"
    job = host.journal.list()[0]
    assert job["state"] == "complete"
    assert job["result"]["readback"]["verified"] is True
    assert len(job["result"]["source_missing"]) == 8
    assert not first["targets"][0]["target_available"]
    assert host.scheduler.due_check()["action"] == "not_due"
    assert len(source.calls) == 1
    mode[0] = "null"
    source.at += timedelta(seconds=300)
    source.overrides.update({(sid, "2026-09"): "-" for sid in JOLTS_PROFILE.series_ids})
    null = host.scheduler.due_check()
    assert null["execution"]["state"] == "awaiting_provider"
    assert null["execution"]["classification"] == "target_source_unavailable"
    assert (
        len(null["execution"]["result"]["target_evidence"]["source_unavailable"]) == 8
    )
    source.at += timedelta(seconds=900)
    source.overrides.update(
        {(sid, "2026-09"): target_value for sid in JOLTS_PROFILE.series_ids}
    )
    complete = host.scheduler.due_check()
    assert complete["execution"]["state"] == "complete"
    assert complete["targets"][0]["target_available"] is True
    assert complete["targets"][0]["classification"] is None
    assert complete["targets"][0]["next_attempt_at"] is None
    assert len(complete["execution"]["attempt_history"]) == 3
    assert len(source.calls) == 3
    assert host.journal.list()[0]["result"]["readback"]["verified"]


def test_early_annual_does_not_block_later_release_five_year_revision(setup):
    from datetime import datetime

    _, host, _, source, _ = setup
    source.at = datetime.fromisoformat("2026-03-01T16:00:00+00:00")
    host.scheduler.calendar_fetcher = lambda: ics()
    host.scheduler.reference_fetcher = lambda: html(REF)
    assert host.scheduler.due_check()["action"] == "annual_maintenance"
    assert host.scheduler.due_check()["action"] == "not_due"
    assert len(source.calls) == 1
    for month, release_date, instant in (
        ("January 2026", "Mar. 20, 2026", "2026-03-20T16:00:00+00:00"),
        ("February 2026", "Apr. 20, 2026", "2026-04-20T16:00:00+00:00"),
    ):
        source.at = datetime.fromisoformat(instant)
        revision = "0" if month == "January 2026" else "1.2"
        source.overrides[OPENINGS_RATE, "2021-01"] = revision
        host.scheduler.calendar_fetcher = lambda: html(
            (month, release_date, "10:00 AM")
        )
        host.scheduler.refresh_calendar()
        assert host.scheduler.due_check()["execution"]["state"] == "complete"
        if month == "January 2026":
            original = source.fetch

            def fail(*_):
                raise CpiTimeSeriesError("synthetic_source_timeout")

            source.fetch = fail
            failed = host.scheduler.due_check()
            assert failed["action"] == "post_release_maintenance"
            assert failed["execution"]["state"] == "unknown"
            assert host.scheduler.due_check()["action"] == "not_due"
            source.at += timedelta(seconds=300)
            source.fetch = original
        maintenance = host.scheduler.due_check()
        assert maintenance["action"] == "post_release_maintenance"
        assert maintenance["execution"]["state"] == "complete"
        assert (maintenance["execution"]["start"], maintenance["execution"]["end"]) == (
            "2021-01",
            "2025-12",
        )
        assert source.calls[-1][1:] == ("2020-01", "2025-12")
        assert host.scheduler.due_check()["action"] == "not_due"
    assert len(source.calls) == 5
    records = host.scheduler.status()["work"]
    assert records["annual:2026"]["state"] == "complete"
    assert len([k for k in records if k.startswith("annual_after:")]) == 2
    points = host.query({"start": "2021-01", "end": "2021-01"})["points"]
    revised = next(p for p in points if p["series_id"] == OPENINGS_RATE)
    assert revised["value"] == "1.2" and revised["revision"] == 3


def test_three_attempts_exhausted_and_retry_history_survives_restart(tmp_path):
    now = [AT]
    sch = scheduler(tmp_path, clock=lambda: now[0], calendar_fetcher=lambda: ics())
    sch.maintenance_only = True
    commands = []
    sch.submit = lambda cmd: (
        commands.append(cmd)
        or dict(
            state="unknown",
            result={},
            classification="source_timeout",
            finished_at=now[0].isoformat(),
        )
    )
    for delay in (0, 300, 900):
        now[0] += timedelta(seconds=delay)
        assert sch.due_check()["execution"]["state"] == "unknown"
    assert len(commands) == 3
    now[0] += timedelta(days=1)
    # New scheduler reads attempts instead of resetting its budget on restart.
    restarted = scheduler(
        tmp_path, clock=lambda: now[0], calendar_fetcher=lambda: ics()
    )
    restarted.maintenance_only = True
    restarted.submit = lambda *_: pytest.fail("exhausted work must not call source")
    assert restarted.due_check()["action"] == "not_due"
    record = restarted.status()["work"]["annual:2026"]
    assert len(record["attempt_history"]) == len(record["request_ids"]) == 3
    assert record["next_attempt_at"] is None


def test_missing_reference_evidence_never_schedules_release(tmp_path):
    sch = scheduler(tmp_path, reference_fetcher=lambda: b"<html>missing</html>")
    result = sch.refresh_calendar()
    assert result["targets"] == []
    assert result["calendar_publications"] == []


def test_api_help_matches_bounded_release_and_maintenance_policy(setup):
    app, host, _, _, _ = setup
    with client(app) as http:
        help_doc = http.get(PREFIX + "/api-help").json()
        policy = help_doc["periodic_policy"]
        assert policy == host.scheduler.status()["policy"]
        assert policy["retry_seconds"] == [300, 900]
        assert policy["max_attempts"] == 3
        assert policy["annual_target_months"] == 60
        assert "each numerically confirmed" in policy["annual_rule"]
        assert "source gaps" in help_doc["collection_parameters"]["completion"]


def test_legacy_failed_calendar_envelope_is_read_only(tmp_path):
    sch = scheduler(tmp_path)
    state = sch._read()
    state["calendar"] = {
        "error": "http_403",
        "next_refresh_at": "2026-11-04T06:19:27+00:00",
        "refresh_attempts": [
            {"source_url": sch.ics_url, "success": False, "http_status": 403}
        ],
    }
    sch._save(state)
    before = sch.state_path.read_bytes()
    result = sch.status()
    assert result["calendar_status"] == "unavailable"
    calendar = result["calendar"]
    assert calendar["source"] == sch.ics_url
    assert calendar["refresh_mode"] == "unavailable"
    assert calendar["cache_valid_until"] is None
    assert calendar["refresh_attempts"][0]["outcome"] == "failed"
    assert "acquired_at" not in calendar
    assert sch.state_path.read_bytes() == before


def test_jolts_reuses_shared_bls_web_headers_and_admission(monkeypatch):
    from market_data_foundation.historical import cpi_release_schedule as shared
    from market_data_foundation.historical.jolts_release_schedule import (
        fetch_official_page,
    )

    calls = []
    monkeypatch.setattr(
        shared,
        "fetch_bls_page",
        lambda url, **options: calls.append((url, options)) or b"ok",
    )
    for url in (
        shared.ICS_URL,
        "https://www.bls.gov/news.release/archives/jolts_11032026.htm",
    ):
        assert fetch_official_page(url) == b"ok"
    for url, options in calls:
        assert (
            options["headers"]["User-Agent"]
            == "Market-Data-Foundation JOLTS calendar/1.0"
        )
        assert options["timeout"] == 20
        assert options["request_control"] is not None
        assert options["allowed_path"] == url.removeprefix("https://www.bls.gov")


def test_strategy_uses_shared_official_time_without_guessing_reference_month(setup):
    _, host, _, _, _ = setup
    host.scheduler.import_calendar(
        ics(event("national", "20261103T100000", summary=TITLE)), AT
    )
    before = host.scheduler.state_path.read_bytes()
    value = host.strategy()
    assert value["next_release_at"] == "2026-11-03T15:00:00+00:00"
    assert value["execution_state"] == "paused"
    assert value["policy"]["annual_target_months"] == 60
    assert host.scheduler.status()["targets"] == []
    assert host.scheduler.state_path.read_bytes() == before


def test_strategy_reports_real_process_and_heartbeat(setup):
    import json
    import os

    _, host, _, _, _ = setup
    host.update_strategy({"enabled": True})
    path = host.auto_strategy.strategy_path.with_name("jolts-worker.json")
    path.write_text(json.dumps({"pid": os.getpid(), "state": "running"}))
    host.auto_strategy.heartbeat("idle")
    view = host.strategy()
    assert view["enabled"] is True
    assert view["execution_state"] == "idle"
    assert view["worker"]["state"] == "idle"
    assert view["worker"]["last_heartbeat_at"] == AT.isoformat()
    assert view["worker"]["process"]["pid"] == os.getpid()
