"""Typed retained input only; no transport, credential loading or source fallback.

The caller must separately qualify original raw identity and acquisition metadata.
Synthetic fixtures exercise the contract but are not source qualification.
"""

from dataclasses import dataclass

from market_data_foundation.historical.bls_employment import (
    EmploymentEvidence,
    EmploymentPlan,
)


@dataclass(frozen=True)
class EmploymentRetainedCandidate:
    evidence: EmploymentEvidence
    plan: EmploymentPlan
