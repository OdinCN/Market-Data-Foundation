"""Consumer-neutral whole-version reads for the two admitted P3 definitions."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TypeVar

from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES

from .codec import dumps
from .model import (
    AcquisitionOccurrence,
    Assessment,
    ContractError,
    DatasetDefinition,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    Grade,
    Key,
    Kind,
    Publication,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    digest,
    instant,
)
from .repository import Ledger
from .rules import Resolver, validate_record

_ADMITTED_DEFINITIONS = frozenset({BLS_CPI_HEADLINES.key, RAW_DAILY_BARS.key})
_T = TypeVar("_T")


class ReadFailure(StrEnum):
    DEFINITION_NOT_ADMITTED = "definition_not_admitted"
    DEFINITION_NOT_FOUND = "definition_not_found"
    VERSION_NOT_FOUND = "version_not_found"
    VERSION_DEFINITION_MISMATCH = "version_definition_mismatch"
    CONTENT_MISSING = "content_missing"
    CONTENT_DIGEST_MISMATCH = "content_digest_mismatch"
    UNPUBLISHED_OR_UNAVAILABLE_AS_OF = "unpublished_or_unavailable_as_of"
    VERSION_WITHDRAWN = "version_withdrawn"
    VERSION_SUPERSEDED = "version_superseded"
    AS_OF_NO_VERSION = "as_of_no_version"
    AS_OF_AMBIGUOUS = "as_of_ambiguous"
    PUBLICATION_STATE_INVALID = "publication_state_invalid"
    LINEAGE_MISSING_OR_MISMATCHED = "lineage_missing_or_mismatched"
    INVALID_AS_OF = "invalid_as_of"
    STORAGE_UNAVAILABLE = "storage_unavailable"


class ReadContractError(Exception):
    """Public failure carrying only a stable classification."""

    def __init__(self, classification: ReadFailure) -> None:
        self.classification = classification
        super().__init__(classification.value)


@dataclass(frozen=True)
class AcquisitionReadRecord:
    """Exact acquisition semantics without a private receipt locator."""

    identity: str
    run: Key
    request_identity: str
    source: Key
    binding: Key
    mapping: str
    scope: Scope
    parameters: tuple[tuple[str, str], ...]
    requested_at: datetime
    obtained_at: datetime | None
    source_observed_at: datetime | None
    source_frame_identity: str | None
    result_mode: str
    completeness: str
    freshness: str
    terminal_result: str
    provider_data_attempts: int | None
    pagination: str
    recovery_summary: str
    external_receipt_present: bool
    canonical_record_digest: str


@dataclass(frozen=True)
class EvidenceMaterialReadRecord:
    """Exact evidence semantics without a private controlled locator."""

    identity: str
    grade: Grade
    digest: str | None
    scope: Scope
    acquired_from: Key | None
    produced_by: Key | None
    durable: bool
    replayable: bool
    assessment: Assessment | None
    named_class: str | None
    controlled_ref_present: bool
    canonical_record_digest: str


@dataclass(frozen=True)
class ExactReadView:
    """One complete immutable version; subsets and pagination are not admitted."""

    definition: DatasetDefinition
    version: DatasetVersion
    content: bytes
    publication: Publication
    binding: SourceBindingRevision | None
    source: SourceOperationRef | None
    acquisitions: tuple[AcquisitionReadRecord, ...]
    materials: tuple[EvidenceMaterialReadRecord, ...]
    content_mode: str = "whole_version"


def _fail(classification: ReadFailure) -> None:
    raise ReadContractError(classification) from None


def _classified(call: Callable[[], _T], failure: ReadFailure) -> _T:
    failed = False
    try:
        result = call()
    except ReadContractError:
        raise
    except Exception:
        failed = True
    if failed:
        _fail(failure)
    return result


def _get(ledger: Ledger, key: Key):
    return _classified(lambda: ledger.get(key), ReadFailure.STORAGE_UNAVAILABLE)


def _definition(
    ledger: Ledger,
    key: Key,
    admitted: frozenset[Key] = _ADMITTED_DEFINITIONS,
) -> DatasetDefinition:
    if not isinstance(key, Key) or key not in admitted:
        _fail(ReadFailure.DEFINITION_NOT_ADMITTED)
    value = _get(ledger, key)
    if not isinstance(value, DatasetDefinition):
        _fail(ReadFailure.DEFINITION_NOT_FOUND)
    return value


def _history(ledger: Ledger, version: DatasetVersion) -> tuple[Publication, ...]:
    history = _classified(
        lambda: ledger.publications_for_version(version.key),
        ReadFailure.STORAGE_UNAVAILABLE,
    )
    if not isinstance(history, tuple) or not all(
        isinstance(item, Publication) for item in history
    ):
        _fail(ReadFailure.PUBLICATION_STATE_INVALID)
    if not history:
        return ()
    ordered = tuple(
        sorted(history, key=lambda item: (item.decision_at, item.request.identity))
    )
    if history != ordered:
        _fail(ReadFailure.PUBLICATION_STATE_INVALID)
    if len({item.key for item in history}) != len(history):
        _fail(ReadFailure.PUBLICATION_STATE_INVALID)
    if len({item.request.occurrence for item in history}) != len(history):
        _fail(ReadFailure.PUBLICATION_STATE_INVALID)
    if len({item.decision_at for item in history}) != len(history):
        _fail(ReadFailure.PUBLICATION_STATE_INVALID)

    allowed = {
        Decision.PUBLISH: {Decision.WITHDRAW, Decision.SUPERSEDE},
        Decision.RESTORE: {Decision.WITHDRAW, Decision.SUPERSEDE},
        Decision.WITHDRAW: {Decision.RESTORE, Decision.SUPERSEDE},
        Decision.SUPERSEDE: set(),
    }
    previous: Publication | None = None
    for publication in history:

        def validate_shape() -> None:
            instant(publication.decision_at)
            instant(publication.usable_at)
            if publication.request.effective_at is not None:
                instant(publication.request.effective_at)
            if (
                publication.usable_at < publication.decision_at
                or not publication.request.identity
                or not publication.request.occurrence
                or not publication.request.rule_revision
                or not publication.request.scope.within(version.scope)
                or (
                    publication.request.predecessor is not None
                    and publication.request.predecessor.kind != Kind.PUBLICATION
                )
                or (
                    publication.request.decision == Decision.SUPERSEDE
                    and publication.request.supersedes_version is None
                )
            ):
                raise ContractError("publication_state_invalid")

        _classified(validate_shape, ReadFailure.PUBLICATION_STATE_INVALID)
        if publication.request.version != version.key:
            _fail(ReadFailure.PUBLICATION_STATE_INVALID)
        if previous is None:
            if (
                publication.request.decision != Decision.PUBLISH
                or publication.request.predecessor is not None
            ):
                _fail(ReadFailure.PUBLICATION_STATE_INVALID)
        elif (
            publication.request.predecessor != previous.key
            or publication.request.decision not in allowed[previous.request.decision]
        ):
            _fail(ReadFailure.PUBLICATION_STATE_INVALID)
        previous = publication
        _classified(
            lambda: validate_record(publication, ledger.get),
            ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
        )
    return history


def _effective(publication: Publication, at: datetime) -> bool:
    effective_at = publication.request.effective_at
    return (
        publication.decision_at <= at
        and publication.usable_at <= at
        and (effective_at is None or effective_at <= at)
    )


def _effective_publication(
    history: tuple[Publication, ...], as_of: datetime | None
) -> Publication | None:
    if not history:
        return None
    if as_of is None:
        candidate = history[-1]
    else:
        effective = tuple(item for item in history if _effective(item, as_of))
        if not effective:
            return None
        candidate = effective[-1]
    return candidate


def _available_publication(
    history: tuple[Publication, ...], as_of: datetime | None
) -> Publication | None:
    publication = _effective_publication(history, as_of)
    if publication is None or publication.request.decision not in {
        Decision.PUBLISH,
        Decision.RESTORE,
    }:
        return None
    return publication


def _record_digest(record: object) -> str:
    return digest(dumps(record).encode())


def _acquisition_record(value: AcquisitionOccurrence) -> AcquisitionReadRecord:
    return AcquisitionReadRecord(
        identity=value.identity,
        run=value.run,
        request_identity=value.request_identity,
        source=value.source,
        binding=value.binding,
        mapping=value.mapping,
        scope=value.scope,
        parameters=value.parameters,
        requested_at=value.requested_at,
        obtained_at=value.obtained_at,
        source_observed_at=value.source_observed_at,
        source_frame_identity=value.source_frame_identity,
        result_mode=value.result_mode,
        completeness=value.completeness,
        freshness=value.freshness,
        terminal_result=value.terminal_result,
        provider_data_attempts=value.provider_data_attempts,
        pagination=value.pagination,
        recovery_summary=value.recovery_summary,
        external_receipt_present=value.external_receipt is not None,
        canonical_record_digest=_record_digest(value),
    )


def _material_record(value: EvidenceMaterial) -> EvidenceMaterialReadRecord:
    return EvidenceMaterialReadRecord(
        identity=value.identity,
        grade=value.grade,
        digest=value.digest,
        scope=value.scope,
        acquired_from=value.acquired_from,
        produced_by=value.produced_by,
        durable=value.durable,
        replayable=value.replayable,
        assessment=value.assessment,
        named_class=value.named_class,
        controlled_ref_present=value.controlled_ref is not None,
        canonical_record_digest=_record_digest(value),
    )


def _lineage(
    ledger: Ledger,
    definition: DatasetDefinition,
    version: DatasetVersion,
    publication: Publication | None,
    *,
    resolver: Resolver | None = None,
) -> tuple[
    SourceBindingRevision | None,
    SourceOperationRef | None,
    tuple[AcquisitionReadRecord, ...],
    tuple[EvidenceMaterialReadRecord, ...],
]:
    def resolve_and_validate_lineage() -> tuple[
        SourceBindingRevision | None,
        SourceOperationRef | None,
        tuple[AcquisitionOccurrence, ...],
        tuple[EvidenceMaterial, ...],
    ]:
        resolve = ledger.get if resolver is None else resolver

        validate_record(definition, resolve)
        validate_record(version, resolve)
        if publication is not None:
            validate_record(publication, resolve)

        binding: SourceBindingRevision | None = None
        source: SourceOperationRef | None = None
        if version.binding is None:
            if version.mapping is not None or version.acquisitions:
                raise ContractError("unbound_version_has_source_lineage")
        else:
            candidate = resolve(version.binding)
            if not isinstance(candidate, SourceBindingRevision):
                raise ContractError("binding_missing")
            validate_record(candidate, resolve)
            binding = candidate

            source_candidate = resolve(binding.source)
            if not isinstance(source_candidate, SourceOperationRef):
                raise ContractError("source_missing")
            validate_record(source_candidate, resolve)
            source = source_candidate

        acquisitions: list[AcquisitionOccurrence] = []
        for key in version.acquisitions:
            candidate = resolve(key)
            if not isinstance(candidate, AcquisitionOccurrence):
                raise ContractError("acquisition_missing")
            validate_record(candidate, resolve)
            acquisitions.append(candidate)

        material_keys = tuple(
            dict.fromkeys(
                (
                    *version.materials,
                    *version.quality_evidence,
                    *(publication.request.evidence if publication is not None else ()),
                )
            )
        )
        materials: list[EvidenceMaterial] = []
        for key in material_keys:
            candidate = resolve(key)
            if not isinstance(candidate, EvidenceMaterial):
                raise ContractError("material_missing")
            validate_record(candidate, resolve)
            materials.append(candidate)
        return binding, source, tuple(acquisitions), tuple(materials)

    binding, source, acquisitions, materials = _classified(
        resolve_and_validate_lineage,
        ReadFailure.LINEAGE_MISSING_OR_MISMATCHED,
    )
    return (
        binding,
        source,
        tuple(_acquisition_record(value) for value in acquisitions),
        tuple(_material_record(value) for value in materials),
    )


def _exact(
    ledger: Ledger,
    definition_key: Key,
    version_key: Key,
    as_of: datetime | None,
    admitted: frozenset[Key] = _ADMITTED_DEFINITIONS,
) -> ExactReadView:
    definition = _definition(ledger, definition_key, admitted)
    if not isinstance(version_key, Key) or version_key.kind != Kind.VERSION:
        _fail(ReadFailure.VERSION_NOT_FOUND)
    version = _get(ledger, version_key)
    if not isinstance(version, DatasetVersion):
        _fail(ReadFailure.VERSION_NOT_FOUND)
    if version.definition != definition.key:
        _fail(ReadFailure.VERSION_DEFINITION_MISMATCH)

    history = _history(ledger, version)
    publication = _effective_publication(history, as_of)
    if publication is None:
        _fail(ReadFailure.UNPUBLISHED_OR_UNAVAILABLE_AS_OF)
    if publication.request.decision == Decision.WITHDRAW:
        _fail(ReadFailure.VERSION_WITHDRAWN)
    if publication.request.decision == Decision.SUPERSEDE:
        _fail(ReadFailure.VERSION_SUPERSEDED)

    digest_failure = False
    storage_failure = False
    try:
        content = ledger.get_content(version.key)
    except ContractError:
        digest_failure = True
    except Exception:
        storage_failure = True
    if digest_failure:
        _fail(ReadFailure.CONTENT_DIGEST_MISMATCH)
    if storage_failure:
        _fail(ReadFailure.STORAGE_UNAVAILABLE)
    if content is None:
        _fail(ReadFailure.CONTENT_MISSING)
    if type(content) is not bytes or digest(content) != version.content_digest:
        _fail(ReadFailure.CONTENT_DIGEST_MISMATCH)

    binding, source, acquisitions, materials = _lineage(
        ledger, definition, version, publication
    )
    return ExactReadView(
        definition=definition,
        version=version,
        content=content,
        publication=publication,
        binding=binding,
        source=source,
        acquisitions=acquisitions,
        materials=materials,
    )


def read_exact_version(
    ledger: Ledger,
    definition: Key,
    version: Key,
    *,
    as_of: datetime | None = None,
) -> ExactReadView:
    """Read one exact version, optionally requiring usability at one instant."""
    if as_of is not None:
        _classified(lambda: instant(as_of), ReadFailure.INVALID_AS_OF)
    return _classified(
        lambda: _exact(ledger, definition, version, as_of),
        ReadFailure.STORAGE_UNAVAILABLE,
    )


def read_as_of(ledger: Ledger, definition: Key, *, as_of: datetime) -> ExactReadView:
    """Resolve only when exactly one exact-definition version is eligible."""
    _classified(lambda: instant(as_of), ReadFailure.INVALID_AS_OF)
    _definition(ledger, definition)
    versions = _classified(
        lambda: ledger.versions_for_definition(definition),
        ReadFailure.STORAGE_UNAVAILABLE,
    )
    if not isinstance(versions, tuple) or any(
        not isinstance(version, DatasetVersion) or version.definition != definition
        for version in versions
    ):
        _fail(ReadFailure.STORAGE_UNAVAILABLE)
    if tuple(sorted(versions, key=lambda item: item.identity)) != versions:
        _fail(ReadFailure.STORAGE_UNAVAILABLE)

    eligible: list[DatasetVersion] = []
    for version in versions:
        if _available_publication(_history(ledger, version), as_of) is not None:
            eligible.append(version)
    if not eligible:
        _fail(ReadFailure.AS_OF_NO_VERSION)
    if len(eligible) != 1:
        _fail(ReadFailure.AS_OF_AMBIGUOUS)
    return _exact(ledger, definition, eligible[0].key, as_of)


def _read_exact_version_admitted(
    ledger: Ledger,
    definition: Key,
    version: Key,
    *,
    as_of: datetime,
    admitted: frozenset[Key],
) -> ExactReadView:
    """Internal producer-local read; public wire admission remains unchanged."""
    _classified(lambda: instant(as_of), ReadFailure.INVALID_AS_OF)
    return _classified(
        lambda: _exact(ledger, definition, version, as_of, admitted),
        ReadFailure.STORAGE_UNAVAILABLE,
    )


def _read_as_of_admitted(
    ledger: Ledger,
    definition: Key,
    *,
    as_of: datetime,
    admitted: frozenset[Key],
) -> ExactReadView:
    """Internal producer-local as-of selector without public wire expansion."""
    _classified(lambda: instant(as_of), ReadFailure.INVALID_AS_OF)
    _definition(ledger, definition, admitted)
    versions = _classified(
        lambda: ledger.versions_for_definition(definition),
        ReadFailure.STORAGE_UNAVAILABLE,
    )
    if not isinstance(versions, tuple) or any(
        not isinstance(version, DatasetVersion) or version.definition != definition
        for version in versions
    ):
        _fail(ReadFailure.STORAGE_UNAVAILABLE)
    if tuple(sorted(versions, key=lambda item: item.identity)) != versions:
        _fail(ReadFailure.STORAGE_UNAVAILABLE)
    eligible = [
        version
        for version in versions
        if _available_publication(_history(ledger, version), as_of) is not None
    ]
    if not eligible:
        _fail(ReadFailure.AS_OF_NO_VERSION)
    if len(eligible) != 1:
        _fail(ReadFailure.AS_OF_AMBIGUOUS)
    return _exact(ledger, definition, eligible[0].key, as_of, admitted)
