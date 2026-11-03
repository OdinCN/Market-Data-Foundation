"""Budget attribution, same-stage MoM and durable calendar/history policy."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from market_data_foundation.historical.m3_capital_goods_source import (
    M3CandidateSource,
    M3SourceError,
)
from market_data_foundation.historical.m3_capital_goods_timeseries import (
    M3TimeSeriesService,
)
from market_data_foundation.historical.m3_reports import query_reports
from market_data_foundation.historical.m3_schedule import M3Scheduler
from tests.test_m3_capital_goods import no_network  # noqa:F401
from tests.test_m3_segment3 import AT, service
from tests.test_m3_segment4 import host
from tests.test_m3_segment6 import CALENDAR


def test_joint_budget_and_upstream_timeout_are_distinct(
    monkeypatch,
):
    from market_data_foundation.historical import m3_capital_goods_timeseries as module
    from market_data_foundation.historical import m3_reports as reports

    s, wire = service()
    s = M3TimeSeriesService(s.store, wire)
    clock = [0.0]

    class Timed(M3CandidateSource):
        def __init__(self):
            pass

        def fetch(self, request, *, timeout=20):
            if timeout < 4.5:
                clock[0] += timeout
                raise M3SourceError("census_m3_timeout", 1)
            clock[0] += 4.5
            return wire.fetch(request)

    s.source = Timed()
    monkeypatch.setattr(module.time, "monotonic", lambda: clock[0])
    result = query_reports(s, "2025-01", "2025-12", allow_upstream=True)
    assert result["provider_requests"] == 6 and result["coverage"]["available"] == 72
    assert not result["budget_exhausted"] and not s.store.rows
    clock[0] = 0
    monkeypatch.setattr(reports, "QUERY_SOURCE_BUDGET_SECONDS", 25)
    limited = query_reports(s, "2025-01", "2025-12", allow_upstream=True)
    assert limited["budget_exhausted"]
    assert (
        limited["reports"]["advm3"]["source_failure"]["classification"]
        == "census_m3_query_deadline"
    )
    assert limited["reports"]["m3"]["coverage"]["available"] == 36


def test_mom_uses_own_report_prior_month_and_cutoff_with_zero_and_missing():
    s, wire = service()
    wire.overrides[("MDM", "NO", "2025-01")] = 100
    wire.overrides[("MDM", "NO", "2025-02")] = 110
    s.collect_range("2025-01", "2025-02", confirm_write=True)
    wire.overrides[("MDM", "NO", "2025-01")] = 200
    wire.overrides[("MDM", "NO", "2025-02")] = 210
    s.collect_range("2025-01", "2025-02", dataset="advm3", confirm_write=True)
    result = query_reports(s, "2025-02", "2025-02", include_mom=True)
    row = next(
        c for c in result["comparisons"] if c["series_id"] == "durable_goods_new_orders"
    )
    assert (
        Decimal(row["m3_mom_percent"]) == 10 and Decimal(row["advm3_mom_percent"]) == 5
    )
    previous = next(
        r
        for r in s.store.rows
        if r["dataset"] == "advm3"
        and r["month"] == "2025-01"
        and r["series_id"] == "durable_goods_new_orders"
    )
    previous["value"] = "0"
    result = query_reports(s, "2025-02", "2025-02", include_mom=True)
    row = next(
        c for c in result["comparisons"] if c["series_id"] == "durable_goods_new_orders"
    )
    assert (
        row["advm3_mom_percent"] is None
        and row["advm3_mom_reason"] == "zero_denominator"
    )
    cutoff = query_reports(
        s, "2025-02", "2025-02", include_mom=True, as_of=AT - timedelta(seconds=1)
    )
    assert all(c["m3_mom_percent"] is None for c in cutoff["comparisons"])


def test_calendar_refresh_failure_preserves_valid_cache_but_expiry_blocks_execution(
    tmp_path,
):
    now = [datetime(2026, 11, 3, 16, tzinfo=UTC)]
    calls = []
    fail = [False]

    def fetch():
        calls.append(1)
        if fail[0]:
            raise OSError("private diagnostic")
        return CALENDAR

    scheduler = M3Scheduler(
        tmp_path / "calendar.json",
        history_start="2006-11",
        clock=lambda: now[0],
        calendar_fetch=fetch,
    )
    assert scheduler.refresh_calendar()["calendar_status"] == "fresh"
    acquired = scheduler.status()["calendar"]["acquired_at"]
    fail[0] = True
    now[0] += timedelta(hours=7)
    status = scheduler.refresh_calendar()
    assert status["calendar_status"] == "degraded_cached_calendar"
    assert status["calendar"][
        "acquired_at"
    ] == acquired and "private diagnostic" not in json.dumps(status)
    before = len(calls)
    scheduler.refresh_calendar()
    assert len(calls) == before
    now[0] += timedelta(hours=25)
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert scheduler.status()["calendar_status"] == "expired"


def test_worker_retries_honor_cooldown_and_history_is_separate_idempotent_readback(
    tmp_path,
):
    now = [datetime(2026, 11, 3, 16, tzinfo=UTC)]
    calls = []

    def submit(dataset, command, **kwargs):
        calls.append((dataset, command, kwargs))
        if dataset == "advm3" and len(calls) == 1:
            return {
                "state": "waiting",
                "result": {
                    "coverage_complete": False,
                    "source_failure": {
                        "retry_at": (now[0] + timedelta(hours=1)).isoformat()
                    },
                },
            }
        return {"state": "complete", "result": {"coverage_complete": True}}

    scheduler = M3Scheduler(
        tmp_path / "calendar.json",
        history_start="2006-11",
        clock=lambda: now[0],
        submit_report=submit,
    )
    scheduler.import_calendar(CALENDAR, acquired_at=now[0])
    scheduler.due_check()
    scheduler.due_check()
    assert scheduler.due_check()["action"] == "cooldown_wait" and len(calls) == 2
    now[0] += timedelta(hours=1, seconds=1)
    scheduler.due_check()
    assert len(calls) == 3 and calls[2][2]["resume"]
    h, s, wire = host(tmp_path / "host")
    s.collect_range("2025-01", "2025-02", confirm_write=True)
    event = {
        "dataset": "m3",
        "reference_month": "2025-02",
        "release_at": "2026-11-02T10:00:00+00:00",
    }
    identity = str(uuid4())
    before = len(wire.calls)
    history = h.maintain_history(event, identity, "2025-01")
    assert (
        history["state"] == "complete"
        and history["stage"] == "history"
        and len(wire.calls) == before
    )
    assert h.maintain_history(event, identity, "2025-01") == history
