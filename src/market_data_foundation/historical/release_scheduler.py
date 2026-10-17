"""Single-host offline release Scheduler/Operator R1; explicit calls only.

No daemon, polling, cron, network, provider selection or demand/ensure entrypoint.
Owner-controlled inputs and consumed claims survive process restart. All raw and
canonical effects remain inside the two existing source-specific job engines.
"""

from dataclasses import asdict, dataclass
from datetime import datetime

from market_data_foundation.canonical.model import digest, instant, require
from market_data_foundation.historical.macro_job_journal import (
    MacroJobJournal,
    valid_identity,
)
from market_data_foundation.historical.release_handlers import (
    FOMC,
    HANDLER_TYPES,
    UNSUPPORTED,
)


def _json(value):
    import json

    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _load(value):
    import json

    return json.loads(value)


def _time(value):
    return datetime.fromisoformat(value)


@dataclass(frozen=True)
class ReleaseSchedule:
    identity: str
    owner: str
    family: str
    reference: str
    evidence_id: str
    not_before: datetime | None
    deadline: datetime | None
    source_release_at: datetime | None = None
    max_slots: int = 2
    max_attempts_per_slot: int = 2
    freshness_seconds: int = 86400

    def __post_init__(self):
        for value in (self.identity, self.owner, self.evidence_id):
            valid_identity(value)
        require(self.family in HANDLER_TYPES, "scheduler_family_unsupported")
        require(
            self.reference == HANDLER_TYPES[self.family].reference,
            "scheduler_reference_unsupported",
        )
        require(
            (self.not_before is None) == (self.deadline is None),
            "schedule_window_incomplete",
        )
        if self.not_before is not None:
            instant(self.not_before)
            instant(self.deadline)
            require(self.not_before < self.deadline, "schedule_window_invalid")
        if self.source_release_at is not None:
            instant(self.source_release_at)
            require(self.family == FOMC, "vintage_is_not_official_release")
            require(
                self.not_before is not None
                and self.source_release_at <= self.not_before,
                "schedule_precedes_release",
            )
        for value, maximum in (
            (self.max_slots, 16),
            (self.max_attempts_per_slot, 2),
            (self.freshness_seconds, 31 * 86400),
        ):
            require(
                type(value) is int and 1 <= value <= maximum, "schedule_budget_invalid"
            )

    def document(self):
        doc = asdict(self)
        for key in ("not_before", "deadline", "source_release_at"):
            doc[key] = None if doc[key] is None else doc[key].isoformat()
        return doc


class ReleaseJournal(MacroJobJournal):
    """Reuse trusted-local SQLite durability and flock; separate scheduler file.

    The source jobs keep their own Macro journals beneath handler roots. Reads
    never initialize or repair either database. Caller holds release-control for
    each mutation. Effects and recovery share a nonblocking family lock after
    the per-slot lock, held through source execution and result observation.
    """

    filename = "macro-release-operator.sqlite3"

    def __init__(self, directory, *, read_only=False):
        super().__init__(directory, read_only=read_only)
        if not read_only:
            with self._connection() as connection, connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS release_schedules (
                        identity TEXT PRIMARY KEY, document TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS release_slots (
                        identity TEXT PRIMARY KEY,
                        family TEXT NOT NULL,
                        evidence TEXT NOT NULL,
                        acquired_at TEXT NOT NULL,
                        document TEXT NOT NULL,
                        UNIQUE(family, evidence), UNIQUE(family, acquired_at)
                    );
                    CREATE TABLE IF NOT EXISTS release_audit (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        event TEXT NOT NULL
                    );
                    """
                )

    def schedules(self):
        with self._connection() as connection:
            return tuple(
                _load(r[0])
                for r in connection.execute(
                    "SELECT document FROM release_schedules ORDER BY rowid"
                )
            )

    def slots(self):
        with self._connection() as connection:
            return tuple(
                _load(r[0])
                for r in connection.execute(
                    "SELECT document FROM release_slots ORDER BY rowid"
                )
            )

    def schedule(self, identity):
        valid_identity(identity)
        found = [d for d in self.schedules() if d["spec"]["identity"] == identity]
        require(bool(found), "schedule_not_found")
        return found[0]

    def slot(self, identity):
        valid_identity(identity)
        found = [d for d in self.slots() if d["identity"] == identity]
        require(bool(found), "release_slot_not_found")
        return found[0]

    def audit(self):
        with self._connection() as connection:
            return tuple(
                _load(r[0])
                for r in connection.execute(
                    "SELECT event FROM release_audit ORDER BY sequence"
                )
            )

    def _save(self, kind, identity, document, at, actor, action):
        instant(at)
        valid_identity(actor)
        require(not self.read_only, "journal_read_only")
        table = {"schedule": "release_schedules", "slot": "release_slots"}[kind]
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            old = connection.execute(
                f"SELECT document FROM {table} WHERE identity = ?", (identity,)
            ).fetchone()
            if old:
                require(
                    at >= _time(_load(old[0])["updated_at"]), "schedule_clock_reversal"
                )
            document = document | {"updated_at": at.isoformat()}
            if kind == "schedule":
                connection.execute(
                    "INSERT INTO release_schedules VALUES (?, ?) "
                    "ON CONFLICT(identity) DO UPDATE SET document=excluded.document",
                    (identity, _json(document)),
                )
            elif old:
                connection.execute(
                    "UPDATE release_slots SET document=? WHERE identity=?",
                    (_json(document), identity),
                )
            else:
                desc = document["descriptor"]
                connection.execute(
                    "INSERT INTO release_slots VALUES (?, ?, ?, ?, ?)",
                    (
                        identity,
                        desc["family"],
                        desc["evidence_sha256"],
                        _time(desc["acquired_at"]).isoformat(),
                        _json(document),
                    ),
                )
            connection.execute(
                "INSERT INTO release_audit(event) VALUES (?)",
                (
                    _json(
                        {
                            "kind": kind,
                            "identity": identity,
                            "actor": actor,
                            "action": action,
                            "at": at.isoformat(),
                            "document": document,
                        }
                    ),
                ),
            )
        return document


class ReleaseOperator:
    def __init__(self, journal: ReleaseJournal, handlers: dict, clock):
        require(type(journal) is ReleaseJournal, "release_journal_required")
        require(
            all(
                f in HANDLER_TYPES and type(h) is HANDLER_TYPES[f]
                for f, h in handlers.items()
            ),
            "scheduler_concrete_handler_required",
        )
        # Separate stable namespaces are part of the restart contract.
        roots = [h.root.resolve() for h in handlers.values()]
        require(len(roots) == len(set(roots)), "scheduler_handler_roots_overlap")
        self.journal, self.handlers, self.clock = journal, dict(handlers), clock

    @staticmethod
    def _enabled(enabled):
        require(enabled is True, "scheduler_explicit_opt_in_required")

    def _owner(self, schedule, actor):
        valid_identity(actor)
        require(schedule["spec"]["owner"] == actor, "scheduler_owner_required")

    def _handler(self, family):
        require(family in self.handlers, "scheduler_handler_unbound")
        handler = self.handlers[family]
        handler.check()
        require(
            all(
                s["handler_root"] == str(handler.root.resolve())
                for s in self.journal.schedules()
                if s["spec"]["family"] == family
            ),
            "scheduler_handler_binding_drift",
        )
        return handler

    def capabilities(self):
        return {
            **{
                f: {
                    "supported": f in self.handlers,
                    "mode": "offline_retained_only",
                    "activation": False,
                }
                for f in HANDLER_TYPES
            },
            **{
                f: {"supported": False, "reason": why} for f, why in UNSUPPORTED.items()
            },
        }

    def register(self, spec: ReleaseSchedule, *, actor, enabled=False):
        self._enabled(enabled)
        require(type(spec) is ReleaseSchedule, "release_schedule_required")
        require(spec.owner == actor, "scheduler_owner_required")
        with self.journal.executor("release-control"):
            handler = self._handler(spec.family)
            existing = [
                s
                for s in self.journal.schedules()
                if s["spec"]["identity"] == spec.identity
            ]
            if existing:
                require(
                    existing[0]["spec"] == spec.document(), "schedule_identity_reused"
                )
                return existing[0]
            return self.journal._save(
                "schedule",
                spec.identity,
                {
                    "spec": spec.document(),
                    "paused": False,
                    "handler_root": str(handler.root.resolve()),
                },
                self.clock(),
                actor,
                "register_offline_no_activation",
            )

    def pause(self, identity, *, actor, enabled=False):
        return self._pause(identity, True, actor, enabled)

    def resume(self, identity, *, actor, enabled=False):
        return self._pause(identity, False, actor, enabled)

    def _pause(self, identity, paused, actor, enabled):
        self._enabled(enabled)
        with self.journal.executor("release-control"):
            schedule = self.journal.schedule(identity)
            self._owner(schedule, actor)
            return self.journal._save(
                "schedule",
                identity,
                schedule | {"paused": paused},
                self.clock(),
                actor,
                "pause" if paused else "resume_no_activation",
            )

    def _window(self, schedule, descriptor, action, at):
        spec = schedule["spec"]
        require(not schedule["paused"], "schedule_paused")
        require(spec["not_before"] is not None, "schedule_time_unknown")
        start, end = _time(spec["not_before"]), _time(spec["deadline"])
        require(
            start <= at and (action == "backfill" or at <= end),
            "schedule_outside_window",
        )
        require(
            start
            <= _time(descriptor["requested_at"])
            <= _time(descriptor["acquired_at"])
            <= min(end, at),
            "slot_evidence_outside_window",
        )
        require(
            spec["source_release_at"] == descriptor["source_release_at"],
            "schedule_release_evidence_mismatch",
        )
        if spec["family"] == FOMC:
            require(spec["source_release_at"] is not None, "schedule_release_unknown")

    def trigger(
        self, schedule_id, identity, candidate, *, actor, action="manual", enabled=False
    ):
        """Register durable intent only; repeated identities never execute a job."""
        self._enabled(enabled)
        valid_identity(identity)
        require(
            action in {"manual", "scheduled", "refresh", "backfill"},
            "trigger_action_invalid",
        )
        with self.journal.executor("release-control"):
            schedule = self.journal.schedule(schedule_id)
            self._owner(schedule, actor)
            handler = self._handler(schedule["spec"]["family"])
            at = self.clock()
            descriptor = handler.describe(candidate, at)
            slots = self.journal.slots()
            old = [s for s in slots if s["identity"] == identity]
            if old:
                require(
                    old[0]["schedule_id"] == schedule_id
                    and old[0]["descriptor"] == descriptor
                    and old[0]["action"] == action,
                    "release_slot_identity_reused",
                )
                return old[0]
            self._window(schedule, descriptor, action, at)
            family_slots = [
                s for s in slots if s["descriptor"]["family"] == handler.family
            ]
            require(
                all(
                    s["state"] not in {"claimed", "outcome_unknown"}
                    or s.get("reconciliation", {}).get("outcome")
                    in {"confirmed_published", "confirmed_not_published"}
                    and bool(s.get("reconciliation", {}).get("scheduler_evidence"))
                    for s in family_slots
                ),
                "scheduler_family_unknown_requires_reconcile",
            )
            require(
                not any(
                    s["descriptor"]["evidence_sha256"] == descriptor["evidence_sha256"]
                    or _time(s["descriptor"]["acquired_at"])
                    == _time(descriptor["acquired_at"])
                    for s in family_slots
                ),
                "release_acquisition_already_claimed",
            )
            if action == "refresh":
                successes = [
                    s["last_success"] for s in family_slots if s["last_success"]
                ]
                require(
                    bool(successes)
                    and _time(descriptor["acquired_at"])
                    > max(_time(s["acquired_at"]) for s in successes),
                    "refresh_newer_acquisition_required",
                )
            require(
                sum(s["schedule_id"] == schedule_id for s in slots)
                < schedule["spec"]["max_slots"],
                "schedule_slot_budget_exhausted",
            )
            job_id = "r" + digest(identity.encode())[:32] + "-a1"
            return self.journal._save(
                "slot",
                identity,
                {
                    "identity": identity,
                    "schedule_id": schedule_id,
                    "descriptor": descriptor,
                    "action": action,
                    "state": "pending",
                    "attempt_jobs": [job_id],
                    "cancel_requested": False,
                    "last_success": None,
                    "reconciliation": {},
                    "classification": "registered",
                    "created_at": at.isoformat(),
                    "budget": {
                        "provider_requests": 0,
                        "attempts": schedule["spec"]["max_attempts_per_slot"],
                        "raw_creates_per_attempt": handler.raw_budget,
                        "ledger_handoffs_per_attempt": 1,
                        "publications_per_attempt": 1,
                    },
                },
                at,
                actor,
                action,
            )

    def _save_slot(self, identity, actor, action, **changes):
        with self.journal.executor("release-control"):
            slot = self.journal.slot(identity)
            return self.journal._save(
                "slot",
                identity,
                slot | changes,
                self.clock(),
                actor,
                action,
            )

    def _access(self, identity, actor):
        slot = self.journal.slot(identity)
        schedule = self.journal.schedule(slot["schedule_id"])
        self._owner(schedule, actor)
        return slot, schedule, self._handler(schedule["spec"]["family"])

    def _execution_guard(self, slot):
        others = [
            s
            for s in self.journal.slots()
            if s["identity"] != slot["identity"]
            and s["descriptor"]["family"] == slot["descriptor"]["family"]
        ]
        require(
            all(
                s["state"] not in {"claimed", "outcome_unknown"}
                or s["reconciliation"].get("outcome")
                in {"confirmed_published", "confirmed_not_published"}
                and bool(s["reconciliation"].get("scheduler_evidence"))
                for s in others
            ),
            "scheduler_family_unknown_requires_reconcile",
        )
        # The existing engines verify exact == latest-as-of. They cannot safely
        # publish an older acquisition beneath an already newer local version.
        require(
            all(
                _time(s["last_success"]["acquired_at"])
                < _time(slot["descriptor"]["acquired_at"])
                for s in others
                if s["last_success"]
            ),
            "older_acquisition_backfill_unsupported",
        )

    def run(self, identity, candidate, *, actor, enabled=False):
        """One consumed invocation; no retry after any interrupted claim."""
        self._enabled(enabled)
        with self.journal.executor("slot-" + digest(identity.encode())[:32]):
            with self.journal.executor("release-control"):
                slot, schedule, handler = self._access(identity, actor)
                if slot["state"] != "pending":
                    return slot
                descriptor = handler.describe(candidate, self.clock())
                require(
                    descriptor == slot["descriptor"], "release_slot_candidate_mismatch"
                )
                self._window(schedule, descriptor, slot["action"], self.clock())
                self._execution_guard(slot)
                slot = self.journal._save(
                    "slot",
                    identity,
                    slot | {"state": "claimed", "classification": "execution_intent"},
                    self.clock(),
                    actor,
                    "consume_attempt",
                )
            try:
                engine = handler.engine(identity, create=True)
                job_id = slot["attempt_jobs"][-1]
                if len(slot["attempt_jobs"]) == 1:
                    engine.submit(job_id, handler.spec(descriptor))
                if self.journal.slot(identity)["cancel_requested"]:
                    engine.journal.cancel(job_id, self.clock())

                def preflight(_):
                    self._execution_guard(self.journal.slot(identity))
                    if len(slot["attempt_jobs"]) > 1:
                        predecessor = engine.journal.get(slot["attempt_jobs"][-2])
                        handler.recovery_evidence(
                            slot | {"attempt_jobs": slot["attempt_jobs"][:-1]},
                            predecessor,
                        )
                    require(
                        not self.journal.slot(identity)["cancel_requested"],
                        "slot_cancelled",
                    )
                    self._window(
                        self.journal.schedule(slot["schedule_id"]),
                        descriptor,
                        slot["action"],
                        self.clock(),
                    )

                with self.journal.executor("family-" + handler.family):
                    self._execution_guard(self.journal.slot(identity))
                    result = engine.run(
                        job_id,
                        enabled=True,
                        preflight=preflight,
                        retained=candidate,
                        effect_preflight=preflight,
                    )
                    return self._finish(identity, actor, handler, result)
            except Exception:
                return self._save_slot(
                    identity,
                    actor,
                    "execution_unresolved",
                    state="outcome_unknown",
                    classification="handler_or_observation_unresolved",
                    reconciliation={"outcome": "unresolved"},
                )

    def _finish(self, identity, actor, handler, result):
        require(
            result["state"] in {"succeeded", "failed", "cancelled", "outcome_unknown"},
            "consumed_attempt_must_not_return_to_pending",
        )
        slot = self.journal.slot(identity)
        recovery = dict(result["reconciliation"] or {})
        if recovery.get("outcome") in {
            "confirmed_published",
            "confirmed_not_published",
        }:
            recovery["scheduler_evidence"] = handler.recovery_evidence(slot, result)
        success = None
        if (
            result["last_success"] is not None
            and recovery.get("outcome") == "confirmed_published"
            and recovery.get("readback_protection_pending") is False
        ):
            success = handler.observe(result, slot["descriptor"], self.clock())
        return self._save_slot(
            identity,
            actor,
            "observe_job",
            state=result["state"],
            classification=result["classification"],
            reconciliation=recovery,
            last_success=success,
            job_state=result["state"],
            attempt_observations=slot.get("attempt_observations", {})
            | {
                result["job_id"]: {
                    key: result[key]
                    for key in (
                        "state",
                        "stage",
                        "attempt_id",
                        "attempts",
                        "effects",
                        "classification",
                        "reconciliation",
                        "last_success",
                    )
                }
            },
        )

    def cancel(self, identity, *, actor, enabled=False):
        self._enabled(enabled)
        with self.journal.executor("release-control"):
            slot, _, handler = self._access(identity, actor)
            if slot["state"] not in {"pending", "claimed"}:
                return slot
            slot = self.journal._save(
                "slot",
                identity,
                slot
                | {
                    "cancel_requested": True,
                    "state": "cancelled" if slot["state"] == "pending" else "claimed",
                },
                self.clock(),
                actor,
                "cancel",
            )
        # No slot lock: existing lifecycle sees cancellation between effects.
        if slot["state"] == "claimed":
            try:
                handler.engine(identity).journal.cancel(
                    slot["attempt_jobs"][-1], self.clock()
                )
            except Exception:
                # Claim remains consumed even if registration has not happened yet.
                pass
        return slot

    def reconcile(self, identity, *, actor, enabled=False):
        """Explicit observation only; never run, acquire, PUT or publish."""
        self._enabled(enabled)
        with self.journal.executor("slot-" + digest(identity.encode())[:32]):
            _, _, admission_handler = self._access(identity, actor)
            with self.journal.executor("family-" + admission_handler.family):
                slot, _, handler = self._access(identity, actor)
                if slot["state"] not in {"claimed", "outcome_unknown"}:
                    return slot
                self._save_slot(
                    identity,
                    actor,
                    "reconcile_intent",
                    state="outcome_unknown",
                    last_success=None,
                    reconciliation={"outcome": "unresolved"},
                )
                try:
                    engine = handler.engine(identity)
                    result = engine.reconcile(slot["attempt_jobs"][-1], enabled=True)
                    return self._finish(identity, actor, handler, result)
                except Exception:
                    return self._save_slot(
                        identity,
                        actor,
                        "reconcile_unresolved",
                        state="outcome_unknown",
                        classification="reconciliation_unresolved",
                        reconciliation={"outcome": "unresolved"},
                    )

    def repair(self, identity, *, actor, enabled=False):
        """One explicit zero-Provider retained successor, gated by source engine.

        Failed acquisition/preflight without verified raw is deliberately not
        retryable. No scheduler-level automatic retry exists (retry budget zero).
        """
        self._enabled(enabled)
        with self.journal.executor("slot-" + digest(identity.encode())[:32]):
            _, _, admission_handler = self._access(identity, actor)
            with self.journal.executor("family-" + admission_handler.family):
                with self.journal.executor("release-control"):
                    slot, schedule, handler = self._access(identity, actor)
                    require(not schedule["paused"], "schedule_paused")
                    self._execution_guard(slot)
                    require(
                        slot["state"] in {"failed", "cancelled", "outcome_unknown"}
                        and slot["last_success"] is None
                        and len(slot["attempt_jobs"]) < slot["budget"]["attempts"],
                        "slot_repair_budget_or_state_rejected",
                    )
                    # Validate before consuming the repair budget; source engine does
                    # the authoritative atomic same-plan/raw reservation below.
                    engine = handler.engine(identity)
                    old = engine.journal.get(slot["attempt_jobs"][-1])
                    require(
                        old["reconciliation"] is not None
                        and old["reconciliation"].get("outcome")
                        == "confirmed_not_published"
                        and old["reconciliation"].get("raw_state") == "verified",
                        "slot_repair_reconciliation_required",
                    )
                    # Fresh reconciliation itself acquires protection. Invalidate
                    # the earlier certificate before it can fail or be interrupted.
                    slot = self.journal._save(
                        "slot",
                        identity,
                        slot
                        | {
                            "state": "outcome_unknown",
                            "reconciliation": {"outcome": "unresolved"},
                            "classification": "repair_fresh_evidence_intent",
                        },
                        self.clock(),
                        actor,
                        "repair_fresh_evidence_intent",
                    )
                    old = engine.reconcile(old["job_id"], enabled=True)
                    require(
                        (old["reconciliation"] or {}).get("outcome")
                        == "confirmed_not_published",
                        "slot_repair_fresh_absence_required",
                    )
                    handler.recovery_evidence(slot, old)
                    new_id = slot["attempt_jobs"][0].removesuffix("-a1") + "-a2"
                    old_id = slot["attempt_jobs"][-1]
                    self.journal._save(
                        "slot",
                        identity,
                        slot
                        | {
                            "state": "claimed",
                            "attempt_jobs": slot["attempt_jobs"] + [new_id],
                            "classification": "repair_intent",
                            "cancel_requested": False,
                            "reconciliation": {},
                        },
                        self.clock(),
                        actor,
                        "consume_repair_budget",
                    )
                try:
                    engine.repair(old_id, new_id, enabled=True)
                    return self._save_slot(
                        identity, actor, "repair_registered", state="pending"
                    )
                except Exception:
                    return self._save_slot(
                        identity,
                        actor,
                        "repair_unresolved",
                        state="outcome_unknown",
                        classification="repair_registration_unresolved",
                    )

    def status(self, schedule_id, *, at):
        """Pure local projection. No handler, job, lock, file creation or refresh."""
        instant(at)
        schedule = self.journal.schedule(schedule_id)
        require(at >= _time(schedule["updated_at"]), "status_clock_reversal")
        slots = [s for s in self.journal.slots() if s["schedule_id"] == schedule_id]
        require(
            all(at >= _time(s["updated_at"]) for s in slots), "status_clock_reversal"
        )
        successes = [s["last_success"] for s in slots if s["last_success"]]
        last = (
            max(successes, key=lambda s: _time(s["acquired_at"])) if successes else None
        )
        unknown = [
            s["identity"]
            for s in slots
            if s["state"] in {"claimed", "outcome_unknown"}
            and (
                s["reconciliation"].get("outcome")
                not in {"confirmed_published", "confirmed_not_published"}
                or not s["reconciliation"].get("scheduler_evidence")
            )
        ]
        spec = schedule["spec"]
        age = (
            None if last is None else (at - _time(last["acquired_at"])).total_seconds()
        )
        stale = age is not None and age > spec["freshness_seconds"]
        calendar_unknown = spec["not_before"] is None or (
            spec["family"] == FOMC and spec["source_release_at"] is None
        )
        overdue = (
            spec["deadline"] is not None
            and at > _time(spec["deadline"])
            and last is None
        )
        alerts = []
        if unknown:
            alerts.append("outcome_unknown")
        if calendar_unknown:
            alerts.append("schedule_time_unknown")
        if stale:
            alerts.append("stale_acquisition")
        if overdue:
            alerts.append("coverage_missing_after_deadline")
        if any(
            s["state"] in {"failed", "outcome_unknown"} and not s["last_success"]
            for s in slots
        ):
            alerts.append("operator_attention_required")
        return {
            "schedule": schedule,
            "slots": slots,
            "last_success": last,
            "coverage": [] if last is None else last["coverage"],
            "unknown_slots": unknown,
            "schedule_time_unknown": calendar_unknown,
            "source_release_unknown": spec["source_release_at"] is None,
            "acquisition_age_seconds": age,
            "freshness": "unknown" if age is None else "stale" if stale else "fresh",
            "alerts": alerts,
            "activation": False,
            "retry_policy": "no_automatic_retry_explicit_retained_repair_only",
        }
