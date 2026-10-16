"""Compatibility path for the delivered CPI journal and on-disk filename."""

from market_data_foundation.historical.macro_job_journal import (
    MacroJobJournal,
    valid_identity,
)

__all__ = ["FileJobJournal", "valid_identity"]


class FileJobJournal(MacroJobJournal):
    """Read existing CPI/PPI journals without migration or renamed files."""

    filename = "cpi-jobs.sqlite3"
