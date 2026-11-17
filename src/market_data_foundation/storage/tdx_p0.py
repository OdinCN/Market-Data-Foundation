"DEV TDX storage composition over B1; local queries never construct a source."

import threading
import tomllib
from datetime import UTC, date, datetime

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_roster import PATTERNS
from market_data_foundation.storage.market_dataset_postgres import (
    connect_dev,
    preflight,
)
from market_data_foundation.storage.tdx_daily_pg import SQL_DIR, Counts, write_batch


def connect():
    with open("/srv/market-data-foundation/config/local/secret.toml", "rb") as stream:
        password = tomllib.load(stream)["postgres"]["password"]
    return connect_dev(password)


def migrate(conn, *, dev_opt_in=False):
    require(dev_opt_in is True, "tdx_dev_migration_opt_in_required")
    preflight(conn)
    with conn.transaction():
        conn.execute("SELECT pg_advisory_xact_lock(721603,1)")
        if not conn.execute("SELECT to_regnamespace('mdf_tdx_daily')").fetchone()[0]:
            conn.execute((SQL_DIR / "001_current.sql").read_text())
        owner = conn.execute(
            (
                "SELECT pg_get_userbyid(nspowner)=current_user FROM pg_namespace "
                "WHERE nspname='mdf_tdx_daily'"
            )
        ).fetchone()
        require(owner == (True,), "tdx_namespace_owner_mismatch")
        if not conn.execute(
            "SELECT to_regclass('mdf_tdx_daily.p0_schema_revision')"
        ).fetchone()[0]:
            conn.execute((SQL_DIR / "006_p0.sql").read_text())
        if not conn.execute(
            "SELECT to_regclass('mdf_tdx_daily.p0_batch_publications')"
        ).fetchone()[0]:
            conn.execute((SQL_DIR / "007_p0_publication.sql").read_text())
        if not conn.execute(
            "SELECT to_regclass('mdf_tdx_daily.p0_visible_keys_idx')"
        ).fetchone()[0]:
            conn.execute((SQL_DIR / "008_p0_read_index.sql").read_text())

        if not conn.execute(
            "SELECT to_regclass('mdf_tdx_daily.roster_name_changes')"
        ).fetchone()[0]:
            conn.execute((SQL_DIR / "009_name_changes.sql").read_text())


def visible_versions(scope, *, materialize_members=False):
    """Select the newest fact version whose batch was confirmed in PostgreSQL."""
    patterns = " OR ".join(
        f"(market={m} AND instrument ~ '^{pattern}$')"
        for m, pattern in PATTERNS.items()
    )
    # Selective queries retain the membership index; broad summaries evaluate
    # lifecycle dates once per member instead of once per historical bar.
    membership = "MATERIALIZED" if materialize_members else "NOT MATERIALIZED"
    return f"""WITH members AS {membership} (
        SELECT market,instrument,(evidence->>'listing_date')::date AS listing_date,
               (evidence->>'delist_date')::date AS delist_date
        FROM mdf_tdx_daily.p0_members WHERE snapshot_id=%(snapshot)s AND ({patterns})
    ), live AS NOT MATERIALIZED (
        SELECT market,instrument,trading_day,open,high,low,close,volume,amount,
            source_observed_at AS received_at,p.available_at,value_source AS source,
            b.batch_id
        FROM mdf_tdx_daily.bars_daily_current b
        JOIN mdf_tdx_daily.p0_batch_publications p
            ON (p.source,p.batch_id)=(b.value_source,b.batch_id)
        WHERE {scope} AND p.available_at <= %(cutoff)s
    ), archived AS (
        SELECT market,instrument,trading_day,
            old_values[1] AS open,old_values[2] AS high,old_values[3] AS low,
            old_values[4] AS close,old_values[5] AS volume,old_values[6] AS amount,
            old_source_observed_at AS received_at,p.available_at,
            old_source AS source,old_batch_id AS batch_id,
            old_value_changed_at AS value_order
        FROM mdf_tdx_daily.bars_daily_corrections b
        JOIN mdf_tdx_daily.p0_batch_publications p
            ON (p.source,p.batch_id)=(b.old_source,b.old_batch_id)
        WHERE {scope} AND p.available_at <= %(cutoff)s
        AND NOT EXISTS (SELECT 1 FROM mdf_tdx_daily.p0_batch_publications successor
            WHERE successor.source=b.source AND successor.batch_id=b.batch_id
              AND successor.available_at <= %(cutoff)s)
        AND NOT EXISTS (SELECT 1 FROM live l
            WHERE (l.market,l.instrument,l.trading_day)=
                  (b.market,b.instrument,b.trading_day))
    ), old_versions AS (
        SELECT DISTINCT ON (trading_day,market,instrument)
            market,instrument,trading_day,open,high,low,close,volume,amount,
            received_at,available_at,source,batch_id
        FROM archived
        ORDER BY trading_day,market,instrument,value_order DESC
    ), versions AS (SELECT * FROM live UNION ALL SELECT * FROM old_versions)"""


def member_scope(has_snapshot=True):
    patterns = " OR ".join(
        f"(b.market={m} AND b.instrument ~ '^{pattern}$')"
        for m, pattern in PATTERNS.items()
    )
    if not has_snapshot:
        return "(" + patterns + ")"
    return """EXISTS (
            SELECT 1 FROM members m
            WHERE m.market=b.market
              AND m.instrument=b.instrument
              AND (m.listing_date IS NULL OR b.trading_day >= m.listing_date)
              AND (m.delist_date IS NULL OR b.trading_day < m.delist_date))"""


def summary_statement():
    # Direct member joins keep this scan parallel-safe. Date bounds become
    # index parameters once per member, instead of JSON/date work per bar.
    # The sentinels follow bars_daily_current's enforced 1990..2100 domain.
    member_patterns = " OR ".join(
        f"(m.market={m} AND m.instrument ~ '^{pattern}$')"
        for m, pattern in PATTERNS.items()
    )
    return f"""WITH current_instruments AS MATERIALIZED (
 SELECT b.market,b.instrument,count(*) AS rows,min(b.trading_day) AS first,
        max(b.trading_day) AS last,min(p.available_at) AS first_available_at,
        max(b.source_observed_at) AS last_received_at
 FROM mdf_tdx_daily.bars_daily_current b
 JOIN mdf_tdx_daily.p0_batch_publications p
   ON (p.source,p.batch_id)=(b.value_source,b.batch_id) AND p.available_at<=%(cutoff)s
 JOIN mdf_tdx_daily.p0_members m
   ON m.market=b.market AND m.instrument=b.instrument AND m.snapshot_id=%(snapshot)s
 WHERE ({member_patterns})
   AND b.trading_day>=COALESCE((m.evidence->>'listing_date')::date,DATE '1990-01-01')
   AND b.trading_day<COALESCE((m.evidence->>'delist_date')::date,DATE '2101-01-01')
 GROUP BY b.market,b.instrument
), archived AS (
 SELECT DISTINCT ON (b.trading_day,b.market,b.instrument)
        b.market,b.instrument,b.trading_day,p.available_at,
        b.old_source_observed_at AS received_at
 FROM mdf_tdx_daily.bars_daily_corrections b
 JOIN mdf_tdx_daily.p0_batch_publications p
   ON (p.source,p.batch_id)=(b.old_source,b.old_batch_id) AND p.available_at<=%(cutoff)s
 JOIN mdf_tdx_daily.p0_members m
   ON m.market=b.market AND m.instrument=b.instrument AND m.snapshot_id=%(snapshot)s
 WHERE ({member_patterns})
   AND b.trading_day>=COALESCE((m.evidence->>'listing_date')::date,DATE '1990-01-01')
   AND b.trading_day<COALESCE((m.evidence->>'delist_date')::date,DATE '2101-01-01')
   AND NOT EXISTS (SELECT 1 FROM mdf_tdx_daily.p0_batch_publications successor
       WHERE successor.source=b.source AND successor.batch_id=b.batch_id
         AND successor.available_at<=%(cutoff)s)
   AND NOT EXISTS (SELECT 1 FROM mdf_tdx_daily.bars_daily_current c
       JOIN mdf_tdx_daily.p0_batch_publications cp
         ON (cp.source,cp.batch_id)=(c.value_source,c.batch_id)
       WHERE (c.market,c.instrument,c.trading_day)=(b.market,b.instrument,b.trading_day)
         AND cp.available_at<=%(cutoff)s)
 ORDER BY b.trading_day,b.market,b.instrument,b.old_value_changed_at DESC
), contributions AS (
 SELECT * FROM current_instruments UNION ALL
 SELECT market,instrument,count(*) AS rows,min(trading_day),max(trading_day),
        min(available_at),max(received_at) FROM archived GROUP BY market,instrument
), instruments AS (
 SELECT market,instrument,sum(rows) AS rows,min(first) AS first,max(last) AS last,
        min(first_available_at) AS first_available_at,
        max(last_received_at) AS last_received_at
 FROM contributions GROUP BY market,instrument
)
SELECT market,sum(rows)::bigint AS rows,count(*) AS instruments,min(first) AS first,
       max(last) AS last,min(first_available_at) AS first_available_at,
       max(last_received_at) AS last_received_at
FROM instruments GROUP BY market ORDER BY market
"""


class PgStore:
    def __init__(self, connector=connect):
        self.connector = connector
        self.writers = threading.local()
        self._summary_lock = threading.Lock()
        self._summary_cache = None

    def close_writer(self):
        connection = getattr(self.writers, "connection", None)
        if connection is not None:
            connection.close()
            self.writers.connection = None

    def write(self, batch):
        conn = getattr(self.writers, "connection", None)
        if conn is None or conn.closed:
            conn = self.writers.connection = self.connector()
        try:
            return self._write(conn, batch)
        except BaseException:
            # An uncertain transaction must be reconciled on a fresh connection.
            self.close_writer()
            raise

    def _write(self, conn, batch):
        result = write_batch(conn, batch, independent_instruments=True)
        # write_batch has committed. A retry completes publication using the
        # same identity and preserves any existing first-visible timestamp.
        conn.execute(
            "INSERT INTO mdf_tdx_daily.p0_batch_publications "
            "VALUES (%s,%s,statement_timestamp()) ON CONFLICT DO NOTHING",
            (batch.source, batch.batch_id),
        )
        if result.replay:
            row = conn.execute(
                "SELECT sum(new_count),sum(unchanged_count),"
                "sum(changed_count),sum(blocked_count) "
                "FROM mdf_tdx_daily.source_refresh_batches "
                "WHERE source=%s AND batch_id=%s",
                (batch.source, batch.batch_id),
            ).fetchone()
            return Counts(*map(int, row), replay=True)
        return result

    def save_universe(self, snapshot):
        with self.connector() as conn, conn.transaction():
            existing = conn.execute(
                "SELECT 1 FROM mdf_tdx_daily.p0_universes WHERE snapshot_id=%s",
                (snapshot["snapshot_id"],),
            ).fetchone()
            if existing:
                return
            conn.execute(
                "INSERT INTO mdf_tdx_daily.p0_universes VALUES (%s,%s,%s,%s)",
                (
                    snapshot["snapshot_id"],
                    snapshot["observed_at"],
                    snapshot["available_at"],
                    Jsonb(snapshot["details"]),
                ),
            )
            with conn.cursor().copy(
                (
                    "COPY mdf_tdx_daily.p0_members (snapshot_id,market,instrument,nam"
                    "e,evidence) FROM STDIN"
                )
            ) as copy:
                for member in snapshot["members"]:
                    copy.write_row(
                        (
                            snapshot["snapshot_id"],
                            member["market"],
                            member["instrument"],
                            member["name"],
                            Jsonb(member["evidence"]),
                        )
                    )
            # Each publication introduces a new snapshot_id. Until statistics
            # know it, PostgreSQL can estimate one member and perform millions
            # of publication-index probes for a full local summary.
            conn.execute("ANALYZE mdf_tdx_daily.p0_members")

    def name_changes(self, day, cutoff, market):
        with self.connector() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT market,instrument,effective_date,old_name,new_name,source_url,"
                "raw_sha256 FROM mdf_tdx_daily.roster_name_changes "
                "WHERE effective_date <= %s AND available_at <= %s "
                "AND (%s::smallint IS NULL OR market=%s) ORDER BY effective_date DESC",
                (day, cutoff, market, market),
            )
            changes = {}
            for row in cur:
                changes.setdefault(
                    (row.pop("market"), row.pop("instrument")), []
                ).append(row)
            return changes

    def observed_names(self, day, cutoff, market):
        with self.connector() as conn:
            rows = conn.execute(
                "SELECT m.market,m.instrument,n.name,max(u.observed_at) "
                "FROM mdf_tdx_daily.p0_members m JOIN mdf_tdx_daily.p0_universes u "
                "USING(snapshot_id) CROSS JOIN LATERAL "
                "(VALUES (m.name),(m.evidence->>'directory_name')) n(name) "
                "WHERE n.name IS NOT NULL AND u.available_at <= %s "
                "AND (u.observed_at AT TIME ZONE 'Asia/Shanghai')::date <= %s "
                "AND (%s::smallint IS NULL OR m.market=%s) GROUP BY 1,2,3",
                (cutoff, day, market, market),
            )
            names = {}
            for exchange, instrument, name, observed in rows:
                names.setdefault((exchange, instrument), []).append((name, observed))
            return names

    def strategy_calendar(self, *, as_of):
        # The scheduler needs dated calendar evidence, never the thousands of
        # membership rows or execution shards needed by an actual collection.
        with self.connector() as conn:
            row = conn.execute(
                "SELECT details->'calendar' FROM mdf_tdx_daily.p0_universes "
                "WHERE available_at <= %s AND "
                "(SELECT count(*)=3 AND bool_and(s->>'state'='COMPLETE') "
                "FROM jsonb_array_elements(details->'markets') s) "
                "ORDER BY (details->>'roster_kind'='exchange_lifecycle') "
                "DESC NULLS LAST, available_at DESC LIMIT 1",
                (as_of,),
            ).fetchone()
            return row[0] or [] if row else []

    def universe(self, snapshot_id=None, as_of=None, *, day=None):
        with self.connector() as conn, conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                (
                    "SELECT * FROM mdf_tdx_daily.p0_universes WHERE available_at <= %"
                    "s AND (%s::text IS NULL OR snapshot_id=%s) "
                    "AND (%s::text IS NOT NULL OR "
                    "(SELECT count(*)=3 AND bool_and(s->>'state'='COMPLETE') "
                    "FROM jsonb_array_elements(details->'markets') s)) "
                    "ORDER BY (details->>'valid_through'=%s::text) DESC NULLS LAST, "
                    " (details->>'roster_kind'='exchange_lifecycle') "
                    "DESC NULLS LAST, "
                    "available_at DESC LIMIT 1"
                ),
                (
                    as_of or datetime.now(UTC),
                    snapshot_id,
                    snapshot_id,
                    snapshot_id,
                    day.isoformat() if day else None,
                ),
            )
            snapshot = cur.fetchone()
            if snapshot is None:
                return None
            cur.execute(
                (
                    "SELECT market,instrument,name,evidence FROM mdf_tdx_daily.p0_mem"
                    "bers WHERE snapshot_id=%s ORDER BY market,instrument"
                ),
                (snapshot["snapshot_id"],),
            )
            snapshot["members"] = cur.fetchall()
            return snapshot

    def query(
        self,
        *,
        start,
        end,
        market=None,
        instrument=None,
        as_of=None,
        offset=0,
        limit=100,
        order="asc",
        snapshot_id=None,
    ):
        require(
            type(start) is date
            and type(end) is date
            and date(1990, 1, 1) <= start <= end <= date(2100, 12, 31),
            "tdx_query_range",
        )
        require(
            market is None or type(market) is int and market in (0, 1, 2),
            "tdx_query_market",
        )
        require(
            instrument is None
            or market is not None
            and len(instrument) == 6
            and instrument.isascii()
            and instrument.isdigit(),
            "tdx_query_instrument",
        )
        require(
            type(offset) is int
            and 0 <= offset
            and type(limit) is int
            and 1 <= limit <= 10000,
            "tdx_query_page",
        )
        require(order in {"asc", "desc"}, "tdx_query_order")
        ordering = "trading_day DESC" if order == "desc" else "trading_day ASC"
        cutoff = as_of or datetime.now(UTC)
        require(cutoff.utcoffset() is not None, "tdx_query_cutoff")
        params = dict(
            start=start,
            end=end,
            market=market,
            instrument=instrument,
            cutoff=cutoff,
            offset=offset,
            limit=limit,
        )
        scope = (
            "trading_day BETWEEN %(start)s AND %(end)s AND (%(market)s::small"
            "int IS NULL OR market=%(market)s) AND (%(instrument)s::text IS N"
            "ULL OR instrument=%(instrument)s)"
        )
        snapshot = self.universe(
            snapshot_id=snapshot_id, as_of=cutoff, day=start if start == end else None
        )
        params["snapshot"] = (snapshot or {}).get("snapshot_id")
        scope += " AND " + member_scope(snapshot is not None)
        cte = visible_versions(scope)
        with (
            self.connector() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute(cte + " SELECT count(*) AS total FROM versions", params)
            total = cur.fetchone()["total"]
            cur.execute(
                cte
                + (
                    f" SELECT * FROM versions ORDER BY {ordering}, market, instrument"
                    " OFFSET %(offset)s LIMIT %(limit)s"
                ),
                params,
            )
            rows = cur.fetchall()
        return dict(
            rows=rows,
            total=total,
            offset=offset,
            limit=limit,
            next_offset=offset + len(rows) if offset + len(rows) < total else None,
            as_of=cutoff,
            price_basis="native_projection",
            units=dict(price="0.001 CNY", amount="0.001 CNY", volume="hand"),
            source_event_time=None,
            provider_requests=0,
            local_only=True,
            return_qualification="NOT_QUALIFIED",
            snapshot_id=(snapshot or {}).get("snapshot_id"),
            membership=(snapshot or {})
            .get("details", {})
            .get("historical_membership", "UNKNOWN_CURRENT_ROSTER_SURVIVOR_BIAS"),
        )

    def has_data(self, as_of=None):
        cutoff = as_of or datetime.now(UTC)
        require(cutoff.utcoffset() is not None, "tdx_query_cutoff")
        snapshot = self.universe(as_of=cutoff)
        with self.connector() as conn, conn.transaction():
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            return conn.execute(
                visible_versions(member_scope(snapshot is not None))
                + " SELECT EXISTS(SELECT 1 FROM versions)",
                {"cutoff": cutoff, "snapshot": (snapshot or {}).get("snapshot_id")},
            ).fetchone()[0]

    def summary(self, as_of=None):
        if as_of is not None:
            return self._summary(as_of)
        # Coalesce expensive default summaries in this existing request host.
        # Append-only publications and immutable roster identity invalidate the
        # result even when another process writes. Retain its actual as_of clock.
        with self._summary_lock:
            cutoff = datetime.now(UTC)
            snapshot = self.universe(as_of=cutoff)
            with self.connector() as conn:
                publications = conn.execute(
                    "SELECT count(*),max(available_at) "
                    "FROM mdf_tdx_daily.p0_batch_publications WHERE available_at<=%s",
                    (cutoff,),
                ).fetchone()
            key = (snapshot or {}).get("snapshot_id"), *publications
            if self._summary_cache and self._summary_cache[0] == key:
                return self._summary_cache[1]
            value = self._summary(cutoff, snapshot=snapshot)
            self._summary_cache = key, value
            return value

    def _summary(self, as_of, *, snapshot=None):
        cutoff = as_of or datetime.now(UTC)
        require(cutoff.utcoffset() is not None, "tdx_query_cutoff")
        snapshot = snapshot or self.universe(as_of=cutoff)
        with (
            self.connector() as conn,
            conn.transaction(),
            conn.cursor(row_factory=dict_row) as cur,
        ):
            cur.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
            cur.execute("SET LOCAL statement_timeout = '30s'")
            cur.execute("SET LOCAL jit = off")
            cur.execute(
                (
                    summary_statement()
                    if snapshot is not None
                    else visible_versions(member_scope(False), materialize_members=True)
                    + """
                , instruments AS (
                SELECT market,instrument,count(*) AS rows,
                       min(trading_day) AS first,max(trading_day) AS last,
                       min(available_at) AS first_available_at,
                       max(received_at) AS last_received_at
                FROM versions GROUP BY market,instrument)
                SELECT market,sum(rows)::bigint AS rows,count(*) AS instruments,
                       min(first) AS first,max(last) AS last,
                       min(first_available_at) AS first_available_at,
                       max(last_received_at) AS last_received_at
                FROM instruments GROUP BY market ORDER BY market"""
                ),
                {"cutoff": cutoff, "snapshot": (snapshot or {}).get("snapshot_id")},
            )
            return dict(
                markets=cur.fetchall(),
                as_of=cutoff,
                provider_requests=0,
                price_basis="native_projection",
                snapshot_id=(snapshot or {}).get("snapshot_id"),
                historical_membership=(snapshot or {})
                .get("details", {})
                .get("historical_membership", "UNKNOWN_CURRENT_ROSTER_SURVIVOR_BIAS"),
                suspension_evidence=(snapshot or {})
                .get("details", {})
                .get("suspension_status", "NOT_BOUND"),
            )
