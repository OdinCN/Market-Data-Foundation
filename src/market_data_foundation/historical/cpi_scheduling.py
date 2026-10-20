"""Durable, finite CPI plans advanced by explicitly invoked trusted host ticks.

No cron, listener, background thread or T09 family adapter. SQLite intent and
quota reservation precede every possible effect. Child jobs own canonical
lineage and retain one immutable Attempt each. A restart observes, never replays.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_data_foundation.canonical.model import digest, instant, require
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical import cpi_history
from market_data_foundation.historical.bls_cpi_direct import (
    MAX_RESPONSE_BYTES,
    SERIES_IDS,
)
from market_data_foundation.historical.bls_cpi_direct_live import TIMEOUT_SECONDS
from market_data_foundation.historical.cpi_job_journal import (
    FileJobJournal,
    valid_identity,
)
from market_data_foundation.historical.cpi_local_job import (
    QUALITY,
    CpiJobSpec,
    CpiLocalJob,
    DirectCpiAcquirer,
)


def history_spec(reference: str, effect_domain: str) -> CpiJobSpec:
    return CpiJobSpec(
        BLS_CPI_HEADLINES.identity,
        BLS_CPI_HEADLINES.revision,
        SERIES_IDS,
        reference,
        cpi_history.SOURCE,
        cpi_history.REVISION,
        "not_declared",
        cpi_history.COMPLETENESS,
        QUALITY,
        "history/" + reference,
        "acquire",
        1,
        MAX_RESPONSE_BYTES,
        TIMEOUT_SECONDS,
        effect_domain,
    )


@dataclass(frozen=True)
class PlanSpec:
    name: str
    start: str
    end: str
    timezone: str
    window_start: str
    window_end: str
    first_due: str
    interval_seconds: int
    cycles: int
    effect_domain: str

    def __post_init__(self):
        valid_identity(self.name)
        cpi_history.months(self.start, self.end)
        require(
            self.effect_domain in {"synthetic", "owner_nonproduction"},
            "cpi_plan_effect_domain_invalid",
        )
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError, ValueError, TypeError:
            require(False, "cpi_plan_timezone_invalid")
        import re

        for value in (self.window_start, self.window_end):
            require(
                type(value) is str
                and re.fullmatch(r"(?:[01][0-9]|2[0-3]):[0-5][0-9]", value),
                "cpi_plan_window_invalid",
            )
        require(self.window_start < self.window_end, "cpi_plan_window_invalid")
        try:
            instant(datetime.fromisoformat(self.first_due))
        except ValueError, TypeError:
            require(False, "cpi_plan_due_invalid")
        require(
            type(self.interval_seconds) is int
            and 60 <= self.interval_seconds <= 366 * 86400,
            "cpi_plan_interval_invalid",
        )
        require(
            type(self.cycles) is int and 1 <= self.cycles <= 12,
            "cpi_plan_cycle_budget_invalid",
        )

    def document(self):
        from dataclasses import asdict

        return asdict(self)


def _verified_read_as_of(job, version, reference):
    # Only independent readback creates last_success. A parent mutation or a
    # terminal child state alone says nothing about when this Version was usable.
    success = job.get("last_success")
    if success is None or version is None:
        return None
    try:
        evidence = success["evidence"]
        if (
            evidence["version_identity"] != version.identity
            or evidence["raw_sha256"] != job["raw"]["sha256"]
            or evidence["coverage"] != list(version.finite_scope.members)
            or job["spec"]["reference"] != reference
            or not evidence["publication_occurrence"]
        ):
            return None
        observed = datetime.fromisoformat(success["observed_at"])
        instant(observed)
        for field in ("known_at", "decision_at", "usable_at"):
            at = datetime.fromisoformat(evidence[field])
            instant(at)
            if observed < at:
                return None
        return success["observed_at"]
    except KeyError, TypeError, ValueError:
        return None


class CpiPlanStore:
    def __init__(self, journal: FileJobJournal, *, initialize: bool = False):
        self.journal = journal
        if initialize:
            require(not journal.read_only, "journal_read_only")
            with journal._connection() as connection:
                connection.executescript("""
                    CREATE TABLE IF NOT EXISTS cpi_plans (
                        identity TEXT PRIMARY KEY, document TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cpi_plan_events (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        identity TEXT NOT NULL, event TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cpi_quota (
                        day TEXT PRIMARY KEY, reserved INTEGER NOT NULL,
                        last_dispatch TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS cpi_wire_pending (
                        singleton INTEGER PRIMARY KEY CHECK(singleton=1),
                        day TEXT NOT NULL
                    );
                """)

    def reader(self):
        return CpiPlanStore(FileJobJournal(self.journal.directory, read_only=True))

    def _get(self, connection, identity):
        valid_identity(identity)
        row = connection.execute(
            "SELECT document FROM cpi_plans WHERE identity=?", (identity,)
        ).fetchone()
        require(row is not None, "cpi_plan_not_found")
        document = json.loads(row[0])
        PlanSpec(**document["spec"])
        require(document["identity"] == identity, "cpi_plan_record_invalid")
        return document

    def get(self, identity):
        with self.journal._connection() as connection:
            return self._get(connection, identity)

    def _write(self, connection, document, at):
        instant(at)
        require(
            at >= datetime.fromisoformat(document["updated_at"]),
            "cpi_plan_clock_reversal",
        )
        document["updated_at"] = at.isoformat()
        encoded = json.dumps(document, sort_keys=True)
        connection.execute(
            "INSERT INTO cpi_plans VALUES (?, ?) ON CONFLICT(identity) "
            "DO UPDATE SET document=excluded.document",
            (document["identity"], encoded),
        )
        connection.execute(
            "INSERT INTO cpi_plan_events(identity,event) VALUES (?, ?)",
            (document["identity"], encoded),
        )

    def accept(self, identity: str, spec: PlanSpec, at: datetime):
        valid_identity(identity)
        require(not self.journal.read_only, "journal_read_only")
        instant(at)
        with self.journal._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            exists = connection.execute(
                "SELECT 1 FROM cpi_plans WHERE identity=?", (identity,)
            ).fetchone()
            if exists:
                old = self._get(connection, identity)
                require(old["spec"] == spec.document(), "cpi_plan_identity_reused")
                return old
            count = connection.execute("SELECT COUNT(*) FROM cpi_plans").fetchone()[0]
            require(count < 50, "cpi_plan_query_budget_exceeded")
            document = {
                "identity": identity,
                "spec": spec.document(),
                "state": "active",
                "cycle": 0,
                "cursor": 0,
                "batches": {},
                "retry_queue": [],
                "inflight_key": None,
                "next_due": spec.first_due,
                "created_at": at.isoformat(),
                "updated_at": at.isoformat(),
            }
            self._write(connection, document, at)
            return document

    def action(self, identity, action, at):
        require(action in {"pause", "resume", "cancel"}, "cpi_plan_action_invalid")
        require(not self.journal.read_only, "journal_read_only")
        # Same lock as the tick: pause/cancel acknowledge only between finite batches.
        with self.journal.executor("plan-" + digest(identity.encode())[:40]):
            with self.journal._connection() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                document = self._get(connection, identity)
                require(
                    document["state"] not in {"cancelled", "completed"},
                    "cpi_plan_terminal",
                )
                document["state"] = {
                    "pause": "paused",
                    "resume": "active",
                    "cancel": "cancelled",
                }[action]
                self._write(connection, document, at)
                return document

    def backfill(self, identity, start, end, at):
        requested = cpi_history.months(start, end)
        require(not self.journal.read_only, "journal_read_only")
        with self.journal.executor("plan-" + digest(identity.encode())[:40]):
            with self.journal._connection() as connection, connection:
                connection.execute("BEGIN IMMEDIATE")
                document = self._get(connection, identity)
                require(document["state"] in {"active", "paused"}, "cpi_plan_terminal")
                selected = []
                for key, batch in document["batches"].items():
                    if batch["reference"] not in requested:
                        continue
                    job = self.journal.get(batch["jobs"][-1])
                    if job["last_success"] is not None:
                        continue
                    # UNKNOWN/running may already have effects. No source replay,
                    # including after confirmed-not-published storage observations.
                    require(
                        job["state"] in {"failed", "cancelled"}
                        and not job["effects"].get("canonical_possible"),
                        "cpi_plan_backfill_reconciliation_required",
                    )
                    require(len(batch["jobs"]) < 3, "cpi_plan_retry_budget_exceeded")
                    selected.append(key)
                require(
                    selected
                    and set(requested)
                    <= {document["batches"][k]["reference"] for k in selected},
                    "cpi_plan_failed_window_required",
                )
                document["retry_queue"] = list(
                    dict.fromkeys(document["retry_queue"] + selected)
                )
                self._write(connection, document, at)
                return document

    def snapshot(
        self, identity: str | None = None, *, effect_domain: str | None = None
    ):
        with self.journal._connection() as connection:
            connection.execute("BEGIN")
            identities = connection.execute(
                "SELECT identity FROM cpi_plans "
                + ("WHERE identity=? " if identity is not None else "")
                + "ORDER BY identity LIMIT 51",
                (identity,) if identity is not None else (),
            ).fetchall()
            require(len(identities) <= 50, "cpi_plan_query_budget_exceeded")
            result = []
            batch_count = 0
            for (identity,) in identities:
                d = self._get(connection, identity)
                batch_count += len(d["batches"])
                require(batch_count <= 4320, "cpi_plan_query_budget_exceeded")
                require(
                    len(json.dumps(d)) <= 2_000_000, "cpi_plan_query_budget_exceeded"
                )
                # Filter before projecting child state or reconciliation evidence.
                # The global scan/batch budgets remain unchanged.
                if (
                    effect_domain is not None
                    and d["spec"]["effect_domain"] != effect_domain
                ):
                    continue
                batches = []
                for key, batch in d["batches"].items():
                    jobs = []
                    for job_id in batch["jobs"]:
                        row = connection.execute(
                            "SELECT document FROM jobs WHERE identity=?", (job_id,)
                        ).fetchone()
                        if row is None:
                            jobs.append(
                                {
                                    "job_identity": job_id,
                                    "state": "pending",
                                    "attempt_identity": None,
                                    "raw_sha256": None,
                                    "version_identity": None,
                                    "publication_occurrence": None,
                                    "read_as_of": None,
                                    "classification": None,
                                    "reconciliation": None,
                                }
                            )
                            continue
                        job = json.loads(row[0])
                        success = job.get("last_success")
                        from market_data_foundation.canonical import codec

                        version = (
                            None
                            if job["plan"] is None
                            else codec.loads(job["plan"]["version"])
                        )
                        jobs.append(
                            {
                                "job_identity": job_id,
                                "state": job["state"],
                                "attempt_identity": job["attempt_id"],
                                "raw_sha256": None
                                if job["raw"] is None
                                else job["raw"]["sha256"],
                                "version_identity": None
                                if version is None
                                else version.identity,
                                "publication_occurrence": None
                                if success is None
                                else success["evidence"]["publication_occurrence"],
                                "read_as_of": _verified_read_as_of(
                                    job, version, batch["reference"]
                                ),
                                "classification": job["classification"],
                                "reconciliation": None
                                if job["reconciliation"] is None
                                else job["reconciliation"]["outcome"],
                            }
                        )
                    batches.append(
                        {
                            "key": key,
                            **batch,
                            "jobs": jobs,
                            "complete": any(
                                j["publication_occurrence"] is not None for j in jobs
                            ),
                        }
                    )
                result.append(
                    {
                        **d,
                        "batches": batches,
                        "completed_batches": sum(b["complete"] for b in batches),
                        "total_batches": len(
                            cpi_history.months(d["spec"]["start"], d["spec"]["end"])
                        )
                        * d["spec"]["cycles"],
                    }
                )
            return result

    def _check_wire(self, connection, qualification, at):
        instant(at)
        day = at.astimezone(UTC).date().isoformat()
        require(
            connection.execute("SELECT 1 FROM cpi_wire_pending").fetchone() is None,
            "cpi_plan_wire_reconciliation_required",
        )
        row = connection.execute(
            "SELECT reserved FROM cpi_quota WHERE day=?", (day,)
        ).fetchone()
        count = row[0] if row else 0
        require(
            count + 1 <= qualification.daily_wire_budget,
            "cpi_plan_daily_quota_exhausted",
        )
        # Usage resets at UTC midnight; physical-wire spacing never does.
        last = connection.execute(
            "SELECT last_dispatch FROM cpi_quota ORDER BY day DESC LIMIT 1"
        ).fetchone()
        if last:
            require(
                (at - datetime.fromisoformat(last[0])).total_seconds()
                >= qualification.minimum_spacing_seconds,
                "cpi_plan_rate_limited",
            )
        return day, count

    def check_wire(self, qualification, at):
        """Advisory early gate keeps an unavailable pending child unstarted."""
        with self.journal._connection() as connection:
            connection.execute("BEGIN")
            self._check_wire(connection, qualification, at)

    def reserve(self, qualification, at):
        """Charge one physical attempt before dispatch, without refunds."""
        require(not self.journal.read_only, "journal_read_only")
        with self.journal._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            day, count = self._check_wire(connection, qualification, at)
            connection.execute(
                "INSERT INTO cpi_quota VALUES (?, ?, ?) ON CONFLICT(day) "
                "DO UPDATE SET reserved=excluded.reserved,"
                "last_dispatch=excluded.last_dispatch",
                (day, count + 1, at.isoformat()),
            )
            connection.execute("INSERT INTO cpi_wire_pending VALUES (1, ?)", (day,))
        return day

    def finish_wire(self, day, at):
        """Space from return, conservatively covering any pre-dispatch delay.

        A crash or failed bookkeeping leaves the durable pending marker intact.
        No subsequent wire is safe until Host reconciliation; a tick never clears
        that uncertainty, refunds the charge or replays the interrupted request.
        """
        require(not self.journal.read_only, "journal_read_only")
        instant(at)
        with self.journal._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            require(
                connection.execute("SELECT day FROM cpi_wire_pending").fetchone()
                == (day,),
                "cpi_plan_wire_reconciliation_required",
            )
            last = connection.execute(
                "SELECT last_dispatch FROM cpi_quota WHERE day=?", (day,)
            ).fetchone()
            require(
                last is not None and at >= datetime.fromisoformat(last[0]),
                "cpi_plan_clock_reversal",
            )
            connection.execute(
                "UPDATE cpi_quota SET last_dispatch=? WHERE day=?",
                (at.isoformat(), day),
            )
            connection.execute("DELETE FROM cpi_wire_pending")


class _WireMeteringError(RuntimeError):
    """Local accounting failures are never transient provider failures."""


def _window_open(spec: PlanSpec, at: datetime) -> bool:
    instant(at)
    local_time = at.astimezone(ZoneInfo(spec.timezone)).strftime("%H:%M")
    return spec.window_start <= local_time < spec.window_end


@dataclass(frozen=True)
class _ScheduledTransport:
    delegate: object
    store: CpiPlanStore
    qualification: cpi_history.HistoryQualification
    clock: Callable[[], datetime]
    spec: PlanSpec

    def _finish(self, day):
        try:
            self.store.finish_wire(day, self.clock())
        except Exception:
            raise _WireMeteringError("cpi_plan_wire_metering_failed") from None

    def post(self, *args, **kwargs):
        try:
            at = self.clock()
            require(_window_open(self.spec, at), "cpi_plan_window_closed")
            day = self.store.reserve(self.qualification, at)
        except Exception:
            # In particular, SQLite OSError must not trigger transport retries.
            raise _WireMeteringError("cpi_plan_wire_admission_failed") from None
        try:
            # A delayed reservation/commit can itself cross midnight. Retain
            # that conservative charge, but never send on an uncharged UTC day.
            boundary = self.clock()
            instant(boundary)
            require(
                boundary.astimezone(UTC).date().isoformat() == day,
                "cpi_plan_wire_day_changed",
            )
            require(_window_open(self.spec, boundary), "cpi_plan_window_closed")
        except Exception:
            self._finish(day)
            raise _WireMeteringError("cpi_plan_wire_admission_failed") from None
        try:
            response = self.delegate.post(*args, **kwargs)
        except Exception:
            self._finish(day)
            raise
        # BaseException/process death deliberately retains the pending marker.
        self._finish(day)
        return response


@dataclass(frozen=True)
class CpiScheduledWorker:
    identity: str
    store: CpiPlanStore
    engine: CpiLocalJob
    acquirer: DirectCpiAcquirer
    preflight: Callable[[CpiJobSpec], None]

    def _complete_if_proven(self, document):
        snapshot = self.store.reader().snapshot(document["identity"])[0]
        if snapshot["completed_batches"] == snapshot["total_batches"]:
            document["state"] = "completed"
            with self.store.journal._connection() as connection, connection:
                self.store._write(connection, document, self.engine.clock())
        return document

    def tick(self, identity: str, *, enabled: bool = False):
        require(enabled is True, "cpi_job_explicit_opt_in_required")
        valid_identity(self.identity)
        require(
            self.store.journal.path.resolve() == self.engine.journal.path.resolve(),
            "cpi_plan_journal_mismatch",
        )
        require(
            type(self.acquirer.history_qualification)
            is cpi_history.HistoryQualification,
            "cpi_history_qualification_required",
        )
        with self.store.journal.executor("plan-" + digest(identity.encode())[:40]):
            d = self.store.get(identity)
            if d["state"] != "active":
                return d
            spec = PlanSpec(**d["spec"])
            now = self.engine.clock()
            instant(now)
            if (
                d["cycle"] >= spec.cycles
                and not d["retry_queue"]
                and d["inflight_key"] is None
            ):
                # Final cursor advancement can commit before completion does.
                # Settle from publication proof before dispatch due/window gates;
                # an exhausted scan alone never proves completion.
                return self._complete_if_proven(d)
            if (
                now < datetime.fromisoformat(d["next_due"])
                and not d["retry_queue"]
                and not d["inflight_key"]
            ) or not _window_open(spec, now):
                return d
            references = cpi_history.months(spec.start, spec.end)
            retry = bool(d["retry_queue"]) and d["inflight_key"] is None
            key = d["inflight_key"] or (
                d["retry_queue"][0] if retry else f"{d['cycle']}:{d['cursor']}"
            )
            batch = d["batches"].get(key)
            if batch is None:
                batch = {"reference": references[d["cursor"]], "jobs": []}
                d["batches"][key] = batch
            if not batch["jobs"] or retry:
                # Retries have an explicit requested window and a distinct child
                # identity. Recording mapping before submit handles crash gaps.
                job_id = (
                    "cpih-"
                    + digest(
                        (identity + ":" + key + ":" + str(len(batch["jobs"]))).encode()
                    )[:48]
                )
                batch["jobs"].append(job_id)
                d["inflight_key"] = key
                if retry:
                    d["retry_queue"].pop(0)
                with self.store.journal._connection() as connection, connection:
                    self.store._write(connection, d, now)
            job_id = batch["jobs"][-1]
            target = history_spec(batch["reference"], spec.effect_domain)
            self.engine.journal.submit(
                job_id, target.document(), "cpi-plan:" + job_id, now
            )
            job = self.engine.journal.get(job_id)
            if job["state"] == "pending":
                self.preflight(target)

                def start_allowed():
                    # Both Host preflights may consume the remaining window.
                    # Recheck after read-only quota admission too: an expired
                    # window retains this exact pending intent without Attempt.
                    if not _window_open(spec, self.engine.clock()):
                        return False
                    self.store.check_wire(
                        self.acquirer.history_qualification, self.engine.clock()
                    )
                    return _window_open(spec, self.engine.clock())

                job = self.engine.run(
                    job_id,
                    enabled=True,
                    preflight=self.preflight,
                    acquirer=replace(
                        self.acquirer,
                        transport=_ScheduledTransport(
                            self.acquirer.transport,
                            self.store,
                            self.acquirer.history_qualification,
                            self.engine.clock,
                            spec,
                        ),
                    ),
                    worker_identity=self.identity,
                    start_allowed=start_allowed,
                )
            elif job["state"] in {"running", "outcome_unknown"}:
                job = self.engine.reconcile(job_id, enabled=True)
            # A terminal UNKNOWN is recorded but blocks advancement until exact
            # reconciliation proves publication; a definite failed batch may be
            # left behind and requested explicitly through backfill.
            proven = job["last_success"] is not None
            settled = proven or job["state"] in {"failed", "cancelled"}
            if settled:
                d["inflight_key"] = None
            if settled and key == f"{d['cycle']}:{d['cursor']}":
                d["cursor"] += 1
                if d["cursor"] == len(references):
                    d["cursor"] = 0
                    d["cycle"] += 1
                    d["next_due"] = (
                        datetime.fromisoformat(d["next_due"])
                        + timedelta(seconds=spec.interval_seconds)
                    ).isoformat()
            # Keep a finished scan active when failures remain, for explicit repair.
            with self.store.journal._connection() as connection, connection:
                self.store._write(connection, d, self.engine.clock())
            return self._complete_if_proven(d)
