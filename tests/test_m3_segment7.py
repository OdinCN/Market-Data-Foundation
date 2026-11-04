"""Wide historical collection, legacy fragment recovery and truthful progress."""

from uuid import uuid4

import pytest

from market_data_foundation.historical.m3_capital_goods import M3Request
from market_data_foundation.historical.m3_capital_goods_source import M3SourceError
from tests.test_m3_capital_goods import no_network  # noqa: F401
from tests.test_m3_segment3 import service
from tests.test_m3_segment4 import host


def test_240_month_default_plan_is_three_bounded_requests_and_progress():
    s, wire = service()
    from market_data_foundation.historical.m3_capital_goods_timeseries import (
        M3TimeSeriesService,
    )

    s = M3TimeSeriesService(s.store, wire)
    assert s.collection_window_months == 240
    progress = []
    result = s.collect_range(
        "2006-11", "2026-10", confirm_write=True, on_progress=progress.append
    )
    assert result["status"] == "complete" and result["provider_requests"] == 3
    assert result["requested_points"] == 720 and result["readback_points"] == 720
    assert result["remaining_requests"] == 0 and not result["pending"]
    assert [x["requested_points"] for x in progress] == [240, 480, 720]
    assert all(len(r.months) == 240 for r in wire.calls)
    with pytest.raises(Exception, match="month_window_invalid"):
        M3Request("m3", "MDM", "2006-11", "2026-11")


def test_legacy_six_fragments_resume_same_identity_without_refetch_or_revision():
    s, wire = service()
    old = s.collect_range(
        "2006-11", "2026-10", confirm_write=True, max_source_requests=6
    )
    captured = [(r["month"], r["series_id"], r["observed_at"]) for r in s.store.rows]
    s.collection_window_months = 240
    done = s.collect_range(
        "2006-11", "2026-10", confirm_write=True, resume_id=old["collection_id"]
    )
    assert (
        done["collection_id"] == old["collection_id"] and done["status"] == "complete"
    )
    assert (
        done["provider_requests"] == 3
        and done["saved_points"] == 708
        and done["saved_revisions"] == 0
    )
    assert all(r.start_month == "2007-03" for r in wire.calls[6:])
    assert captured == [
        (r["month"], r["series_id"], r["observed_at"]) for r in s.store.rows[:12]
    ]
    again = s.collect_range(
        "2006-11", "2026-10", confirm_write=True, resume_id=old["collection_id"]
    )
    assert (
        again["provider_requests"] == 0
        and again["saved_points"] == 0
        and len(s.store.rows) == 720
    )


def test_actual_source_missing_completes_but_unrequested_never_becomes_source_missing():
    s, wire = service()
    s.collection_window_months = 240
    wire.fail_at = 2
    partial = s.collect_range("2006-11", "2026-10", confirm_write=True)
    assert partial["remaining_requests"] == 2 and len(partial["pending"]) == 480
    assert not partial["source_missing"]
    assert {r["reason"] for r in partial["pending"]} == {
        "not_requested",
        "source_request_failed",
    }
    assert {r["reason"] for r in partial["missing"]} == {
        "not_requested",
        "source_request_failed",
    }
    wire.fail_at = None
    original = wire.fetch

    # Use a real parsed table with a missing last month, rather than fabricate cells.
    def missing(req):
        import json

        from market_data_foundation.historical.m3_capital_goods import parse_response

        snap = original(req)
        table = json.loads(snap.raw)
        table.pop()
        return parse_response(
            json.dumps(table).encode(), req, obtained_at=snap.obtained_at
        )

    wire.fetch = missing
    done = s.collect_range(
        "2006-11", "2026-10", confirm_write=True, resume_id=partial["collection_id"]
    )
    assert done["status"] == "complete" and done["remaining_requests"] == 0
    assert len(done["source_missing"]) == 2 and not done["pending"]
    assert done["readback_points"] == 720


def test_host_publishes_progress_and_preserves_rate_wait_without_false_missing(
    tmp_path,
):
    h, s, wire = host(tmp_path)
    s.collection_window_months = 240

    def wait(req):
        raise M3SourceError("census_m3_rate_wait", 0, 429, "2026-11-03T01:00:00+00:00")

    wire.fetch = wait
    result = h.submit(
        {
            "request_id": str(uuid4()),
            "action": "initialize",
            "start": "2006-11",
            "end": "2026-10",
        }
    )
    assert result["state"] == "waiting" and result["classification"] == "m3_source_wait"
    assert result["result"]["provider_attempts"] == 0
    assert (
        len(result["result"]["pending"]) == 720
        and not result["result"]["readback"]["source_missing"]
    )
    assert result["result"]["source_failure"]["retry_at"]


def test_confirmed_fragment_with_missing_stored_row_is_not_reported_as_source_gap():
    s, wire = service()
    s.collection_window_months = 240
    first = s.collect_range("2025-01", "2025-02", confirm_write=True)
    s.store.rows.pop()
    result = s.collect_range(
        "2025-01", "2025-02", confirm_write=True, resume_id=first["collection_id"]
    )
    assert result["storage_failure"] == "census_m3_storage_outcome_unknown"
    assert result["readback_status"] == "incomplete" and result["status"] == "partial"
    assert not result["source_missing"] and not result["pending"]
    assert result["missing"][0]["reason"] == "storage_readback_missing"
    assert result["provider_requests"] == 0


def test_interrupted_job_resumes_and_active_job_does_not_duplicate(
    tmp_path,
):
    h, s, wire = host(tmp_path)
    s.collection_window_months = 240
    command = {
        "request_id": str(uuid4()),
        "action": "initialize",
        "start": "2006-11",
        "end": "2026-10",
    }
    wire.fail_at = 2
    partial = h.submit(command)
    assert partial["state"] == "failed" and partial["result"]["requested_points"] == 240
    h.journal.save(dict(partial, state="running", finished_at=None))
    assert h.jobs()["jobs"][0]["state"] == "unknown"
    wire.fail_at = None
    done = h.submit(command, resume=True)
    assert done["state"] == "complete" and done["request_id"] == command["request_id"]
    assert done["result"]["provider_requests"] == 2 and len(s.store.rows) == 720
    with h.journal.execution():
        h.journal.save(dict(done, state="running", finished_at=None))
        active = h.submit(command, resume=True)
        assert active["state"] == "running"
        assert len(wire.calls) == 4
