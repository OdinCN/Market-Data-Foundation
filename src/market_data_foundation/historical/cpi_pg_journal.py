"""Authoritative PG Job/Attempt/event candidate, with no automatic DDL or IO.

A trusted Host supplies fresh exclusive connections and independently verified
schema identity. Nothing connects until an explicitly enabled method is called.
State commits never share the canonical writer transaction. No mirror, fallback,
repair, takeover or retry path exists. Read-only instances issue no state writes.
"""

import copy
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

from psycopg.pq import TransactionStatus
from psycopg.types.json import Jsonb

from market_data_foundation.canonical.model import ContractError, instant, require
from market_data_foundation.historical.cpi_pg_binding import (
    BACKFILL_JOB,
    BACKFILL_SCOPE,
    DOMAIN,
    INITIAL_JOB,
    SCHEMA,
    CpiPgBinding,
    backfill_constraint,
    check_backfill_parent,
)
from market_data_foundation.historical.macro_job_journal import (
    _TERMINAL,
    _TRANSITIONS,
    valid_identity,
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
            "cpi_pg_schema_identity_required",
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
}


class PostgresJobJournal:
    """Bounded INIT plus exact R8 successor. Construction does no IO.

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
        binding: CpiPgBinding,
        *,
        enabled=False,
        read_only=False,
    ):
        require(type(binding) is CpiPgBinding, "cpi_pg_binding_required")
        require(type(profile) is PgConfig, "cpi_pg_profile_required")
        require(type(identity) is PgJournalIdentity, "cpi_pg_schema_identity_required")
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
        require(self.enabled is True, "cpi_pg_explicit_opt_in_required")
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
                "cpi_pg_shared_connection_forbidden",
            )
            require(
                _idle(connection) and connection.autocommit is True,
                "cpi_pg_exclusive_idle_connection_required",
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
                "cpi_pg_connection_identity_mismatch",
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
            raise PgJournalUnavailable("cpi_pg_storage_unavailable") from None

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
            "cpi_pg_connection_identity_mismatch",
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
            "cpi_pg_schema_or_permission_mismatch",
        )

    @contextmanager
    def _transaction(self, *, write=False):
        if write:
            require(not self.read_only, "journal_read_only")
            require(not self._poisoned, "cpi_pg_observation_required")
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
                        "cpi_pg_transaction_outcome_unknown"
                        if write
                        else "cpi_pg_storage_unavailable"
                    )
                else:
                    try:
                        if write:
                            c.commit()
                        else:
                            c.rollback()
                        require(_idle(c), "cpi_pg_transaction_not_idle")
                    except Exception:
                        failure = (
                            "cpi_pg_commit_outcome_unknown"
                            if write
                            else "cpi_pg_storage_unavailable"
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
            not self.read_only and not self._poisoned, "cpi_pg_executor_unavailable"
        )
        require(self._executor_connection is None, "job_executor_busy")
        with self._connection() as c:
            held = c.execute(
                "SELECT pg_try_advisory_lock(1129335045, 202609)"
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
            "cpi_pg_executor_required",
        )
        c = self._executor_connection
        require(not c.closed, "cpi_pg_executor_lost")
        # A lost lock connection stops the caller before the next durable effect.
        failed = False
        try:
            c.execute("SELECT 1").fetchone()
        except Exception:
            failed = True
        if failed:
            self._poisoned = True
            raise PgJournalUnavailable("cpi_pg_executor_lost") from None

    def _read(self, c, identity, *, locked=False):
        row = c.execute(
            f"SELECT document, event_count FROM {SCHEMA}.jobs WHERE identity = %s"
            + (" FOR UPDATE" if locked else ""),
            (identity,),
        ).fetchone()
        require(row is not None, "job_not_found")
        document, count = row
        require(
            document["binding"] == self.binding.document(), "cpi_pg_binding_mismatch"
        )
        self.binding.check_spec(document["spec"])
        require(document["job_id"] == identity, "cpi_pg_job_identity_mismatch")
        if identity == BACKFILL_JOB:
            require(
                document.get("parent_job_id") == INITIAL_JOB,
                "cpi_backfill_parent_mismatch",
            )
        else:
            require(
                document.get("parent_job_id") is None, "cpi_backfill_parent_mismatch"
            )
        return copy.deepcopy(document), count

    def _event(self, c, document, count):
        require(count <= 256, "job_event_budget_exhausted")
        c.execute(
            f"INSERT INTO {SCHEMA}.job_events (job_id, sequence, event) "
            "VALUES (%s,%s,%s)",
            (document["job_id"], count, Jsonb(document)),
        )

    def _backfill_storage(self, c):
        self._check_schema(c, True)
        rows = c.execute(
            "SELECT conname, pg_catalog.pg_get_constraintdef(oid), convalidated, "
            "condeferrable FROM pg_catalog.pg_constraint WHERE conrelid=%s::regclass "
            "AND conname IN ('jobs_scope_key','jobs_scope_check') ORDER BY conname",
            (f"{SCHEMA}.jobs",),
        ).fetchall()
        require(
            rows
            == [
                ("jobs_scope_check", backfill_constraint(), True, False),
                ("jobs_scope_key", "UNIQUE (scope)", True, False),
            ],
            "cpi_backfill_exact_two_scope_migration_required",
        )

    def backfill_eligibility(self):
        try:
            snapshot = self.snapshot()
            documents = {doc["job_id"]: doc for doc, _ in snapshot}
            if BACKFILL_JOB in documents:
                return "cpi_backfill_successor_slot_used"
            if INITIAL_JOB not in documents:
                return "cpi_backfill_parent_missing"
            check_backfill_parent(documents[INITIAL_JOB])
            with self._transaction() as c:
                self._backfill_storage(c)
        except ContractError as error:
            # Only contract classifications; storage exceptions are sanitized.
            return error.args[0]
        return None

    def submit_backfill(self, identity, parent_job_id, spec, scope, at):
        require(
            identity == BACKFILL_JOB and parent_job_id == INITIAL_JOB,
            "cpi_backfill_parent_mismatch",
        )
        return self._submit(identity, spec, scope, at, backfill=True)

    def submit(self, identity, spec, scope, at):
        require(identity != BACKFILL_JOB, "cpi_backfill_parent_required")
        return self._submit(identity, spec, scope, at, backfill=False)

    def _submit(self, identity, spec, scope, at, *, backfill):
        valid_identity(identity)
        instant(at)
        self.binding.check_spec(spec, scope)
        with self._transaction(write=True) as c:
            # Serialization covers first insert and identity/scope conflicts alike.
            c.execute(f"LOCK TABLE {SCHEMA}.jobs IN SHARE ROW EXCLUSIVE MODE")
            rows = c.execute(f"SELECT identity FROM {SCHEMA}.jobs LIMIT 3").fetchall()
            if backfill:
                require(
                    set(rows) <= {(INITIAL_JOB,), (BACKFILL_JOB,)}
                    and (INITIAL_JOB,) in rows,
                    "cpi_backfill_parent_missing",
                )
                parent, count = self._read(c, INITIAL_JOB, locked=True)
                self._verify_history(c, parent, count)
                check_backfill_parent(parent)
                self._backfill_storage(c)
            if (identity,) in rows:
                require(
                    backfill
                    or rows == [(identity,)]
                    or set(rows) == {(INITIAL_JOB,), (BACKFILL_JOB,)},
                    "job_scope_already_registered",
                )
                document, _ = self._read(c, identity)
                require(document["spec"] == spec, "job_identity_reused")
            else:
                require(backfill or not rows, "job_scope_already_registered")
                document = dict(
                    job_id=identity,
                    **({"parent_job_id": INITIAL_JOB} if backfill else {}),
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
            "cpi_pg_event_mismatch",
        )
        attempts = c.execute(
            f"SELECT document FROM {SCHEMA}.attempts WHERE job_id = %s",
            (job_id,),
        ).fetchall()
        require(
            attempts == ([(doc,)] if doc["attempts"] == 1 else []),
            "cpi_pg_attempt_mismatch",
        )
        return events

    def snapshot(self, identity=None):
        if identity is not None:
            valid_identity(identity)
        with self._transaction() as c:
            identities = (
                [(identity,)]
                if identity is not None
                else c.execute(f"SELECT identity FROM {SCHEMA}.jobs LIMIT 3").fetchall()
            )
            require(len(identities) <= 2, "job_query_budget_exceeded")
            if len(identities) == 2:
                require(
                    set(identities) == {(INITIAL_JOB,), (BACKFILL_JOB,)},
                    "job_scope_already_registered",
                )
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
        require(set(changes) <= _MUTABLE, "cpi_pg_immutable_identity")
        self._executor(identity)
        with self._transaction(write=True) as c:
            doc, count = self._read(c, identity, locked=True)
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
                    "cpi_pg_attempt_required",
                )
            else:
                require(
                    not {"attempt_id", "attempts", "started_at", "worker_identity"}
                    & changes.keys(),
                    "cpi_pg_attempt_immutable",
                )
            if "effects" in changes:
                require(state == "running", "cpi_pg_effect_requires_attempt")
                # Effect reservations cannot be undone, decreased or reused.
                require(
                    all(
                        k not in doc["effects"] or doc["effects"][k] == v
                        for k, v in changes["effects"].items()
                        if k.endswith("possible")
                    ),
                    "cpi_pg_effect_regression",
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
