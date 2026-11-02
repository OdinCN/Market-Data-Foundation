"""Official PCE percent persistence and migration contracts, entirely offline."""

import copy
from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import psycopg
import pytest

from market_data_foundation.historical import cpi_timeseries as core
from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    _latest_view_sql,
    _relational_content,
    _revision_ddl,
    _revision_shape,
    _row_document,
    digest,
)
from market_data_foundation.historical.pce_timeseries import PceTimeSeriesService
from market_data_foundation.historical.pce_timeseries_metadata import (
    compact_pce_metadata,
)
from market_data_foundation.historical.pce_timeseries_migration import (
    LEGACY_COLUMNS,
    SCHEMA,
    migrate_pce_percent_schema,
)
from market_data_foundation.historical.pce_timeseries_store import (
    InMemoryTimeSeriesStore,
    PceStorageError,
    PostgresTimeSeriesStore,
    raw_locator,
)

AT = datetime(2026, 10, 30, tzinfo=UTC)
SID = "DPCERG"


def point(official="0.3", **kwargs):
    calculations = {
        "bea": {"NoteRef": "index-note", "pct_1m": {"NoteRef": "official-note"}},
        "description": "Keep this unknown source description",
    }
    if official is not None:
        calculations["bea_pct_1m"] = official
    return replace(
        SourcePoint(SID, "2026-09", Decimal("100.27"), calculations, ()), **kwargs
    )


def batch(p, at=AT):
    return SourceBatch(
        (p,), str(at).encode(), {"seriesid": [SID], "request_count": 2}, at, 3
    )


@pytest.mark.parametrize("official", ["0.3", "0", "-0.1", None])
def test_typed_pce_roundtrip_and_memory_serialization(official):
    p = point(official)
    values = _relational_content(p, profile=PCE_PROFILE)
    assert len(values) == 4
    expected = None if official is None else Decimal(official)
    assert values[1] == expected
    assert "bea_pct_1m" not in values[-2]
    document = _row_document(
        (SID, date(2026, 9, 1), 1, AT, "batch", *values, "sha", AT, "active", None),
        profile=PCE_PROFILE,
    )
    assert document["bea_pct_1m"] == official
    assert document["provider_calculations"] == p.provider_calculations
    assert not any(key.startswith("bls_") for key in document)
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch(p),))
    saved = store.read_range((SID,), "2026-09", "2026-09")[0]
    assert saved["bea_pct_1m"] == official
    assert saved["provider_calculations"] == p.provider_calculations


@pytest.mark.parametrize("value", [True, {}, [], "NaN", "Infinity", "-Infinity", "NA"])
def test_official_percent_uses_safe_finite_decimal_validation(value):
    with pytest.raises(PceStorageError):
        InMemoryTimeSeriesStore().save_batches((batch(point(value)),))


def test_official_and_note_updates_create_immutable_revisions():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    p = point()
    store.save_batches((batch(p),))
    original = copy.deepcopy(store._history)
    assert store.save_batches((batch(point("0.30"), AT + timedelta(seconds=1)),)) == {
        "saved_points": 0,
        "saved_revisions": 0,
    }
    assert store.save_batches((batch(point("0.4"), AT + timedelta(seconds=2)),)) == {
        "saved_points": 1,
        "saved_revisions": 1,
    }
    changed = point("0.4")
    changed.provider_calculations["bea"]["pct_1m"]["NoteRef"] = "updated-note"
    assert (
        store.save_batches((batch(changed, AT + timedelta(seconds=3)),))[
            "saved_revisions"
        ]
        == 1
    )
    assert store._history[(SID, p.month)][0] == original[(SID, p.month)][0]
    assert (
        store.read_range((SID,), p.month, p.month, as_of=AT)[0]["bea_pct_1m"] == "0.3"
    )


def test_partial_refresh_fails_without_erasing_known_official_or_copying_across_index():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    store.save_batches((batch(point()),))
    before = copy.deepcopy(store._history)
    with pytest.raises(
        PceStorageError, match="official_percent_missing_unchanged_index"
    ):
        store.save_batches((batch(point(None), AT + timedelta(seconds=1)),))
    assert store._history == before
    changed = point(None, index_value=Decimal("101"))
    store.save_batches((batch(changed, AT + timedelta(seconds=2)),))
    assert (
        store.read_range((SID,), changed.month, changed.month)[0]["bea_pct_1m"] is None
    )
    assert store._history[(SID, changed.month)][0]["bea_pct_1m"] == "0.3"


class PgConnection:
    autocommit = True

    def __init__(self, old=None, rows=()):
        self.old, self.rows, self.statements = old, rows, []
        self.rolled_back = False

    def execute(self, statement, params=()):
        self.statements.append((statement, params))
        return SimpleNamespace(
            fetchone=lambda: self.old if "SELECT revision" in statement else None,
            fetchall=lambda: self.rows,
        )

    @contextmanager
    def transaction(self):
        try:
            yield
        except Exception:
            self.rolled_back = True
            raise


class Raw:
    def put_verified(self, raw):
        return raw_locator(digest(raw))


@pytest.mark.parametrize("official", ["0.3", None])
def test_pg_insert_and_read_use_only_pce_columns(official):
    p = point(official)
    values = _relational_content(p, profile=PCE_PROFILE)
    conn = PgConnection(rows=[(SID, date(2026, 9, 1), 1, AT, "batch", *values, "sha")])
    store = PostgresTimeSeriesStore(conn, Raw(), clock=lambda: AT)
    store.save_batches((batch(p),))
    statement, params = next(
        (s, p) for s, p in conn.statements if f"INSERT INTO {SCHEMA}.revisions" in s
    )
    assert "bea_pct_1m" in statement and "bls_pct" not in statement
    assert statement.count("%s") == len(params) == 11
    assert params[6] == (None if official is None else Decimal(official))
    assert params[7].obj == values[-2]
    result = store.read_range((SID,), p.month, p.month)[0]
    assert result["bea_pct_1m"] == official
    assert result["provider_calculations"] == p.provider_calculations
    assert "bea_pct_1m" in conn.statements[-1][0]


def test_pg_official_change_revises_and_partial_missing_rolls_back():
    values = _relational_content(point(), profile=PCE_PROFILE)
    old = (7, AT, *values, "active")
    conn = PgConnection(old=old)
    store = PostgresTimeSeriesStore(conn, Raw())
    assert (
        store.save_batches((batch(point("0.4"), AT + timedelta(seconds=1)),))[
            "saved_revisions"
        ]
        == 1
    )
    insert = next(
        p for s, p in conn.statements if f"INSERT INTO {SCHEMA}.revisions" in s
    )
    assert insert[2] == 8
    conn.statements.clear()
    with pytest.raises(
        PceStorageError, match="official_percent_missing_unchanged_index"
    ):
        store.save_batches((batch(point(None), AT + timedelta(seconds=2)),))
    assert conn.rolled_back
    assert not any(".revisions SET" in s for s, _ in conn.statements)


@pytest.mark.parametrize("profile", [CPI_PROFILE, PPI_PROFILE, PCE_PROFILE])
def test_profile_specific_schema_and_views(profile):
    ddl = _revision_ddl(profile=profile)
    view = _latest_view_sql(event_columns=True, profile=profile)
    assert ("bea_pct_1m" in ddl) == (profile is PCE_PROFILE)
    assert ("bea_pct_1m" in view) == (profile is PCE_PROFILE)
    assert ("bls_pct_1m" in ddl) == (profile is not PCE_PROFILE)
    assert set(_revision_shape(profile)).issuperset({"available_at", "index_value"})


def test_official_and_derived_api_units_and_request_accounting():
    store = InMemoryTimeSeriesStore(clock=lambda: AT)
    previous = point(None, month="2026-08", index_value=Decimal("100"))
    acquired = replace(batch(point()), points=(previous, point()))
    source = SimpleNamespace(profile=PCE_PROFILE, fetch=lambda *args: (acquired,))
    result = PceTimeSeriesService(store, source).update_range(
        "2026-08", "2026-09", series_ids=(SID,), include_buffer=False
    )
    p = result["points"][-1]
    assert p["bea_pct_1m"] == "0.3" and p["mom_pct"] == "0.27"
    assert p["change_1m_ratio"] == "0.0027"
    assert result["units"]["bea_pct_1m"] == result["units"]["mom_pct"] == "percent"
    assert result["units"]["change_1m_ratio"] == "ratio"
    assert not any(k.startswith("bls_") for k in result["units"])
    assert "NULL never means 0" in result["official_calculation_method"]
    assert "index coverage only" in result["official_calculation_method"]
    assert "has not yet been collected" in result["official_calculation_method"]
    assert result["provider_requests"] == 2
    assert result["provider_attempts"] == result["source_request_count"] == 3


def test_multiple_gaps_budget_counts_two_http_requests_per_pce_window():
    local = [
        {
            "series_id": SID,
            "month": month,
            "index_value": "100",
            "available_at": AT.isoformat(),
        }
        for month in core.month_range("1975-01", "2026-12")
        if int(month[:4]) % 2 == 0
    ]
    store = SimpleNamespace(
        operation=nullcontext,
        read_snapshot=nullcontext,
        read_range=lambda *a, **kw: local,
    )
    source = SimpleNamespace(
        profile=PCE_PROFILE,
        plan_windows=lambda first, last: [(first, last)],
        max_range_requests=48,
        fetch=lambda *a: pytest.fail("request budget must fail before fetching"),
    )
    with pytest.raises(core.CpiTimeSeriesError, match="request_budget_exceeded"):
        PceTimeSeriesService(store, source).query_range(
            "1976-01", "2026-12", series_ids=(SID,)
        )


class MigrationConnection:
    info = SimpleNamespace(transaction_status=psycopg.pq.TransactionStatus.INTRANS)

    def __init__(self):
        self.statements = []
        legacy = _revision_shape(PCE_PROFILE)
        legacy.pop("bea_pct_1m")
        legacy.update(dict.fromkeys(LEGACY_COLUMNS, "numeric"))
        rates = {
            **legacy,
            "month_label": "text",
            **{f"change_{k}m_ratio": "numeric" for k in (1, 3, 6, 12)},
        }
        self.columns = [
            (t, c, v)
            for t, cols in {
                "revisions": legacy,
                "latest": legacy,
                PCE_PROFILE.rates_view: rates,
            }.items()
            for c, v in cols.items()
        ]
        self.constraints = [
            ("CHECK ((series_id = ANY (ARRAY['DPCERG'::text, 'DPCCRG'::text])))",)
        ]
        self.non_pce = self.nonempty = self.preexisting = False
        self.legacy_column_grants = False
        self.metadata_rows = []
        self.grants = [
            ("latest", None, 10, "odin_dba", "SELECT", False),
            (PCE_PROFILE.rates_view, None, 10, "odin_dba", "SELECT", True),
            ("latest", "index_value", 0, "unknown (OID=0)", "SELECT", False),
        ]
        self.views = [
            ("latest", "original_owner", ["security_barrier=true"], "kept comment"),
            (PCE_PROFILE.rates_view, "original_owner", None, None),
        ]
        self.fail_drop = False

    def execute(self, statement, params=()):
        self.statements.append((statement, params))
        one, rows = None, []
        if "information_schema.columns" in statement:
            rows = self.columns
        elif "pg_get_constraintdef" in statement:
            rows = self.constraints
        elif "series_id NOT IN" in statement:
            one = (self.non_pce,)
        elif "bls_pct_1m IS NOT NULL" in statement:
            one = (self.nonempty,)
        elif "provider_extra ?" in statement:
            one = (self.preexisting,)
        elif "SELECT series_id,month,revision,provider_extra" in statement:
            rows = self.metadata_rows
        elif "t.attname=ANY" in statement:
            one = (self.legacy_column_grants,)
        elif "SELECT count(*)" in statement:
            one = (19,)
        elif "obj_description" in statement:
            rows = self.views
        elif "NULL::text AS column_name" in statement:
            rows = self.grants
        elif "SELECT DISTINCT c.relname" in statement:
            rows = [("latest", 20, "default_reader")]
        elif statement.startswith("DROP VIEW") and self.fail_drop:
            raise psycopg.errors.DependentObjectsStillExist("synthetic dependent")
        return SimpleNamespace(fetchone=lambda: one, fetchall=lambda: rows)


def mutations(conn):
    return [
        s
        for s, _ in conn.statements
        if s.startswith(("ALTER", "DROP", "CREATE", "GRANT", "REVOKE", "UPDATE"))
    ]


@pytest.mark.parametrize(
    "schema",
    [
        CPI_PROFILE.schema,
        PPI_PROFILE.schema,
        "public",
        "arbitrary",
        SCHEMA + "_rehearsal_x;DROP SCHEMA public",
        SCHEMA + "_rehearsal_parent",
    ],
)
def test_migration_rejects_unapproved_targets_before_io(schema):
    conn = MigrationConnection()
    with pytest.raises(PceStorageError, match="schema_forbidden"):
        migrate_pce_percent_schema(conn, schema=schema)
    assert not conn.statements


@pytest.mark.parametrize(
    "status",
    [
        psycopg.pq.TransactionStatus.IDLE,
        psycopg.pq.TransactionStatus.INERROR,
        psycopg.pq.TransactionStatus.ACTIVE,
    ],
)
def test_migration_requires_already_open_caller_transaction(status):
    conn = MigrationConnection()
    conn.info = SimpleNamespace(transaction_status=status)
    with pytest.raises(PceStorageError, match="transaction_required"):
        migrate_pce_percent_schema(conn)
    assert not conn.statements


@pytest.mark.parametrize(
    ("attribute", "value", "reason"),
    [
        ("columns", [], "legacy_shape_required"),
        ("constraints", [], "pce_series_constraint_required"),
        ("non_pce", True, "non_pce_history"),
        ("nonempty", True, "nonempty_legacy_percent"),
        ("preexisting", True, "preexisting_official_metadata"),
        ("legacy_column_grants", True, "legacy_column_grants"),
        ("views", [], "views_required"),
        (
            "grants",
            [("latest", "bls_pct_1m", 10, "odin_dba", "SELECT", False)],
            "unsupported_view_grant",
        ),
    ],
)
def test_migration_safety_rejections_precede_ddl(attribute, value, reason):
    conn = MigrationConnection()
    setattr(conn, attribute, value)
    with pytest.raises(PceStorageError, match=reason):
        migrate_pce_percent_schema(conn)
    assert not mutations(conn)


@pytest.mark.parametrize("rehearsal", [False, True])
def test_migration_preserves_history_grants_owners_and_outer_transaction(rehearsal):
    conn = MigrationConnection()
    schema = SCHEMA + "_rehearsal_parent" if rehearsal else SCHEMA
    result = migrate_pce_percent_schema(conn, schema=schema, allow_rehearsal=rehearsal)
    assert result["schema"] == schema and result["revisions_preserved"] == 19
    assert result["official_values_backfilled"] == 0 and result["committed"] is False
    statements = [s for s, _ in conn.statements]
    assert conn.statements[0] == (
        "SELECT pg_advisory_xact_lock(%s)",
        (PCE_PROFILE.lock_id,),
    )
    assert statements[1] == (
        f'LOCK TABLE "{schema}".batches,"{schema}".revisions IN ACCESS EXCLUSIVE MODE'
    )
    joined = "\n".join(statements)
    assert "CASCADE" not in joined and "UPDATE " not in joined
    assert "ALTER SCHEMA" not in joined and "CREATE ROLE" not in joined
    assert "COMMIT" not in joined and "ROLLBACK" not in joined
    assert "GRANT SELECT ON" in joined and 'TO "odin_dba" WITH GRANT OPTION' in joined
    assert 'GRANT SELECT ("index_value")' in joined and "TO PUBLIC" in joined
    assert 'FROM "default_reader"' in joined
    assert joined.count('OWNER TO "original_owner"') == 2
    assert "security_barrier" in joined and "kept comment" in joined
    alter = next(s for s in statements if "ADD COLUMN bea_pct_1m" in s)
    assert "DEFAULT" not in alter
    assert all("DROP COLUMN " + name + " RESTRICT" in alter for name in LEGACY_COLUMNS)
    assert statements.index(
        next(
            s
            for s in statements
            if s.startswith("DROP VIEW") and "pce_timeseries_rates" in s
        )
    ) < statements.index(
        next(s for s in statements if s.startswith("DROP VIEW") and ".latest" in s)
    )


def test_migration_external_dependents_fail_closed_without_cascade():
    conn = MigrationConnection()
    conn.fail_drop = True
    with pytest.raises(psycopg.errors.DependentObjectsStillExist):
        migrate_pce_percent_schema(conn)
    assert not any("ALTER TABLE" in s for s, _ in conn.statements)


def test_metadata_compaction_preserves_notes_descriptions_unknown_and_mismatches():
    extra = {
        "bea": {
            "SeriesCode": SID,
            "TimePeriod": "2026M09",
            "LineNumber": "1",
            "TableName": "T20804",
            "CL_UNIT": "Level",
            "UNIT_MULT": "0",
            "METRIC_NAME": "Fisher Price Index",
            "NoteRef": "index-note",
            "pct_1m": {"NoteRef": "official-note"},
            "LineDescription": "Historical description",
            "Description": "Unknown custom description",
            "unknown": ["preserved"],
        },
        "other": "unchanged",
    }
    before = copy.deepcopy(extra)
    cleaned = compact_pce_metadata(extra, series_id=SID)
    assert extra == before
    assert cleaned == {
        "bea": {
            "TimePeriod": "2026M09",
            "NoteRef": "index-note",
            "pct_1m": {"NoteRef": "official-note"},
            "LineDescription": "Historical description",
            "Description": "Unknown custom description",
            "unknown": ["preserved"],
        },
        "other": "unchanged",
    }
    p = point()
    p.provider_calculations.clear()
    p.provider_calculations.update({**copy.deepcopy(extra), "bea_pct_1m": "0.3"})
    values = _relational_content(p, profile=PCE_PROFILE)
    assert values[1] == Decimal("0.3")
    assert values[-2] == cleaned
    assert p.provider_calculations == {**before, "bea_pct_1m": "0.3"}
    mismatch = {
        "bea": {
            "SeriesCode": "unexpected",
            "TableName": "T20807",
            "LineNumber": 1,
            "CL_UNIT": "Index",
            "UNIT_MULT": 0,
            "METRIC_NAME": "Other",
        }
    }
    assert compact_pce_metadata(mismatch, series_id=SID) == mismatch
    assert compact_pce_metadata({"bea": {"SeriesCode": SID}}, series_id=SID) is None
    assert (
        compact_pce_metadata({"bea": {"LineNumber": "25"}}, series_id="DPCCRG") is None
    )
    for bea in (None, "raw", ["unparsed"], 0):
        assert compact_pce_metadata({"bea": bea}, series_id=SID) == {"bea": bea}
    assert compact_pce_metadata({"bea": {}, "other": 1}, series_id=SID) == {"other": 1}


def test_migration_compacts_only_metadata_and_does_not_backfill_official():
    conn = MigrationConnection()
    extra = {"bea": {"SeriesCode": SID, "NoteRef": "kept", "Description": "kept"}}
    unchanged = {"bea": {"SeriesCode": "unexpected", "NoteRef": "keep"}}
    empty = {"bea": {"SeriesCode": SID}}
    conn.metadata_rows = [
        (SID, date(2026, 9, 1), 7, extra),
        (SID, date(2026, 8, 1), 3, unchanged),
        (SID, date(2026, 7, 1), 2, empty),
    ]
    before = copy.deepcopy(conn.metadata_rows)
    result = migrate_pce_percent_schema(conn)
    assert result["metadata_rows_compacted"] == 2
    assert "deferred" not in result["metadata_cleanup"]
    assert conn.metadata_rows == before
    updates = [(s, p) for s, p in conn.statements if s.startswith("UPDATE")]
    assert len(updates) == 2
    assert updates[1][1] == (None, SID, date(2026, 7, 1), 2)
    statement, params = updates[0]
    assert statement == (
        f'UPDATE "{SCHEMA}".revisions SET provider_extra=%s '
        "WHERE series_id=%s AND month=%s AND revision=%s"
    )
    assert params[0].obj == {"bea": {"NoteRef": "kept", "Description": "kept"}}
    assert params[1:] == (SID, date(2026, 9, 1), 7)


@pytest.mark.parametrize("legacy", [False, True])
def test_explicit_schema_creation_checks_pce_shape_without_silent_migration(legacy):
    class SchemaConnection(PgConnection):
        def execute(self, statement, params=()):
            self.statements.append((statement, params))
            one, rows = None, []
            if "pg_get_userbyid(nspowner)" in statement:
                one = (True,)
            elif "SELECT relkind" in statement:
                one = ("v",)
            elif "information_schema.columns" in statement:
                revision = _revision_shape(PCE_PROFILE)
                if legacy:
                    revision.pop("bea_pct_1m")
                    revision.update(dict.fromkeys(LEGACY_COLUMNS, "numeric"))
                shapes = {
                    "revisions": revision,
                    "latest": revision,
                    "batches": {
                        "batch_id": "text",
                        "raw_sha256": "text",
                        "observed_at": "timestamp with time zone",
                        "request": "jsonb",
                        "provider_attempts": "integer",
                    },
                }
                rows = [
                    (t, c, v) for t, shape in shapes.items() for c, v in shape.items()
                ]
            return SimpleNamespace(fetchone=lambda: one, fetchall=lambda: rows)

    conn = SchemaConnection()
    store = PostgresTimeSeriesStore(conn, None)
    if legacy:
        with pytest.raises(PceStorageError, match="schema_shape_mismatch"):
            store.ensure_schema()
        assert conn.rolled_back
    else:
        store.ensure_schema()
    assert not any(s.startswith(("ALTER", "DROP")) for s, _ in conn.statements)
