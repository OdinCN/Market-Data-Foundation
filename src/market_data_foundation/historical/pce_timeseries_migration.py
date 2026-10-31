"""Operator-only PCE official-percent migration; no connection or implicit commit."""

from __future__ import annotations

import re

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    _latest_view_sql,
    _rates_view_sql,
    _revision_shape,
)
from market_data_foundation.historical.pce_timeseries_metadata import (
    compact_pce_metadata,
)

SCHEMA = PCE_PROFILE.schema
LEGACY_COLUMNS = tuple(f"bls_pct_{k}m" for k in (1, 3, 6, 12))


def _fail(reason):
    raise CpiStorageError("pce_timeseries_migration_" + reason)


def _identifier(value):
    return sql.Identifier(value).as_string()


def migrate_pce_percent_schema(connection, *, schema=SCHEMA, allow_rehearsal=False):
    """Migrate only PCE, inside the caller's already open psycopg transaction.

    An explicit rehearsal target must be named
    ``mdf_pce_timeseries_dev_r1_rehearsal_<lowercase identifier>`` and pass the
    same shape, validated series constraint and content checks as production.
    Caller must roll back on any error. DROP uses RESTRICT, never CASCADE.
    No old observation receives a current official value or availability time.
    Only exact redundant identities/constants are compacted; all uncertain
    metadata, descriptions and notes remain lossless.
    """
    if not isinstance(schema, str) or (
        schema != SCHEMA
        and not (
            allow_rehearsal is True
            and re.fullmatch(SCHEMA + r"_rehearsal_[a-z][a-z0-9_]{0,19}", schema)
        )
    ):
        _fail("schema_forbidden")
    if (
        getattr(getattr(connection, "info", None), "transaction_status", None)
        != psycopg.pq.TransactionStatus.INTRANS
    ):
        _fail("transaction_required")
    target = _identifier(schema)
    # Match writer lock order: advisory lock before either table lock.
    connection.execute("SELECT pg_advisory_xact_lock(%s)", (PCE_PROFILE.lock_id,))
    connection.execute(
        f"LOCK TABLE {target}.batches,{target}.revisions IN ACCESS EXCLUSIVE MODE"
    )
    rows = connection.execute(
        "SELECT table_name,column_name,data_type FROM information_schema.columns "
        "WHERE table_schema=%s AND table_name IN ('revisions','latest',%s)",
        (schema, PCE_PROFILE.rates_view),
    ).fetchall()
    actual = {name: {} for name in ("revisions", "latest", PCE_PROFILE.rates_view)}
    for table, column, kind in rows:
        actual[table][column] = kind
    legacy = _revision_shape(PCE_PROFILE)
    legacy.pop("bea_pct_1m")
    legacy.update(dict.fromkeys(LEGACY_COLUMNS, "numeric"))
    rates = {
        **legacy,
        "month_label": "text",
        **{f"change_{k}m_ratio": "numeric" for k in (1, 3, 6, 12)},
    }
    if actual != {"revisions": legacy, "latest": legacy, PCE_PROFILE.rates_view: rates}:
        _fail("legacy_shape_required")
    constraints = connection.execute(
        "SELECT pg_get_constraintdef(c.oid) FROM pg_constraint c "
        "JOIN pg_class t ON t.oid=c.conrelid "
        "JOIN pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=%s AND t.relname='revisions' "
        "AND c.contype='c' AND c.convalidated",
        (schema,),
    ).fetchall()
    # PostgreSQL renders IN as = ANY (ARRAY[...]); accept only the fixed PCE
    # predicate, with no extra terms that could admit other identities.
    expected_constraint = "CHECK(series_id=ANY(ARRAY['DPCERG'::text,'DPCCRG'::text]))"
    if not any(
        re.sub(r'[\s()"]', "", definition)
        == re.sub(r'[\s()"]', "", expected_constraint)
        for (definition,) in constraints
    ):
        _fail("pce_series_constraint_required")
    if connection.execute(
        f"SELECT EXISTS(SELECT 1 FROM {target}.revisions "
        "WHERE series_id NOT IN ('DPCERG','DPCCRG'))"
    ).fetchone()[0]:
        _fail("non_pce_history")
    if connection.execute(
        f"SELECT EXISTS(SELECT 1 FROM {target}.revisions WHERE "
        + " OR ".join(f"{name} IS NOT NULL" for name in LEGACY_COLUMNS)
        + ")"
    ).fetchone()[0]:
        _fail("nonempty_legacy_percent")
    # Never treat an earlier JSON attribute as a historical official observation.
    # Reject it rather than expose conflicting numeric NULL and JSON official data.
    if connection.execute(
        f"SELECT EXISTS(SELECT 1 FROM {target}.revisions "
        "WHERE provider_extra ? 'bea_pct_1m')"
    ).fetchone()[0]:
        _fail("preexisting_official_metadata")
    count = connection.execute(f"SELECT count(*) FROM {target}.revisions").fetchone()[0]
    metadata_rows = connection.execute(
        f"SELECT series_id,month,revision,provider_extra FROM {target}.revisions "
        "WHERE jsonb_typeof(provider_extra->'bea')='object'"
    ).fetchall()
    compacted = []
    for series, month, revision, extra in metadata_rows:
        cleaned = compact_pce_metadata(extra, series_id=series)
        if cleaned != extra:
            compacted.append((cleaned, series, month, revision))
    if connection.execute(
        "SELECT EXISTS(SELECT 1 FROM pg_attribute t "
        "JOIN pg_class c ON c.oid=t.attrelid "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(t.attacl) a "
        "WHERE n.nspname=%s AND c.relname='revisions' AND t.attname=ANY(%s))",
        (schema, list(LEGACY_COLUMNS)),
    ).fetchone()[0]:
        _fail("legacy_column_grants")
    views = connection.execute(
        "SELECT c.relname,pg_get_userbyid(c.relowner),c.reloptions,"
        "obj_description(c.oid,'pg_class') FROM pg_class c "
        "JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=%s AND c.relname IN ('latest',%s) AND c.relkind='v'",
        (schema, PCE_PROFILE.rates_view),
    ).fetchall()
    if {row[0] for row in views} != {"latest", PCE_PROFILE.rates_view}:
        _fail("views_required")
    grants = connection.execute(
        "SELECT c.relname,NULL::text AS column_name,a.grantee,"
        "pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(COALESCE(c.relacl,"
        "acldefault('r',c.relowner))) a "
        "WHERE n.nspname=%s AND c.relname IN ('latest',%s) "
        "UNION ALL SELECT c.relname,t.attname,a.grantee,"
        "pg_get_userbyid(a.grantee),a.privilege_type,a.is_grantable "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "JOIN pg_attribute t ON t.attrelid=c.oid AND t.attnum>0 "
        "CROSS JOIN LATERAL aclexplode(t.attacl) a "
        "WHERE n.nspname=%s AND c.relname IN ('latest',%s)",
        (schema, PCE_PROFILE.rates_view, schema, PCE_PROFILE.rates_view),
    ).fetchall()
    allowed = {
        "SELECT",
        "INSERT",
        "UPDATE",
        "DELETE",
        "TRUNCATE",
        "REFERENCES",
        "TRIGGER",
        "MAINTAIN",
    }
    for view, column, grantee, role, privilege, grantable in grants:
        if privilege not in allowed or column in LEGACY_COLUMNS:
            _fail("unsupported_view_grant")
    for name, owner, options, comment in views:
        if any(
            option.split("=", 1)[0]
            not in {"security_barrier", "security_invoker", "check_option"}
            or "=" not in option
            for option in options or ()
        ):
            _fail("unsupported_view_option")

    # Removing view columns is impossible with CREATE OR REPLACE. RESTRICT
    # deliberately aborts on any external dependent, leaving rollback to caller.
    connection.execute(f"DROP VIEW {target}.{PCE_PROFILE.rates_view} RESTRICT")
    connection.execute(f"DROP VIEW {target}.latest RESTRICT")
    connection.execute(
        f"ALTER TABLE {target}.revisions ADD COLUMN bea_pct_1m numeric, "
        + ", ".join("DROP COLUMN " + name + " RESTRICT" for name in LEGACY_COLUMNS)
    )
    for cleaned, series, month, revision in compacted:
        connection.execute(
            f"UPDATE {target}.revisions SET provider_extra=%s "
            "WHERE series_id=%s AND month=%s AND revision=%s",
            (None if cleaned is None else Jsonb(cleaned), series, month, revision),
        )
    connection.execute(
        _latest_view_sql(event_columns=True, profile=PCE_PROFILE).replace(
            SCHEMA, target
        )
    )
    connection.execute(_rates_view_sql(profile=PCE_PROFILE).replace(SCHEMA, target))
    # New views can inherit default ACLs. Remove those before replaying the
    # captured ACL, so default privileges cannot silently broaden access.
    defaults = connection.execute(
        "SELECT DISTINCT c.relname,a.grantee,pg_get_userbyid(a.grantee) "
        "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "CROSS JOIN LATERAL aclexplode(c.relacl) a "
        "WHERE n.nspname=%s AND c.relname IN ('latest',%s)",
        (schema, PCE_PROFILE.rates_view),
    ).fetchall()
    for view, grantee, role in defaults:
        who = "PUBLIC" if grantee == 0 else _identifier(role)
        connection.execute(f"REVOKE ALL ON {target}.{_identifier(view)} FROM {who}")
    for view, column, grantee, role, privilege, grantable in grants:
        who = "PUBLIC" if grantee == 0 else _identifier(role)
        field = "" if column is None else " (" + _identifier(column) + ")"
        suffix = " WITH GRANT OPTION" if grantable else ""
        connection.execute(
            f"GRANT {privilege}{field} ON {target}.{_identifier(view)} TO {who}{suffix}"
        )
    for name, owner, options, comment in views:
        view = f"{target}.{_identifier(name)}"
        for option in options or ():
            key, value = option.split("=", 1)
            literal = sql.Literal(value).as_string()
            connection.execute(f"ALTER VIEW {view} SET ({_identifier(key)}={literal})")
        if comment is not None:
            connection.execute(
                f"COMMENT ON VIEW {view} IS {sql.Literal(comment).as_string()}"
            )
        connection.execute(f"ALTER VIEW {view} OWNER TO {_identifier(owner)}")
    return {
        "schema": schema,
        "revisions_preserved": count,
        "official_values_backfilled": 0,
        "available_at_backfilled": False,
        "legacy_columns_removed": list(LEGACY_COLUMNS),
        "view_grants_restored": len(grants),
        "schema_and_table_grants_unchanged": True,
        "metadata_rows_compacted": len(compacted),
        "metadata_cleanup": (
            "exact verified T20804 identities/constants compacted; "
            "all other metadata, descriptions and notes preserved"
        ),
        "committed": False,
    }
