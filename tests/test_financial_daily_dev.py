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


@pytest.mark.live_provider
@pytest.mark.postgres_integration
def test_financial_daily_existing_r1_upgrade_preserves_content():
    if os.environ.get("MDF_FINANCIAL_DAILY_DEV_OPT_IN") != "1":
        pytest.skip("explicit private DEV storage opt-in required")
    from market_data_foundation.historical.financial_daily import (
        DATASET_ID,
        DEFINITION_REVISION,
    )
    from market_data_foundation.historical.financial_daily_store import (
        DDL,
        PREVIOUS_REVISION,
        REVISION,
    )

    schema = "test_financial_daily_upgrade_" + uuid4().hex[:10]
    service = build_service(write=True, schema=schema)
    c = service.store.connection
    try:
        with c.transaction():
            c.execute(f"CREATE SCHEMA {schema}")
            c.execute(f"REVOKE ALL ON SCHEMA {schema} FROM PUBLIC")
            for sql in DDL.read_text().split(";"):
                if sql.strip():
                    c.execute(sql.replace("{s}", schema))
            c.execute(
                f"INSERT INTO {schema}.schema_metadata VALUES(true,%s,%s,%s)",
                (PREVIOUS_REVISION, DATASET_ID, DEFINITION_REVISION),
            )
        at = datetime.now(UTC)
        batch = replace(payload(at=at), metadata_observed_at=at)
        # Seed the actual old r1 layout, before upgrading it with current code.
        from psycopg.types.json import Jsonb

        from market_data_foundation.historical.financial_daily import day, digest

        raw_locator = service.store.raw_store.put_verified(batch.raw)
        meta_locator = service.store.raw_store.put_verified(batch.metadata_raw)
        c.execute(
            f"INSERT INTO {schema}.batches VALUES "
            "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                batch.batch_id,
                batch.series_id,
                day(batch.start),
                day(batch.end),
                digest(batch.raw),
                raw_locator,
                len(batch.raw),
                digest(batch.metadata_raw),
                meta_locator,
                len(batch.metadata_raw),
                Jsonb(batch.metadata),
                batch.metadata_observed_at,
                batch.observed_at,
                batch.observed_at,
                Jsonb(batch.request),
                batch.origin,
                batch.attempts,
            ),
        )
        for point in batch.points:
            c.execute(
                f"INSERT INTO {schema}.revisions VALUES(%s,%s,1,%s,%s,%s,%s,%s)",
                (
                    point.series_id,
                    day(point.observation_date),
                    point.value,
                    Jsonb(point.provider_extra),
                    batch.batch_id,
                    batch.observed_at,
                    batch.observed_at,
                ),
            )
        saved = dict(batch_id=batch.batch_id)
        assert service.store.migrate()["state"] == "upgraded"
        assert service.store.migrate()["state"] == "already_prepared"
        assert (
            c.execute(
                f"SELECT schema_revision FROM {schema}.schema_metadata"
            ).fetchone()[0]
            == REVISION
        )
        assert (
            c.execute(f"SELECT batch_id FROM {schema}.batches").fetchone()[0]
            == saved["batch_id"]
        )
        assert (
            service.query(START, END, selected=("DGS2",))["rows"][0]["value"]
            == "2.0000"
        )
    finally:
        service.close()


@pytest.mark.live_provider
@pytest.mark.postgres_integration
def test_financial_daily_derived_equal_value_retains_changed_input_versions():
    """Synthetic Treasury-shaped bytes in a dedicated test namespace only."""
    if os.environ.get("MDF_FINANCIAL_DAILY_DEV_OPT_IN") != "1":
        pytest.skip("explicit private DEV storage opt-in required")
    from market_data_foundation.historical.claims_source import ClaimsResponse
    from market_data_foundation.historical.financial_daily_origin import (
        OriginSource,
        derive,
    )
    from tests.test_financial_daily import Control
    from tests.test_financial_daily_origin import treasury_raw

    service = build_service(
        write=True, schema="test_financial_daily_legs_" + uuid4().hex[:10]
    )
    try:
        service.store.migrate()
        for i in range(2):
            raw = treasury_raw(value="4.20" if i == 0 else "4.30")
            if i:
                raw = raw.replace(b"-0.12", b"-0.02")
            source = OriginSource(
                controls={"home.treasury.gov": Control()},
                transport=lambda _request: ClaimsResponse(200, raw),
            )
            for sid in ("DGS10", "DFII10"):
                service.store.save(source.fetch(sid, "2026-10-30", "2026-10-30"))
            inputs = service.query(
                "2026-10-30", "2026-10-30", selected=("DGS10", "DFII10")
            )
            batch = derive(inputs)
            saved = service.store.save(batch)
            assert saved["saved_points"] == 1
            r = service.query("2026-10-30", "2026-10-30", selected=("T10YIE",))["rows"][
                0
            ]
            assert r["value"] == "4.32" and r["revision"] == i + 1
            assert [leg["revision"] for leg in r["provider_extra"]["inputs"]] == [
                i + 1,
                i + 1,
            ]
        assert service.store.save(batch)["saved_points"] == 0
        assert service.recover(batch.batch_id)["saved_points"] == 0
    finally:
        service.close()
