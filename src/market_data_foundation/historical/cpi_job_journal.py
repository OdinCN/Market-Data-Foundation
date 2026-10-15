"""Single-host CPI journal. SQLite is local state, never the canonical ledger.

Use one trusted, owner-only directory on a local filesystem. flock excludes a
second local executor/reconciler; SQLite commits the intent before each effect.
This is deliberately not a distributed or PostgreSQL job-state implementation.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from market_data_foundation.canonical.model import instant, require

_ID = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
_TERMINAL = frozenset({"succeeded", "failed", "cancelled", "outcome_unknown"})
_TRANSITIONS = {
    "pending": {"running", "failed", "cancelled"},
    "running": _TERMINAL,
}


def valid_identity(value: str) -> None:
    require(type(value) is str and _ID.fullmatch(value) is not None, "job_id_invalid")


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


class FileJobJournal:
    """Persistent, non-reusable job/range identities and append-only history.

    Construction explicitly initializes local files. ``read_only`` never creates
    a directory, database, lock, job or attempt and is suitable for the CLI.
    Directory ownership/security and durable local disk are host preconditions.
    """

    def __init__(self, directory: Path, *, read_only: bool = False) -> None:
        self.directory = directory
        self.path = directory / "cpi-jobs.sqlite3"
        self.read_only = read_only
        if not read_only:
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            require(not directory.is_symlink(), "journal_directory_invalid")
            fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            with self._connection() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS jobs (
                        identity TEXT PRIMARY KEY,
                        scope TEXT NOT NULL UNIQUE,
                        document TEXT NOT NULL
                    );
                    CREATE TABLE IF NOT EXISTS history (
                        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                        identity TEXT NOT NULL,
                        event TEXT NOT NULL
                    );
                    """
                )

    @contextmanager
    def _connection(self):
        require(not self.path.is_symlink(), "journal_path_invalid")
        mode = "ro" if self.read_only else "rw"
        connection = sqlite3.connect(
            self.path.resolve().as_uri() + "?mode=" + mode,
            uri=True,
            timeout=2,
        )
        try:
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
        finally:
            connection.close()

    @contextmanager
    def executor(self, identity: str):
        """Held for the complete action; never steal a live executor's lock."""
        valid_identity(identity)
        require(not self.read_only, "journal_read_only")
        descriptor = os.open(
            self.directory / (identity + ".lock"),
            os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
            0o600,
        )
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                require(False, "job_executor_busy")
            yield
        finally:
            os.close(descriptor)

    def submit(self, identity: str, spec: dict, scope: str, at: datetime) -> dict:
        valid_identity(identity)
        instant(at)
        require(not self.read_only, "journal_read_only")
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM jobs WHERE identity = ?", (identity,)
            ).fetchone()
            if row is not None:
                document = json.loads(row[0])
                require(document["spec"] == spec, "job_identity_reused")
                return document
            require(
                connection.execute(
                    "SELECT 1 FROM jobs WHERE scope = ?", (scope,)
                ).fetchone()
                is None,
                "job_scope_already_registered",
            )
            document = {
                "job_id": identity,
                "spec": spec,
                "state": "pending",
                "stage": "registered",
                "attempt_id": None,
                "attempts": 0,
                "created_at": at.isoformat(),
                "updated_at": at.isoformat(),
                "started_at": None,
                "finished_at": None,
                "cancel_requested": False,
                "classification": None,
                "failure_stage": None,
                "plan": None,
                "raw": None,
                "effects": {},
                "reconciliation": None,
                "last_success": None,
            }
            connection.execute(
                "INSERT INTO jobs VALUES (?, ?, ?)", (identity, scope, _json(document))
            )
            self._event(connection, document)
            return document

    @staticmethod
    def _event(connection, document):
        event = {
            key: document[key]
            for key in (
                "state",
                "stage",
                "attempt_id",
                "updated_at",
                "classification",
                "failure_stage",
                "effects",
                "reconciliation",
                "cancel_requested",
                "last_success",
            )
        }
        connection.execute(
            "INSERT INTO history(identity, event) VALUES (?, ?)",
            (document["job_id"], _json(event)),
        )

    def get(self, identity: str) -> dict:
        valid_identity(identity)
        with self._connection() as connection:
            row = connection.execute(
                "SELECT document FROM jobs WHERE identity = ?", (identity,)
            ).fetchone()
        require(row is not None, "job_not_found")
        return json.loads(row[0])

    def list_jobs(self) -> tuple[dict, ...]:
        with self._connection() as connection:
            return tuple(
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT document FROM jobs ORDER BY rowid"
                )
            )

    def history(self, identity: str) -> tuple[dict, ...]:
        valid_identity(identity)
        with self._connection() as connection:
            return tuple(
                json.loads(row[0])
                for row in connection.execute(
                    "SELECT event FROM history WHERE identity = ? ORDER BY sequence",
                    (identity,),
                )
            )

    def _update(self, identity: str, at: datetime, **changes) -> dict:
        """Internal single-executor transition. Cancellation has its own flag."""
        instant(at)
        require(not self.read_only, "journal_read_only")
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM jobs WHERE identity = ?", (identity,)
            ).fetchone()
            require(row is not None, "job_not_found")
            document = json.loads(row[0])
            require(
                at >= datetime.fromisoformat(document["updated_at"]),
                "job_clock_reversal",
            )
            state = changes.get("state", document["state"])
            require(
                state == document["state"]
                or state in _TRANSITIONS.get(document["state"], set()),
                "job_state_regression",
            )
            if document["state"] in _TERMINAL:
                require(
                    set(changes) <= {"reconciliation", "stage", "classification"},
                    "job_terminal_immutable",
                )
            if "effects" in changes:
                changes["effects"] = document["effects"] | changes["effects"]
            reconciliation = changes.get("reconciliation")
            if (
                reconciliation
                and reconciliation.get("outcome") == "confirmed_published"
            ):
                changes["last_success"] = {
                    "observed_at": at.isoformat(),
                    "evidence": reconciliation["evidence"],
                }
            document.update(changes, updated_at=at.isoformat())
            connection.execute(
                "UPDATE jobs SET document = ? WHERE identity = ?",
                (_json(document), identity),
            )
            self._event(connection, document)
            return document

    def cancel(self, identity: str, at: datetime) -> dict:
        valid_identity(identity)
        instant(at)
        require(not self.read_only, "journal_read_only")
        with self._connection() as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT document FROM jobs WHERE identity = ?", (identity,)
            ).fetchone()
            require(row is not None, "job_not_found")
            document = json.loads(row[0])
            if document["state"] in _TERMINAL:
                return document
            require(
                at >= datetime.fromisoformat(document["updated_at"]),
                "job_clock_reversal",
            )
            document.update(cancel_requested=True, updated_at=at.isoformat())
            connection.execute(
                "UPDATE jobs SET document = ? WHERE identity = ?",
                (_json(document), identity),
            )
            self._event(connection, document)
            return document
