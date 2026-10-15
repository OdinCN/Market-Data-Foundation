"""CPI reference/coverage guard over existing canonical consumer reads.

No source, storage client construction or journal dependency. Wire v1 stays
unchanged: callers using it directly retain its exact-version/as-of contract.
This optional CPI guard expresses a caller's named reference expectation,
which the generic wire deliberately has no field for.
"""

from datetime import datetime

from market_data_foundation.canonical.consumer_read import (
    ExactReadView,
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.model import Grade, Key, Kind
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.cpi_retained_handoff import (
    AUGUST_REFERENCE_SEMANTICS,
    MEASURES,
    SEPTEMBER_REFERENCE_SEMANTICS,
    _json,
    validate_cpi_projection,
)

_REFERENCES = {
    item.reference_period: item
    for item in (AUGUST_REFERENCE_SEMANTICS, SEPTEMBER_REFERENCE_SEMANTICS)
}


class CpiReadRejected(Exception):
    def __init__(self, classification: str):
        self.classification = classification
        super().__init__(classification)


def read_local_cpi(
    ledger: Ledger,
    *,
    reference: str,
    as_of: datetime,
    version_identity: str | None = None,
) -> ExactReadView:
    """Refuse a stale/different reference or partial CPI target without effects.

    Canonical miss/unavailable/ambiguous/invalid publication failures propagate
    with their existing stable classifications. Freshness is reference evidence,
    never wall-clock age or an assumption about a future provider release.
    """
    if reference not in _REFERENCES:
        raise CpiReadRejected("reference_not_admitted")
    if version_identity is None:
        view = read_as_of(ledger, BLS_CPI_HEADLINES.key, as_of=as_of)
    else:
        view = read_exact_version(
            ledger,
            BLS_CPI_HEADLINES.key,
            Key(Kind.VERSION, version_identity),
            as_of=as_of,
        )
    members = view.version.finite_scope.members
    expected = {f"{reference}:bls:cpi:{measure}" for measure in MEASURES}
    if members and any(member.split(":", 1)[0] != reference for member in members):
        raise CpiReadRejected("reference_stale_or_mismatched")
    if len(members) != 4 or set(members) != expected:
        raise CpiReadRejected("coverage_partial_or_mismatched")
    if not {Grade.ORIGINAL, Grade.PROJECTED} <= view.publication.request.claimed_grades:
        raise CpiReadRejected("quality_not_admitted")
    try:
        validate_cpi_projection(
            _json(view.content)["data"], reference=_REFERENCES[reference]
        )
    except Exception:
        # Never expose malformed content, validator messages or private values.
        raise CpiReadRejected("cpi_payload_invalid_or_mismatched") from None
    return view
