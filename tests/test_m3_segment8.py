"""Joint report stages keep captures, failures and cutoff inputs independent."""

from datetime import timedelta
from uuid import uuid4

from market_data_foundation.historical.m3_reports import (
    child_ids,
    collect_reports,
    query_reports,
)
from tests.test_m3_capital_goods import no_network  # noqa: F401
from tests.test_m3_segment3 import AT, service
from tests.test_m3_segment4 import host


def test_joint_initialization_adopts_proved_full_work_and_populates_other_stage():
    s, wire = service()
    s.collection_window_months = 240
    original = s.collect_range("2006-11", "2026-10", confirm_write=True)
    ids = child_ids(str(uuid4()))
    progress = []
    done = collect_reports(
        s,
        "2006-11",
        "2026-10",
        collection_ids=ids,
        reuse_existing=True,
        on_progress=progress.append,
    )
    assert done["status"] == "complete" and done["requested_points"] == 1440
    assert done["report_collections"]["m3"] == original["collection_id"]
    assert done["provider_requests"] == 3 and done["saved_points"] == 720
    assert done["reports"]["m3"]["provider_requests"] == 0
    assert (
        s.store.status("m3")["observation_count"]
        == s.store.status("advm3")["observation_count"]
        == 720
    )
    again = collect_reports(
        s, "2006-11", "2026-10", collection_ids=done["report_collections"]
    )
    assert again["provider_requests"] == 0 and again["saved_points"] == 0


def test_one_report_failure_does_not_discard_or_block_other_report():
    s, wire = service()
    wire.fail_at = 1
    done = collect_reports(
        s, "2025-01", "2025-02", collection_ids=child_ids(str(uuid4()))
    )
    assert done["reports"]["m3"]["status"] == "partial"
    assert done["reports"]["advm3"]["status"] == "complete"
    assert done["coverage"]["available"] == 6 and len(done["pending"]) == 6
    assert all(p["dataset"] == "m3" for p in done["pending"])


def test_comparison_exact_decimal_and_missing_cutoff_stay_local():
    s, wire = service()
    s.collection_window_months = 240
    collect_reports(s, "2025-01", "2025-02", collection_ids=child_ids(str(uuid4())))
    current = query_reports(s, "2025-01", "2025-02")
    assert current["coverage"] == {"expected": 12, "available": 12}
    assert all(
        c["difference"] == "0" and c["official_event_revision"] is None
        for c in current["comparisons"]
    )
    before = len(wire.calls)
    history = query_reports(s, "2025-01", "2025-02", as_of=AT - timedelta(seconds=1))
    assert history["coverage"]["available"] == 0 and len(wire.calls) == before
    assert all(c["difference"] is None for c in history["comparisons"])
    assert not history["persisted"]


def test_joint_host_keeps_legacy_receipt_and_publishes_per_report_details(tmp_path):
    h, s, wire = host(tmp_path)
    h.joint_collections = True
    s.collection_window_months = 240
    command = {
        "request_id": str(uuid4()),
        "action": "initialize",
        "start": "2025-01",
        "end": "2025-02",
    }
    done = h.submit(command)
    assert done["state"] == "complete" and done["result"]["coverage"]["available"] == 12
    assert set(done["report_collections"]) == {"m3", "advm3"}
    assert h.submit(command, resume=True) == done and len(wire.calls) == 6
    assert h.status()["reports"]["advm3"]["observation_count"] == 6


def test_joint_query_fills_a_year_in_three_requests_without_persisting():
    from market_data_foundation.historical.m3_capital_goods_timeseries import (
        M3TimeSeriesService,
    )

    s, wire = service()
    s = M3TimeSeriesService(s.store, wire)
    s.collect_range("2025-10", "2026-09", confirm_write=True)
    before = len(wire.calls)
    result = query_reports(s, "2025-10", "2026-09", allow_upstream=True)
    assert len(wire.calls) - before == 3 and result["coverage"] == {
        "expected": 72,
        "available": 72,
    }
    assert s.store.status("advm3")["observation_count"] == 0
    assert all(p["available_at"] is None for p in result["reports"]["advm3"]["points"])
    assert not result["persisted"]


def test_scheduled_stage_routes_to_its_own_source_and_manual_update_is_joint(tmp_path):
    h, s, wire = host(tmp_path)
    h.joint_collections = True
    s.collection_window_months = 240
    command = {
        "request_id": str(uuid4()),
        "action": "update",
        "start": "2025-01",
        "end": "2025-02",
    }
    scheduled = h.submit_report("advm3", command)
    assert scheduled["state"] == "complete" and scheduled["dataset"] == "advm3"
    assert scheduled["result"]["coverage"]["available"] == 6
    assert all(r.dataset == "advm3" for r in wire.calls)
    command["request_id"] = str(uuid4())
    manual = h.submit(command)
    assert manual["state"] == "complete" and manual["result"]["provider_requests"] == 6
    assert manual["result"]["saved_revisions"] == 0


def test_joint_query_keeps_other_stage_after_source_failure():
    s, wire = service()
    wire.fail_at = 1
    result = query_reports(s, "2025-01", "2025-02", allow_upstream=True)
    assert result["reports"]["m3"]["source_failure"]["http_status"] == 204
    assert result["reports"]["advm3"]["coverage"]["available"] == 6
    assert len(result["comparisons"]) == 6
    assert all(
        c["difference"] is None for c in result["comparisons"] if c["m3_value"] is None
    )
    assert not s.store.rows
