"""Local SQLite durable intent/receipt journal and fenced single-host claim.

No canonical Material/Version/Publication writes. Use an isolated ignored
artifacts directory (or a temporary test directory), never a production DB.
"""

import fcntl
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Callable, Iterator
from uuid import uuid4

from market_data_foundation.domain_contracts.market_planning import Plan
from market_data_foundation.domain_contracts.market_universe import canonical_bytes
from market_data_foundation.historical.market_daily_codec import decode_plan
from market_data_foundation.historical.market_daily_source import (
    DailyError,
    Failure,
    Lookup,
    LookupStatus,
    NativeField,
    NativeRow,
    Page,
    PageRequest,
)


def page_metadata(page: Page) -> str:
    data = asdict(page)
    del data["raw"]
    return json.dumps(
        data,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda value: value.astimezone(UTC).isoformat(),
    )


def decode_page(metadata: str, raw: bytes) -> Page:
    data = json.loads(metadata)
    data["rows"] = tuple(
        NativeRow(
            row["day"],
            row["locator"],
            tuple(NativeField(**field) for field in row["fields"]),
        )
        for row in data["rows"]
    )
    for name in ("obtained_at", "received_at", "known_at", "source_time"):
        if data[name] is not None:
            data[name] = datetime.fromisoformat(data[name])
    return Page(raw=raw, **data)


@dataclass(frozen=True)
class MaterialReceipt:
    request: PageRequest
    digest: str
    byte_length: int
    locator: str
    metadata: str
    state: str
    classification: Failure | None

    @property
    def attempt_identity(self) -> str:
        return self.request.attempt_identity


@dataclass(frozen=True)
class Claim:
    job_identity: str
    owner: str
    generation: int


class Journal:
    def __init__(
        self,
        directory: Path,
        *,
        clock_ns: Callable[[], int] = time.time_ns,
        fault: Callable[[str], None] = lambda _: None,
    ):
        self.directory = Path(directory).resolve()
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = self.directory / "market-daily.sqlite3"
        self.clock_ns = clock_ns
        self.fault = fault
        with self._db() as db:
            db.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, intent BLOB NOT NULL,
                    accepted_ns INTEGER NOT NULL, cancelled INTEGER NOT NULL DEFAULT 0,
                    owner TEXT, generation INTEGER NOT NULL DEFAULT 0,
                    lease_until_ns INTEGER NOT NULL DEFAULT 0,
                    last_request_ns INTEGER, counters TEXT NOT NULL DEFAULT '{}',
                    stop_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS attempts (
                    id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id),
                    shard TEXT NOT NULL, page_index INTEGER NOT NULL,
                    request_id TEXT NOT NULL UNIQUE, request BLOB NOT NULL,
                    state TEXT NOT NULL, classification TEXT,
                    started_ns INTEGER NOT NULL, response_identity TEXT,
                    observed_bytes INTEGER, observed_rows INTEGER,
                    observed_at_ns INTEGER, response_digest TEXT,
                    UNIQUE(job, shard, page_index)
                );
                CREATE TABLE IF NOT EXISTS materials (
                    attempt TEXT PRIMARY KEY REFERENCES attempts(id),
                    digest TEXT NOT NULL, byte_length INTEGER NOT NULL,
                    raw BLOB NOT NULL, metadata TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    sequence INTEGER PRIMARY KEY, job TEXT NOT NULL,
                    attempt TEXT, event TEXT NOT NULL, at_ns INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reconciliations (
                    attempt TEXT PRIMARY KEY REFERENCES attempts(id),
                    job TEXT NOT NULL, shard TEXT NOT NULL,
                    request_id TEXT NOT NULL, status TEXT NOT NULL,
                    evidence TEXT NOT NULL, response_identity TEXT,
                    at_ns INTEGER NOT NULL
                );
                """
            )
        os.chmod(self.path, 0o600)

    def _event(
        self, db: sqlite3.Connection, job: str, event: str, attempt: str | None = None
    ) -> None:
        db.execute(
            "INSERT INTO events(job,attempt,event,at_ns) VALUES(?,?,?,?)",
            (job, attempt, event, self.clock_ns()),
        )

    def events(self, job: str) -> tuple[dict, ...]:
        with self._db(readonly=True) as db:
            return tuple(
                dict(row)
                for row in db.execute(
                    "SELECT * FROM events WHERE job=? ORDER BY sequence", (job,)
                )
            )

    @contextmanager
    def _db(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        # DELETE journal mode permits genuine read-only reconciliation without
        # creating a WAL/SHM file or opening a write transaction.
        db = None
        try:
            db = sqlite3.connect(
                self.path.as_uri() + ("?mode=ro" if readonly else "?mode=rwc"),
                uri=True,
                timeout=5,
            )
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            if readonly:
                db.execute("PRAGMA query_only=ON")
            else:
                db.execute("PRAGMA synchronous=FULL")
            if not readonly:
                db.execute("BEGIN IMMEDIATE")
            yield db
            if not readonly:
                db.commit()
        except sqlite3.Error as error:
            if db is not None and not readonly:
                db.rollback()
            classification = (
                Failure.UNAVAILABLE
                if isinstance(error, sqlite3.OperationalError)
                else Failure.CORRUPT
            )
            raise DailyError(classification) from None
        except BaseException:
            if db is not None and not readonly:
                db.rollback()
            raise
        finally:
            if db is not None:
                db.close()

    def accept(self, plan: Plan, execution_contract: bytes) -> str:
        intent = canonical_bytes(plan) + b"\n" + execution_contract
        with self._db() as db:
            row = db.execute(
                "SELECT intent FROM jobs WHERE id=?", (plan.spec.identity,)
            ).fetchone()
            if row is not None:
                if row["intent"] != intent:
                    raise DailyError(Failure.IDENTITY)
            else:
                db.execute(
                    "INSERT INTO jobs(id,intent,accepted_ns) VALUES(?,?,?)",
                    (plan.spec.identity, intent, self.clock_ns()),
                )
                self._event(db, plan.spec.identity, "ACCEPTED")
                self.fault("before_accept_commit")
        self.fault("after_accept_commit")
        return plan.spec.identity

    def check_intent(self, plan: Plan, execution_contract: bytes) -> None:
        with self._db(readonly=True) as db:
            row = db.execute(
                "SELECT intent FROM jobs WHERE id=?", (plan.spec.identity,)
            ).fetchone()
            if (
                row is None
                or row["intent"] != canonical_bytes(plan) + b"\n" + execution_contract
            ):
                raise DailyError(Failure.IDENTITY)

    def load_plan(self, job: str) -> Plan:
        with self._db(readonly=True) as db:
            row = db.execute("SELECT intent FROM jobs WHERE id=?", (job,)).fetchone()
            if row is None:
                raise DailyError(Failure.IDENTITY)
            plan = decode_plan(row["intent"].split(b"\n", 1)[0])
            if plan.spec.identity != job:
                raise DailyError(Failure.CORRUPT)
            return plan

    @contextmanager
    def claim(self, job: str, lease_seconds: int) -> Iterator[Claim]:
        # Never unlink or replace this inode. flock survives a lease timeout;
        # an expired metadata lease cannot steal a still-running host worker.
        with (self.directory / "host-worker.lock").open("a+b") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise DailyError(Failure.BUSY) from None
            token = None
            try:
                with self._db() as db:
                    row = db.execute(
                        "SELECT generation FROM jobs WHERE id=?", (job,)
                    ).fetchone()
                    if row is None:
                        raise DailyError(Failure.IDENTITY)
                    token = Claim(job, uuid4().hex, row["generation"] + 1)
                    db.execute(
                        "UPDATE jobs SET owner=?, generation=?, lease_until_ns=? "
                        "WHERE id=?",
                        (
                            token.owner,
                            token.generation,
                            self.clock_ns() + lease_seconds * 10**9,
                            job,
                        ),
                    )
                yield token
            finally:
                if token is not None:
                    with self._db() as db:
                        db.execute(
                            "UPDATE jobs SET owner=NULL,lease_until_ns=0 "
                            "WHERE id=? AND owner=? AND generation=?",
                            (job, token.owner, token.generation),
                        )
                fcntl.flock(lock, fcntl.LOCK_UN)

    def _fence(self, db: sqlite3.Connection, claim: Claim) -> sqlite3.Row:
        row = db.execute(
            "SELECT * FROM jobs WHERE id=?", (claim.job_identity,)
        ).fetchone()
        if (
            row is None
            or row["owner"] != claim.owner
            or row["generation"] != claim.generation
            or self.clock_ns() >= row["lease_until_ns"]
        ):
            raise DailyError(Failure.LEASE)
        return row

    def renew(self, claim: Claim, lease_seconds: int) -> None:
        with self._db() as db:
            self._fence(db, claim)
            db.execute(
                "UPDATE jobs SET lease_until_ns=? WHERE id=?",
                (self.clock_ns() + lease_seconds * 10**9, claim.job_identity),
            )

    def cancel(self, job: str) -> None:
        with self._db() as db:
            row = db.execute("SELECT cancelled FROM jobs WHERE id=?", (job,)).fetchone()
            if row is not None and row["cancelled"]:
                return
            if (
                db.execute("UPDATE jobs SET cancelled=1 WHERE id=?", (job,)).rowcount
                != 1
            ):
                raise DailyError(Failure.IDENTITY)
            self._event(db, job, "CANCELLED")

    def resume(self, claim: Claim) -> None:
        with self._db() as db:
            row = self._fence(db, claim)
            if not row["cancelled"]:
                return
            db.execute("UPDATE jobs SET cancelled=0 WHERE id=?", (claim.job_identity,))
            self._event(db, claim.job_identity, "RESUMED")

    def block(self, claim: Claim, failure: Failure) -> None:
        with self._db() as db:
            row = self._fence(db, claim)
            self._block(db, row, failure)

    def _block(
        self, db: sqlite3.Connection, row: sqlite3.Row, failure: Failure
    ) -> None:
        if row["stop_reason"] != failure.value:
            db.execute(
                "UPDATE jobs SET stop_reason=? WHERE id=?", (failure.value, row["id"])
            )
            self._event(db, row["id"], "BLOCKED:" + failure.value)

    def job(self, identity: str) -> dict:
        with self._db(readonly=True) as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise DailyError(Failure.IDENTITY)
            result = dict(row)
            result["counters"] = json.loads(row["counters"])
            return result

    def attempts(self, job: str) -> tuple[dict, ...]:
        with self._db(readonly=True) as db:
            return tuple(
                dict(row)
                for row in db.execute(
                    "SELECT * FROM attempts WHERE job=? ORDER BY shard,page_index",
                    (job,),
                )
            )

    def attempt(self, request: PageRequest) -> dict | None:
        with self._db(readonly=True) as db:
            row = db.execute(
                "SELECT * FROM attempts WHERE id=?", (request.attempt_identity,)
            ).fetchone()
            if row is None:
                return None
            if row["request"] != canonical_bytes(request):
                raise DailyError(Failure.CORRUPT)
            return dict(row)

    def charge(
        self, claim: Claim, axis: str, amount: int, limit: int, wall_seconds: int
    ) -> None:
        with self._db() as db:
            row = self._fence(db, claim)
            if row["cancelled"]:
                raise DailyError(Failure.CANCELLED)
            if row["stop_reason"]:
                raise DailyError(Failure(row["stop_reason"]))
            elapsed = self.clock_ns() - row["accepted_ns"]
            counters = json.loads(row["counters"])
            denied = (
                elapsed < 0
                or elapsed >= wall_seconds * 10**9
                or type(amount) is not int
                or amount < 0
                or counters.get(axis, 0) + amount > limit
            )
            if denied:
                self._block(db, row, Failure.BUDGET)
            else:
                counters[axis] = counters.get(axis, 0) + amount
                db.execute(
                    "UPDATE jobs SET counters=? WHERE id=?",
                    (json.dumps(counters, sort_keys=True), claim.job_identity),
                )
        # Raising inside _db would roll back STOP and leave a crash window
        # before the runner's fallback block. Denied effects are never charged.
        if denied:
            raise DailyError(Failure.BUDGET)

    def begin(
        self,
        claim: Claim,
        request: PageRequest,
        max_requests: int,
        rate: int,
        wall_seconds: int,
    ) -> bool:
        with self._db() as db:
            row = self._fence(db, claim)
            if request.shard.job_identity != claim.job_identity:
                raise DailyError(Failure.IDENTITY)
            old = db.execute(
                "SELECT request FROM attempts WHERE id=?", (request.attempt_identity,)
            ).fetchone()
            if old is not None:
                if old["request"] != canonical_bytes(request):
                    raise DailyError(Failure.IDENTITY)
                return False
            now = self.clock_ns()
            counters = json.loads(row["counters"])
            if row["cancelled"]:
                raise DailyError(Failure.CANCELLED)
            if row["stop_reason"]:
                raise DailyError(Failure(row["stop_reason"]))
            if (
                now < row["accepted_ns"]
                or now - row["accepted_ns"] >= wall_seconds * 10**9
                or counters.get("requests", 0) >= max_requests
                or (
                    row["last_request_ns"] is not None
                    and (now - row["last_request_ns"]) * rate < 10**9
                )
            ):
                raise DailyError(Failure.BUDGET)
            counters["requests"] = counters.get("requests", 0) + 1
            db.execute(
                "UPDATE jobs SET counters=?,last_request_ns=? WHERE id=?",
                (json.dumps(counters, sort_keys=True), now, claim.job_identity),
            )
            db.execute(
                "INSERT INTO attempts(id,job,shard,page_index,request_id,request,"
                "state,classification,started_ns) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    request.attempt_identity,
                    claim.job_identity,
                    request.shard.identity,
                    request.page_index,
                    request.identity,
                    canonical_bytes(request),
                    "STARTED",
                    None,
                    now,
                ),
            )
            self._event(db, claim.job_identity, "STARTED", request.attempt_identity)
            self.fault("before_attempt_commit")
        self.fault("after_attempt_commit")
        return True

    def mark(
        self,
        claim: Claim,
        request: PageRequest,
        state: str,
        failure: Failure,
        *,
        reconciliation: Lookup | None = None,
    ) -> None:
        with self._db() as db:
            row = self._fence(db, claim)
            changed = db.execute(
                "UPDATE attempts SET state=?,classification=? "
                "WHERE id=? AND job=? AND state='STARTED'",
                (state, failure.value, request.attempt_identity, claim.job_identity),
            ).rowcount
            if changed != 1:
                raise DailyError(Failure.IDENTITY)
            if state == "REJECTED" and failure == Failure.BUDGET:
                # Refusal, STOP and any original FOUND proof commit together;
                # no material or cumulative counter is added by this path.
                self._block(db, row, failure)
            self._event(
                db,
                claim.job_identity,
                state + ":" + failure.value,
                request.attempt_identity,
            )
            if reconciliation is not None:
                self._record_reconciliation(db, claim, request, reconciliation)
        if reconciliation is not None:
            self.fault("after_reconciliation_commit")

    def reconciliation(self, request: PageRequest) -> dict | None:
        """Read the original accepted observation without effects or Source lookup."""
        with self._db(readonly=True) as db:
            row = db.execute(
                "SELECT r.*, a.request FROM reconciliations r "
                "JOIN attempts a ON a.id=r.attempt WHERE r.attempt=?",
                (request.attempt_identity,),
            ).fetchone()
            if row is None:
                return None
            if (
                row["request"] != canonical_bytes(request)
                or row["job"] != request.shard.job_identity
                or row["shard"] != request.shard.identity
                or row["request_id"] != request.identity
            ):
                raise DailyError(Failure.CORRUPT)
            result = dict(row)
            del result["request"]
            return result

    def _record_reconciliation(
        self, db: sqlite3.Connection, claim: Claim, request: PageRequest, result: Lookup
    ) -> None:
        # Called inside the SAME transaction as the accepted state/material.
        # One immutable observation per original Attempt bounds journal growth.
        attempt = db.execute(
            "SELECT * FROM attempts WHERE id=? AND job=?",
            (request.attempt_identity, claim.job_identity),
        ).fetchone()
        response_identity = (
            sha256(
                result.page.raw
                + page_metadata(result.page).encode("utf-8", errors="surrogatepass")
            ).hexdigest()
            if result.page is not None
            else None
        )
        if (
            attempt is None
            or attempt["request"] != canonical_bytes(request)
            or result.request_identity != request.identity
            or (
                result.page is not None
                and result.page.request_identity != request.identity
            )
            or result.status == LookupStatus.UNKNOWN
            or response_identity != attempt["response_identity"]
            or (result.status == LookupStatus.ABSENT and attempt["state"] != "ABSENT")
            or (
                result.status == LookupStatus.FOUND
                and attempt["state"] not in ("SUCCEEDED", "REJECTED")
            )
        ):
            raise DailyError(Failure.IDENTITY)
        values = (
            request.attempt_identity,
            claim.job_identity,
            request.shard.identity,
            request.identity,
            result.status.value,
            result.evidence,
            response_identity,
        )
        old = db.execute(
            "SELECT attempt,job,shard,request_id,status,evidence,response_identity "
            "FROM reconciliations WHERE attempt=?",
            (request.attempt_identity,),
        ).fetchone()
        if old is not None:
            if tuple(old) != values:
                raise DailyError(Failure.IDENTITY)
            return
        db.execute(
            "INSERT INTO reconciliations VALUES(?,?,?,?,?,?,?,?)",
            (*values, self.clock_ns()),
        )
        self._event(
            db, claim.job_identity, "RECONCILIATION_ACCEPTED", request.attempt_identity
        )
        self.fault("after_reconciliation_insert")

    def accept_reconciliation_observation(
        self, claim: Claim, request: PageRequest, result: Lookup
    ) -> None:
        """Accept/replay proof for an already terminal original Attempt."""
        with self._db() as db:
            self._fence(db, claim)
            self._record_reconciliation(db, claim, request, result)
        self.fault("after_reconciliation_commit")

    def reopen_unknown(self, claim: Claim, request: PageRequest) -> None:
        with self._db() as db:
            self._fence(db, claim)
            changed = db.execute(
                "UPDATE attempts SET state='STARTED' "
                "WHERE id=? AND job=? AND state='UNKNOWN'",
                (request.attempt_identity, claim.job_identity),
            ).rowcount
            if changed != 1:
                raise DailyError(Failure.IDENTITY)
            self._event(db, claim.job_identity, "RECONCILING", request.attempt_identity)

    def observe_response(
        self, claim: Claim, request: PageRequest, page: Page, limits: dict[str, int]
    ) -> None:
        """Account actual returned effects once, including over-budget replies."""
        raw_digest = sha256(page.raw).hexdigest()
        with self._db() as db:
            row = self._fence(db, claim)
            attempt = db.execute(
                "SELECT * FROM attempts WHERE id=? AND job=?",
                (request.attempt_identity, claim.job_identity),
            ).fetchone()
            if attempt is None or attempt["request"] != canonical_bytes(request):
                raise DailyError(Failure.IDENTITY)
            if attempt["observed_at_ns"] is not None:
                if (
                    attempt["response_digest"] != raw_digest
                    or attempt["observed_bytes"] != len(page.raw)
                    or attempt["observed_rows"] != len(page.rows)
                ):
                    raise DailyError(Failure.IDENTITY)
            else:
                counters = json.loads(row["counters"])
                for axis, amount in (
                    ("response_bytes", len(page.raw)),
                    ("rows", len(page.rows)),
                    ("returned_pages", 1),
                ):
                    counters[axis] = counters.get(axis, 0) + amount
                observed_ns = self.clock_ns()
                # No decoded metadata is serialized in this transaction. Even an
                # unserializable Page cannot erase returned effects or their
                # applicable STOP. observed_at_ns is the once-only marker;
                # response_identity may still be NULL after a serializer fault.
                if (
                    len(page.raw) > request.budget.max_response_bytes
                    or any(
                        counters.get(axis, 0) > limits[axis]
                        for axis in ("response_bytes", "rows")
                    )
                    or any(
                        counters.get(axis, 0) + amount > limits[axis]
                        for axis, amount in (
                            ("material_bytes", len(page.raw)),
                            ("pages", 1),
                        )
                    )
                    or observed_ns < row["accepted_ns"]
                    or observed_ns - row["accepted_ns"]
                    >= request.budget.wall_seconds * 10**9
                ):
                    self._block(db, row, Failure.BUDGET)
                db.execute(
                    "UPDATE jobs SET counters=? WHERE id=?",
                    (json.dumps(counters, sort_keys=True), claim.job_identity),
                )
                db.execute(
                    "UPDATE attempts SET observed_bytes=?,observed_rows=?,"
                    "observed_at_ns=?,response_digest=? WHERE id=?",
                    (
                        len(page.raw),
                        len(page.rows),
                        observed_ns,
                        raw_digest,
                        request.attempt_identity,
                    ),
                )
                self._event(
                    db,
                    claim.job_identity,
                    "RESPONSE_OBSERVED",
                    request.attempt_identity,
                )
        self.fault("after_response_commit")

        # Hash malformed Unicode losslessly for original-identity verification
        # of rejected responses only. Material acceptance still requires strict
        # UTF-8; these hash input bytes are never persisted as metadata/material.
        metadata = page_metadata(page).encode("utf-8", errors="surrogatepass")
        identity = sha256(page.raw + metadata).hexdigest()
        with self._db() as db:
            row = self._fence(db, claim)
            attempt = db.execute(
                "SELECT response_identity FROM attempts WHERE id=? AND job=?",
                (request.attempt_identity, claim.job_identity),
            ).fetchone()
            if attempt["response_identity"] is not None:
                if attempt["response_identity"] != identity:
                    raise DailyError(Failure.IDENTITY)
                return
            counters = json.loads(row["counters"])
            if (
                counters.get("metadata_bytes", 0) + len(metadata)
                > limits["metadata_bytes"]
            ):
                self._block(db, row, Failure.BUDGET)
            db.execute(
                "UPDATE attempts SET response_identity=? WHERE id=?",
                (identity, request.attempt_identity),
            )

    def finish(
        self,
        claim: Claim,
        request: PageRequest,
        page: Page,
        failure: Failure | None,
        limits: dict[str, int],
        *,
        reconciliation: Lookup | None = None,
    ) -> MaterialReceipt:
        metadata = page_metadata(page)
        raw_digest = sha256(page.raw).hexdigest()
        with self._db() as db:
            row = self._fence(db, claim)
            attempt = db.execute(
                "SELECT * FROM attempts WHERE id=? AND job=?",
                (request.attempt_identity, claim.job_identity),
            ).fetchone()
            if attempt is None or attempt["request"] != canonical_bytes(request):
                raise DailyError(Failure.IDENTITY)
            old = db.execute(
                "SELECT * FROM materials WHERE attempt=?", (request.attempt_identity,)
            ).fetchone()
            if old is not None:
                if old["raw"] != page.raw or old["metadata"] != metadata:
                    raise DailyError(Failure.IDENTITY)
            else:
                if attempt["state"] != "STARTED":
                    raise DailyError(Failure.IDENTITY)
                counters = json.loads(row["counters"])
                amounts = {
                    "material_bytes": len(page.raw),
                    "metadata_bytes": len(metadata.encode()),
                    "pages": 1,
                }
                if any(
                    counters.get(axis, 0) > limits[axis]
                    for axis in ("response_bytes", "rows")
                ):
                    raise DailyError(Failure.BUDGET)
                if any(
                    counters.get(axis, 0) + amount > limits[axis]
                    for axis, amount in amounts.items()
                ):
                    raise DailyError(Failure.BUDGET)
                for axis, amount in amounts.items():
                    counters[axis] = counters.get(axis, 0) + amount
                db.execute(
                    "UPDATE jobs SET counters=? WHERE id=?",
                    (json.dumps(counters, sort_keys=True), claim.job_identity),
                )
                db.execute(
                    "INSERT INTO materials VALUES(?,?,?,?,?)",
                    (
                        request.attempt_identity,
                        raw_digest,
                        len(page.raw),
                        page.raw,
                        metadata,
                    ),
                )
                self.fault("after_material_insert")
                db.execute(
                    "UPDATE attempts SET state=?,classification=? WHERE id=?",
                    (
                        "REJECTED" if failure else "SUCCEEDED",
                        failure.value if failure else None,
                        request.attempt_identity,
                    ),
                )
                self._event(
                    db,
                    claim.job_identity,
                    "REJECTED:" + failure.value if failure else "SUCCEEDED",
                    request.attempt_identity,
                )
                self.fault("before_receipt_commit")
            if reconciliation is not None:
                self._record_reconciliation(db, claim, request, reconciliation)
        self.fault("after_receipt_commit")
        if reconciliation is not None:
            self.fault("after_reconciliation_commit")
        return self.receipt(request)

    def receipt(self, request: PageRequest) -> MaterialReceipt | None:
        with self._db(readonly=True) as db:
            row = db.execute(
                "SELECT m.*, a.state, a.classification, a.request, a.response_identity "
                "FROM materials m JOIN attempts a ON a.id=m.attempt WHERE m.attempt=?",
                (request.attempt_identity,),
            ).fetchone()
            if row is None:
                return None
            if (
                row["request"] != canonical_bytes(request)
                or row["byte_length"] != len(row["raw"])
                or sha256(row["raw"]).hexdigest() != row["digest"]
                or sha256(row["raw"] + row["metadata"].encode()).hexdigest()
                != row["response_identity"]
            ):
                raise DailyError(Failure.CORRUPT)
            return MaterialReceipt(
                request,
                row["digest"],
                row["byte_length"],
                f"journal:{request.attempt_identity}",
                row["metadata"],
                row["state"],
                Failure(row["classification"]) if row["classification"] else None,
            )

    def read_material(self, receipt: MaterialReceipt) -> Page:
        current = self.receipt(receipt.request)
        if current != receipt:
            raise DailyError(Failure.IDENTITY)
        with self._db(readonly=True) as db:
            row = db.execute(
                "SELECT raw FROM materials WHERE attempt=?", (receipt.attempt_identity,)
            ).fetchone()
            if (
                row is None
                or len(row["raw"]) != receipt.byte_length
                or sha256(row["raw"]).hexdigest() != receipt.digest
            ):
                raise DailyError(Failure.CORRUPT)
            try:
                return decode_page(receipt.metadata, row["raw"])
            except ValueError, TypeError, KeyError:
                raise DailyError(Failure.CORRUPT) from None
