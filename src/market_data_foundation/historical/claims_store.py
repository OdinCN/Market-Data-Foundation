"""Explicit Claims schema, changed-value revisions and short read-only snapshots."""

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from threading import RLock

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.claims_contract import (
    SERIES_IDS,
    ClaimsError,
    day,
    instant,
    validate_series,
    weeks,
)
from market_data_foundation.historical.claims_source import (
    ClaimsBatch,
    parse_observations,
)
from market_data_foundation.historical.claims_timeseries import ClaimsObservation
from market_data_foundation.historical.cpi_timeseries_store import (
    batch_identity,
    digest,
)
from market_data_foundation.historical.verified_garage_raw import VerifiedGarageRawStore

SCHEMA = "mdf_claims_timeseries_dev_r1"
RAW_PREFIX = "macro/claims-timeseries/r1/"
LOCK_ID = 7312026110202


def storage_boundary(classification):
    def decorate(method):
        @wraps(method)
        def run(*args, **kwargs):
            try:
                return method(*args, **kwargs)
            except psycopg.Error:
                raise ClaimsError(classification) from None

        return run

    return decorate


class GarageClaimsRawStore(VerifiedGarageRawStore):
    def __init__(self, credentials, **options):
        super().__init__(
            credentials,
            raw_prefix=RAW_PREFIX,
            error_factory=ClaimsError,
            error_prefix="claims",
            **options,
        )


def schema_name(value):
    if type(value) is not str or not (
        value == SCHEMA or re.fullmatch(r"test_claims_[a-z0-9_]{1,40}", value)
    ):
        raise ClaimsError("claims_schema_invalid")
    return value


def validate_batch(batch):
    try:
        if not isinstance(batch, ClaimsBatch):
            raise ValueError
        instant(batch.observed_at)
        if type(batch.attempts) is not int or not 1 <= batch.attempts <= 3:
            raise ValueError
        request = batch.request
        if (
            set(request) != {"url", "parameters", "request_count"}
            or request["request_count"] != 1
        ):
            raise ValueError
        if request["url"] != "https://api.stlouisfed.org/fred/series/observations":
            raise ValueError
        p = request["parameters"]
        if set(p) != {
            "series_id",
            "file_type",
            "units",
            "observation_start",
            "observation_end",
            "output_type",
            "sort_order",
            "limit",
        }:
            raise ValueError
        if any(
            p[k] != v
            for k, v in {
                "file_type": "json",
                "units": "lin",
                "output_type": "1",
                "sort_order": "asc",
                "limit": "100000",
            }.items()
        ):
            raise ValueError
        parsed = parse_observations(
            batch.raw, p["series_id"], p["observation_start"], p["observation_end"]
        )
        if parsed != batch.points:
            raise ValueError
    except ClaimsError, ValueError, TypeError, KeyError, AttributeError:
        raise ClaimsError("claims_batch_invalid") from None


def migration_sql(schema=SCHEMA):
    s = schema_name(schema)
    series = ",".join(f"'{sid}'" for sid in SERIES_IDS)
    return (
        f"CREATE SCHEMA IF NOT EXISTS {s}",
        f"""CREATE TABLE IF NOT EXISTS {s}.batches (
            batch_id text PRIMARY KEY, raw_sha256 text NOT NULL,
            raw_locator text NOT NULL, observed_at timestamptz NOT NULL,
            request jsonb NOT NULL, provider_attempts integer NOT NULL
                CHECK(provider_attempts BETWEEN 1 AND 3))""",
        f"""CREATE TABLE IF NOT EXISTS {s}.revisions (
            series_id text NOT NULL CHECK(series_id IN ({series})),
            week_ending date NOT NULL CHECK(extract(isodow FROM week_ending)=6),
            revision integer NOT NULL CHECK(revision>0),
            observed_at timestamptz NOT NULL,
            available_at timestamptz CHECK(available_at>=observed_at),
            batch_id text NOT NULL REFERENCES {s}.batches(batch_id),
            value numeric CHECK(value>=0), provider_extra jsonb NOT NULL,
            PRIMARY KEY(series_id,week_ending,revision))""",
        f"""CREATE OR REPLACE VIEW {s}.latest AS
            SELECT * FROM (
            SELECT DISTINCT ON(series_id,week_ending) r.* FROM {s}.revisions r
            WHERE available_at IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM {s}.revisions u
                WHERE u.series_id=r.series_id AND u.week_ending=r.week_ending
                AND u.revision>r.revision AND u.available_at IS NULL)
            ORDER BY series_id,week_ending,revision DESC) selected
            ORDER BY week_ending DESC,array_position(ARRAY[{series}],series_id)""",
        f"""CREATE OR REPLACE VIEW {s}.claims_weekly_changes AS
            SELECT c.*, c.value-p.value AS weekly_change,
            c.value/NULLIF(p.value,0)-1 AS weekly_change_ratio,
            p.revision AS previous_revision, p.available_at AS previous_available_at
            FROM {s}.latest c LEFT JOIN {s}.latest p
            ON p.series_id=c.series_id AND p.week_ending=c.week_ending-7
            ORDER BY c.week_ending DESC,
                array_position(ARRAY[{series}],c.series_id)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.release_events (
            event_id text PRIMARY KEY, source_date date NOT NULL)""",
        f"""CREATE TABLE IF NOT EXISTS {s}.release_evidence (
            evidence_id text PRIMARY KEY,
            event_id text NOT NULL REFERENCES {s}.release_events(event_id),
            raw_sha256 text NOT NULL, raw_locator text NOT NULL,
            obtained_at timestamptz NOT NULL,
            available_at timestamptz CHECK(available_at>=obtained_at),
            document jsonb NOT NULL)""",
    )


class PostgresClaimsStore:
    def __init__(
        self, connection, raw_store, *, schema=SCHEMA, clock=lambda: datetime.now(UTC)
    ):
        if not connection.autocommit:
            raise ClaimsError("claims_autocommit_required")
        self.connection, self.raw_store, self.clock = connection, raw_store, clock
        self.schema, self._lock = schema_name(schema), RLock()

    def _top_level(self):
        if self.connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise ClaimsError("claims_top_level_transaction_required")

    @contextmanager
    def operation(self):
        with self._lock:
            self._top_level()
            acquired = self.connection.execute(
                "SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)
            ).fetchone()[0]
            if not acquired:
                raise ClaimsError("claims_busy")
            try:
                yield
            finally:
                try:
                    if not self.connection.execute(
                        "SELECT pg_advisory_unlock(%s)", (LOCK_ID,)
                    ).fetchone()[0]:
                        raise ValueError
                except Exception:
                    self.connection.close()
                    raise ClaimsError("claims_lock_release_failed") from None

    @contextmanager
    def read_snapshot(self):
        try:
            with self._lock:
                self._top_level()
                with self.connection.transaction():
                    self.connection.execute(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    yield
        except psycopg.Error:
            raise ClaimsError("claims_snapshot_failed") from None

    @storage_boundary("claims_schema_failed")
    def ensure_schema(self):
        s = self.schema
        with self.operation(), self.connection.transaction():
            self.connection.execute(migration_sql(s)[0])
            if self.connection.execute(
                "SELECT pg_get_userbyid(nspowner)=current_user "
                "FROM pg_namespace WHERE nspname=%s",
                (s,),
            ).fetchone() != (True,):
                raise ClaimsError("claims_schema_owner_mismatch")
            for sql in migration_sql(s)[1:]:
                self.connection.execute(sql)
            columns = dict(
                self.connection.execute(
                    "SELECT column_name,data_type FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='revisions'",
                    (s,),
                ).fetchall()
            )
            if columns != {
                "series_id": "text",
                "week_ending": "date",
                "revision": "integer",
                "observed_at": "timestamp with time zone",
                "available_at": "timestamp with time zone",
                "batch_id": "text",
                "value": "numeric",
                "provider_extra": "jsonb",
            }:
                raise ClaimsError("claims_schema_shape_mismatch")
            if self.connection.execute(
                "SELECT bool_and(pg_get_userbyid(c.relowner)=current_user) "
                "FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=%s AND c.relkind IN ('r','v')",
                (s,),
            ).fetchone() != (True,):
                raise ClaimsError("claims_object_owner_mismatch")
            if self.connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname='odin_dba'"
            ).fetchone() != (1,):
                raise ClaimsError("claims_readonly_role_missing")
            for sql in (
                f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC, odin_dba",
                f"GRANT USAGE ON SCHEMA {s} TO odin_dba",
                f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM PUBLIC, odin_dba",
                f"GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO odin_dba",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} "
                "REVOKE ALL ON TABLES FROM PUBLIC, odin_dba",
                f"ALTER DEFAULT PRIVILEGES IN SCHEMA {s} "
                "GRANT SELECT ON TABLES TO odin_dba",
            ):
                self.connection.execute(sql)

    @storage_boundary("claims_storage_read_failed")
    def read_range(self, series, start, end, cutoff=None, basis="available"):
        validate_series(series)
        weeks(start, end)
        if basis not in {"observed", "available"}:
            raise ClaimsError("claims_cutoff_basis_unsupported")
        if cutoff is not None:
            instant(cutoff)
        try:
            rows = self.connection.execute(
                f"SELECT series_id,week_ending,value,observed_at,available_at,revision,"
                f"provider_extra,batch_id FROM {self.schema}.revisions r "
                "WHERE series_id=ANY(%s) AND week_ending BETWEEN %s AND %s "
                "AND available_at IS NOT NULL "
                f"AND NOT EXISTS (SELECT 1 FROM {self.schema}.revisions u "
                "WHERE u.series_id=r.series_id AND u.week_ending=r.week_ending "
                "AND u.revision>r.revision AND u.available_at IS NULL "
                "AND (%s::timestamptz IS NULL OR u.observed_at<=%s::timestamptz)) "
                f"AND (%s::timestamptz IS NULL OR {basis}_at<=%s::timestamptz) "
                "ORDER BY week_ending DESC,series_id,revision",
                (list(series), day(start), day(end), cutoff, cutoff, cutoff, cutoff),
            ).fetchall()
        except psycopg.errors.UndefinedTable:
            raise ClaimsError("claims_schema_not_prepared") from None
        return [
            ClaimsObservation(
                sid,
                week.isoformat(),
                value,
                observed,
                available,
                revision,
                extra,
                batch_id=bid,
            )
            for sid, week, value, observed, available, revision, extra, bid in rows
        ]

    @storage_boundary("claims_commit_or_confirmation_outcome_unknown")
    def save_batches(self, batches, *, missing_only=False):
        batches = tuple(batches)
        if type(missing_only) is not bool:
            raise ClaimsError("claims_missing_only_invalid")
        # Validate the entire response set before retaining or admitting any rows.
        for b in batches:
            validate_batch(b)
            if b.observed_at > instant(self.clock()):
                raise ClaimsError("claims_future_observation_rejected")
        retained = [(b, self.raw_store.put_verified(b.raw)) for b in batches]
        inserted = []
        with self.operation():
            with self.connection.transaction():
                for b, locator in sorted(
                    retained, key=lambda pair: pair[0].observed_at
                ):
                    bid = batch_identity(b)
                    self.connection.execute(
                        f"INSERT INTO {self.schema}.batches VALUES (%s,%s,%s,%s,%s,%s) "
                        "ON CONFLICT DO NOTHING",
                        (
                            bid,
                            digest(b.raw),
                            locator,
                            b.observed_at,
                            Jsonb(b.request),
                            b.attempts,
                        ),
                    )
                    for p in b.points:
                        old = self.connection.execute(
                            "SELECT revision,observed_at,value "
                            f"FROM {self.schema}.revisions "
                            "WHERE series_id=%s AND week_ending=%s "
                            "ORDER BY revision DESC LIMIT 1",
                            (p.series_id, day(p.week_ending)),
                        ).fetchone()
                        if old and (missing_only or old[2] == p.value):
                            continue
                        if old and b.observed_at <= old[1]:
                            raise ClaimsError("claims_observation_order_conflict")
                        revision = old[0] + 1 if old else 1
                        self.connection.execute(
                            f"INSERT INTO {self.schema}.revisions "
                            "(series_id,week_ending,revision,observed_at,"
                            "batch_id,value,provider_extra) "
                            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                            (
                                p.series_id,
                                day(p.week_ending),
                                revision,
                                b.observed_at,
                                bid,
                                p.value,
                                Jsonb(p.provider_extra),
                            ),
                        )
                        inserted.append(revision)
            # First commit has returned. Confirmation is a separate committed write.
            # Interrupted confirmation stays invisible until explicit replay.
            with self.connection.transaction():
                # A replay may have a different raw package or capture time. The
                # committed pending rows are confirmed at replay time, never
                # backdated to the original capture or source realtime date.
                for b in batches:
                    for p in b.points:
                        self.connection.execute(
                            f"UPDATE {self.schema}.revisions "
                            "SET available_at=clock_timestamp() "
                            "WHERE available_at IS NULL AND series_id=%s "
                            "AND week_ending=%s AND value IS NOT DISTINCT FROM %s "
                            "AND observed_at<=%s",
                            (p.series_id, day(p.week_ending), p.value, b.observed_at),
                        )
        return {
            "saved_points": len(inserted),
            "saved_revisions": sum(r > 1 for r in inserted),
        }

    @storage_boundary("claims_storage_read_failed")
    def status(self):
        with self.read_snapshot():
            counts = self.connection.execute(
                "SELECT count(*),count(*)-count(DISTINCT(series_id,week_ending)) "
                f"FROM {self.schema}.revisions"
            ).fetchone()
            batch_count = self.connection.execute(
                f"SELECT count(*) FROM {self.schema}.batches"
            ).fetchone()[0]
            pending = self.connection.execute(
                f"SELECT count(*) FROM {self.schema}.revisions "
                "WHERE available_at IS NULL"
            ).fetchone()[0]
            latest = dict(
                self.connection.execute(
                    f"SELECT series_id,max(week_ending) FROM {self.schema}.latest "
                    "WHERE value IS NOT NULL GROUP BY series_id"
                ).fetchall()
            )
            return {
                "schema": self.schema,
                "observation_count": counts[0],
                "revision_count": counts[1],
                "batch_count": batch_count,
                "pending_observation_count": pending,
                "latest_weeks": {
                    sid: latest[sid].isoformat() if sid in latest else None
                    for sid in SERIES_IDS
                },
            }

    def close(self):
        self.connection.close()
