"""Deterministic reference ledger for hosted CI and semantic tests."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .codec import dumps, loads
from .model import (
    DatasetVersion,
    EvidenceMaterial,
    Key,
    Kind,
    Publication,
    PublicationRequest,
    Record,
    digest,
    require,
)
from .rules import build_publication, requirement_for_version, validate_record


class Ledger(Protocol):
    def publish(
        self, request: PublicationRequest, clock: Callable[[], datetime]
    ) -> Publication: ...

    def get(self, key: Key) -> Record | None: ...

    def put(self, record: Record) -> Record: ...

    def put_content(self, version: DatasetVersion, content: bytes) -> None: ...

    def get_content(self, version: Key) -> bytes | None: ...

    def publication_by_occurrence(self, occurrence: str) -> Publication | None: ...

    def publications_for_version(self, version: Key) -> tuple[Publication, ...]: ...

    def versions_for_definition(self, definition: Key) -> tuple[DatasetVersion, ...]:
        """Return exact-definition versions in ascending identity order."""
        ...


def _key_tuple(key: Key) -> tuple[str, str, str]:
    return key.kind.value, key.identity, key.revision


def prepare_publication(
    ledger: Ledger, request: PublicationRequest, clock: Callable[[], datetime]
) -> Publication:
    existing = ledger.publication_by_occurrence(request.occurrence)
    if existing is not None:
        require(existing.request == request, "occurrence_identity_conflict")
        validate_record(existing, ledger.get)
        return existing
    publication = build_publication(
        request,
        resolve=ledger.get,
        history=ledger.publications_for_version(request.version),
        existing_occurrence=None,
        decision_at=clock(),
    )
    validate_record(publication, ledger.get)
    version = ledger.get(request.version)
    require(isinstance(version, DatasetVersion), "publication_version_missing")
    requirement = requirement_for_version(version, ledger.get)
    needs_durable = requirement is not None and requirement.durable
    needs_replay = request.claims_replayable or (
        requirement is not None and requirement.replayable
    )
    if needs_durable or needs_replay:
        proven = []
        for key in request.evidence:
            material = ledger.get(key)
            require(
                isinstance(material, EvidenceMaterial), "publication_evidence_missing"
            )
            if material.grade not in request.claimed_grades or not material.durable:
                continue
            # R1 proves only its existing bounded canonical-content carrier.
            # External locators have no admitted retention contract and fail closed.
            prefix = "mdf-content://"
            reference = material.controlled_ref or ""
            if not reference.startswith(prefix) or not reference[len(prefix) :]:
                continue
            carrier = ledger.get(Key(Kind.VERSION, reference[len(prefix) :]))
            if not isinstance(carrier, DatasetVersion):
                continue
            content = ledger.get_content(carrier.key)
            if (
                material.key in (*carrier.materials, *carrier.quality_evidence)
                and material.scope.within(carrier.scope)
                and content is not None
                and digest(content) == material.digest == carrier.content_digest
            ):
                proven.append(material)
        if needs_durable:
            require(bool(proven), "durable_material_unproven")
        if needs_replay:
            require(
                any(material.replayable for material in proven),
                "replayable_material_unproven",
            )
    return publication


@dataclass
class InMemoryLedger:
    """Explicit serialization makes restart-style roundtrips deterministic."""

    _records: dict[tuple[str, str, str], str]
    _contents: dict[str, tuple[str, bytes]]
    _publication_occurrences: dict[str, str]

    def __init__(self) -> None:
        self._records = {}
        self._contents = {}
        self._publication_occurrences = {}

    def operator_reads(self):
        """Create a fresh operator-only serialized admission session."""
        from .operator_reads import MemoryOperatorReads

        return MemoryOperatorReads(self._records)

    def get(self, key: Key) -> Record | None:
        payload = self._records.get(_key_tuple(key))
        if payload is None:
            return None
        value = loads(payload)
        require(hasattr(value, "key"), "stored_record_invalid")
        return value

    def publish(
        self, request: PublicationRequest, clock: Callable[[], datetime]
    ) -> Publication:
        publication = prepare_publication(self, request, clock)
        stored = self.put(publication)
        require(isinstance(stored, Publication), "stored_publication_invalid")
        return stored

    def put(self, record: Record) -> Record:
        key = record.key
        encoded = dumps(record)
        existing = self._records.get(_key_tuple(key))
        if existing is not None:
            require(existing == encoded, "canonical_identity_conflict")
            value = loads(existing)
            require(hasattr(value, "key"), "stored_record_invalid")
            return value

        if isinstance(record, Publication):
            occurrence = record.request.occurrence
            existing_identity = self._publication_occurrences.get(occurrence)
            require(
                existing_identity in {None, record.request.identity},
                "occurrence_identity_conflict",
            )
            self._publication_occurrences[occurrence] = record.request.identity

        self._records[_key_tuple(key)] = encoded
        return record

    def put_content(self, version: DatasetVersion, content: bytes) -> None:
        require(type(content) is bytes, "mutable_content")
        require(digest(content) == version.content_digest, "content_digest_mismatch")
        existing = self._contents.get(version.identity)
        candidate = (version.content_digest, content)
        if existing is not None:
            require(existing == candidate, "version_content_conflict")
            return
        self._contents[version.identity] = candidate

    def get_content(self, version: Key) -> bytes | None:
        require(version.kind == Kind.VERSION, "content_key_kind")
        candidate = self._contents.get(version.identity)
        if candidate is None:
            return None
        expected_digest, content = candidate
        require(digest(content) == expected_digest, "stored_content_mismatch")
        return content

    def publication_by_occurrence(self, occurrence: str) -> Publication | None:
        identity = self._publication_occurrences.get(occurrence)
        if identity is None:
            return None
        value = self.get(Key(Kind.PUBLICATION, identity))
        require(isinstance(value, Publication), "stored_publication_invalid")
        return value

    def publications_for_version(self, version: Key) -> tuple[Publication, ...]:
        require(version.kind == Kind.VERSION, "publication_version_kind")
        publications: list[Publication] = []
        for (kind, _identity, _revision), payload in self._records.items():
            if kind != Kind.PUBLICATION.value:
                continue
            value = loads(payload)
            require(isinstance(value, Publication), "stored_publication_invalid")
            if value.request.version == version:
                publications.append(value)
        return tuple(
            sorted(
                publications,
                key=lambda item: (item.decision_at, item.request.identity),
            )
        )

    def versions_for_definition(self, definition: Key) -> tuple[DatasetVersion, ...]:
        require(definition.kind == Kind.DEFINITION, "version_definition_kind")
        versions: list[DatasetVersion] = []
        for (kind, _identity, _revision), payload in self._records.items():
            if kind != Kind.VERSION.value:
                continue
            value = loads(payload)
            require(isinstance(value, DatasetVersion), "stored_version_invalid")
            if value.definition == definition:
                versions.append(value)
        return tuple(sorted(versions, key=lambda item: item.identity))

    def snapshot(self) -> str:
        import json

        payload = {
            "records": [
                [list(key), value] for key, value in sorted(self._records.items())
            ],
            "contents": [
                [identity, content_digest, content.hex()]
                for identity, (content_digest, content) in sorted(
                    self._contents.items()
                )
            ],
            "occurrences": sorted(self._publication_occurrences.items()),
        }
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))

    @classmethod
    def from_snapshot(cls, payload: str) -> "InMemoryLedger":
        import json

        data = json.loads(payload)
        ledger = cls()
        for key, value in data["records"]:
            ledger._records[tuple(key)] = value
        for identity, content_digest, content_hex in data["contents"]:
            content = bytes.fromhex(content_hex)
            require(digest(content) == content_digest, "snapshot_content_mismatch")
            ledger._contents[identity] = (content_digest, content)
        ledger._publication_occurrences = dict(data["occurrences"])
        return ledger
