"""Explicit private DEV storage qualification; never required by cloud CI."""

import os
from dataclasses import replace
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from market_data_foundation.historical.financial_daily import SERIES_IDS, FinancialError
from market_data_foundation.historical.financial_daily_runtime import build_service
from tests.test_financial_daily import END, START, payload


def storage_probe(*, live_opt_in=False):
    if live_opt_in is not True:
        raise FinancialError("financial_dev_storage_opt_in_required")
    schema = "test_financial_daily_s1_" + uuid4().hex[:10]
    service = build_service(write=True, schema=schema)
    receipt = {
        "origin": "controlled_fixture",
        "official_source_requests": 0,
        "schema": schema,
        "series": {},
    }
    try:
        receipt["migration"] = service.store.migrate()
        # Only the dedicated synthetic namespace may be used for this probe.
        assert service.store.schema == schema
        for sid in SERIES_IDS:
            at = datetime.now(UTC)
            batch = replace(payload(sid, at=at), metadata_observed_at=at)
            if sid == "DGS2":
                batch_for_old_replay = batch
            saved = service.store.save(batch)
            repeated = service.store.save(replace(batch, observed_at=datetime.now(UTC)))
            assert repeated["saved_points"] == 0
            service.store.record_attempt(
                sid, START, END, "complete", bid=saved["batch_id"]
            )
            result = service.query(START, END, selected=(sid,))
            assert len(result["rows"]) == 5
            assert sum(r["value"] is not None for r in result["rows"]) == 2
            assert all(r["origin"] == "controlled_fixture" for r in result["rows"])
            receipt["series"][sid] = dict(
                batch_id=saved["batch_id"],
                saved_points=saved["saved_points"],
                repeat_saved_points=0,
                rows=result["rows"],
            )
        prior_cutoff = datetime.now(UTC)
        first = service.query(START, END, selected=("DGS2",))
        at = datetime.now(UTC)
        revised = replace(
            payload(
                "DGS2",
                {"2026-10-30": "3.0000", "2026-11-02": ".", "2026-11-03": "-0.125"},
                at=at,
            ),
            metadata_observed_at=at,
        )
        changed = service.store.save(revised)
        assert changed["saved_revisions"] == 1
        before = service.query(START, END, selected=("DGS2",), as_of=prior_cutoff)
        after = service.query(START, END, selected=("DGS2",))
        assert (
            before["rows"][0]["value"] == "2.0000"
            and after["rows"][0]["value"] == "3.0000"
        )
        assert before["rows"][0]["observed_at"] == first["rows"][0]["observed_at"]
        receipt["controlled_revision"] = {
            "before_cutoff": prior_cutoff.isoformat(),
            "before": before["rows"][0],
            "after": after["rows"][0],
        }
        service.store.save(batch_for_old_replay)
        assert (
            service.query(START, END, selected=("DGS2",))["rows"][0]["value"]
            == "3.0000"
        )
        receipt["old_batch_replay_did_not_rewind_current"] = True
        at = datetime.now(UTC)
        interrupted = replace(
            payload(
                "DGS10",
                {"2026-10-30": "4.0000", "2026-11-02": ".", "2026-11-03": "-0.125"},
                at=at,
            ),
            metadata_observed_at=at,
        )

        def lost_confirm():
            raise KeyboardInterrupt

        with pytest.raises(KeyboardInterrupt):
            service.store.save(interrupted, before_confirm=lost_confirm)
        pending = service.query(START, END, selected=("DGS10",))
        assert (
            pending["rows"][0]["state"] == "commit_pending"
            and pending["rows"][0]["value"] is None
        )
        recovery = service.recover(interrupted.batch_id)
        recovered = service.query(START, END, selected=("DGS10",))
        assert recovered["rows"][0]["value"] == "4.0000"
        assert (
            recovered["rows"][0]["observed_at"] == interrupted.observed_at.isoformat()
        )
        assert recovery["saved_points"] == 0
        receipt["interrupted_confirmation"] = {
            "batch_id": interrupted.batch_id,
            "pending": pending["rows"][0],
            "recovered": recovered["rows"][0],
            "provider_requests": 0,
        }
        old = service.query(
            START, END, selected=SERIES_IDS, as_of=datetime(2000, 1, 1, tzinfo=UTC)
        )
        assert all(
            r["state"] == "not_known_at_cutoff" and r["value"] is None
            for r in old["rows"]
        )
        receipt["early_cutoff_did_not_borrow_future"] = True
        # A failed refresh leaves the prior confirmed amount visible and separately
        # reports the request failure. A later explicit success clears that marker.
        service.store.record_attempt(
            "EFFR", START, END, "failed", "financial_http_failure"
        )
        partial = service.query(START, END, selected=("EFFR", "SOFR"))
        assert partial["rows"][0]["value"] == "2.0000"
        assert partial["rows"][0]["latest_request_state"] == "failed"
        assert partial["rows"][5]["value"] == "2.0000"
        service.store.record_attempt(
            "EFFR", START, END, "complete", bid=receipt["series"]["EFFR"]["batch_id"]
        )
        receipt["partial_failure_preserved_other_series"] = True
        receipt["readback"] = service.query(START, END)
        receipt["status"] = "passed"
        return receipt
    finally:
        service.close()


@pytest.mark.live_provider
@pytest.mark.postgres_integration
def test_financial_daily_private_dev_storage():
    if os.environ.get("MDF_FINANCIAL_DAILY_DEV_OPT_IN") != "1":
        pytest.skip("explicit private DEV storage opt-in required")
    try:
        result = storage_probe(live_opt_in=True)
    except Exception as error:
        raise FinancialError("financial_dev_probe_" + type(error).__name__) from None
    assert result["status"] == "passed"
