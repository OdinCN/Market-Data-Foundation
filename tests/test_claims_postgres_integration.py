"""Explicit temporary Claims schema, no upstream provider or other family writes."""

import os
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest

from market_data_foundation.historical.claims_collection import build_service
from market_data_foundation.historical.claims_store import PostgresClaimsStore
from market_data_foundation.historical.claims_timeseries import ClaimsTimeSeriesService
from market_data_foundation.historical.cpi_timeseries_store import digest
from tests.test_claims_storage import AT, batch

pytestmark = pytest.mark.postgres_integration


class Archive:
    def put_verified(self, raw):
        return "test://" + digest(raw)


def test_claims_changed_values_cutoffs_first_capture_views_and_readonly_grants():
    if os.getenv("MDF_CLAIMS_PG_TEST") != "1":
        pytest.skip("Explicit isolated Claims PostgreSQL opt-in required")
    runtime = build_service()
    conn = runtime.store.connection
    schema = "test_claims_" + uuid4().hex
    store = PostgresClaimsStore(conn, Archive(), schema=schema)
    service = ClaimsTimeSeriesService(store)
    try:
        store.ensure_schema()
        store.ensure_schema()
        assert store.save_batches([batch()]) == dict(saved_points=2, saved_revisions=0)
        first = service.query_range("2026-09-19", "2026-09-26", series_ids=("ICSA",))
        cutoff = first["rows"][0]["provenance"]["available_at"]
        # A later realtime interval, capture date, numeric formatting and raw package
        # change must not create an economic revision.
        equal = batch(
            values=("100.0", "120.00"),
            observed=AT + timedelta(days=1),
            realtime="2026-10-02",
        )
        assert store.save_batches([equal])["saved_points"] == 0
        assert (
            store.save_batches(
                [batch(values=("100",), observed=AT + timedelta(days=1))]
            )["saved_points"]
            == 0
        )
        assert store.status()["observation_count"] == 2  # missing source row preserved
        revised = batch(values=("200", "120"), observed=AT + timedelta(days=2))
        assert store.save_batches([revised]) == dict(saved_points=1, saved_revisions=1)
        historical = service.query_range(
            "2026-09-26", "2026-09-26", series_ids=("ICSA",), as_of=cutoff
        )
        current = service.query_range("2026-09-26", "2026-09-26", series_ids=("ICSA",))
        assert historical["rows"][0]["weekly_change"]["value"] == "20"
        assert current["rows"][0]["weekly_change"]["value"] == "-80"
        prior = service.query_range("2026-09-19", "2026-09-19", series_ids=("ICSA",))[
            "rows"
        ][0]
        assert prior["first_captured_value"] == "100"
        assert prior["value"] == "200"
        assert (
            conn.execute(
                f"SELECT weekly_change FROM {schema}.claims_weekly_changes "
                "WHERE week_ending='2026-09-26'"
            ).fetchone()[0]
            == -80
        )
        # Pending commits stay invisible; replay confirms at its actual time.
        conn.execute(
            f"UPDATE {schema}.revisions SET available_at=NULL WHERE revision=2"
        )
        pending = service.query_range("2026-09-19", "2026-09-19", series_ids=("ICSA",))[
            "rows"
        ][0]
        assert pending["value"] is None
        # Recovery also works with a new raw package and acquisition time.
        replay = batch(
            values=("200.0", "120.00"),
            observed=AT + timedelta(days=3),
            realtime="2026-10-03",
        )
        assert store.save_batches([replay])["saved_points"] == 0
        confirmed = service.query_range(
            "2026-09-19", "2026-09-19", series_ids=("ICSA",)
        )["rows"][0]
        assert confirmed["value"] == "200"
        assert confirmed["provenance"]["available_at"] > cutoff
        privileges = conn.execute(
            "SELECT has_table_privilege('odin_dba',%s,'SELECT'),"
            "has_table_privilege('odin_dba',%s,"
            "'INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
            (schema + ".revisions", schema + ".revisions"),
        ).fetchone()
        assert privileges == (True, False)
        # Verify a pending write in another session never blocks a reader.
        writer = build_service()
        try:
            with writer.store.connection.transaction():
                writer.store.connection.execute(
                    "SELECT pg_advisory_xact_lock(%s)", (7312026110202,)
                )
                assert (
                    service.query_range(
                        "2026-09-26", "2026-09-26", series_ids=("ICSA",)
                    )["rows"][0]["value"]
                    == "120"
                )
        finally:
            writer.close()
        # A provider '.' does not establish an official DOL null.
        assert (
            store.save_batches(
                [batch(values=("200", "."), observed=AT + timedelta(days=3))]
            )["saved_revisions"]
            == 1
        )
        assert (
            service.query_range("2026-09-26", "2026-09-26", series_ids=("ICSA",))[
                "rows"
            ][0]["missing_reason"]
            == "provider_missing"
        )
        # Validate all series before mutating any series in the admission.
        invalid = replace(batch(sid="CCSA"), raw=b"{}")
        with pytest.raises(Exception):
            store.save_batches([batch(sid="ICNSA"), invalid])
        assert (
            conn.execute(
                f"SELECT count(*) FROM {schema}.revisions WHERE series_id='ICNSA'"
            ).fetchone()[0]
            == 0
        )
        from market_data_foundation.historical.claims_release import (
            ClaimsReleaseRecords,
        )
        from tests.test_claims_release import URL, pages

        records = ClaimsReleaseRecords(store)
        before_import = conn.execute("SELECT clock_timestamp()").fetchone()[0]
        material = "\f".join(pages()).encode()
        obtained = AT + timedelta(days=30)
        imported = records.import_material(material, URL, obtained_at=obtained)
        assert imported["obtained_at"] == obtained.isoformat()
        assert imported["available_at"] > before_import.isoformat()
        assert records.read(as_of=before_import)["events"] == []
        replay = records.import_material(material, URL, obtained_at=obtained)
        assert replay["available_at"] == imported["available_at"]
        event = records.read()["events"][0]
        assert len([c for c in event["claims"] if c["role"] == "actual"]) == 6
        assert event["actual_available"]
        changed = pages()
        changed[0] = changed[0].replace(
            "initial claims was 100", "initial claims was 101"
        )
        changed[2] = changed[2].replace(
            "Initial Claims (SA) 100 90 +10", "Initial Claims (SA) 101 90 +11"
        )
        records.import_material("\f".join(changed).encode(), URL, obtained_at=obtained)
        conflict = records.read()["events"][0]
        assert conflict["conflicts"] and not conflict["actual_available"]
        assert len(records.read(read_mode="source_history")["events"]) == 2
        # Importing event evidence never alters continuous economic revisions.
        assert store.status()["revision_count"] == 2
    finally:
        conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        runtime.close()
