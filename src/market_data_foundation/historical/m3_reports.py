"""Joint presentation of independently captured M3 report stages."""

import time
from decimal import Decimal
from uuid import NAMESPACE_URL, uuid5

from market_data_foundation.historical.m3_capital_goods_store import SERIES_IDS, _same
from market_data_foundation.historical.m3_capital_goods_timeseries import (
    QUERY_SOURCE_BUDGET_SECONDS,
    _months,
)

DATASETS = ("m3", "advm3")
COMPARISON_BASIS = "current_source_snapshots_not_official_event_revisions"


def child_ids(collection_id):
    return {
        d: str(uuid5(NAMESPACE_URL, f"mdf:m3:joint:{collection_id}:{d}"))
        for d in DATASETS
    }


def query_reports(service, start, end, **kwargs):
    deadline = time.monotonic() + QUERY_SOURCE_BUDGET_SECONDS
    reports = {
        d: service.query_range(start, end, dataset=d, _deadline=deadline, **kwargs)
        for d in DATASETS
    }
    for report in reports.values():
        report["local_only"] = not kwargs.get("allow_upstream", False)
    selected = kwargs.get("series_ids", SERIES_IDS)
    indexed = {
        d: {(p["series_id"], p["month"]): p for p in reports[d]["points"]}
        for d in DATASETS
    }
    comparison_basis = (
        COMPARISON_BASIS
        if kwargs.get("as_of") is None
        else "stored_source_snapshots_at_cutoff_not_official_event_revisions"
    )
    comparisons = []
    for month in reversed(_months(start, end)):
        for series in selected:
            points = {d: indexed[d][series, month] for d in DATASETS}
            full, advance = points["m3"]["value"], points["advm3"]["value"]
            comparisons.append(
                {
                    "month": month,
                    "series_id": series,
                    "m3_value": full,
                    "advm3_value": advance,
                    "difference": str(Decimal(full) - Decimal(advance))
                    if full is not None and advance is not None
                    else None,
                    "difference_direction": "m3_minus_advm3",
                    "reason": None
                    if full is not None and advance is not None
                    else "one_or_both_reports_missing",
                    "comparison_basis": comparison_basis,
                    "official_event_revision": None,
                }
            )
    expected = sum(r["coverage"]["expected"] for r in reports.values())
    available = sum(r["coverage"]["available"] for r in reports.values())
    return {
        "dataset": "both",
        "reports": reports,
        "comparisons": comparisons,
        "comparison_basis": comparison_basis,
        "coverage": {"expected": expected, "available": available},
        "status": "complete"
        if expected == available
        else "partial"
        if available
        else "unavailable",
        "provider_requests": sum(r["provider_requests"] for r in reports.values()),
        "provider_attempts": None
        if any(r["provider_attempts"] is None for r in reports.values())
        else sum(r["provider_attempts"] for r in reports.values()),
        "persisted": False,
    }


def reusable_collection(service, dataset, start, end):
    """Adopt only proved complete source work, never an unrelated local value."""
    expected = {(s, m) for s in SERIES_IDS for m in _months(start, end)}
    actual = {
        (r["series_id"], r["month"]): r
        for r in service.store.read_range(dataset, start, end)
    }
    if actual.keys() != expected:
        return None
    for identity in service.store.collection_candidates(dataset, start, end):
        covered = set()
        valid = True
        for _, meta in service.store.collection_progress(identity):
            if (meta.get("dataset"), meta.get("start"), meta.get("end")) != (
                dataset,
                start,
                end,
            ) or meta.get("confirmed") is not True:
                valid = False
                break
            for row in meta["expected"]:
                key = (row["series_id"], row["month"])
                if key not in actual or key in covered or not _same(row, actual[key]):
                    valid = False
                    break
                covered.add(key)
        if valid and covered == expected:
            return identity
    return None


def collect_reports(
    service, start, end, *, collection_ids, reuse_existing=False, on_progress=None
):
    identities = dict(collection_ids)
    if reuse_existing:
        for d in DATASETS:
            identities[d] = reusable_collection(service, d, start, end) or identities[d]
    reports = {}

    def publish(dataset=None, progress=None):
        if on_progress:
            on_progress(
                {
                    "report_collections": identities,
                    "reports": dict(reports),
                    "active_report": dataset,
                    "active_progress": progress,
                    "completion": "joint_collection_in_progress",
                }
            )

    publish()
    for d in DATASETS:
        result = service.collect_range(
            start,
            end,
            dataset=d,
            confirm_write=True,
            resume_id=identities[d],
            on_progress=lambda progress, d=d: publish(d, progress),
        )
        local = service.query_range(start, end, dataset=d)
        result["coverage"] = {
            "expected": local["coverage"]["expected"],
            "available": local["coverage"]["available"],
            "missing": len(result["missing"]),
            "official_null": 0,
        }
        result["readback_verified"] = (
            result["readback_status"] == "confirmed" and result["persisted"] is True
        )
        reports[d] = result
        publish(d)
    missing = [dict(p, dataset=d) for d, r in reports.items() for p in r["missing"]]
    failures = [
        dict(r["source_failure"], dataset=d)
        for d, r in reports.items()
        if r["source_failure"]
    ]
    return {
        "reports": reports,
        "report_collections": identities,
        "status": "complete"
        if all(r["status"] == "complete" for r in reports.values())
        else "partial",
        "coverage": {
            k: sum(r["coverage"][k] for r in reports.values())
            for k in ("expected", "available", "missing", "official_null")
        },
        "missing": missing,
        "source_missing": [
            dict(p, dataset=d) for d, r in reports.items() for p in r["source_missing"]
        ],
        "pending": [
            dict(p, dataset=d) for d, r in reports.items() for p in r["pending"]
        ],
        "storage_failure": next(
            (r["storage_failure"] for r in reports.values() if r["storage_failure"]),
            None,
        ),
        "source_failure": failures[0] if failures else None,
        "source_failures": failures,
        "persisted": True
        if all(r["persisted"] is True for r in reports.values())
        else None,
        "readback_status": "confirmed"
        if all(r["readback_verified"] for r in reports.values())
        else "incomplete",
        **{
            k: sum(r[k] for r in reports.values())
            for k in (
                "remaining_requests",
                "requested_points",
                "completed_requests",
                "readback_points",
                "saved_points",
                "saved_revisions",
                "provider_requests",
            )
        },
        "provider_attempts": None
        if any(r["provider_attempts"] is None for r in reports.values())
        else sum(r["provider_attempts"] for r in reports.values()),
    }
