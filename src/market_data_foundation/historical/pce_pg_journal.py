"""Authoritative PG Job/Attempt/event candidate, with no automatic DDL or IO.

A trusted Host supplies fresh exclusive connections and independently verified
schema identity. Nothing connects until an explicitly enabled method is called.
State commits never share the canonical writer transaction. No mirror, fallback,
repair, takeover or retry path exists. Read-only instances issue no state writes.
"""

import copy
import hashlib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from market_data_foundation.canonical.model import ContractError, instant, require
from market_data_foundation.historical.macro_job_journal import (
    _TERMINAL,
    _TRANSITIONS,
    valid_identity,
)
from market_data_foundation.historical.pce_pg_binding import (
    BACKFILL_SCOPE,
    DOMAIN,
    SCHEMA,
    PcePgBinding,
    backfill_identity,
    check_backfill_parent,
    check_job,
    job_identity,
)
from market_data_foundation.storage.postgres_runtime_config import PgConfig


def _idle(connection):
    return (
        not connection.closed
        and connection.info.transaction_status is TransactionStatus.IDLE
        and connection._num_transactions == 0
        and connection._tpc is None
        and connection._pipeline is None
        and getattr(connection, "_pool", None) is None
    )


class PgJournalUnavailable(ContractError):
    """Safe classification only; no driver details, credentials or exception chain."""


@dataclass(frozen=True)
class PgJournalIdentity:
    schema_owner: str
    schema_oid: int

    def __post_init__(self):
        valid_identity(self.schema_owner.replace("_", "-"))
        require(
            type(self.schema_oid) is int and self.schema_oid > 0,
            "pce_pg_schema_identity_required",
        )


_MUTABLE = {
    "state",
    "stage",
    "attempt_id",
    "attempts",
    "started_at",
    "finished_at",
    "worker_identity",
    "classification",
    "failure_stage",
    "plan",
    "raw",
    "effects",
    "reconciliation",
    "writer_session",
}


class PostgresJobJournal:
    """Bounded one-scope store. Construction neither connects nor initializes.

    connector must accept PgConfig options, add Host-owned authentication and
    return an exclusive nonpooled connection. Authentication/CSRF and independent
    schema/ACL qualification remain Host obligations, not claims of this class.
    An ambiguous write poisons this instance; use a fresh reader for observation.
    """

    def __init__(
        self,
        connector,
        profile: PgConfig,
        identity: PgJournalIdentity,
        binding: PcePgBinding,
        *,
        enabled=False,
        read_only=False,
    ):
        require(type(binding) is PcePgBinding, "pce_pg_binding_required")
        require(type(profile) is PgConfig, "pce_pg_profile_required")
        require(profile.sslmode == "verify-full", "pce_verify_full_required")
        require(type(identity) is PgJournalIdentity, "pce_pg_schema_identity_required")
        self.connector, self.profile, self.identity = connector, profile, identity
        self.binding, self.enabled, self.read_only = binding, enabled, read_only
        self._poisoned = False
        self._executor_connection = None
        self._executor_job = None
        self._executor_owner = self
        self._excluded = []

    def exclude_connection(self, connection):
        self._excluded.append(connection)

    def reader(self):
        reader = type(self)(
            self.connector,
            self.profile,
            self.identity,
            self.binding,
            enabled=self.enabled,
            read_only=True,
        )
        # Share registrations so existing descendants also exclude late writers.
        reader._excluded = self._excluded
        # Resolve ownership at use time, including readers derived before a lock
        # is acquired and readers derived from another reader.
        reader._executor_owner = self._executor_owner
        return reader

    @contextmanager
    def _connection(self):
        require(self.enabled is True, "pce_pg_explicit_opt_in_required")
        connection = None
        failure = False
        borrowed = False
        try:
            connection = self.connector(
                **self.profile.connection_kwargs(),
                autocommit=True,
                connect_timeout=5,
                options="-c statement_timeout=3000",
            )
            borrowed = any(connection is c for c in self._excluded) or (
                connection is self._executor_owner._executor_connection
            )
            require(
                not borrowed,
                "pce_pg_shared_connection_forbidden",
            )
            require(
                _idle(connection) and connection.autocommit is True,
                "pce_pg_exclusive_idle_connection_required",
            )
            self.binding.target.check_connection(connection)
            require(
                connection.info.user == self.profile.user
                and connection.pgconn.ssl_in_use is True
                and connection.info.get_parameters().get("sslmode")
                == self.profile.sslmode
                and (
                    self.profile.sslrootcert is None
                    or connection.info.get_parameters().get("sslrootcert")
                    == str(self.profile.sslrootcert)
                ),
                "pce_pg_connection_identity_mismatch",
            )
            yield connection
        except ContractError:
            raise
        except Exception:
            failure = True
        finally:
            if connection is not None and not borrowed:
                try:
                    connection.close()
                except Exception:
                    failure = True
        if failure:
            raise PgJournalUnavailable("pce_pg_storage_unavailable") from None

    def _check_schema(self, c, write):
        row = c.execute(
            "SELECT current_user, session_user, current_database(), "
            "host(inet_server_addr()), inet_server_port()"
        ).fetchone()
        require(
            row
            == (
                self.profile.user,
                self.profile.user,
                self.binding.target.database,
                self.binding.target.host,
                5432,
            ),
            "pce_pg_connection_identity_mismatch",
        )
        rows = c.execute(
            "SELECT n.oid, pg_catalog.pg_get_userbyid(n.nspowner), c.relname, "
            "c.relkind, c.relpersistence, c.relrowsecurity, c.relforcerowsecurity, "
            "pg_catalog.pg_get_userbyid(c.relowner), "
            "pg_catalog.has_schema_privilege(n.oid, 'USAGE'), "
            "(pg_catalog.has_table_privilege(c.oid, 'SELECT') AND "
            "(NOT %s OR (pg_catalog.has_table_privilege(c.oid, 'INSERT') AND "
            "(c.relname = 'job_events' OR "
            "pg_catalog.has_table_privilege(c.oid, 'UPDATE'))))) "
            "FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_class c "
            "ON c.relnamespace = n.oid WHERE n.nspname = %s "
            "AND c.relname IN ('jobs','attempts','job_events') "
            "ORDER BY c.relname LIMIT 4",
            (write, SCHEMA),
        ).fetchall()
        require(
            rows
            == [
                (
                    self.identity.schema_oid,
                    self.identity.schema_owner,
                    t,
                    "r",
                    "p",
                    False,
                    False,
                    self.identity.schema_owner,
                    True,
                    True,
                )
                for t in ("attempts", "job_events", "jobs")
            ],
            "pce_pg_schema_or_permission_mismatch",
        )

    @contextmanager
    def _transaction(self, *, write=False):
        if write:
            require(not self.read_only, "journal_read_only")
            require(not self._poisoned, "pce_pg_observation_required")
        failure = None
        try:
            with self._connection() as c:
                try:
                    c.execute(
                        "BEGIN ISOLATION LEVEL READ COMMITTED READ WRITE"
                        if write
                        else "BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY"
                    )
                    c.execute("SET LOCAL search_path = pg_catalog")
                    c.execute("SET LOCAL statement_timeout = '3000ms'")
                    c.execute("SET LOCAL lock_timeout = '2000ms'")
                    c.execute(
                        "SET LOCAL idle_in_transaction_session_timeout = '5000ms'"
                    )
                    if write:
                        c.execute("SET LOCAL synchronous_commit = on")
                    self._check_schema(c, write)
                    yield c
                except ContractError:
                    c.rollback()
                    raise
                except Exception:
                    # Driver failure never grants permission to retry an effect.
                    failure = (
                        "pce_pg_transaction_outcome_unknown"
                        if write
                        else "pce_pg_storage_unavailable"
                    )
                else:
                    try:
                        if write:
                            c.commit()
                        else:
                            c.rollback()
                        require(_idle(c), "pce_pg_transaction_not_idle")
                    except Exception:
                        failure = (
                            "pce_pg_commit_outcome_unknown"
                            if write
                            else "pce_pg_storage_unavailable"
                        )
                if failure and write:
                    self._poisoned = True
        except PgJournalUnavailable:
            if write:
                self._poisoned = True
            raise
        if failure:
            raise PgJournalUnavailable(failure) from None

    @contextmanager
    def executor(self, identity):
        valid_identity(identity)
        require(
            not self.read_only and not self._poisoned, "pce_pg_executor_unavailable"
        )
        require(self._executor_connection is None, "job_executor_busy")
        with self._connection() as c:
            held = c.execute(
                "SELECT pg_try_advisory_lock(1346585925, 202607)"
            ).fetchone()[0]
            require(held is True, "job_executor_busy")
            self._executor_connection, self._executor_job = c, identity
            try:
                yield
            finally:
                self._executor_connection = self._executor_job = None
                # Closing this dedicated connection releases the session lock.

    def _executor(self, identity):
        require(
            self._executor_job == identity and self._executor_connection is not None,
            "pce_pg_executor_required",
        )
        c = self._executor_connection
        require(not c.closed, "pce_pg_executor_lost")
        # A lost lock connection stops the caller before the next durable effect.
        failed = False
        try:
            c.execute("SELECT 1").fetchone()
        except Exception:
            failed = True
        if failed:
            self._poisoned = True
            raise PgJournalUnavailable("pce_pg_executor_lost") from None

    def _read(self, c, identity, *, locked=False):
        row = c.execute(
            f"SELECT document, event_count FROM {SCHEMA}.jobs WHERE identity = %s"
            + (" FOR UPDATE" if locked else ""),
            (identity,),
        ).fetchone()
        require(row is not None, "job_not_found")
        document, count = row
        require(
            document["binding"] == self.binding.document(), "pce_pg_binding_mismatch"
        )
        check_job(document, identity)
        if identity == backfill_identity():
            require(
                self.identity.schema_oid == 141635, "pce_backfill_schema_oid_mismatch"
            )
            self._identities(c)
            parent, parent_count = self._read(c, job_identity())
            self._verify_history(c, parent, parent_count)
            check_backfill_parent(parent)
        return copy.deepcopy(document), count

    def _event(self, c, document, count):
        require(count <= 256, "job_event_budget_exhausted")
        c.execute(
            f"INSERT INTO {SCHEMA}.job_events (job_id, sequence, event) "
            "VALUES (%s,%s,%s)",
            (document["job_id"], count, Jsonb(document)),
        )

    def _identities(self, c):
        rows = c.execute(f"SELECT identity FROM {SCHEMA}.jobs LIMIT 3").fetchall()
        require(len(rows) <= 2, "job_query_budget_exceeded")
        ids = {row[0] for row in rows}
        require(
            ids <= {job_identity(), backfill_identity()}, "job_scope_already_registered"
        )
        require(not ids or job_identity() in ids, "pce_backfill_parent_missing")
        return rows

    def _backfill_storage(self, c):
        # The actual DEV schema retains UNIQUE(scope) to enforce exactly one
        # parent slot and one successor slot. Check the exact-two identity/scope
        # migration, rather than pretending the original fixed-scope table is
        # eligible or disabling the unique constraint.
        require(self.identity.schema_oid == 141635, "pce_backfill_schema_oid_mismatch")
        defs = dict(
            c.execute(
                "SELECT conname, pg_catalog.pg_get_constraintdef(oid) "
                "FROM pg_catalog.pg_constraint WHERE conrelid=%s::regclass "
                "AND conname IN ('jobs_scope_key','jobs_scope_check')",
                (f"{SCHEMA}.jobs",),
            ).fetchall()
        )
        # The exact approved PG18 migration is pinned by canonical
        # pg_get_constraintdef bytes. Mere marker inclusion is unsafe:
        # adding a third scope or swapping parent/child also contains them.
        expected_sha = {
            "jobs_scope_check": (
                "e5392056dd44ea55d9b0449e133011b55e13e5f53d91f7f689b8ac3959e4803f"
            ),
            "jobs_scope_key": (
                "c873ec4010dea78cfd364442ad0dc932cda450f9ecfe64904a9e7257dfb753f2"
            ),
        }
        require(
            set(defs) == set(expected_sha)
            and all(
                isinstance(defs[name], str)
                and hashlib.sha256(defs[name].encode("utf-8")).hexdigest() == digest
                for name, digest in expected_sha.items()
            ),
            "pce_backfill_exact_two_scope_migration_required",
        )

    def backfill_eligibility(self):
        with self._transaction() as c:
            rows = self._identities(c)
            if (job_identity(),) not in rows:
                return "pce_backfill_parent_missing"
            parent, count = self._read(c, job_identity())
            self._verify_history(c, parent, count)
            try:
                check_backfill_parent(parent)
                if (backfill_identity(),) in rows:
                    return "pce_backfill_successor_slot_used"
                self._backfill_storage(c)
            except ContractError as error:
                return str(error)
        return None

    def submit(self, identity, spec, scope, at):
        require(identity == job_identity(), "pce_pg_job_identity_mismatch")
        return self._submit(identity, spec, scope, at, backfill=False)

    def submit_backfill(self, identity, parent_job_id, spec, scope, at):
        require(identity == backfill_identity(), "pce_pg_job_identity_mismatch")
        require(parent_job_id == job_identity(), "pce_pg_parent_mismatch")
        return self._submit(identity, spec, scope, at, backfill=True)

    def _submit(self, identity, spec, scope, at, *, backfill):
        valid_identity(identity)
        instant(at)
        self.binding.check_spec(spec, scope)
        with self._transaction(write=True) as c:
            c.execute(f"LOCK TABLE {SCHEMA}.jobs IN SHARE ROW EXCLUSIVE MODE")
            rows = self._identities(c)
            if backfill:
                parent, count = self._read(c, job_identity(), locked=True)
                self._verify_history(c, parent, count)
                check_backfill_parent(parent)
            if (identity,) in rows:
                document, _ = self._read(c, identity)
                require(document["spec"] == spec, "job_identity_reused")
                # Replay observes this exact slot, even after failure/UNKNOWN.
            else:
                if backfill:
                    self._backfill_storage(c)
                else:
                    require(not rows, "job_scope_already_registered")
                document = dict(
                    job_id=identity,
                    **({"parent_job_id": job_identity()} if backfill else {}),
                    spec=copy.deepcopy(spec),
                    binding=self.binding.document(),
                    state="pending",
                    stage="registered",
                    attempt_id=None,
                    attempts=0,
                    created_at=at.isoformat(),
                    updated_at=at.isoformat(),
                    started_at=None,
                    finished_at=None,
                    cancel_requested=False,
                    classification=None,
                    failure_stage=None,
                    plan=None,
                    raw=None,
                    effects={},
                    reconciliation=None,
                    last_success=None,
                )
                c.execute(
                    f"INSERT INTO {SCHEMA}.jobs "
                    "(identity, scope, document, event_count) "
                    "VALUES (%s,%s,%s,%s)",
                    (
                        identity,
                        BACKFILL_SCOPE if backfill else DOMAIN,
                        Jsonb(document),
                        1,
                    ),
                )
                self._event(c, document, 1)
        return document

    def _verify_history(self, c, doc, count):
        job_id = doc["job_id"]
        events = tuple(
            r[0]
            for r in c.execute(
                f"SELECT event FROM {SCHEMA}.job_events WHERE job_id = %s "
                "ORDER BY sequence LIMIT 257",
                (job_id,),
            ).fetchall()
        )
        require(
            1 <= count <= 256 and len(events) == count and events[-1] == doc,
            "pce_pg_event_mismatch",
        )
        attempts = c.execute(
            f"SELECT document FROM {SCHEMA}.attempts WHERE job_id = %s",
            (job_id,),
        ).fetchall()
        require(
            attempts == ([(doc,)] if doc["attempts"] == 1 else []),
            "pce_pg_attempt_mismatch",
        )
        return events

    def snapshot(self, identity=None):
        if identity is not None:
            valid_identity(identity)
        with self._transaction() as c:
            identities = [(identity,)] if identity is not None else self._identities(c)
            require(len(identities) <= 2, "job_query_budget_exceeded")
            result = []
            for (job_id,) in identities:
                doc, count = self._read(c, job_id)
                events = self._verify_history(c, doc, count)
                result.append((doc, events))
        return tuple(result)

    def get(self, identity):
        return self.snapshot(identity)[0][0]

    def history(self, identity):
        return self.snapshot(identity)[0][1]

    def list_jobs(self):
        return tuple(doc for doc, _ in self.snapshot())

    def _save(self, c, document, count, *, claim=False):
        count += 1
        require(count <= 256, "job_event_budget_exhausted")
        if document["attempts"]:
            if claim:
                c.execute(
                    f"INSERT INTO {SCHEMA}.attempts (job_id, attempt_id, document) "
                    "VALUES (%s,%s,%s)",
                    (document["job_id"], document["attempt_id"], Jsonb(document)),
                )
            else:
                c.execute(
                    f"UPDATE {SCHEMA}.attempts SET document = %s WHERE job_id = %s",
                    (Jsonb(document), document["job_id"]),
                )
        c.execute(
            f"UPDATE {SCHEMA}.jobs SET document = %s, event_count = %s "
            "WHERE identity = %s",
            (Jsonb(document), count, document["job_id"]),
        )
        self._event(c, document, count)

    def _update(self, identity, at, **changes):
        valid_identity(identity)
        instant(at)
        require(set(changes) <= _MUTABLE, "pce_pg_immutable_identity")
        self._executor(identity)
        with self._transaction(write=True) as c:
            doc, count = self._read(c, identity, locked=True)
            require(
                not (identity == job_identity() and doc["state"] == "failed"),
                "pce_initial_failed_immutable",
            )
            require(
                at >= datetime.fromisoformat(doc["updated_at"]), "job_clock_reversal"
            )
            state = changes.get("state", doc["state"])
            require(
                state == doc["state"] or state in _TRANSITIONS.get(doc["state"], set()),
                "job_state_regression",
            )
            if doc["state"] in _TERMINAL:
                require(
                    set(changes) <= {"reconciliation", "stage", "classification"},
                    "job_terminal_immutable",
                )
            claim = doc["state"] == "pending" and state == "running"
            if claim:
                require(
                    changes.get("attempts") == 1
                    and changes.get("attempt_id") == identity + "-a1"
                    and changes.get("started_at") == at.isoformat(),
                    "pce_pg_attempt_required",
                )
            else:
                require(
                    not {"attempt_id", "attempts", "started_at", "worker_identity"}
                    & changes.keys(),
                    "pce_pg_attempt_immutable",
                )
            if "effects" in changes:
                require(state == "running", "pce_pg_effect_requires_attempt")
                # Effect reservations cannot be undone, decreased or reused.
                require(
                    all(
                        k not in doc["effects"] or doc["effects"][k] == v
                        for k, v in changes["effects"].items()
                        if k.endswith("possible")
                    ),
                    "pce_pg_effect_regression",
                )
                changes["effects"] = doc["effects"] | changes["effects"]
            reconciliation = changes.get("reconciliation")
            if (
                reconciliation
                and reconciliation.get("outcome") == "confirmed_published"
            ):
                changes["last_success"] = dict(
                    observed_at=at.isoformat(), evidence=reconciliation["evidence"]
                )
            doc.update(copy.deepcopy(changes), updated_at=at.isoformat())
            self._save(c, doc, count, claim=claim)
        return doc

    def cancel(self, identity, at):
        valid_identity(identity)
        instant(at)
        with self._transaction(write=True) as c:
            doc, count = self._read(c, identity, locked=True)
            if doc["state"] not in _TERMINAL:
                require(
                    at >= datetime.fromisoformat(doc["updated_at"]),
                    "job_clock_reversal",
                )
                doc.update(cancel_requested=True, updated_at=at.isoformat())
                self._save(c, doc, count)
        return doc
