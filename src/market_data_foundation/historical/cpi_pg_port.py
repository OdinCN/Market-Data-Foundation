"""Internal B1 acceptance/read port; no HTTP routes, login or worker deployment.

The Host's authorize callback must authenticate the current server-side subject,
check write entitlement and (for cookie requests) CSRF/origin before granting.
A grant is a trusted Host result, never request-body authorization evidence.
"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.cpi_local_job import CpiJobSpec
from market_data_foundation.historical.cpi_pg_binding import CpiPgBinding
from market_data_foundation.historical.cpi_pg_journal import PostgresJobJournal
from market_data_foundation.historical.macro_job_journal import valid_identity


@dataclass(frozen=True)
class CpiPgOperator:
    identity: str
    write_eligible: bool

    def __post_init__(self):
        valid_identity(self.identity)
        require(type(self.write_eligible) is bool, "operator_not_authorized")


@dataclass(frozen=True)
class CpiPgJobsPort:
    journal: PostgresJobJournal
    spec: CpiJobSpec
    clock: Callable[[], datetime]
    authorize: Callable[[], CpiPgOperator | None]

    def __post_init__(self):
        require(type(self.journal) is PostgresJobJournal, "cpi_pg_journal_required")
        require(type(self.spec) is CpiJobSpec, "cpi_job_spec_invalid")
        self.journal.binding.check_spec(self.spec.document(), self.spec.scope)

    def _authorize(self, *, write=False):
        grant = None
        try:
            grant = self.authorize()
        except Exception:
            pass
        require(
            type(grant) is CpiPgOperator
            and grant.identity == self.journal.binding.operator
            and (not write or grant.write_eligible),
            "operator_not_authorized",
        )

    def accept(self, identity: str, intent: CpiPgBinding):
        self._authorize(write=True)
        require(
            type(intent) is CpiPgBinding and intent == self.journal.binding,
            "cpi_pg_binding_mismatch",
        )
        # Returns a pending durable record, never a publication/success claim.
        return self.journal.submit(
            identity, self.spec.document(), self.spec.scope, self.clock()
        )

    def snapshot(self, identity: str | None = None):
        self._authorize()
        return self.journal.reader().snapshot(identity)
