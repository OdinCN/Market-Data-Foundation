"""Independent six-cell MARTS persistence. Construction and reads perform no DDL."""

from __future__ import annotations

import hashlib
import json
import os
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from threading import RLock
from uuid import uuid4

import psycopg

from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailBatch,
    RetailCell,
    _now,
    parse_range_response,
)

SCHEMA = "retail_timeseries_candidate"


class RetailStorageError(RuntimeError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
        raise RetailStorageError("retail_schema_invalid")
    return '"' + value + '"'


def read_cutoff(value):
    if value is None:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise RetailStorageError("retail_aware_cutoff_required")
    return value.astimezone(UTC)


def collection_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise RetailStorageError("retail_collection_id_invalid")
    return value


def batch_id(collection, request):
    return hashlib.sha256(
        (collection_key(collection) + request.identity).encode()
    ).hexdigest()


def content(cell):
    return cell.value, cell.status, cell.reason, cell.source_value


def validate_batch(batch):
    if (
        parse_range_response(
            batch.raw, batch.request.start_month, batch.request.end_month
        )
        != batch.cells
        or batch.observed_at.utcoffset() is None
        or type(batch.attempts) is not int
        or not 1 <= batch.attempts <= 3
    ):
        raise RetailStorageError("retail_batch_invalid")


@dataclass(frozen=True)
class RetailRevision:
    cell: RetailCell
    revision: int
    batch_id: str
    observed_at: datetime
    available_at: datetime | None


class FileRetailBatchArchive:
    """Small durable replay boundary; guarded raw stays under ignored artifacts/."""

    def __init__(self, directory):
        self.directory = Path(directory).resolve()
        if "artifacts" not in self.directory.parts:
            raise RetailStorageError("retail_artifacts_directory_required")

    def _write_once(self, path, value):
        encoded = json.dumps(value, sort_keys=True).encode()
        self.directory.mkdir(parents=True, exist_ok=True)
        # Publish without replacing another writer's evidence. A stale temporary
        # file from a crash cannot prevent replay of a fully published batch.
        if path.exists():
            if path.read_bytes() != encoded:
                raise RetailStorageError("retail_replay_conflict")
            return
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with temporary.open("xb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                if path.read_bytes() != encoded:
                    raise RetailStorageError("retail_replay_conflict") from None
        finally:
            temporary.unlink(missing_ok=True)
        descriptor = os.open(self.directory, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def register(self, collection, plan):
        self._write_once(
            self.directory / (collection_key(collection) + ".plan.json"), asdict(plan)
        )

    def stage(self, collection, batch):
        validate_batch(batch)
        bid = batch_id(collection, batch.request)
        self._write_once(
            self.directory / (bid + ".json"),
            {
                "raw": batch.raw.decode("utf-8"),
                "observed_at": batch.observed_at.isoformat(),
                "attempts": batch.attempts,
            },
        )
        return str(self.directory / (bid + ".json"))

    def load(self, collection, request):
        path = self.directory / (batch_id(collection, request) + ".json")
        if not path.exists():
            return None
        record = json.loads(path.read_text())
        raw = record["raw"].encode()
        batch = RetailBatch(
            request,
            parse_range_response(raw, request.start_month, request.end_month),
            raw,
            datetime.fromisoformat(record["observed_at"]),
            record["attempts"],
        )
        validate_batch(batch)
        return batch


class InMemoryRetailStore:
    def __init__(self, *, clock=lambda: datetime.now(UTC)):
        self.clock = clock
        self.revisions, self.batches, self.pending, self.plans = {}, {}, {}, {}
        self.lock = RLock()

    def ensure_schema(self):
        pass

    def register_collection(self, collection, plan):
        collection_key(collection)
        if collection in self.plans and self.plans[collection] != plan:
            raise RetailStorageError("retail_replay_conflict")
        self.plans[collection] = plan

    def stage_batch(self, collection, batch):
        validate_batch(batch)
        bid = batch_id(collection, batch.request)
        if bid in self.pending and self.pending[bid] != batch:
            raise RetailStorageError("retail_replay_conflict")
        self.pending[bid] = batch

    def retained_batch(self, collection, request):
        return self.pending.get(batch_id(collection, request))

    def save_batch(self, collection, batch):
        self.last_write_counts = None
        bid = batch_id(collection, batch.request)
        self.stage_batch(collection, batch)
        with self.lock:
            if bid in self.batches:
                self.last_write_counts = (0, 0)
                return 0
            changes = []
            for cell in batch.cells:
                versions = self.revisions.get(cell.key, [])
                old = versions[-1] if versions else None
                if old and content(old.cell) == content(cell):
                    continue
                if old and batch.observed_at <= old.observed_at:
                    raise RetailStorageError("retail_observation_order_conflict")
                changes.append((cell, len(versions) + 1))
            available = _now(self.clock)
            if available < batch.observed_at:
                raise RetailStorageError("retail_clock_regressed")
            for cell, revision in changes:
                self.revisions.setdefault(cell.key, []).append(
                    RetailRevision(cell, revision, bid, batch.observed_at, available)
                )
            self.batches[bid] = batch
            self.last_write_counts = (
                sum(revision == 1 for _, revision in changes),
                sum(revision > 1 for _, revision in changes),
            )
            return len(changes)

    def read_range(self, start, end, *, as_of=None, basis="observed"):
        as_of = read_cutoff(as_of)
        if basis not in {"observed", "available"}:
            raise RetailStorageError("retail_basis_invalid")
        with self.lock:
            result = {}
            for key, versions in self.revisions.items():
                if not start <= key[0] <= end:
                    continue
                eligible = []
                for version in versions:
                    at = getattr(version, basis + "_at")
                    if at is None or (as_of is not None and at > as_of):
                        continue
                    if basis == "available" and any(
                        later.revision > version.revision
                        and later.available_at is None
                        and (as_of is None or later.observed_at <= as_of)
                        for later in versions
                    ):
                        continue
                    eligible.append(version)
                if eligible:
                    result[key] = eligible[-1]
            return result

    def status(self):
        inputs = {}
        for (month, category, kind), versions in self.revisions.items():
            if kind == "SM" and versions[-1].cell.value is not None:
                inputs.setdefault(month, set()).add(category)
        required = {
            "headline": {"44X72"},
            "control": {"44X72", "441", "444", "447", "722"},
            "ex_auto": {"44X72", "441"},
        }
        return {
            "latest_months": {
                basket: max(
                    (month for month, present in inputs.items() if operands <= present),
                    default=None,
                )
                for basket, operands in required.items()
            },
            "batch_count": len(self.batches),
            "observation_count": sum(map(len, self.revisions.values())),
            "revision_count": sum(len(v) - 1 for v in self.revisions.values()),
            "latest_month": max(
                (
                    k[0]
                    for k, versions in self.revisions.items()
                    if k[1:] == ("44X72", "SM") and versions[-1].cell.value is not None
                ),
                default=None,
            ),
        }


def rates_view_sql(schema=SCHEMA):
    s = identifier(schema)
    levels = ",".join(
        f"max(value) FILTER(WHERE category_code='{c}' AND data_type_code='SM') AS v{i}"
        for i, c in enumerate(("44X72", "441", "444", "447", "722"))
    )
    rates = ",".join(
        f"c.{name}::numeric(64,32)/nullif(p{h}.{name},0)-1 AS {name}_change_{h}m_ratio"
        for name in ("headline", "control")
        for h in (1, 3, 6, 12)
    )
    joins = " ".join(
        f"LEFT JOIN levels p{h} ON p{h}.month=(c.month-interval '{h} month')::date"
        for h in (1, 3, 6, 12)
    )
    extra_rates = ",".join(
        f"c.ex_auto::numeric(64,32)/nullif(p{h}.ex_auto,0)-1 "
        f"AS ex_auto_change_{h}m_ratio"
        for h in (1, 3, 6, 12)
    )
    return f"""CREATE OR REPLACE VIEW {s}.retail_timeseries_rates AS
 WITH inputs AS (SELECT month,{levels},
 max(value) FILTER(WHERE data_type_code='MPCSM') AS official_headline_mpcsm_percent
 FROM {s}.latest GROUP BY month),
 levels AS (SELECT month,v0 AS headline,v0-v1-v2-v3-v4 AS control,
 official_headline_mpcsm_percent,v0-v1 AS ex_auto FROM inputs)
 SELECT c.month,c.headline,c.control,c.official_headline_mpcsm_percent,{rates},
 c.ex_auto,{extra_rates},'mdf_retail_control_v1'::text AS control_derivation_identity,
 'SM44X72-SM441-SM444-SM447-SM722'::text AS control_formula,
 'mdf_retail_ex_auto_v1'::text AS ex_auto_derivation_identity,
 'SM44X72-SM441:v1'::text AS ex_auto_formula
 FROM levels c {joins};"""


def schema_sql(schema=SCHEMA):
    s = identifier(schema)
    return f"""CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.batches (
 batch_id TEXT PRIMARY KEY, collection_id TEXT NOT NULL, request_identity TEXT NOT NULL,
 start_month DATE NOT NULL CHECK(extract(day FROM start_month)=1),
 end_month DATE NOT NULL CHECK(extract(day FROM end_month)=1),
 raw_sha256 TEXT NOT NULL,
 raw_locator TEXT NOT NULL, observed_at TIMESTAMPTZ NOT NULL,
 available_at TIMESTAMPTZ, provider_attempts INTEGER NOT NULL
 CHECK(provider_attempts BETWEEN 1 AND 3),
 CHECK(end_month>=start_month),
 CHECK(available_at IS NULL OR available_at>=observed_at),
 UNIQUE(collection_id,request_identity));
CREATE TABLE IF NOT EXISTS {s}.revisions (
 month DATE NOT NULL CHECK(extract(day FROM month)=1), category_code TEXT NOT NULL,
 data_type_code TEXT NOT NULL, revision INTEGER NOT NULL CHECK(revision>0),
 value NUMERIC(27,9), source_value TEXT, status TEXT NOT NULL,
 reason TEXT, source_pointer TEXT, raw_sha256 TEXT NOT NULL,
 batch_id TEXT NOT NULL REFERENCES {s}.batches(batch_id),
 observed_at TIMESTAMPTZ NOT NULL, available_at TIMESTAMPTZ,
 CHECK(available_at IS NULL OR available_at>=observed_at),
 PRIMARY KEY(month,category_code,data_type_code,revision),
 CHECK((data_type_code='SM' AND category_code IN ('44X72','441','444','447','722'))
 OR (data_type_code='MPCSM' AND category_code='44X72')),
 CHECK(status IN ('available','unavailable','suppressed','missing')),
 CHECK((status='available')=(value IS NOT NULL)),
 CHECK(value IS NULL OR (value != 'NaN'::numeric AND abs(value) < 1e18)));
CREATE OR REPLACE VIEW {s}.latest AS
 SELECT DISTINCT ON(month,category_code,data_type_code) * FROM {s}.revisions
 ORDER BY month,category_code,data_type_code,revision DESC;
{rates_view_sql(schema)}"""


class PostgresRetailStore:
    """Injected autocommit connection only; no credentials or connection discovery."""

    def __init__(
        self, connection, archive, *, schema=SCHEMA, clock=lambda: datetime.now(UTC)
    ):
        self._lock = RLock()
        self.s = identifier(schema)
        self.schema, self.connection, self.archive, self.clock = (
            schema,
            connection,
            archive,
            clock,
        )
        if not connection.autocommit:
            raise RetailStorageError("retail_autocommit_required")

    @contextmanager
    def read_snapshot(self):
        with self._lock:
            info = getattr(self.connection, "info", None)
            if (
                info is not None
                and info.transaction_status != psycopg.pq.TransactionStatus.IDLE
            ):
                raise RetailStorageError("retail_top_level_transaction_required")
            with self.connection.transaction():
                self.connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                yield

    def ensure_schema(self):
        self.connection.execute(schema_sql(self.schema))

    def register_collection(self, collection, plan):
        self.archive.register(collection, plan)

    def stage_batch(self, collection, batch):
        return self.archive.stage(collection, batch)

    def retained_batch(self, collection, request):
        return self.archive.load(collection, request)

    def save_batch(self, collection, batch):
        self.last_write_counts = None
        info = getattr(self.connection, "info", None)
        if (
            info is not None
            and info.transaction_status != psycopg.pq.TransactionStatus.IDLE
        ):
            raise RetailStorageError("retail_top_level_transaction_required")
        locator = self.stage_batch(collection, batch)
        bid, changed = batch_id(collection, batch.request), 0
        observations = revisions = 0
        c, s = self.connection, self.s
        try:
            with c.transaction():
                c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (self.schema,))
                existing = c.execute(
                    f"SELECT raw_sha256,observed_at FROM {s}.batches WHERE batch_id=%s",
                    (bid,),
                ).fetchone()
                digest = hashlib.sha256(batch.raw).hexdigest()
                if existing and tuple(existing) != (digest, batch.observed_at):
                    raise RetailStorageError("retail_replay_conflict")
                if not existing:
                    c.execute(
                        f"INSERT INTO {s}.batches "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s)",
                        (
                            bid,
                            collection,
                            batch.request.identity,
                            date.fromisoformat(batch.request.start_month + "-01"),
                            date.fromisoformat(batch.request.end_month + "-01"),
                            digest,
                            locator,
                            batch.observed_at,
                            batch.attempts,
                        ),
                    )
                    for cell in batch.cells:
                        key = (
                            date.fromisoformat(cell.month + "-01"),
                            cell.category_code,
                            cell.data_type_code,
                        )
                        old = c.execute(
                            "SELECT revision,value,status,reason,source_value,"
                            f"observed_at FROM {s}.revisions "
                            "WHERE month=%s AND category_code=%s AND data_type_code=%s "
                            "ORDER BY revision DESC LIMIT 1",
                            key,
                        ).fetchone()
                        if old and tuple(old[1:5]) == content(cell):
                            continue
                        if old and batch.observed_at <= old[5]:
                            raise RetailStorageError(
                                "retail_observation_order_conflict"
                            )
                        c.execute(
                            f"INSERT INTO {s}.revisions VALUES("
                            + ",".join(["%s"] * 13)
                            + ")",
                            (
                                *key,
                                old[0] + 1 if old else 1,
                                cell.value,
                                cell.source_value,
                                cell.status,
                                cell.reason,
                                cell.source_pointer,
                                digest,
                                bid,
                                batch.observed_at,
                                None,
                            ),
                        )
                        changed += 1
                        observations += old is None
                        revisions += old is not None
            # Confirmation is sampled after acknowledged commit; replay confirms
            # unknown outcomes now, never fabricates an earlier availability time.
            available = _now(self.clock)
            if available < batch.observed_at:
                raise RetailStorageError("retail_clock_regressed")
            with c.transaction():
                c.execute(
                    f"UPDATE {s}.revisions SET available_at=%s "
                    "WHERE batch_id=%s AND available_at IS NULL",
                    (available, bid),
                )
                c.execute(
                    f"UPDATE {s}.batches SET available_at=%s "
                    "WHERE batch_id=%s AND available_at IS NULL",
                    (available, bid),
                )
        except RetailStorageError:
            raise
        except psycopg.OperationalError:
            raise RetailStorageError("retail_storage_outcome_unknown") from None
        except Exception:
            raise RetailStorageError("retail_storage_write_failed") from None
        self.last_write_counts = (observations, revisions)
        return changed

    def read_range(self, start, end, *, as_of=None, basis="observed"):
        as_of = read_cutoff(as_of)
        if basis not in {"observed", "available"}:
            raise RetailStorageError("retail_basis_invalid")
        parameters = [
            date.fromisoformat(start + "-01"),
            date.fromisoformat(end + "-01"),
        ]
        condition = ""
        if basis == "available":
            condition = (
                " AND r.available_at IS NOT NULL AND NOT EXISTS ("
                f"SELECT 1 FROM {self.s}.revisions pending "
                "WHERE pending.month=r.month AND pending.category_code=r.category_code "
                "AND pending.data_type_code=r.data_type_code "
                "AND pending.revision>r.revision AND pending.available_at IS NULL"
            )
            if as_of is not None:
                condition += " AND pending.observed_at <= %s"
                parameters.append(as_of)
            condition += ")"
        if as_of is not None:
            condition += f" AND r.{basis}_at <= %s"
            parameters.append(as_of)
        try:
            rows = self.connection.execute(
                "SELECT DISTINCT ON(r.month,r.category_code,r.data_type_code) "
                "r.month,r.category_code,r.data_type_code,r.value,r.status,r.reason,"
                "r.source_value,r.source_pointer,r.raw_sha256,"
                "r.revision,r.batch_id,r.observed_at,r.available_at "
                f"FROM {self.s}.revisions r "
                f"WHERE r.month BETWEEN %s AND %s{condition} "
                "ORDER BY r.month,r.category_code,r.data_type_code,r.revision DESC",
                parameters,
            ).fetchall()
            result = {}
            for row in rows:
                cell = RetailCell(row[0].strftime("%Y-%m"), *row[1:9])
                result[cell.key] = RetailRevision(cell, *row[9:])
            return result
        except Exception:
            raise RetailStorageError("retail_storage_read_failed") from None

    def status(self):
        with self.read_snapshot():
            return self._status()

    def _status(self):
        try:
            row = self.connection.execute(
                "SELECT count(*),"
                "count(*)-count(DISTINCT(month,category_code,data_type_code)),"
                f"(SELECT to_char(max(month),'YYYY-MM') FROM {self.s}.latest "
                "WHERE category_code='44X72' AND data_type_code='SM' "
                f"AND value IS NOT NULL) FROM {self.s}.revisions"
            ).fetchone()
            batches = self.connection.execute(
                f"SELECT count(*) FROM {self.s}.batches"
            ).fetchone()[0]
            latest = self.connection.execute(
                "SELECT "
                + ",".join(
                    f"to_char(max(month) FILTER (WHERE {basket} IS NOT NULL),'YYYY-MM')"
                    for basket in ("headline", "control", "ex_auto")
                )
                + f" FROM {self.s}.retail_timeseries_rates"
            ).fetchone()
            return dict(
                latest_months=dict(zip(("headline", "control", "ex_auto"), latest)),
                observation_count=row[0],
                revision_count=row[1],
                latest_month=row[2],
                batch_count=batches,
            )
        except Exception:
            raise RetailStorageError("retail_storage_read_failed") from None
