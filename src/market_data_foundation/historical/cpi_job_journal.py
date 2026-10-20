"""Compatibility path for the delivered CPI journal and on-disk filename."""

import json

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.macro_job_journal import (
    MacroJobJournal,
    valid_identity,
)

__all__ = ["FileJobJournal", "valid_identity"]


class FileJobJournal(MacroJobJournal):
    """Read existing CPI/PPI journals without migration or renamed files."""

    filename = "cpi-jobs.sqlite3"

    def snapshot(self, identity: str | None = None) -> tuple[tuple[dict, tuple], ...]:
        """Bounded consistent job/history read; never initializes local state."""
        if identity is not None:
            valid_identity(identity)
        with self._connection() as connection:
            connection.execute("BEGIN")
            rows = connection.execute(
                "SELECT identity, document FROM jobs "
                + ("WHERE identity = ? " if identity is not None else "")
                + "ORDER BY rowid LIMIT 101",
                () if identity is None else (identity,),
            ).fetchall()
            require(len(rows) <= 100, "job_query_budget_exceeded")
            if identity is not None:
                require(bool(rows), "job_not_found")
            result = []
            for job_id, document in rows:
                events = connection.execute(
                    "SELECT event FROM history WHERE identity = ? "
                    "ORDER BY sequence LIMIT 257",
                    (job_id,),
                ).fetchall()
                require(len(events) <= 256, "job_query_budget_exceeded")
                result.append(
                    (json.loads(document), tuple(json.loads(e[0]) for e in events))
                )
            return tuple(result)
