"""Normal job/worker paths over deterministic numeric and material ports."""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from market_data_foundation.historical.m3_release_acquisition import select_m3_material
from market_data_foundation.historical.m3_schedule import CALENDAR_URL
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from tests.test_m3_jolts_release_material import PDF_TEXT
from tests.test_m3_segment3 import AT
from tests.test_m3_segment4 import host


def configured(tmp_path, monkeypatch):
    from market_data_foundation.historical import m3_release_material

    h, s, wire = host(tmp_path)
    clock = [AT + timedelta(hours=16)]
    h.clock = lambda: clock[0]
    s.store.clock = h.clock
    wire.at = clock[0]
    snapshot = dict(
        calendar_status="fresh",
        calendar={"source": CALENDAR_URL},
        targets=[
            dict(
                key=d + ":2026-09",
                dataset=d,
                reference_month="2026-09",
                source=CALENDAR_URL,
                release_at=date,
                status="scheduled",
            )
            for d, date in [
                ("m3", "2026-11-03T15:00:00+00:00"),
                ("advm3", "2026-10-27T12:30:00+00:00"),
            ]
        ],
    )
    h.scheduler.status = lambda: snapshot
    text = [PDF_TEXT]
    calls = []
    monkeypatch.setattr(
        m3_release_material,
        "PdfReader",
        lambda _: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_: text[0])]
        ),
    )

    def fetch(url, family):
        calls.append((url, family))
        return b"synthetic bounded PDF transport"

    h.material_fetcher = fetch
    ReleaseRecords(s.store).prepare()
    return h, s, wire, clock, snapshot, text, calls


def command():
    return dict(
        request_id=str(uuid4()), action="update", start="2026-09", end="2026-09"
    )


def test_normal_m3_job_material_and_event_repeat_without_extra_source(
    tmp_path, monkeypatch
):
    h, s, wire, _, _, _, calls = configured(tmp_path, monkeypatch)
    cmd = command()
    done = h.submit_report("m3", cmd)
    assert done["state"] == "complete" and done["result"]["readback"]["verified"]
    material = done["result"]["release_material"]
    assert material["state"] == "retained" and material["event_ids"] == [
        "census:m3:full:2026-11-03"
    ]
    assert material["reports"]["m3"]["selection"]["report_stage"] == "full"
    assert ReleaseRecords(s.store).read(event_id=material["event_ids"][0])["events"]
    before = len(wire.calls)
    assert h.submit_report("m3", cmd, resume=True) == done
    assert len(wire.calls) == before and len(calls) == 1


def test_rolling_wrong_month_failure_and_material_only_recovery(tmp_path, monkeypatch):
    h, s, wire, clock, _, text, calls = configured(tmp_path, monkeypatch)
    cmd = command()
    text[0] = PDF_TEXT.replace("SEPTEMBER 2026", "OCTOBER 2026")
    first = h.submit_report("m3", cmd)
    before = len(wire.calls)
    assert first["state"] == "complete" and first["numeric_result"]["persisted"]
    assert first["result"]["release_material"]["reports"]["m3"]["reasons"] == [
        "release_material_calendar_identity_mismatch"
    ]
    assert ReleaseRecords(s.store).read()["events"] == []
    assert h.submit_report("m3", cmd) == first
    clock[0] += timedelta(hours=1, seconds=1)
    text[0] = PDF_TEXT
    recovered = h.submit_report("m3", cmd, resume=True)
    assert (
        recovered["state"] == "complete"
        and recovered["result"]["release_material"]["state"] == "retained"
    )
    assert recovered["numeric_result"] == first["numeric_result"]
    assert (
        recovered["finished_at"] == first["finished_at"]
        and recovered["started_at"] == first["started_at"]
    )
    assert len(wire.calls) == before and len(calls) == 2


def test_joint_material_stage_failure_does_not_erase_numeric_or_other_stage(
    tmp_path, monkeypatch
):
    h, s, wire, _, _, _, calls = configured(tmp_path, monkeypatch)
    h.joint_collections = True
    done = h.submit(command())
    material = done["result"]["release_material"]
    assert done["state"] == "complete" and all(
        r["readback_verified"] for r in done["result"]["reports"].values()
    )
    assert (
        material["state"] == "partial"
        and material["reports"]["m3"]["state"] == "retained"
    )
    assert material["reports"]["advm3"]["reasons"] == ["m3_report_stage_unproven"]
    assert material["event_ids"] == ["census:m3:full:2026-11-03"]
    assert len(ReleaseRecords(s.store).read()["events"]) == 1 and len(calls) == 2


def test_interruption_after_numeric_save_resumes_material_without_numeric_repeat(
    tmp_path, monkeypatch
):
    from market_data_foundation.web import m3_product_host as module

    h, s, wire, *_ = configured(tmp_path, monkeypatch)
    cmd = command()
    capture = module.capture_m3_material

    def interrupt(*_):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "capture_m3_material", interrupt)
    with pytest.raises(KeyboardInterrupt):
        h.submit_report("m3", cmd)
    before = len(wire.calls)
    assert h.journal.read(cmd["request_id"])["numeric_result"]["persisted"]
    monkeypatch.setattr(module, "capture_m3_material", capture)
    resumed = h.submit_report("m3", cmd, resume=True)
    assert (
        resumed["state"] == "complete"
        and resumed["result"]["release_material"]["state"] == "retained"
    )
    assert len(wire.calls) == before


def test_material_calendar_read_failure_keeps_numeric_success(tmp_path, monkeypatch):
    h, _, _, _, _, _, _ = configured(tmp_path, monkeypatch)

    def fail():
        raise ValueError("synthetic state error")

    h.scheduler.status = fail
    result = h.submit_report("m3", command())
    assert result["state"] == "complete" and result["result"]["readback"]["verified"]
    assert result["result"]["release_material"]["reasons"] == [
        "m3_material_acquisition_failed"
    ]


@pytest.mark.parametrize(
    "change",
    [
        {"calendar_status": "expired"},
        {"dataset": "advm3"},
        {"reference_month": "2026-10"},
        {"status": "TBA"},
    ],
)
def test_m3_selector_requires_calendar_stage_period_and_due_time(change):
    from datetime import UTC, datetime

    snapshot = dict(
        calendar_status="fresh",
        calendar={"source": CALENDAR_URL},
        targets=[
            dict(
                dataset="m3",
                reference_month="2026-09",
                status="scheduled",
                source=CALENDAR_URL,
                release_at="2026-11-03T15:00:00+00:00",
            )
        ],
    )
    if "calendar_status" in change:
        snapshot.update(change)
    else:
        snapshot["targets"][0].update(change)
    assert (
        select_m3_material(
            snapshot, "m3", "2026-09", "2026-09", datetime(2026, 11, 3, 16, tzinfo=UTC)
        )
        is None
    )


def scheduler_host(tmp_path, monkeypatch):
    from market_data_foundation.historical.m3_schedule import M3Scheduler
    from tests.test_m3_segment6 import CALENDAR

    h, s, wire, clock, _, _, calls = configured(tmp_path, monkeypatch)
    h.scheduler.clock = h.clock
    h.scheduler.status = M3Scheduler.status.__get__(h.scheduler)
    h.scheduler.maintain_history = None
    h.scheduler.import_calendar(
        CALENDAR.replace(b"10/27/2026", b"TBA"), acquired_at=clock[0]
    )
    return h, s, wire, clock, calls


def test_due_check_automatically_recovers_same_numeric_job_after_material_cooldown(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
    from market_data_foundation.historical.m3_schedule import M3Scheduler

    h, s, wire, clock, calls = scheduler_host(tmp_path, monkeypatch)
    real_fetch = h.material_fetcher
    failures = [True]

    def fetch(url, family):
        if failures.pop() if failures else False:
            calls.append((url, family))
            raise CpiTimeSeriesError("release_http_503")
        return real_fetch(url, family)

    h.material_fetcher = fetch
    first = h.scheduler.due_check()
    job = h.journal.read(first["request_id"])
    numeric_calls = len(wire.calls)
    assert (
        job["state"] == "complete"
        and job["result"]["release_material"]["state"] == "partial"
    )
    assert h.scheduler.status()["targets"][1]["target"]["material_retry"][
        "reasons"
    ] == ["release_http_503"]
    clock[0] += timedelta(minutes=5)
    assert h.scheduler.due_check()["action"] == "material_cooldown_wait"
    assert len(calls) == 1
    # A new scheduler instance reads the persisted cooldown and original task.
    restored = M3Scheduler(
        h.scheduler.state_path,
        history_start=h.scheduler.history_start,
        clock=h.clock,
        submit_report=h.submit_report,
    )
    clock[0] += timedelta(minutes=56)
    tick = restored.due_check()
    assert (
        tick["action"] == "material_retry" and tick["request_id"] == first["request_id"]
    )
    recovered = h.journal.read(first["request_id"])
    assert recovered["result"]["release_material"]["state"] == "retained"
    assert recovered["numeric_result"] == job["numeric_result"]
    assert recovered["finished_at"] == job["finished_at"]
    assert recovered["numeric_finished_at"] == job["numeric_finished_at"]
    assert len(wire.calls) == numeric_calls and len(calls) == 2
    assert len(h.executions()["executions"]) == 1
    assert restored.due_check()["action"] == "already_checked"
    assert ReleaseRecords(s.store).read(event_id="census:m3:full:2026-11-03")["events"]


@pytest.mark.parametrize(
    "reason,delay",
    [("release_http_503", timedelta(hours=1)), ("release_http_403", timedelta(days=1))],
)
def test_material_retry_failure_is_bounded_and_respects_provider_cooldown(
    tmp_path, monkeypatch, reason, delay
):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
    from tests.test_m3_segment6 import CALENDAR

    h, _, wire, clock, calls = scheduler_host(tmp_path, monkeypatch)

    def fail(url, family):
        calls.append((url, family))
        raise CpiTimeSeriesError(reason)

    h.material_fetcher = fail
    first = h.scheduler.due_check()
    before = len(wire.calls)
    for attempt in (2, 3):
        clock[0] += delay - timedelta(seconds=1)
        h.scheduler.import_calendar(
            CALENDAR.replace(b"10/27/2026", b"TBA"), acquired_at=clock[0]
        )
        assert h.scheduler.due_check()["action"] == "material_cooldown_wait"
        clock[0] += timedelta(seconds=1)
        tick = h.scheduler.due_check()
        assert tick["action"] == "material_retry"
        assert tick["material_retry"]["attempts"] == attempt
        assert tick["request_id"] == first["request_id"]
    for _ in range(3):
        assert h.scheduler.due_check()["action"] == "already_checked"
    work = h.scheduler.status()["targets"][1]["target"]
    assert work["state"] == "complete"
    assert work["material_retry"]["state"] == "pending_exhausted"
    assert work["material_retry"]["reasons"] == [reason]
    assert len(calls) == 3 and len(wire.calls) == before
    assert "material_recovered_at" not in h.journal.read(first["request_id"])


def test_completed_legacy_and_older_material_failures_are_not_backfilled(
    tmp_path, monkeypatch
):
    h, _, _, clock, calls = scheduler_host(tmp_path, monkeypatch)
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError

    def fail(*_):
        calls.append("failure")
        raise CpiTimeSeriesError("release_http_503")

    h.material_fetcher = fail
    h.scheduler.due_check()
    with h.scheduler._locked():
        state = h.scheduler._load()
        # Add a newer, already complete legacy stage; old partial material stays.
        state["events"].append(
            dict(
                state["events"][1],
                key="m3:2026-10",
                reference_month="2026-10",
                release_at=clock[0].isoformat(),
            )
        )
        state["work"]["m3:2026-10"] = dict(
            state="complete",
            attempts=1,
            result={"state": "complete", "result": {"coverage_complete": True}},
        )
        h.scheduler._save(state)
    clock[0] += timedelta(hours=2)
    assert h.scheduler.due_check()["action"] == "already_checked"
    assert len(calls) == 1


def test_material_retry_does_not_rebind_changed_calendar_identity(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError

    h, _, wire, clock, calls = scheduler_host(tmp_path, monkeypatch)

    def fail(*_):
        calls.append("503")
        raise CpiTimeSeriesError("release_http_503")

    h.material_fetcher = fail
    h.scheduler.due_check()
    before = len(wire.calls)
    with h.scheduler._locked():
        state = h.scheduler._load()
        # Date must actually differ to change the report identity.
        state["events"][1]["release_at"] = "2026-11-02T15:00:00+00:00"
        h.scheduler._save(state)
    clock[0] += timedelta(hours=2)
    assert h.scheduler.due_check()["action"] == "already_checked"
    assert (
        h.scheduler.status()["targets"][1]["target"]["material_retry"]["state"]
        == "calendar_changed"
    )
    assert len(calls) == 1 and len(wire.calls) == before


def test_interrupted_automatic_material_attempt_respects_reservation_and_recovers(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError

    h, _, wire, clock, calls = scheduler_host(tmp_path, monkeypatch)
    original = h.material_fetcher

    def fail_once(*_):
        calls.append("503")
        raise CpiTimeSeriesError("release_http_503")

    h.material_fetcher = fail_once
    tick = h.scheduler.due_check()
    numeric_calls = len(wire.calls)
    clock[0] += timedelta(minutes=61)
    original_submit = h.scheduler.submit_report

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    h.scheduler.submit_report = interrupt
    with pytest.raises(KeyboardInterrupt):
        h.scheduler.due_check()
    assert (
        h.scheduler.status()["targets"][1]["target"]["material_retry"]["state"]
        == "running"
    )
    assert h.scheduler.due_check()["action"] == "already_checked"
    clock[0] += timedelta(minutes=61)
    h.scheduler.submit_report = original_submit
    h.material_fetcher = original
    recovered = h.scheduler.due_check()
    assert (
        recovered["action"] == "material_retry"
        and recovered["request_id"] == tick["request_id"]
    )
    assert recovered["material_retry"]["state"] == "retained"
    assert len(wire.calls) == numeric_calls


def test_tba_calendar_is_readable_pauses_material_and_restored_identity_recovers(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
    from tests.test_m3_segment6 import CALENDAR

    h, _, wire, clock, calls = scheduler_host(tmp_path, monkeypatch)
    fetch = h.material_fetcher
    first_fetch = [True]

    def fetch_once_unavailable(url, family):
        if first_fetch.pop() if first_fetch else False:
            calls.append((url, family))
            raise CpiTimeSeriesError("release_http_503")
        return fetch(url, family)

    h.material_fetcher = fetch_once_unavailable
    h.auto_strategy.update(enabled=True, history_start=h.scheduler.history_start)
    first = h.scheduler.due_check()
    original = h.journal.read(first["request_id"])
    numeric_calls = len(wire.calls)
    start = clock[0]
    clock[0] = start + timedelta(minutes=5)
    h.scheduler.import_calendar(
        CALENDAR.replace(b"10/27/2026", b"TBA").replace(b"11/3/2026", b"TBA"),
        acquired_at=clock[0],
    )
    for minutes in (5, 61, 62):
        clock[0] = start + timedelta(minutes=minutes)
        assert h.scheduler.due_check()["action"] == "not_due"
        assert h.strategy()["enabled"] is True
        schedule = h.schedule()
        assert schedule["calendar_status"] == "fresh"
        target = schedule["targets"][1]
        assert target["status"] == "TBA" and target["release_at"] is None
        assert target["target"]["state"] == "complete"
        assert target["target"]["material_retry"]["state"] == "calendar_changed"
        assert target["target"]["material_retry"]["reasons"] == [
            "m3_material_calendar_changed"
        ]
        assert h.journal.read(first["request_id"]) == original
        assert len(wire.calls) == numeric_calls and len(calls) == 1
    # Restore the same official identity after its original material cooldown.
    h.scheduler.import_calendar(
        CALENDAR.replace(b"10/27/2026", b"TBA"), acquired_at=clock[0]
    )
    recovered = h.scheduler.due_check()
    assert recovered["action"] == "material_retry"
    assert recovered["request_id"] == first["request_id"]
    assert recovered["material_retry"]["state"] == "retained"
    final = h.journal.read(first["request_id"])
    assert final["numeric_result"] == original["numeric_result"]
    assert final["numeric_finished_at"] == original["numeric_finished_at"]
    assert final["finished_at"] == original["finished_at"]
    assert len(wire.calls) == numeric_calls and len(calls) == 2
    assert len(h.executions()["executions"]) == 1
