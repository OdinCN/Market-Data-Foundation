"""Independent M3 persistence, verified raw material and local revision cutoffs."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from threading import RLock
from uuid import UUID

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.bls_cpi_material_store import BUCKET
from market_data_foundation.historical.gdp_store import instant
from market_data_foundation.historical.m3_capital_goods import (
    SERIES,
    M3Request,
    M3Snapshot,
    check,
    parse_response,
)
from market_data_foundation.historical.verified_garage_raw import VerifiedGarageRawStore

SCHEMA = "mdf_m3_capital_goods_dev_r1"
LOCK_ID = 7312026110302
RAW_PREFIX = "macro/m3-capital-goods/r1/"
SERIES_IDS = tuple(SERIES.values())


@dataclass(frozen=True)
class M3Batch:
    snapshots: tuple[M3Snapshot, ...]
    collection_id: str | None = None
    collection_scope: tuple[str, str] | None = None

    @classmethod
    def from_raw(cls, raw):
        """Restore the retained capture, preserving its original acquisition times."""
        try:
            check(
                type(raw) is bytes and 0 < len(raw) <= 8 * 1024 * 1024,
                "batch_material_invalid",
            )
            document = json.loads(raw)
            check(
                set(document)
                in ({"schema", "snapshots"}, {"schema", "snapshots", "collection"})
                and document["schema"] == "mdf.m3.capture-batch.r1",
                "batch_material_invalid",
            )
            snapshots = []
            for item in document["snapshots"]:
                check(
                    set(item) == {"request", "observed_at", "raw_base64"},
                    "batch_material_invalid",
                )
                snapshots.append(
                    parse_response(
                        base64.b64decode(item["raw_base64"], validate=True),
                        M3Request(**item["request"]),
                        obtained_at=datetime.fromisoformat(item["observed_at"]),
                    )
                )
            collection = document.get("collection")
            check(
                collection is None or set(collection) == {"id", "start", "end"},
                "batch_material_invalid",
            )
            batch = cls(
                tuple(snapshots),
                None if collection is None else collection["id"],
                None
                if collection is None
                else (collection["start"], collection["end"]),
            )
            check(batch.raw == raw, "batch_material_noncanonical")
            return batch
        except Exception:
            check(False, "batch_material_invalid")

    def __post_init__(self):
        check(
            type(self.snapshots) is tuple and 1 <= len(self.snapshots) <= 3,
            "batch_invalid",
        )
        check(all(type(s) is M3Snapshot for s in self.snapshots), "batch_invalid")
        first = self.snapshots[0].request
        check(
            (self.collection_id is None) == (self.collection_scope is None),
            "collection_scope_invalid",
        )
        if self.collection_id is not None:
            try:
                valid = str(UUID(self.collection_id)) == self.collection_id
            except TypeError, ValueError, AttributeError:
                valid = False
            check(
                valid
                and type(self.collection_scope) is tuple
                and len(self.collection_scope) == 2,
                "collection_id_invalid",
            )
            start, end = self.collection_scope
            month_date(start)
            month_date(end)
            check(
                start <= first.start_month <= first.end_month <= end,
                "collection_scope_invalid",
            )
        targets = [
            (s.request.category_code, s.request.data_type_code) for s in self.snapshots
        ]
        check(
            len(set(targets)) == len(targets) and set(targets) <= set(SERIES),
            "batch_matrix_invalid",
        )
        for s in self.snapshots:
            check(
                (s.request.dataset, s.request.start_month, s.request.end_month)
                == (first.dataset, first.start_month, first.end_month),
                "batch_scope_conflict",
            )
            check(
                parse_response(s.raw, s.request, obtained_at=s.obtained_at) == s,
                "batch_projection_mismatch",
            )

    @property
    def dataset(self):
        return self.snapshots[0].request.dataset

    @property
    def request(self):
        return self.snapshots[0].request

    @property
    def raw(self):
        document = {
            "schema": "mdf.m3.capture-batch.r1",
            "snapshots": [
                {
                    "request": s.request.__dict__,
                    "observed_at": s.obtained_at.isoformat(),
                    "raw_base64": base64.b64encode(s.raw).decode(),
                }
                for s in self.snapshots
            ],
        }
        if self.collection_id is not None:
            document["collection"] = {
                "id": self.collection_id,
                "start": self.collection_scope[0],
                "end": self.collection_scope[1],
            }
        return json.dumps(
            document,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()

    @property
    def identity(self):
        return hashlib.sha256(self.raw).hexdigest()


def month_date(value):
    from market_data_foundation.historical.cpi_timeseries import month_number

    month_number(value)
    return date.fromisoformat(value + "-01")


def raw_locator(sha):
    return f"mdf-s3://{BUCKET}/{RAW_PREFIX}{sha}/raw-response.bin"


class GarageM3RawStore(VerifiedGarageRawStore):
    def __init__(self, credentials, **options):
        super().__init__(
            credentials, raw_prefix=RAW_PREFIX, error_prefix="m3_raw", **options
        )


def migration_sql():
    return (
        f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.batches (
            batch_id text PRIMARY KEY, raw_sha256 text NOT NULL,
            raw_locator text NOT NULL,
            dataset text NOT NULL CHECK(dataset IN ('m3','advm3')),
            captured_from timestamptz NOT NULL, captured_to timestamptz NOT NULL,
            request jsonb NOT NULL, CHECK(captured_to>=captured_from))""",
        f"""CREATE TABLE IF NOT EXISTS {SCHEMA}.revisions (
            dataset text NOT NULL CHECK(dataset IN ('m3','advm3')),
            series_id text NOT NULL CHECK(series_id IN (
                {",".join(repr(s) for s in SERIES_IDS)})),
            month date NOT NULL CHECK(extract(day FROM month)=1),
            revision integer NOT NULL CHECK(revision>0), value numeric,
            status text NOT NULL CHECK(status IN ('available','missing')),
            reason text, observed_at timestamptz NOT NULL,
            available_at timestamptz CHECK(available_at>=observed_at),
            batch_id text NOT NULL REFERENCES {SCHEMA}.batches(batch_id),
            source jsonb NOT NULL,
            CHECK((status='available' AND value IS NOT NULL AND reason IS NULL)
                OR (status='missing' AND value IS NULL AND reason IS NOT NULL)),
            CHECK(value IS NULL OR value::text NOT IN ('NaN','Infinity','-Infinity')),
            PRIMARY KEY(dataset,series_id,month,revision))""",
        f"""CREATE OR REPLACE VIEW {SCHEMA}.latest AS
            SELECT DISTINCT ON(dataset,series_id,month) *
            FROM {SCHEMA}.revisions ORDER BY dataset,series_id,month,revision DESC""",
        f"""CREATE OR REPLACE VIEW {SCHEMA}.m3_timeseries_rates AS
            SELECT c.*, CASE WHEN c.available_at IS NOT NULL
                AND p.available_at IS NOT NULL THEN
                (c.value/nullif(p.value,0)-1)*100 END AS change_1m_percent
            FROM {SCHEMA}.latest c LEFT JOIN {SCHEMA}.latest p
                ON p.dataset=c.dataset AND p.series_id=c.series_id
                AND p.month=(c.month-interval '1 month')::date""",
    )


def _rows(batch):
    for s in batch.snapshots:
        for cell in s.cells:
            yield {
                "dataset": batch.dataset,
                "series_id": cell.series,
                "month": cell.reference_month,
                "value": None if cell.value is None else str(cell.value),
                "status": cell.status,
                "reason": cell.reason,
                "observed_at": s.obtained_at.isoformat(),
                "available_at": None,
                "batch_id": batch.identity,
                "source": {
                    "category_code": cell.category_code,
                    "data_type_code": cell.data_type_code,
                    "time_slot_id": cell.time_slot_id,
                    "source_pointer": cell.source_pointer,
                    "raw_sha256": s.raw_sha256,
                    "report_stage": s.request.report_stage,
                    "unit": cell.unit,
                    "seasonal_adjustment": cell.seasonal_adjustment,
                    "frequency": cell.frequency,
                    "annualized": cell.annualized,
                    "version_basis": s.version_basis,
                    "official_release_event": None,
                    "source_version_date": None,
                },
            }


def collection_metadata(batch, *, confirmed=False):
    if batch.collection_id is None:
        return [s.request.persisted() for s in batch.snapshots]
    check(len(batch.snapshots) == 1, "collection_fragment_invalid")
    request = batch.request
    return {
        "collection_id": batch.collection_id,
        "dataset": batch.dataset,
        "start": batch.collection_scope[0],
        "end": batch.collection_scope[1],
        "category_code": request.category_code,
        "data_type_code": request.data_type_code,
        "fragment_start": request.start_month,
        "fragment_end": request.end_month,
        "confirmed": confirmed,
        "expected": [
            {k: row[k] for k in ("series_id", "month", "value", "status", "reason")}
            for row in _rows(batch)
        ],
    }


def _same(a, b):
    from decimal import Decimal

    return (
        None if a["value"] is None else Decimal(a["value"]),
        a["status"],
    ) == (None if b["value"] is None else Decimal(b["value"]), b["status"])


def _select(rows, dataset, start, end, as_of, basis):
    check(basis in {"observed", "available"}, "cutoff_basis_invalid")
    cutoff = None if as_of is None else instant(as_of)
    grouped = {}
    for row in rows:
        if row["dataset"] == dataset and start <= row["month"] <= end:
            grouped.setdefault((row["series_id"], row["month"]), []).append(row)
    selected = []
    for history in grouped.values():
        eligible = [
            r
            for r in history
            if (
                cutoff is None
                or instant(datetime.fromisoformat(r["observed_at"])) <= cutoff
            )
        ]
        if basis == "available":
            # A committed but unconfirmed newer row must not reveal a stale value.
            if (
                eligible
                and max(eligible, key=lambda r: r["revision"])["available_at"] is None
            ):
                continue
            eligible = [
                r
                for r in eligible
                if r["available_at"] is not None
                and (
                    cutoff is None
                    or datetime.fromisoformat(r["available_at"]) <= cutoff
                )
            ]
        if eligible:
            selected.append(copy.deepcopy(max(eligible, key=lambda r: r["revision"])))
    return sorted(selected, key=lambda r: (r["month"], r["series_id"]))


class InMemoryM3Store:
    def __init__(self, raw_store, *, clock=lambda: datetime.now(UTC)):
        self.raw_store, self.clock = raw_store, clock
        self.rows, self.batches = [], {}
        self.confirmed_fragments = set()
        self.lock = RLock()

    def save_batch(self, batch):
        check(type(batch) is M3Batch, "batch_invalid")
        with self.lock:
            if batch.identity in self.batches:
                self.confirm_batch_availability(batch.identity)
                return {"saved_points": 0, "saved_revisions": 0}
            check(
                self.raw_store.put_verified(batch.raw) == raw_locator(batch.identity),
                "raw_locator_mismatch",
            )
            planned = copy.deepcopy(self.rows)
            inserted = []
            for row in _rows(batch):
                history = [
                    r
                    for r in planned
                    if all(r[k] == row[k] for k in ("dataset", "series_id", "month"))
                ]
                old = max(history, key=lambda r: r["revision"]) if history else None
                if old and _same(old, row):
                    continue
                check(
                    old is None or row["observed_at"] > old["observed_at"],
                    "capture_order_conflict",
                )
                row["revision"] = 1 if old is None else old["revision"] + 1
                planned.append(row)
                inserted.append(row)
            self.rows = planned
            self.batches[batch.identity] = batch
            self.confirm_batch_availability(batch.identity)
            return {
                "saved_points": len(inserted),
                "saved_revisions": sum(r["revision"] > 1 for r in inserted),
            }

    def confirm_batch_availability(self, batch_id):
        with self.lock:
            pending = [
                r
                for r in self.rows
                if r["batch_id"] == batch_id and r["available_at"] is None
            ]
            if not pending:
                return 0
            at = instant(self.clock())
            check(
                all(at >= datetime.fromisoformat(r["observed_at"]) for r in pending),
                "confirmation_clock_invalid",
            )
            for row in pending:
                row["available_at"] = at.isoformat()
            return len(pending)

    def confirm_pending(self, dataset, start, end, *, max_batches=6):
        with self.lock:
            batches = sorted(
                {
                    r["batch_id"]
                    for r in self.rows
                    if r["dataset"] == dataset
                    and start <= r["month"] <= end
                    and r["available_at"] is None
                }
            )[:max_batches]
            return sum(
                self.confirm_batch_availability(batch_id) for batch_id in batches
            )

    def collection_progress(self, collection_id):
        with self.lock:
            return [
                (
                    identity,
                    collection_metadata(
                        batch, confirmed=identity in self.confirmed_fragments
                    ),
                )
                for identity, batch in self.batches.items()
                if batch.collection_id == collection_id
            ]

    def mark_collection_fragment(self, batch_id):
        with self.lock:
            check(
                batch_id in self.batches
                and self.batches[batch_id].collection_id is not None,
                "collection_fragment_not_found",
            )
            self.confirmed_fragments.add(batch_id)

    def read_range(self, dataset, start, end, as_of=None, basis="available"):
        with self.lock:
            return _select(self.rows, dataset, start, end, as_of, basis)


class PostgresM3Store:
    def __init__(self, connection, raw_store, *, clock=lambda: datetime.now(UTC)):
        check(connection.autocommit, "autocommit_required")
        self.connection, self.raw_store, self.clock = connection, raw_store, clock
        self.lock = RLock()

    @contextmanager
    def operation(self):
        with self.lock:
            check(
                self.connection.info.transaction_status
                == psycopg.pq.TransactionStatus.IDLE,
                "top_level_transaction_required",
            )
            with self.connection.transaction():
                self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
                yield

    def ensure_schema(self):
        with self.operation():
            commands = migration_sql()
            self.connection.execute(commands[0])
            check(
                self.connection.execute(
                    "SELECT pg_get_userbyid(nspowner)=current_user "
                    "FROM pg_namespace WHERE nspname=%s",
                    (SCHEMA,),
                ).fetchone()
                == (True,),
                "schema_owner_mismatch",
            )
            for sql in commands[1:]:
                self.connection.execute(sql)
            columns = self.connection.execute(
                "SELECT column_name,data_type FROM information_schema.columns "
                "WHERE table_schema=%s AND table_name=%s",
                (SCHEMA, "revisions"),
            ).fetchall()
            check(
                dict(columns)
                == {
                    "dataset": "text",
                    "series_id": "text",
                    "month": "date",
                    "revision": "integer",
                    "value": "numeric",
                    "status": "text",
                    "reason": "text",
                    "observed_at": "timestamp with time zone",
                    "available_at": "timestamp with time zone",
                    "batch_id": "text",
                    "source": "jsonb",
                },
                "schema_shape_mismatch",
            )

    def save_batch(self, batch):
        check(type(batch) is M3Batch, "batch_invalid")
        inserted = []
        # Check under the family lock, but never hold a database transaction
        # across Garage I/O. Recheck after verification to close the race.
        with self.operation():
            exists = self.connection.execute(
                f"SELECT 1 FROM {SCHEMA}.batches WHERE batch_id=%s", (batch.identity,)
            ).fetchone()
        if not exists:
            check(
                self.raw_store.put_verified(batch.raw) == raw_locator(batch.identity),
                "raw_locator_mismatch",
            )
        with self.operation():
            exists = self.connection.execute(
                f"SELECT 1 FROM {SCHEMA}.batches WHERE batch_id=%s", (batch.identity,)
            ).fetchone()
            if not exists:
                self.connection.execute(
                    f"INSERT INTO {SCHEMA}.batches VALUES(%s,%s,%s,%s,%s,%s,%s)",
                    (
                        batch.identity,
                        batch.identity,
                        raw_locator(batch.identity),
                        batch.dataset,
                        min(s.obtained_at for s in batch.snapshots),
                        max(s.obtained_at for s in batch.snapshots),
                        Jsonb(collection_metadata(batch)),
                    ),
                )
                for row in _rows(batch):
                    old = self.connection.execute(
                        "SELECT revision,value,status,reason,observed_at "
                        f"FROM {SCHEMA}.revisions "
                        "WHERE dataset=%s AND series_id=%s AND month=%s "
                        "ORDER BY revision DESC LIMIT 1",
                        (row["dataset"], row["series_id"], month_date(row["month"])),
                    ).fetchone()
                    if old and _same(
                        dict(value=old[1], status=old[2], reason=old[3]), row
                    ):
                        continue
                    check(
                        old is None
                        or datetime.fromisoformat(row["observed_at"]) > old[4],
                        "capture_order_conflict",
                    )
                    row["revision"] = 1 if old is None else old[0] + 1
                    self.connection.execute(
                        f"INSERT INTO {SCHEMA}.revisions "
                        "VALUES(%s,%s,%s,%s,%s,%s,%s,%s,NULL,%s,%s)",
                        (
                            row["dataset"],
                            row["series_id"],
                            month_date(row["month"]),
                            row["revision"],
                            row["value"],
                            row["status"],
                            row["reason"],
                            datetime.fromisoformat(row["observed_at"]),
                            batch.identity,
                            Jsonb(row["source"]),
                        ),
                    )
                    inserted.append(row)
        self.confirm_batch_availability(batch.identity)
        return {
            "saved_points": len(inserted),
            "saved_revisions": sum(r["revision"] > 1 for r in inserted),
        }

    def confirm_batch_availability(self, batch_id):
        with self.operation():
            rows = self.connection.execute(
                f"SELECT observed_at FROM {SCHEMA}.revisions "
                "WHERE batch_id=%s AND available_at IS NULL",
                (batch_id,),
            ).fetchall()
            if not rows:
                return 0
            at = instant(self.clock())
            check(all(at >= row[0] for row in rows), "confirmation_clock_invalid")
            self.connection.execute(
                f"UPDATE {SCHEMA}.revisions SET available_at=%s "
                "WHERE batch_id=%s AND available_at IS NULL",
                (at, batch_id),
            )
            return len(rows)

    def confirm_pending(self, dataset, start, end, *, max_batches=6):
        # No source or Garage access: these rows have already committed after
        # successful raw verification. Confirmation uses the present clock.
        with self.lock, self.connection.transaction():
            self.connection.execute("SET TRANSACTION READ ONLY")
            rows = self.connection.execute(
                f"SELECT DISTINCT batch_id FROM {SCHEMA}.revisions "
                "WHERE dataset=%s AND month BETWEEN %s AND %s "
                "AND available_at IS NULL ORDER BY batch_id LIMIT %s",
                (dataset, month_date(start), month_date(end), max_batches),
            ).fetchall()
        return sum(self.confirm_batch_availability(row[0]) for row in rows)

    def collection_progress(self, collection_id):
        with self.lock, self.connection.transaction():
            self.connection.execute("SET TRANSACTION READ ONLY")
            return self.connection.execute(
                f"SELECT batch_id,request FROM {SCHEMA}.batches "
                "WHERE jsonb_typeof(request)='object' "
                "AND request->>'collection_id'=%s ORDER BY captured_from,batch_id",
                (collection_id,),
            ).fetchall()

    def mark_collection_fragment(self, batch_id):
        with self.operation():
            row = self.connection.execute(
                f"UPDATE {SCHEMA}.batches SET request=jsonb_set("
                "request,'{confirmed}','true'::jsonb) "
                "WHERE batch_id=%s AND jsonb_typeof(request)='object' "
                "AND request ? 'collection_id' RETURNING batch_id",
                (batch_id,),
            ).fetchone()
            check(row is not None, "collection_fragment_not_found")

    def read_range(self, dataset, start, end, as_of=None, basis="available"):
        # Parameterized query and short read-only snapshot; never a source call.
        with self.lock, self.connection.transaction():
            self.connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            found = self.connection.execute(
                "SELECT dataset,series_id,month,revision,value,status,reason,"
                f"observed_at,available_at,batch_id,source FROM {SCHEMA}.revisions "
                "WHERE dataset=%s AND month BETWEEN %s AND %s ORDER BY revision",
                (dataset, month_date(start), month_date(end)),
            ).fetchall()
        rows = []
        for row in found:
            d = dict(
                zip(
                    (
                        "dataset",
                        "series_id",
                        "month",
                        "revision",
                        "value",
                        "status",
                        "reason",
                        "observed_at",
                        "available_at",
                        "batch_id",
                        "source",
                    ),
                    row,
                    strict=True,
                )
            )
            d["month"] = d["month"].strftime("%Y-%m")
            d["value"] = None if d["value"] is None else str(d["value"])
            for k in ("observed_at", "available_at"):
                d[k] = None if d[k] is None else instant(d[k]).isoformat()
            rows.append(d)
        return _select(rows, dataset, start, end, as_of, basis)
