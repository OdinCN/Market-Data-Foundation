"""GDP quarterly revisions with verified raw retention and strict local cutoffs."""

from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from threading import RLock

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.bls_cpi_material_store import BUCKET
from market_data_foundation.historical.cpi_timeseries_store import (
    batch_identity,
    digest,
)
from market_data_foundation.historical.gdp_contract import (
    METRICS,
    SERIES_IDS,
    TABLES,
    GdpSourceError,
    quarter_start,
    validate_range,
    validate_series,
)
from market_data_foundation.historical.gdp_source import GdpSourceBatch, parse_response
from market_data_foundation.historical.pce_timeseries_source import _decode
from market_data_foundation.historical.verified_garage_raw import VerifiedGarageRawStore

SCHEMA = "mdf_gdp_timeseries_dev_r1"
RAW_PREFIX = "macro/gdp-timeseries/r1/"
LOCK_ID = 7312026110201
_COLUMNS = (
    "series_id,quarter,revision,observed_at,batch_id,value,provider_extra,available_at"
)


class GdpStorageError(RuntimeError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


class GarageGdpRawStore(VerifiedGarageRawStore):
    def __init__(self, credentials, *, transport=None, clock=None):
        super().__init__(
            credentials,
            raw_prefix=RAW_PREFIX,
            error_factory=GdpStorageError,
            error_prefix="gdp_timeseries",
            transport=transport,
            clock=clock,
        )


def instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise GdpStorageError("gdp_timeseries_aware_time_required")
    return value.astimezone(UTC)


def schema_name(value):
    if type(value) is not str or not (
        value == SCHEMA or re.fullmatch(r"test_gdp_[a-z0-9_]{1,40}", value)
    ):
        raise GdpStorageError("gdp_timeseries_schema_invalid")
    return value


def raw_locator(sha):
    if type(sha) is not str or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise GdpStorageError("gdp_timeseries_digest_invalid")
    return f"mdf-s3://{BUCKET}/{RAW_PREFIX}{sha}/raw-response.bin"


def quarter_label(value):
    if (
        not isinstance(value, date)
        or value.day != 1
        or value.month not in (1, 4, 7, 10)
    ):
        raise GdpStorageError("gdp_timeseries_quarter_key_invalid")
    return f"{value.year:04d}-Q{(value.month - 1) // 3 + 1}"


def validate_batch(batch):
    try:
        if not isinstance(batch, GdpSourceBatch):
            raise ValueError
        instant(batch.observed_at)
        if type(batch.attempts) is not int or not 1 <= batch.attempts <= 3:
            raise ValueError
        request = batch.request
        fixed = {
            "method": "GetData",
            "DataSetName": "NIPA",
            "Frequency": "Q",
            "ResultFormat": "JSON",
            "provider": "BEA",
            "request_count": 1,
            "retention_class": "credential_free_bea_results_projection",
            "exact_original_retained": False,
        }
        if type(request) is not dict or set(request) != set(fixed) | {
            "TableName",
            "Year",
            "seriesid",
            "start_quarter",
            "end_quarter",
        }:
            raise ValueError
        if any(
            type(request[k]) is not type(v) or request[k] != v for k, v in fixed.items()
        ):
            raise ValueError
        table, start, end = (
            request["TableName"],
            request["start_quarter"],
            request["end_quarter"],
        )
        if table not in TABLES or type(request["seriesid"]) is not list:
            raise ValueError
        series = validate_series(tuple(request["seriesid"]))
        validate_range(start, end)
        if request["Year"] != ",".join(
            map(str, range(int(start[:4]), int(end[:4]) + 1))
        ):
            raise ValueError
        if any(
            not any(m.series_id == s and m.table == table for m in METRICS)
            for s in series
        ):
            raise ValueError
        retained = _decode(batch.raw)
        if set(retained) != {"retention_class", "exact_original_retained", "Results"}:
            raise ValueError
        if (
            retained["retention_class"] != fixed["retention_class"]
            or retained["exact_original_retained"] is not False
        ):
            raise ValueError
        parsed = parse_response(
            json.dumps({"BEAAPI": {"Results": retained["Results"]}}).encode(),
            series,
            start,
            end,
            table=table,
        )
        if parsed != batch.points:
            raise ValueError
    except ValueError, TypeError, KeyError, GdpSourceError, GdpStorageError:
        raise GdpStorageError("gdp_timeseries_batch_invalid") from None


def _document(point, batch, revision):
    return {
        "series_id": point.series_id,
        "quarter": point.quarter,
        "revision": revision,
        "observed_at": instant(batch.observed_at).isoformat(),
        "batch_id": batch_identity(batch),
        "value": None if point.value is None else str(point.value),
        "provider_extra": copy.deepcopy(point.provider_extra),
        "available_at": None,
        "raw_sha256": digest(batch.raw),
        "raw_locator": raw_locator(digest(batch.raw)),
    }


def _row_document(row):
    sid, quarter, rev, observed, bid, value, extra, available, raw_sha = row
    return {
        "series_id": sid,
        "quarter": quarter_label(quarter),
        "revision": rev,
        "observed_at": instant(observed).isoformat(),
        "batch_id": bid,
        "value": None if value is None else str(value),
        "provider_extra": extra,
        "available_at": None if available is None else instant(available).isoformat(),
        "raw_sha256": raw_sha,
        "raw_locator": raw_locator(raw_sha),
    }


def _same_value(old, point):
    return (None if old["value"] is None else Decimal(old["value"])) == point.value


def _read_args(series, start, end, as_of, basis):
    validate_series(series)
    validate_range(start, end)
    if basis not in ("observed", "available"):
        raise GdpStorageError("gdp_timeseries_cutoff_basis_invalid")
    return None if as_of is None else instant(as_of)


def _verify_raw(raw_store, batches):
    if raw_store is None:
        raise GdpStorageError("gdp_timeseries_raw_store_required")
    for batch in batches:
        validate_batch(batch)
    for batch in batches:
        if raw_store.put_verified(batch.raw) != raw_locator(digest(batch.raw)):
            raise GdpStorageError("gdp_timeseries_raw_locator_mismatch")


def _confirmed_at(clock, inserted):
    at = instant(clock())
    if any(at < datetime.fromisoformat(d["observed_at"]) for d in inserted):
        raise GdpStorageError("gdp_timeseries_confirmation_clock_invalid")
    return at


def migration_sql(schema=SCHEMA):
    schema = schema_name(schema)
    identities = ",".join(
        f"('{m.series_id}','{m.value_unit}','{m.statistic}','{m.chained_base or ''}')"
        for m in METRICS
    )
    series = ",".join(f"'{s}'" for s in SERIES_IDS)
    return (
        f"CREATE SCHEMA IF NOT EXISTS {schema}",
        f"""CREATE TABLE IF NOT EXISTS {schema}.batches (
            batch_id text PRIMARY KEY, raw_sha256 text NOT NULL,
            observed_at timestamptz NOT NULL, request jsonb NOT NULL,
            provider_attempts integer NOT NULL CHECK(provider_attempts BETWEEN 1 AND 3)
        )""",
        f"""CREATE TABLE IF NOT EXISTS {schema}.revisions (
            series_id text NOT NULL CHECK(series_id IN ({series})),
            quarter date NOT NULL CHECK(
                extract(day FROM quarter)=1
                AND extract(month FROM quarter) IN (1,4,7,10)),
            revision integer NOT NULL CHECK(revision>0),
            observed_at timestamptz NOT NULL,
            batch_id text NOT NULL REFERENCES {schema}.batches(batch_id),
            value numeric, provider_extra jsonb NOT NULL,
            available_at timestamptz CHECK(available_at>=observed_at),
            PRIMARY KEY(series_id,quarter,revision)
        )""",
        f"""CREATE OR REPLACE VIEW {schema}.latest AS
            SELECT r.*,m.value_unit,m.statistic,
                nullif(m.chained_base,'') AS chained_base
            FROM (SELECT DISTINCT ON(series_id,quarter) {_COLUMNS}
                FROM {schema}.revisions ORDER BY series_id,quarter,revision DESC) r
            JOIN (VALUES {identities}) m(series_id,value_unit,statistic,chained_base)
            USING(series_id)""",
        f"""CREATE OR REPLACE VIEW {schema}.gdp_timeseries_rates AS
            SELECT c.*,to_char(c.quarter,'YYYY-"Q"Q') AS quarter_label,
            CASE WHEN c.statistic='SAAR_level'
                AND c.available_at IS NOT NULL THEN
                round(c.value/nullif(p1.value,0)-1,4) END AS change_1q_ratio,
            CASE WHEN c.statistic='SAAR_level'
                AND c.available_at IS NOT NULL THEN
                round(c.value/nullif(p4.value,0)-1,4) END AS change_4q_ratio
            FROM {schema}.latest c
            LEFT JOIN {schema}.latest p1 ON p1.series_id=c.series_id
                AND p1.quarter=(c.quarter-interval '3 months')::date
                AND p1.available_at IS NOT NULL
            LEFT JOIN {schema}.latest p4 ON p4.series_id=c.series_id
                AND p4.quarter=(c.quarter-interval '12 months')::date
                AND p4.available_at IS NOT NULL""",
    )


class InMemoryGdpStore:
    """Test port; production factory uses PostgreSQL and verified Garage."""

    def __init__(self, raw_store, *, clock=lambda: datetime.now(UTC)):
        self.raw_store, self.clock = raw_store, clock
        self.history, self.batches = {}, {}
        self._lock = RLock()

    @contextmanager
    def operation(self):
        with self._lock:
            yield

    def read_range(self, series, start, end, as_of=None, basis="observed"):
        as_of = _read_args(series, start, end, as_of, basis)
        with self.operation():
            result = []
            for (sid, q), history in sorted(self.history.items()):
                if sid not in series or not start <= q <= end:
                    continue
                candidates = [
                    d
                    for d in history
                    if (basis != "available" or d["available_at"] is not None)
                    and (
                        as_of is None
                        or datetime.fromisoformat(
                            d["available_at" if basis == "available" else "observed_at"]
                        )
                        <= as_of
                    )
                ]
                if not candidates:
                    continue
                selected = candidates[-1]
                if basis == "available" and any(
                    d["revision"] > selected["revision"]
                    and d["available_at"] is None
                    and (
                        as_of is None
                        or datetime.fromisoformat(d["observed_at"]) <= as_of
                    )
                    for d in history
                ):
                    continue
                result.append(copy.deepcopy(selected))
            return result

    def save_batches(self, batches, *, missing_only=False):
        if type(missing_only) is not bool:
            raise GdpStorageError("gdp_timeseries_missing_only_invalid")
        batches = tuple(batches)
        _verify_raw(self.raw_store, batches)
        with self.operation():
            history, inserted = copy.deepcopy(self.history), []
            for batch in sorted(batches, key=lambda b: b.observed_at):
                for point in batch.points:
                    rows = history.setdefault((point.series_id, point.quarter), [])
                    if rows and (missing_only or _same_value(rows[-1], point)):
                        continue
                    if rows and instant(batch.observed_at) <= datetime.fromisoformat(
                        rows[-1]["observed_at"]
                    ):
                        raise GdpStorageError(
                            "gdp_timeseries_observation_order_conflict"
                        )
                    doc = _document(point, batch, len(rows) + 1)
                    rows.append(doc)
                    inserted.append(doc)
            self.history = history
            self.batches.update({batch_identity(b): b for b in batches})
            if inserted:
                try:
                    at = _confirmed_at(self.clock, inserted).isoformat()
                    for doc in inserted:
                        doc["available_at"] = at
                except Exception:
                    raise GdpStorageError(
                        "gdp_timeseries_confirmation_outcome_unknown"
                    ) from None
            return {
                "saved_points": len(inserted),
                "saved_revisions": sum(d["revision"] > 1 for d in inserted),
            }

    def confirm_current_availability(self, series, start, end):
        _read_args(series, start, end, None, "observed")
        with self.operation():
            rows = [
                history[-1]
                for (sid, q), history in self.history.items()
                if sid in series
                and start <= q <= end
                and history[-1]["available_at"] is None
            ]
            at = _confirmed_at(self.clock, rows) if rows else None
            for doc in rows:
                doc["available_at"] = at.isoformat()
            return {
                "confirmed_points": len(rows),
                "confirmed_at": None if at is None else at.isoformat(),
            }

    def status(self):
        with self.operation():
            latest = {s: None for s in SERIES_IDS}
            for (s, q), history in self.history.items():
                if history[-1]["value"] is not None and (
                    latest[s] is None or latest[s] < q
                ):
                    latest[s] = q
            return {
                "schema": SCHEMA,
                "latest_quarters": latest,
                "observation_count": sum(map(len, self.history.values())),
                "revision_count": sum(len(h) - 1 for h in self.history.values()),
                "batch_count": len(self.batches),
            }

    def close(self):
        pass


class PostgresGdpStore:
    def __init__(
        self, connection, raw_store, *, schema=SCHEMA, clock=lambda: datetime.now(UTC)
    ):
        if not connection.autocommit:
            raise GdpStorageError("gdp_timeseries_autocommit_required")
        self.connection, self.raw_store, self.clock = connection, raw_store, clock
        self.schema = schema_name(schema)
        self._lock = RLock()

    def _top_level(self):
        if self.connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise GdpStorageError("gdp_timeseries_top_level_transaction_required")

    @contextmanager
    def operation(self):
        with self._lock:
            acquired = False
            failed = False
            try:
                acquired = self.connection.execute(
                    "SELECT pg_try_advisory_lock(%s)", (LOCK_ID,)
                ).fetchone()[0]
            except Exception:
                raise GdpStorageError("gdp_timeseries_storage_failed") from None
            if not acquired:
                raise GdpStorageError("gdp_timeseries_busy")
            try:
                yield
            except BaseException:
                failed = True
                raise
            finally:
                try:
                    if not self.connection.execute(
                        "SELECT pg_advisory_unlock(%s)", (LOCK_ID,)
                    ).fetchone()[0]:
                        raise ValueError
                except Exception:
                    self.connection.close()
                    if not failed:
                        raise GdpStorageError(
                            "gdp_timeseries_lock_release_failed"
                        ) from None

    def ensure_schema(self):
        self._top_level()
        try:
            with self.operation(), self.connection.transaction():
                commands = migration_sql(self.schema)
                self.connection.execute(commands[0])
                owner = self.connection.execute(
                    "SELECT pg_get_userbyid(nspowner)=current_user "
                    "FROM pg_namespace WHERE nspname=%s",
                    (self.schema,),
                ).fetchone()
                if owner != (True,):
                    raise GdpStorageError("gdp_timeseries_schema_owner_mismatch")
                for command in commands[1:]:
                    self.connection.execute(command)
                columns = self.connection.execute(
                    "SELECT column_name,data_type FROM information_schema.columns "
                    "WHERE table_schema=%s AND table_name='revisions'",
                    (self.schema,),
                ).fetchall()
                if dict(columns) != {
                    "series_id": "text",
                    "quarter": "date",
                    "revision": "integer",
                    "observed_at": "timestamp with time zone",
                    "batch_id": "text",
                    "value": "numeric",
                    "provider_extra": "jsonb",
                    "available_at": "timestamp with time zone",
                }:
                    raise GdpStorageError("gdp_timeseries_schema_shape_mismatch")
        except GdpStorageError:
            raise
        except Exception:
            raise GdpStorageError("gdp_timeseries_schema_failed") from None

    def read_range(self, series, start, end, as_of=None, basis="observed"):
        as_of = _read_args(series, start, end, as_of, basis)
        params = [list(series), quarter_start(start), quarter_start(end)]
        conditions = ""
        if basis == "available":
            conditions += " AND r.available_at IS NOT NULL"
            conditions += f""" AND NOT EXISTS (
                SELECT 1 FROM {self.schema}.revisions u
                WHERE u.series_id=r.series_id AND u.quarter=r.quarter
                AND u.revision>r.revision AND u.available_at IS NULL"""
            if as_of is not None:
                conditions += " AND u.observed_at<=%s"
                params.append(as_of)
            conditions += ")"
        if as_of is not None:
            conditions += (
                " AND r."
                + ("available_at" if basis == "available" else "observed_at")
                + "<=%s"
            )
            params.append(as_of)
        fields = ",".join("r." + name for name in _COLUMNS.split(","))
        try:
            rows = self.connection.execute(
                f"SELECT DISTINCT ON(r.series_id,r.quarter) {fields},b.raw_sha256 "
                f"FROM {self.schema}.revisions r "
                f"JOIN {self.schema}.batches b USING(batch_id) "
                "WHERE r.series_id=ANY(%s) AND r.quarter BETWEEN %s AND %s"
                + conditions
                + " ORDER BY r.series_id,r.quarter,r.revision DESC",
                params,
            ).fetchall()
            return [_row_document(row) for row in rows]
        except Exception:
            raise GdpStorageError("gdp_timeseries_storage_read_failed") from None

    def save_batches(self, batches, *, missing_only=False):
        self._top_level()
        if type(missing_only) is not bool:
            raise GdpStorageError("gdp_timeseries_missing_only_invalid")
        batches = tuple(batches)
        _verify_raw(self.raw_store, batches)
        inserted = []
        with self.operation():
            try:
                with self.connection.transaction():
                    for batch in sorted(batches, key=lambda b: b.observed_at):
                        bid = batch_identity(batch)
                        self.connection.execute(
                            f"INSERT INTO {self.schema}.batches "
                            "(batch_id,raw_sha256,observed_at,"
                            "request,provider_attempts) "
                            "VALUES(%s,%s,%s,%s,%s) ON CONFLICT(batch_id) DO NOTHING",
                            (
                                bid,
                                digest(batch.raw),
                                batch.observed_at,
                                Jsonb(batch.request),
                                batch.attempts,
                            ),
                        )
                        for point in batch.points:
                            old = self.connection.execute(
                                "SELECT revision,observed_at,value "
                                f"FROM {self.schema}.revisions "
                                "WHERE series_id=%s AND quarter=%s "
                                "ORDER BY revision DESC LIMIT 1",
                                (point.series_id, quarter_start(point.quarter)),
                            ).fetchone()
                            if old is not None and (
                                missing_only or old[2] == point.value
                            ):
                                continue
                            if old is not None and batch.observed_at <= old[1]:
                                raise GdpStorageError(
                                    "gdp_timeseries_observation_order_conflict"
                                )
                            revision = 1 if old is None else old[0] + 1
                            doc = _document(point, batch, revision)
                            self.connection.execute(
                                f"INSERT INTO {self.schema}.revisions ({_COLUMNS}) "
                                "VALUES(%s,%s,%s,%s,%s,%s,%s,NULL)",
                                (
                                    point.series_id,
                                    quarter_start(point.quarter),
                                    revision,
                                    batch.observed_at,
                                    bid,
                                    point.value,
                                    Jsonb(point.provider_extra),
                                ),
                            )
                            inserted.append(doc)
            except GdpStorageError:
                raise
            except Exception:
                raise GdpStorageError("gdp_timeseries_commit_outcome_unknown") from None
            if inserted:
                try:
                    at = _confirmed_at(self.clock, inserted)
                    with self.connection.transaction():
                        for doc in inserted:
                            self.connection.execute(
                                f"UPDATE {self.schema}.revisions SET available_at=%s "
                                "WHERE series_id=%s AND quarter=%s "
                                "AND revision=%s AND available_at IS NULL",
                                (
                                    at,
                                    doc["series_id"],
                                    quarter_start(doc["quarter"]),
                                    doc["revision"],
                                ),
                            )
                except Exception:
                    raise GdpStorageError(
                        "gdp_timeseries_confirmation_outcome_unknown"
                    ) from None
        return {
            "saved_points": len(inserted),
            "saved_revisions": sum(d["revision"] > 1 for d in inserted),
        }

    def confirm_current_availability(self, series, start, end):
        _read_args(series, start, end, None, "observed")
        self._top_level()
        try:
            with self.operation(), self.connection.transaction():
                self.connection.execute(
                    f"LOCK TABLE {self.schema}.revisions IN SHARE ROW EXCLUSIVE MODE"
                )
                rows = self.connection.execute(
                    "SELECT series_id,quarter,revision,observed_at "
                    f"FROM {self.schema}.latest "
                    "WHERE series_id=ANY(%s) AND quarter BETWEEN %s AND %s "
                    "AND available_at IS NULL",
                    (list(series), quarter_start(start), quarter_start(end)),
                ).fetchall()
                at = (
                    _confirmed_at(
                        self.clock, [{"observed_at": r[3].isoformat()} for r in rows]
                    )
                    if rows
                    else None
                )
                for sid, q, revision, _ in rows:
                    self.connection.execute(
                        f"UPDATE {self.schema}.revisions SET available_at=%s "
                        "WHERE series_id=%s AND quarter=%s AND revision=%s "
                        "AND available_at IS NULL",
                        (at, sid, q, revision),
                    )
            return {
                "confirmed_points": len(rows),
                "confirmed_at": None if at is None else at.isoformat(),
            }
        except GdpStorageError:
            raise
        except Exception:
            raise GdpStorageError(
                "gdp_timeseries_confirmation_outcome_unknown"
            ) from None

    def status(self):
        try:
            rows = self.connection.execute(
                f"SELECT series_id,max(quarter) FROM {self.schema}.latest "
                "WHERE value IS NOT NULL GROUP BY series_id"
            ).fetchall()
            counts = self.connection.execute(
                "SELECT count(*),count(*)-count(DISTINCT(series_id,quarter)) "
                f"FROM {self.schema}.revisions"
            ).fetchone()
            batches = self.connection.execute(
                f"SELECT count(*) FROM {self.schema}.batches"
            ).fetchone()[0]
            latest = {sid: quarter_label(q) for sid, q in rows}
            return {
                "schema": self.schema,
                "latest_quarters": {s: latest.get(s) for s in SERIES_IDS},
                "observation_count": counts[0],
                "revision_count": counts[1],
                "batch_count": batches,
            }
        except Exception:
            raise GdpStorageError("gdp_timeseries_storage_read_failed") from None

    def close(self):
        self.connection.close()
