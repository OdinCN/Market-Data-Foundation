"""Bounded P3 contracts: eight logical responsibilities, no active source access."""

from dataclasses import dataclass, fields
from datetime import datetime
from enum import StrEnum
from hashlib import sha256


class ContractError(ValueError):
    """A semantic gate failed; messages contain classifications only."""


def require(condition: bool, classification: str) -> None:
    if not condition:
        raise ContractError(classification)


def instant(value: datetime) -> None:
    require(value.tzinfo is not None and value.utcoffset() is not None, "naive_time")


class Kind(StrEnum):
    SOURCE = "SourceOperationRef"
    BINDING = "SourceBindingRevision"
    RUN = "DataRun"
    ACQUISITION = "AcquisitionOccurrence"
    MATERIAL = "EvidenceMaterial"
    DEFINITION = "DatasetDefinition"
    VERSION = "DatasetVersion"
    PUBLICATION = "Publication"


@dataclass(frozen=True)
class Key:
    kind: Kind
    identity: str
    revision: str = ""

    def __post_init__(self) -> None:
        require(isinstance(self.kind, Kind), "invalid_reference_kind")
        require(bool(self.identity), "empty_identity")
        require(
            bool(self.revision)
            == (self.kind in {Kind.SOURCE, Kind.BINDING, Kind.DEFINITION}),
            "revision_required_or_unexpected",
        )


@dataclass(frozen=True)
class Scope:
    """None means public; a restricted scope is an explicit set of opaque names."""

    principals: frozenset[str] | None = None

    def __post_init__(self) -> None:
        require(
            self.principals is None
            or (
                isinstance(self.principals, frozenset)
                and all(":" in p and bool(p.split(":", 1)[1]) for p in self.principals)
            ),
            "invalid_principal_scope",
        )

    def within(self, parent: Scope) -> bool:
        return parent.principals is None or (
            self.principals is not None and self.principals <= parent.principals
        )


class Grade(StrEnum):
    ORIGINAL = "provider_original"
    DECODED = "transport_decoded"
    PROJECTED = "source_projection"
    NORMALIZED = "mdf_normalized"
    ASSESSMENT = "assessment"
    OTHER = "explicit_other"


@dataclass(frozen=True)
class EvidenceRequirement:
    """Grades are classes, not a numeric ladder. Allowed classes are explicit."""

    allowed: frozenset[Grade]
    durable: bool = False
    replayable: bool = False

    def __post_init__(self) -> None:
        require(
            isinstance(self.allowed, frozenset)
            and bool(self.allowed)
            and all(isinstance(g, Grade) for g in self.allowed),
            "invalid_evidence_requirement",
        )


@dataclass(frozen=True)
class Transform:
    identity: str
    revision: str
    parameters: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        require(bool(self.identity and self.revision), "transform_revision_required")
        require(
            len(dict(self.parameters)) == len(self.parameters), "duplicate_parameter"
        )


@dataclass(frozen=True)
class OutputMapping:
    identity: str
    definition: Key
    semantic_revision: str
    temporal_revision: str
    scope: Scope
    evidence: EvidenceRequirement


@dataclass(frozen=True)
class SourceOperationRef:
    identity: str
    contract_revision: str
    owner: str
    product: str
    operation: str
    projection_revision: str
    variant: str
    build_revision: str | None
    registry_revision: str | None
    provider_app_revision: str | None
    access_effect: str
    acquisition_mode: str
    scope: Scope
    temporal_contract: str
    completeness_contract: str
    pagination_contract: str
    freshness_contract: str
    evidence_capability: frozenset[Grade]

    @property
    def key(self) -> Key:
        return Key(Kind.SOURCE, self.identity, self.contract_revision)


@dataclass(frozen=True)
class SourceBindingRevision:
    identity: str
    revision: str
    source: Key
    mappings: tuple[OutputMapping, ...]
    compatibility_policy: str
    limitations: tuple[str, ...] = ()
    silent_fallback: bool = False

    @property
    def key(self) -> Key:
        return Key(Kind.BINDING, self.identity, self.revision)


class RunKind(StrEnum):
    COLLECT = "collect"
    SAMPLE = "sample"
    RECORDING = "recording"
    REPROCESS = "reprocess"
    DERIVE = "derive"
    VALIDATE = "validate"


@dataclass(frozen=True)
class DataRun:
    identity: str
    kind: RunKind
    requested_scope: Scope
    started_at: datetime
    finished_at: datetime | None
    result: str
    binding: Key | None = None
    inputs: tuple[Key, ...] = ()
    transform: Transform | None = None
    trigger: str | None = None
    deadline: datetime | None = None
    budget: int | None = None

    @property
    def key(self) -> Key:
        return Key(Kind.RUN, self.identity)


@dataclass(frozen=True)
class AcquisitionOccurrence:
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
    external_receipt: str | None = None
    provider_data_attempts: int | None = None
    pagination: str = "unknown"
    recovery_summary: str = "unknown"

    @property
    def key(self) -> Key:
        return Key(Kind.ACQUISITION, self.identity)


@dataclass(frozen=True)
class Assessment:
    """Typed evidence embedded in EvidenceMaterial, not another platform object."""

    target: Key
    rule_revision: str
    evaluated_at: datetime
    result: str
    basis: tuple[Key, ...]


@dataclass(frozen=True)
class EvidenceMaterial:
    identity: str
    grade: Grade
    digest: str | None
    scope: Scope
    acquired_from: Key | None = None
    produced_by: Key | None = None
    controlled_ref: str | None = None
    durable: bool = False
    replayable: bool = False
    assessment: Assessment | None = None
    named_class: str | None = None

    @property
    def key(self) -> Key:
        return Key(Kind.MATERIAL, self.identity)


@dataclass(frozen=True)
class DatasetDefinition:
    identity: str
    revision: str
    domain_contract: str
    purpose: str
    source_qualified: bool
    key_semantics: str
    unit_semantics: str
    temporal_semantics: str
    revision_semantics: str
    completeness_semantics: str
    scope: Scope

    @property
    def key(self) -> Key:
        return Key(Kind.DEFINITION, self.identity, self.revision)


@dataclass(frozen=True)
class FiniteScope:
    members: tuple[str, ...]
    start: datetime | None = None
    end: datetime | None = None

    def __post_init__(self) -> None:
        require(isinstance(self.members, tuple), "scope_not_frozen")
        require(len(set(self.members)) == len(self.members), "duplicate_member")
        require(all(m and m != "*" for m in self.members), "unbounded_member")
        require((self.start is None) == (self.end is None), "unbounded_window")
        if self.start is not None:
            instant(self.start)
            instant(self.end)
            require(self.start < self.end, "invalid_window")


@dataclass(frozen=True)
class DatasetVersion:
    identity: str
    definition: Key
    finite_scope: FiniteScope
    scope: Scope
    frozen_at: datetime
    run: Key
    content_digest: str
    binding: Key | None = None
    mapping: str | None = None
    acquisitions: tuple[Key, ...] = ()
    materials: tuple[Key, ...] = ()
    inputs: tuple[Key, ...] = ()
    transform: Transform | None = None
    completeness: str = "unknown"
    quality_evidence: tuple[Key, ...] = ()
    supersedes: Key | None = None

    @property
    def key(self) -> Key:
        return Key(Kind.VERSION, self.identity)


class Decision(StrEnum):
    PUBLISH = "publish"
    WITHDRAW = "withdraw"
    RESTORE = "restore"
    SUPERSEDE = "supersede"


@dataclass(frozen=True)
class PublicationRequest:
    """Caller supplies causation, never the authoritative decision timestamp."""

    identity: str
    occurrence: str
    version: Key
    rule_revision: str
    decision: Decision
    scope: Scope
    evidence: tuple[Key, ...]
    claimed_grades: frozenset[Grade]
    reason: str
    predecessor: Key | None = None
    effective_at: datetime | None = None
    usable_at: datetime | None = None
    required_completeness: str | None = None
    claims_replayable: bool = False
    supersedes_version: Key | None = None


@dataclass(frozen=True)
class Publication:
    request: PublicationRequest
    decision_at: datetime
    usable_at: datetime

    @property
    def key(self) -> Key:
        return Key(Kind.PUBLICATION, self.request.identity)


class Relation(StrEnum):
    DEFINED_BY = "defined_by"
    MAPPED_BY = "mapped_by"
    PRODUCED_BY = "produced_by"
    ACQUIRED_FROM = "acquired_from"
    EVIDENCED_BY = "evidenced_by"
    DERIVED_FROM = "derived_from"
    TRANSFORMED_BY = "transformed_by"
    SUPERSEDES = "supersedes"
    WITHDRAWS = "withdraws"
    RESTORES = "restores"
    SOURCE_CONTRACT = "source_contract"
    TARGETS = "targets"
    ASSESSES = "assesses"


@dataclass(frozen=True)
class Lineage:
    relation: Relation
    target: Key | Transform
    mapping: str | None = None


Record = (
    SourceOperationRef
    | SourceBindingRevision
    | DataRun
    | AcquisitionOccurrence
    | EvidenceMaterial
    | DatasetDefinition
    | DatasetVersion
    | Publication
)


def digest(content: bytes) -> str:
    return sha256(content).hexdigest()


def immutable_shape(record: Record) -> None:
    """Dataclass freezing alone does not freeze caller-provided lists/dicts."""

    def check(value: object) -> None:
        require(not isinstance(value, (list, dict, set, bytearray)), "mutable_contract")
        if isinstance(value, (tuple, frozenset)):
            for item in value:
                check(item)
        elif hasattr(value, "__dataclass_fields__"):
            for field in fields(value):
                check(getattr(value, field.name))
        elif isinstance(value, datetime):
            instant(value)

    check(record)
