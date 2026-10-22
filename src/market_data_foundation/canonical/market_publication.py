"""Immutable market Version and independent authoritative Publication values."""

from dataclasses import dataclass
from datetime import UTC, datetime

from market_data_foundation.canonical.market_material import (
    DailyAssessment,
    byte_digest,
)
from market_data_foundation.canonical.model import Decision, Key, Kind, instant, require
from market_data_foundation.domain_contracts.market_universe import (
    content_id,
    frozen_tuple,
    text_fields,
)


@dataclass(frozen=True)
class MarketVersion:
    key: Key
    assessment: DailyAssessment
    frozen_at: datetime
    admission_subjects: tuple[str, ...]

    def __post_init__(self) -> None:
        require(self.key.kind == Kind.VERSION, "version_key_required")
        instant(self.frozen_at)
        require(
            self.frozen_at.astimezone(UTC)
            >= self.assessment.evaluated_at.astimezone(UTC),
            "version_before_assessment",
        )
        frozen_tuple(self.admission_subjects)
        require(
            bool(self.admission_subjects)
            and len(set(self.admission_subjects)) == len(self.admission_subjects),
            "explicit_admission_subjects_required",
        )
        text_fields(*self.admission_subjects)

    @property
    def digest(self) -> str:
        return content_id(self)

    @property
    def content_digest(self) -> str:
        return byte_digest(self.assessment.content)

    @property
    def range_key(self) -> str:
        unit = self.assessment.unit
        return content_id(
            ("market.range.d3.r1", unit.definition, unit.instrument, unit.window)
        )


@dataclass(frozen=True)
class MarketPublicationRequest:
    key: Key
    occurrence: str
    version: Key
    decision: Decision
    predecessor: Key | None
    evidence: tuple[str, ...]
    rule_revision: str
    usable_at: datetime | None = None
    effective_at: datetime | None = None

    def __post_init__(self) -> None:
        require(
            self.key.kind == Kind.PUBLICATION and self.version.kind == Kind.VERSION,
            "publication_key_mismatch",
        )
        text_fields(self.occurrence, self.rule_revision)
        frozen_tuple(self.evidence)
        require(bool(self.evidence), "publication_evidence_required")
        text_fields(*self.evidence)
        require(isinstance(self.decision, Decision), "invalid_publication_decision")
        # No replacement-group implementation or silent single-Version substitute.
        require(self.decision != Decision.SUPERSEDE, "replacement_groups_unsupported")
        for value in (self.usable_at, self.effective_at):
            if value is not None:
                instant(value)


@dataclass(frozen=True)
class MarketPublication:
    request: MarketPublicationRequest
    decision_at: datetime
    usable_at: datetime

    def __post_init__(self) -> None:
        instant(self.decision_at)
        instant(self.usable_at)
        require(
            self.usable_at.astimezone(UTC) >= self.decision_at.astimezone(UTC),
            "publication_time_backdated",
        )

    @property
    def key(self) -> Key:
        return self.request.key


@dataclass(frozen=True)
class PublicationHead:
    """Canonical per-Version anchor, stored outside the replaceable projection."""

    version: Key
    count: int
    head: Key | None
    history_digest: str


def publication_head(
    version: Key, history: tuple[MarketPublication, ...]
) -> PublicationHead:
    """Writer/recovery helper; readers must preflight counts before hashing."""
    return PublicationHead(
        version, len(history), history[-1].key if history else None, content_id(history)
    )


def validate_history(version: Key, history: tuple[MarketPublication, ...]) -> None:
    seen = set()
    previous = None
    for event in history:
        request = event.request
        require(
            event.usable_at.astimezone(UTC)
            == (request.usable_at or event.decision_at).astimezone(UTC),
            "publication_usable_time_mismatch",
        )
        require(
            request.version == version and event.key not in seen,
            "publication_history_identity_mismatch",
        )
        require(
            request.predecessor == (previous.key if previous else None),
            "publication_predecessor_mismatch",
        )
        require(
            previous is None
            or event.decision_at.astimezone(UTC) > previous.decision_at.astimezone(UTC),
            "publication_time_not_monotonic",
        )
        allowed = (
            {Decision.PUBLISH}
            if previous is None
            else {Decision.RESTORE}
            if previous.request.decision == Decision.WITHDRAW
            else {Decision.WITHDRAW}
        )
        require(request.decision in allowed, "invalid_publication_transition")
        seen.add(event.key)
        previous = event


def active_at(history: tuple[MarketPublication, ...], at: datetime) -> bool:
    instant(at)
    # Same-zone datetime comparison ignores fold; compare actual instants.
    at = at.astimezone(UTC)
    applicable = tuple(
        event
        for event in history
        if event.decision_at.astimezone(UTC) <= at
        and event.usable_at.astimezone(UTC) <= at
        and (
            event.request.effective_at is None
            or event.request.effective_at.astimezone(UTC) <= at
        )
    )
    return bool(
        applicable
        and applicable[-1].request.decision in (Decision.PUBLISH, Decision.RESTORE)
    )
