"""One-off CPI relational migration; caller owns backup and transaction."""

from datetime import datetime
from types import SimpleNamespace

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.cpi_timeseries_store import (
    SCHEMA,
    CpiStorageError,
    _latest_view_sql,
    _numeric,
    _relational_content,
    canonical,
    digest,
    raw_locator,
)


def migrate_relational_schema(connection):
    """Convert the existing schema inside a caller-owned transaction.

    Caller stops writers and takes a database backup first. Any validation or
    conversion failure rolls back with that transaction. No implicit commit.
    """
    if connection.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
        raise CpiStorageError("cpi_timeseries_migration_transaction_required")
    connection.execute(
        f"LOCK TABLE {SCHEMA}.batches,{SCHEMA}.revisions,{SCHEMA}.latest "
        "IN ACCESS EXCLUSIVE MODE"
    )
    batch_locations = connection.execute(
        f"SELECT raw_sha256,raw_locator FROM {SCHEMA}.batches"
    ).fetchall()
    if any(locator != raw_locator(sha) for sha, locator in batch_locations):
        raise CpiStorageError("cpi_timeseries_migration_batch_locator_mismatch")
    rows = connection.execute(
        f"SELECT r.series_id,r.month,r.revision,r.observed_at,r.batch_id,"
        f"r.content_sha256,r.document,b.raw_sha256,b.raw_locator,b.observed_at "
        f"FROM {SCHEMA}.revisions r JOIN {SCHEMA}.batches b USING(batch_id) "
        "ORDER BY r.series_id,r.month,r.revision"
    ).fetchall()
    pointers = connection.execute(
        f"SELECT series_id,month,revision FROM {SCHEMA}.latest"
    ).fetchall()
    expected, converted = {}, []
    observed = {}
    allowed = {
        "series_id",
        "month",
        "revision",
        "observed_at",
        "index_value",
        "availability",
        "provider_calculations",
        "footnotes",
        "raw_sha256",
        "raw_locator",
    }
    for series, month, revision, at, bid, sha, doc, raw_sha, locator, batch_at in rows:
        key = (series, month)
        point = SimpleNamespace(
            index_value=_numeric(doc.get("index_value")),
            provider_calculations=doc.get("provider_calculations"),
            footnotes=doc.get("footnotes"),
        )
        content = {
            name: doc[name]
            for name in (
                "index_value",
                "availability",
                "provider_calculations",
                "footnotes",
            )
        }
        if (
            set(doc) != allowed
            or doc["series_id"] != series
            or doc["month"] != month
            or doc["revision"] != revision
            or datetime.fromisoformat(doc["observed_at"]) != at
            or at != batch_at
            or doc["raw_sha256"] != raw_sha
            or doc["raw_locator"] != locator
            or locator != raw_locator(raw_sha)
            or digest(canonical(content).encode()) != sha
            or doc["availability"]
            != ("source_unavailable" if point.index_value is None else "available")
            or revision != expected.get(key, 0) + 1
            or (key in observed and at <= observed[key])
        ):
            raise CpiStorageError("cpi_timeseries_migration_inconsistent_history")
        expected[key], observed[key] = revision, at
        values = _relational_content(point)
        converted.append(
            (
                *values[:5],
                Jsonb(values[5]) if values[5] is not None else None,
                Jsonb(values[6]),
                series,
                month,
                revision,
            )
        )
    if sorted(pointers) != sorted(
        (*key, revision) for key, revision in expected.items()
    ):
        raise CpiStorageError("cpi_timeseries_migration_latest_mismatch")
    if connection.execute(f"SELECT count(*) FROM {SCHEMA}.revisions").fetchone()[
        0
    ] != len(rows):
        raise CpiStorageError("cpi_timeseries_migration_batch_missing")
    connection.execute(f"""ALTER TABLE {SCHEMA}.revisions
        ADD COLUMN index_value numeric,
        ADD COLUMN bls_pct_1m numeric,
        ADD COLUMN bls_pct_3m numeric,
        ADD COLUMN bls_pct_6m numeric,
        ADD COLUMN bls_pct_12m numeric,
        ADD COLUMN provider_extra jsonb,
        ADD COLUMN footnotes jsonb""")
    with connection.cursor() as cursor:
        cursor.executemany(
            f"UPDATE {SCHEMA}.revisions SET index_value=%s,bls_pct_1m=%s,bls_pct_3m=%s,"
            "bls_pct_6m=%s,bls_pct_12m=%s,provider_extra=%s,footnotes=%s "
            "WHERE series_id=%s AND month=%s AND revision=%s",
            converted,
        )
    connection.execute(f"DROP TABLE {SCHEMA}.latest")
    connection.execute(f"DROP INDEX {SCHEMA}.cpi_timeseries_observation_range")
    connection.execute(f"""ALTER TABLE {SCHEMA}.revisions
        DROP CONSTRAINT revisions_series_id_month_observed_at_key,
        DROP CONSTRAINT revisions_month_check,
        ALTER COLUMN month TYPE date USING (month || '-01')::date,
        ADD CONSTRAINT revisions_month_check CHECK(EXTRACT(DAY FROM month)=1),
        ADD CONSTRAINT revisions_index_value_check CHECK(index_value > 0
            AND index_value != 'NaN'::numeric AND index_value != 'Infinity'::numeric),
        ALTER COLUMN footnotes SET NOT NULL,
        ADD CONSTRAINT revisions_footnotes_check CHECK(jsonb_typeof(footnotes)='array'),
        DROP COLUMN content_sha256, DROP COLUMN document""")
    connection.execute(f"ALTER TABLE {SCHEMA}.batches DROP COLUMN raw_locator")
    connection.execute(_latest_view_sql())
    return {"revisions": len(rows), "latest": len(expected)}
