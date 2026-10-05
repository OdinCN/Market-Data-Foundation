"""Small application boundary for the accepted P3 canonical semantics."""

from collections.abc import Callable
from datetime import datetime

from .model import (
    DatasetVersion,
    Publication,
    PublicationRequest,
    Record,
    digest,
    require,
)
from .repository import Ledger
from .rules import (
    Outcome,
    OutcomeResolution,
    eligible_as_of,
    validate_record,
)


class CanonicalKernel:
    def __init__(self, ledger: Ledger, clock: Callable[[], datetime]) -> None:
        self.ledger = ledger
        self.clock = clock

    def add(self, record: Record) -> Record:
        require(not isinstance(record, Publication), "publication_requires_publish_api")
        validate_record(record, self.ledger.get)
        return self.ledger.put(record)

    def persist_version(
        self, version: DatasetVersion, content: bytes
    ) -> DatasetVersion:
        require(digest(content) == version.content_digest, "content_digest_mismatch")
        validate_record(version, self.ledger.get)
        self.ledger.put_content(version, content)
        stored = self.ledger.put(version)
        require(isinstance(stored, DatasetVersion), "stored_version_invalid")
        return stored

    def publish(self, request: PublicationRequest) -> Publication:
        return self.ledger.publish(request, self.clock)

    def eligible(self, version: DatasetVersion, at: datetime) -> bool:
        return eligible_as_of(self.ledger.publications_for_version(version.key), at)

    def reconcile_publication(
        self,
        occurrence: str,
        observed: Outcome,
    ) -> OutcomeResolution:
        existing = self.ledger.publication_by_occurrence(occurrence)
        if observed == Outcome.CONFIRMED_COMMITTED:
            require(existing is not None, "confirmed_commit_missing")
            return OutcomeResolution(occurrence, observed, existing.key)
        if observed == Outcome.CONFIRMED_NOT_COMMITTED:
            require(existing is None, "confirmed_absence_conflict")
            return OutcomeResolution(occurrence, observed)
        if existing is not None:
            return OutcomeResolution(
                occurrence,
                Outcome.CONFIRMED_COMMITTED,
                existing.key,
            )
        return OutcomeResolution(occurrence, Outcome.OUTCOME_UNKNOWN)
