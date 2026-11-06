"""Exact-key consumption and bounded durable receipts using the real query path."""

import copy
import json
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

from market_data_foundation.historical.claims_collection import ClaimsCollectionService
from market_data_foundation.historical.claims_contract import SERIES_IDS, weeks
from market_data_foundation.historical.claims_timeseries import ClaimsObservation
from market_data_foundation.web.claims_jobs import (
    RECEIPT_GAP_LIMIT,
    ClaimsJobRunner,
    actuals_ready,
)

AT = datetime(2026, 11, 2, 18, tzinfo=UTC)


def service_for(observations):
    store = SimpleNamespace(
        read_snapshot=nullcontext,
        read_range=lambda *args: observations,
    )
    source = SimpleNamespace(fetch=lambda *args: (), request_count=0, attempts=0)
    return ClaimsCollectionService(store, source, clock=lambda: AT)


def submit(service, directory, start, end, material=None):
    runner = ClaimsJobRunner(
        service,
        directory,
        clock=lambda: AT,
        capture_material=lambda _: material or dict(status="unbound"),
    )
    job = runner.submit(
        dict(request_id=str(uuid4()), action="init", start=start, end=end)
    )
    assert runner.journal.read(job["job_id"]) == job
    return job


@pytest.mark.parametrize("missing", [None, "local_not_provided", "provider_missing"])
@pytest.mark.parametrize("series_id", SERIES_IDS)
def test_actual_requires_exact_nonnull_consumable_key_despite_newer_week(
    tmp_path, missing, series_id
):
    actuals = {
        s: "2026-10-24" if s.startswith("IC") else "2026-10-17" for s in SERIES_IDS
    }
    observations = []
    for sid in SERIES_IDS:
        for week in weeks("2026-10-17", "2026-10-31"):
            is_gap = sid == series_id and week == actuals[sid]
            if is_gap and missing == "local_not_provided":
                continue
            value = None if is_gap and missing == "provider_missing" else Decimal("0")
            observations.append(
                ClaimsObservation(sid, week, value, AT, AT, 1, batch_id="synthetic")
            )
    material = dict(
        status="available",
        event_id="dol-claims-2026-10-29",
        actual_weeks=actuals,
        readback_verified=True,
    )
    job = submit(
        service_for(observations), tmp_path, "2026-10-17", "2026-10-31", material
    )
    assert job["state"] == "complete", job
    result = job["result"]
    assert result["series_coverage"][series_id]["latest_week"] == "2026-10-31"
    assert actuals_ready(result) is (missing is None)
    assert result["readback"]["actuals_verified"] is (missing is None)
    if missing:
        assert result["readback"]["actual_missing"] == [
            dict(
                series_id=series_id,
                week_ending=actuals[series_id],
                missing_reason=missing,
            )
        ]
    # Zero is consumable. Continuous values need no equality/version binding to DOL.
    assert result["event_binding_status"] == "scope_only"


@pytest.mark.parametrize("populated", [False, True])
def test_twenty_year_receipt_never_persists_rows_and_caps_gap_samples(
    tmp_path, populated
):
    start, end = "2006-11-02", "2026-11-02"
    target_weeks = weeks(start, end)
    observations = (
        [
            ClaimsObservation(s, w, Decimal("100"), AT, AT, 1, batch_id="synthetic")
            for s in SERIES_IDS
            for w in target_weeks
        ]
        if populated
        else []
    )
    service = service_for(observations)
    original = service.init_range

    def with_full_payload(*args):
        result = original(*args)
        # Exercise the old expanded-written failure with realistic derived rows.
        result["rows"] = service.query_range(*args)["rows"]
        return result

    service.init_range = with_full_payload
    job = submit(service, tmp_path, start, end)
    assert job["state"] == "complete", job
    result = job["result"]
    assert "rows" not in result
    assert result["coverage"]["expected"] == 6264
    assert result["coverage"]["available"] == (6264 if populated else 0)
    for item in result["series_coverage"].values():
        for field in ("missing_weeks", "trailing_missing_weeks"):
            assert len(item[field]) <= RECEIPT_GAP_LIMIT
            assert item[field + "_total"] == (0 if populated else 1044)
            assert item[field + "_truncated"] is (not populated)
    assert len(json.dumps(job).encode()) < 12_000
    assert len(next(tmp_path.glob("*.json")).read_bytes()) < 12_000


@pytest.mark.parametrize(
    "corrupt", ["value", "revision", "batch_id", "available_at", "key"]
)
def test_receipt_compaction_preserves_exact_readback_validation(tmp_path, corrupt):
    observations = [
        ClaimsObservation(
            s, "2026-10-24", Decimal("100"), AT, AT, 1, batch_id="synthetic"
        )
        for s in SERIES_IDS
    ]
    service = service_for(observations)
    original = service.query_range
    calls = []

    def query(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(kwargs)
        if len(calls) == 2:
            result = copy.deepcopy(result)
            row = result["rows"][0]
            if corrupt == "value":
                row["value"] = "101"
            elif corrupt == "key":
                row["week_ending"] = "2026-10-31"
            else:
                row["provenance"][corrupt] = (
                    None if corrupt == "available_at" else "changed"
                )
        return result

    service.query_range = query
    job = submit(service, tmp_path, "2026-10-24", "2026-10-24")
    assert job["state"] == "unknown"
    assert job["classification"] == "claims_job_readback_mismatch"
    assert calls[-1] == dict(allow_upstream=False, as_of_basis="available")


def test_existing_history_verification_uses_each_reports_native_week_without_writes():
    from market_data_foundation.web.claims_host import ClaimsTimeSeriesHost

    actuals = {
        s: "2026-10-31" if s.startswith("IC") else "2026-10-24" for s in SERIES_IDS
    }
    observations = [
        ClaimsObservation(s, w, Decimal("100"), AT, AT, 1, batch_id="fixture")
        for s in SERIES_IDS
        for w in weeks("2026-09-12", actuals[s])
    ]
    service = service_for(observations)
    calls = []

    @__import__("contextlib").contextmanager
    def read():
        calls.append("local_read")
        yield service

    host = SimpleNamespace(service=read)
    receipt = dict(
        material=dict(status="available", readback_verified=True, actual_weeks=actuals),
        readback=dict(
            verified=True,
            as_of_basis="available",
            actuals_verified=True,
            actual_weeks=actuals,
        ),
    )
    result = ClaimsTimeSeriesHost.verify_existing_history(
        host, "2026-09-12", "2026-11-05", receipt
    )
    assert result["coverage"] == dict(
        expected=45, available=45, missing_rows=0, provider_missing_values=0
    )
    assert (
        result["provider_requests"] == 0
        and result["completion"] == "existing_history_readback_verified"
    )
    assert actuals_ready(result)
    observations.pop()
    assert (
        ClaimsTimeSeriesHost.verify_existing_history(
            host, "2026-09-12", "2026-11-05", receipt
        )
        is None
    )
    assert calls == ["local_read", "local_read"]
