"""Independent migration rejection and unchanged-value checks; no live services."""

import runpy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest

from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    InMemoryTimeSeriesStore,
    canonical,
    digest,
    raw_locator,
)

MIGRATE = runpy.run_path(
    str(Path(__file__).parents[1] / "scripts/cpi_timeseries_migrate.py")
)["migrate_relational_schema"]
NOW = datetime(2026, 10, 28, tzinfo=UTC)
SERIES = "CUSR0000SA0"
RAW_SHA = digest(b"original")


def legacy_row():
    content = {
        "index_value": "336.298",
        "availability": "available",
        "provider_calculations": {"net_changes": {}, "pct_changes": {"12": "3.7"}},
        "footnotes": [{}],
    }
    doc = {
        "series_id": SERIES,
        "month": "2026-09",
        "revision": 1,
        "observed_at": NOW.isoformat(),
        "raw_sha256": RAW_SHA,
        "raw_locator": raw_locator(RAW_SHA),
        **content,
    }
    return [
        SERIES,
        "2026-09",
        1,
        NOW,
        "batch",
        digest(canonical(content).encode()),
        doc,
        RAW_SHA,
        raw_locator(RAW_SHA),
        NOW,
    ]


class ValidationConnection:
    """Supplies existing rows; any attempted schema/data mutation fails the test."""

    info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.INTRANS)

    def __init__(self, row, pointers=None):
        self.row = row
        self.pointers = [(SERIES, "2026-09", 1)] if pointers is None else pointers
        self.statements = []

    def execute(self, sql, params=()):
        self.statements.append(sql)
        assert sql.startswith(("SELECT ", "LOCK TABLE ")), "mutation before rejection"
        if "r.content_sha256" in sql:
            return SimpleNamespace(fetchall=lambda: [self.row])
        if "SELECT series_id,month,revision" in sql:
            return SimpleNamespace(fetchall=lambda: self.pointers)
        if "raw_locator" in sql:
            return SimpleNamespace(fetchall=lambda: [(RAW_SHA, raw_locator(RAW_SHA))])
        return SimpleNamespace(fetchone=lambda: (1,), fetchall=lambda: [])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unknown_field", "would otherwise disappear"),
        ("series_id", "CUUR0000SA0"),
        ("month", "2026-08"),
        ("revision", 2),
        ("observed_at", "2026-10-29T00:00:00+00:00"),
        ("index_value", "999"),
        ("footnotes", [{"text": "modified"}]),
        ("raw_locator", "unexpected-location"),
    ],
)
def test_migration_rejects_inconsistent_document_before_ddl(field, value):
    row = legacy_row()
    row[6][field] = value
    conn = ValidationConnection(row)
    with pytest.raises(CpiStorageError, match="inconsistent_history"):
        MIGRATE(conn)


def test_migration_rejects_inconsistent_latest_before_ddl():
    conn = ValidationConnection(legacy_row(), pointers=[])
    with pytest.raises(CpiStorageError, match="latest_mismatch"):
        MIGRATE(conn)


def test_migration_requires_explicit_caller_transaction():
    conn = ValidationConnection(legacy_row())
    conn.info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.IDLE)
    with pytest.raises(CpiStorageError, match="transaction_required"):
        MIGRATE(conn)
    assert not conn.statements


def test_decimal_scale_and_signed_zero_do_not_create_revision():
    store = InMemoryTimeSeriesStore()
    first = SourcePoint(
        SERIES,
        "2026-09",
        Decimal("336.2980"),
        {"pct_changes": {"1": "-0.0", "12": "3.70"}},
        (),
    )
    second = SourcePoint(
        SERIES,
        "2026-09",
        Decimal("336.298"),
        {"net_changes": {}, "pct_changes": {"1": "0.00", "12": "3.7"}},
        (),
    )
    store.save_batches((SourceBatch((first,), b"first", {}, NOW, 1),))
    result = store.save_batches(
        (SourceBatch((second,), b"second", {}, NOW + timedelta(minutes=1), 1),)
    )
    assert result == {"saved_points": 0, "saved_revisions": 0}
    saved = store.read_range((SERIES,), "2026-09", "2026-09")[0]
    assert saved["revision"] == 1
    assert saved["observed_at"] == NOW.isoformat()
