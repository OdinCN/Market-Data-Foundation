"""Idempotent CPI confirmation/withdrawal migration, executed only by the operator.

Requires the existing two-table relational schema and a caller-owned transaction.
No connection, credentials, provider request or implicit commit occurs here.
"""

import psycopg

from market_data_foundation.historical.cpi_timeseries_store import (
    SCHEMA,
    CpiStorageError,
    _latest_view_sql,
    _rates_view_sql,
)


def migrate_event_schema(connection):
    """Preserve existing observations, grants and dependent views.

    Existing available_at stays NULL: neither observed_at nor migration time is
    evidence of the original successful commit. CREATE OR REPLACE appends view
    columns without dropping the original view identity, grants or dependencies.
    The caller owns backup, execution, transaction commit and live verification.
    """
    if connection.info.transaction_status != psycopg.pq.TransactionStatus.INTRANS:
        raise CpiStorageError("cpi_timeseries_migration_transaction_required")
    connection.execute(
        f"LOCK TABLE {SCHEMA}.batches,{SCHEMA}.revisions IN ACCESS EXCLUSIVE MODE"
    )
    connection.execute(f"""ALTER TABLE {SCHEMA}.revisions
        ADD COLUMN IF NOT EXISTS available_at timestamptz,
        ADD COLUMN IF NOT EXISTS version_status text NOT NULL DEFAULT 'active',
        ADD COLUMN IF NOT EXISTS evidence_url text""")
    # Withdrawal batches retain a caller-verified evidence reference and make
    # zero provider requests. Keep the existing constraint name, allow zero.
    connection.execute(f"""ALTER TABLE {SCHEMA}.batches
        DROP CONSTRAINT IF EXISTS batches_provider_attempts_check,
        ADD CONSTRAINT batches_provider_attempts_check CHECK(provider_attempts >= 0)""")
    constraints = {
        "cpi_version_status_check": "version_status IN ('active','withdrawn')",
        "cpi_withdrawal_evidence_check": (
            "version_status != 'withdrawn' OR "
            "(index_value IS NULL AND evidence_url IS NOT NULL)"
        ),
    }
    for name, expression in constraints.items():
        exists = connection.execute(
            "SELECT 1 FROM pg_constraint WHERE conrelid=%s::regclass AND conname=%s",
            (f"{SCHEMA}.revisions", name),
        ).fetchone()
        if not exists:
            connection.execute(
                f"ALTER TABLE {SCHEMA}.revisions "
                f"ADD CONSTRAINT {name} CHECK({expression})"
            )
    connection.execute(_latest_view_sql(event_columns=True))
    connection.execute(_rates_view_sql())
    return {"available_at_backfilled": False, "fact_tables": 2}
