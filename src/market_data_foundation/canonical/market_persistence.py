"""Offline atomic ledger substitute, not a PG/Garage durability qualification.

Writes copy state under one lock and swap only after synchronous index work.
Existing reader views retain the old committed snapshot. Blob writes may leave
unreferenced bytes on rollback; they cannot create canonical or published facts.
"""

from copy import deepcopy
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from threading import RLock
from typing import Callable

from market_data_foundation.canonical.market_material import (
    DailyDecoder,
    MaterialBytes,
    MaterialReceipt,
    assess_daily,
    byte_digest,
    read_material,
)
from market_data_foundation.canonical.market_projection import (
    ALGORITHM_REVISION,
    SCHEMA_REVISION,
    AdmissionGuard,
    IndexGeneration,
    MemoryProjectionView,
    MonthKey,
    ProjectionIndex,
    ReadCode,
    SlotKey,
    VersionGuard,
    candidate,
    check,
    index_publication,
    index_version,
    version_slots,
)
from market_data_foundation.canonical.market_publication import (
    MarketPublication,
    MarketPublicationRequest,
    MarketVersion,
    PublicationHead,
    active_at,
    publication_head,
    validate_history,
)
from market_data_foundation.canonical.model import (
    ContractError,
    Decision,
    Key,
    Kind,
    instant,
    require,
)
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageManifest,
    CoverageStatus,
)
from market_data_foundation.domain_contracts.market_planning import PublicationUnit
from market_data_foundation.domain_contracts.market_universe import (
    canonical_bytes,
    content_id,
    text_fields,
)


class Fault(StrEnum):
    AFTER_CANONICAL = "after_canonical"
    AFTER_PROJECTION = "after_projection"
    BEFORE_COMMIT = "before_commit"
    AFTER_COMMIT = "after_commit"


class InjectedFailure(RuntimeError):
    pass


class CommitUnknown(RuntimeError):
    """Only the caller's acknowledgement is unknown, never proof of absence."""


def inject(fault: Fault | None, stage: Fault) -> None:
    if fault == stage:
        raise InjectedFailure(stage.value)


class Verification(StrEnum):
    COMMITTED = "COMMITTED"
    ABSENT = "ABSENT"
    CONFLICT = "CONFLICT"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class CommitReceipt:
    operation: str
    identity: str
    payload_digest: str
    result_digest: str


@dataclass
class _State:
    index: ProjectionIndex
    materials: dict[Key, MaterialReceipt] = field(default_factory=dict)
    versions: dict[Key, MarketVersion] = field(default_factory=dict)
    publications: dict[Key, MarketPublication] = field(default_factory=dict)
    publication_heads: dict[Key, PublicationHead] = field(default_factory=dict)
    occurrences: dict[str, Key] = field(default_factory=dict)
    admissions: dict[str, AdmissionGuard] = field(default_factory=dict)
    receipts: dict[tuple[str, str], CommitReceipt] = field(default_factory=dict)
    manifests: dict[Key, "MarketManifestMaterial"] = field(default_factory=dict)


def required_admissions(version: MarketVersion) -> tuple[str, ...]:
    """Exact subjects to be supplied by a controlled future admission adapter."""
    assessment = version.assessment
    unit = assessment.unit
    subjects = {
        "version:" + content_id(version.key),
        "definition:" + content_id(unit.definition),
        "universe:" + unit.universe_revision,
        "calendar:" + unit.calendar_revision,
        "retention:market.daily.d3.r1",
    }
    for material in assessment.materials:
        subjects.update(
            (
                "source:" + content_id(material.source),
                "binding:" + content_id(material.binding),
                "mapping:"
                + content_id(
                    (material.source, material.binding, material.mapping_revision)
                ),
            )
        )
    return tuple(sorted(subjects))


def _same_coverage_scope(actual: PublicationUnit, planned: PublicationUnit) -> bool:
    """Scope must be exact; date ranges may split or overlap planned units."""
    return (
        actual.definition == planned.definition
        and actual.instrument == planned.instrument
        and actual.universe_revision == planned.universe_revision
        and actual.calendar_revision == planned.calendar_revision
        and actual.timezone == planned.timezone
    )


@dataclass(frozen=True)
class LedgerBackup:
    ledger_binding: str
    materials: tuple[MaterialReceipt, ...]
    versions: tuple[MarketVersion, ...]
    publications: tuple[MarketPublication, ...]
    admissions: tuple[AdmissionGuard, ...]
    receipts: tuple[CommitReceipt, ...]
    covered_months: tuple[MonthKey, ...]
    manifests: tuple["MarketManifestMaterial", ...] = ()
    generation: str = ""


@dataclass(frozen=True)
class MarketCoverageManifest:
    d1_revision: str
    versions: tuple[Key, ...]
    missing_units: tuple[str, ...]
    material_readback: CoverageStatus
    formal_publication: CoverageStatus
    global_status: CoverageStatus


@dataclass(frozen=True)
class MarketManifestMaterial:
    key: Key
    source: CoverageManifest
    assessment: MarketCoverageManifest
    recorded_at: datetime
    ledger_binding: str
    generation: str
    locator: str

    @property
    def payload(self) -> bytes:
        return canonical_bytes(
            (
                self.source,
                self.assessment,
                self.recorded_at,
                self.ledger_binding,
                self.generation,
            )
        )

    @property
    def digest(self) -> str:
        return byte_digest(self.payload)


class _Reader:
    """Facade exposes only open(), never writer methods or repair effects."""

    def __init__(self, ledger) -> None:
        self.__ledger = ledger

    def open(
        self, ledger_binding: str, expected_generation: str | None = None
    ) -> MemoryProjectionView:
        ledger = self.__ledger
        with ledger._lock:
            check(ledger.available, ReadCode.UNAVAILABLE)
            state = ledger._state
            generation = state.index.generation
            check(
                generation.schema_revision == SCHEMA_REVISION
                and generation.algorithm_revision == ALGORITHM_REVISION
            )
            check(ledger_binding == generation.ledger_binding, ReadCode.BINDING)
            check(
                expected_generation is None
                or expected_generation == generation.generation,
                ReadCode.GENERATION,
            )
            check(generation.ready, ReadCode.NOT_READY)
            now = ledger._clock()
            instant(now)
            return MemoryProjectionView(state, now, ledger._store)


class MemoryMarketLedger:
    replacement_groups_supported = False

    def __init__(
        self,
        ledger_binding: str,
        store: MaterialBytes,
        decoders: tuple[DailyDecoder, ...],
        clock: Callable[[], datetime],
    ) -> None:
        text_fields(ledger_binding)
        self._store = store
        self._clock = clock
        self._lock = RLock()
        self._decoders = {content_id(item.contract): item for item in decoders}
        require(len(self._decoders) == len(decoders), "duplicate_decoder_contract")
        self._state = _State(
            ProjectionIndex(IndexGeneration(ledger_binding, "initial", False))
        )
        self.available = True
        self.reader = _Reader(self)

    def _transaction(
        self,
        operation: str,
        identity: str,
        payload: object,
        mutate: Callable,
        fault: Fault | None,
    ) -> CommitReceipt:
        payload_digest = content_id(payload)
        with self._lock:
            require(self.available, "ledger_unavailable")
            previous = self._state.receipts.get((operation, identity))
            if previous is not None:
                require(
                    previous.payload_digest == payload_digest,
                    "same_identity_different_content",
                )
                return previous
            draft = deepcopy(self._state)
            result = mutate(draft)
            inject(fault, Fault.BEFORE_COMMIT)
            receipt = CommitReceipt(
                operation, identity, payload_digest, content_id(result)
            )
            draft.receipts[(operation, identity)] = receipt
            self._state = draft
            if fault == Fault.AFTER_COMMIT:
                raise CommitUnknown("commit_acknowledgement_unknown")
            return receipt

    def verify(
        self, operation: str, identity: str, payload_digest: str
    ) -> Verification:
        """Read only original-identity reconciliation; no retry or new occurrence."""
        with self._lock:
            if not self.available:
                return Verification.UNKNOWN
            receipt = self._state.receipts.get((operation, identity))
            if receipt is None:
                return Verification.ABSENT
            return (
                Verification.COMMITTED
                if receipt.payload_digest == payload_digest
                else Verification.CONFLICT
            )

    def retain(
        self, receipt: MaterialReceipt, raw: bytes, fault: Fault | None = None
    ) -> CommitReceipt:
        require(
            type(raw) is bytes
            and len(raw) == receipt.byte_length
            and byte_digest(raw) == receipt.digest,
            "material_bytes_mismatch",
        )
        require(receipt.byte_length <= 8 * 1024 * 1024, "material_capacity_exceeded")

        def mutate(state):
            require(receipt.key not in state.materials, "material_identity_conflict")
            require(receipt.key not in state.manifests, "material_identity_conflict")
            try:
                self._store.put(receipt.locator, raw)
            except ContractError:
                raise
            except Exception:
                raise ContractError("material_write_unavailable") from None
            read_material(
                self._store, receipt
            )  # independent adapter read, before canonical
            state.materials[receipt.key] = receipt
            inject(fault, Fault.AFTER_CANONICAL)
            return receipt

        return self._transaction(
            "material", content_id(receipt.key), receipt, mutate, fault
        )

    def _utc_now(self) -> datetime:
        at = self._clock()
        instant(at)
        return at.astimezone(UTC)

    def _validate_version(self, state: _State, version: MarketVersion) -> None:
        assessment = version.assessment
        for receipt in assessment.materials:
            # Independent acquisition facts are never inferred from received/known.
            # UNKNOWN remains retainable, but cannot qualify a Version for service.
            require(
                receipt.requested_at is not None
                and receipt.obtained_at is not None
                and receipt.requested_at.astimezone(UTC)
                <= receipt.obtained_at.astimezone(UTC)
                <= version.frozen_at.astimezone(UTC),
                "material_acquisition_timing_unverified",
            )
        require(
            set(required_admissions(version)) <= set(version.admission_subjects),
            "required_admission_subject_missing",
        )
        require(
            # Dataclass datetime equality ignores fold within one timezone and
            # can reject equivalent instants across zones during a repeated hour.
            all(
                canonical_bytes(state.materials.get(item.key)) == canonical_bytes(item)
                for item in assessment.materials
            ),
            "canonical_material_missing",
        )
        decoder = self._decoders.get(content_id(assessment.contract))
        require(decoder is not None, "decoder_contract_not_registered")
        verified = assess_daily(
            assessment.unit,
            assessment.materials,
            assessment.evidence,
            decoder,
            self._store,
            assessment.evaluated_at,
        )
        require(verified == assessment, "assessment_replay_mismatch")
        require(
            version.frozen_at.astimezone(UTC) <= self._utc_now(),
            "version_frozen_in_future",
        )

    def commit_version(
        self, version: MarketVersion, fault: Fault | None = None
    ) -> CommitReceipt:
        def mutate(state):
            require(version.key not in state.versions, "version_identity_conflict")
            self._validate_version(state, version)
            state.versions[version.key] = version
            state.publication_heads[version.key] = publication_head(version.key, ())
            inject(fault, Fault.AFTER_CANONICAL)
            index_version(state.index, version)
            inject(fault, Fault.AFTER_PROJECTION)
            return version

        return self._transaction(
            "version", content_id(version.key), version, mutate, fault
        )

    def set_admission(
        self, subject: str, revision: str, allowed: bool, fault: Fault | None = None
    ) -> CommitReceipt:
        """Offline explicit facts only; this method does not grant Source rights."""
        text_fields(subject, revision)
        require(type(allowed) is bool, "explicit_admission_state_required")
        payload = (subject, revision, allowed)

        def mutate(state):
            old = state.admissions.get(subject)
            guard = AdmissionGuard(
                subject, revision, allowed, old.change + 1 if old else 1
            )
            state.admissions[subject] = guard
            return guard

        return self._transaction(
            "admission", content_id((subject, revision)), payload, mutate, fault
        )

    def publish(
        self, request: MarketPublicationRequest, fault: Fault | None = None
    ) -> CommitReceipt:
        def mutate(state):
            require(
                request.key not in state.publications, "publication_identity_conflict"
            )
            require(
                request.occurrence not in state.occurrences,
                "publication_occurrence_conflict",
            )
            require(request.version in state.versions, "publication_version_missing")
            version = state.versions[request.version]
            if request.decision in (Decision.PUBLISH, Decision.RESTORE):
                self._validate_version(state, version)
                require(
                    all(
                        state.admissions.get(subject)
                        and state.admissions[subject].allowed
                        for subject in version.admission_subjects
                    ),
                    "publication_admission_denied",
                )
            at = (
                self._clock()
            )  # authoritative adapter clock, never a request decision_at
            instant(at)
            require(
                at.astimezone(UTC) >= version.frozen_at.astimezone(UTC),
                "publication_before_version",
            )
            event = MarketPublication(request, at, request.usable_at or at)
            previous = state.index.histories[request.version]
            anchor = state.publication_heads.get(request.version)
            require(
                anchor == publication_head(request.version, previous)
                and state.index.guards.get(request.version)
                == VersionGuard(
                    request.version,
                    anchor.count,
                    anchor.history_digest,
                    anchor.count,
                ),
                "publication_head_mismatch",
            )
            history = (*previous, event)
            validate_history(request.version, history)
            state.publications[event.key] = event
            state.occurrences[request.occurrence] = event.key
            state.publication_heads[request.version] = publication_head(
                request.version, history
            )
            inject(fault, Fault.AFTER_CANONICAL)
            index_publication(state.index, event)
            inject(fault, Fault.AFTER_PROJECTION)
            return event

        return self._transaction(
            "publication", content_id(request.key), request, mutate, fault
        )

    def checkpoint(self) -> LedgerBackup:
        with self._lock:
            state = self._state
            return LedgerBackup(
                state.index.generation.ledger_binding,
                tuple(state.materials.values()),
                tuple(state.versions.values()),
                tuple(state.publications.values()),
                tuple(state.admissions.values()),
                tuple(state.receipts.values()),
                tuple(sorted(state.index.months, key=content_id)),
                tuple(state.manifests.values()),
                state.index.generation.generation,
            )

    def _reindex(
        self,
        state: _State,
        generation: str,
        months: tuple[MonthKey, ...],
        fault: Fault | None,
    ) -> ProjectionIndex:
        self._validate_recovery_receipts(state)
        for material in state.manifests.values():
            self._read_manifest(material)
        index = ProjectionIndex(
            IndexGeneration(state.index.generation.ledger_binding, generation, False),
            set(months),
        )
        for version in state.versions.values():
            self._validate_version(state, version)
            index_version(index, version)
        for event in sorted(
            state.publications.values(),
            key=lambda value: value.decision_at.astimezone(UTC),
        ):
            require(
                event.request.version in state.versions, "restore_orphan_publication"
            )
            index_publication(index, event)
        for key, history in index.histories.items():
            validate_history(key, history)
            require(
                all(
                    state.versions[key].frozen_at.astimezone(UTC)
                    <= event.decision_at.astimezone(UTC)
                    <= self._utc_now()
                    for event in history
                ),
                "restore_publication_before_version",
            )
        # Recovery trusts canonical facts + receipts, never old heads or guards.
        state.publication_heads = {
            key: publication_head(key, history)
            for key, history in index.histories.items()
        }
        inject(fault, Fault.AFTER_PROJECTION)
        index.generation = replace(index.generation, ready=True)
        return index

    def rebuild(
        self,
        generation: str,
        covered_months: tuple[MonthKey, ...],
        fault: Fault | None = None,
    ) -> CommitReceipt:
        """Explicit stop-writes rebuild; readers never initiate this operation."""
        text_fields(generation)

        def mutate(state):
            require(
                generation != state.index.generation.generation
                and all(
                    (operation, generation) not in state.receipts
                    for operation in ("restore", "rebuild")
                ),
                "new_generation_required",
            )
            state.index = self._reindex(state, generation, covered_months, fault)
            return state.index.generation

        return self._transaction(
            "rebuild", generation, (generation, covered_months), mutate, fault
        )

    def suspend(self) -> None:
        with self._lock:
            state = deepcopy(self._state)
            state.index.generation = replace(state.index.generation, ready=False)
            self._state = state

    def restore(
        self, backup: LedgerBackup, generation: str, fault: Fault | None = None
    ) -> CommitReceipt:
        """Exact offline recovery validates graph before atomic generation switch.

        No timestamp is synthesized for old facts. No projection in a backup is
        trusted. The byte store must already contain independently readable data.
        """
        text_fields(generation)

        def mutate(state):
            require(
                backup.ledger_binding == state.index.generation.ledger_binding,
                "restore_binding_mismatch",
            )
            require(
                not any(
                    (
                        state.versions,
                        state.materials,
                        state.publications,
                        state.manifests,
                        state.admissions,
                        state.receipts,
                    )
                ),
                "restore_requires_empty_ledger",
            )
            require(
                generation != state.index.generation.generation,
                "new_generation_required",
            )
            require(
                generation != backup.generation
                and all(
                    item.operation not in ("rebuild", "restore")
                    or item.identity != generation
                    for item in backup.receipts
                ),
                "new_generation_required",
            )
            for items, key in (
                (backup.materials, lambda item: item.key),
                (backup.versions, lambda item: item.key),
                (backup.publications, lambda item: item.key),
                (backup.admissions, lambda item: item.subject),
                (backup.receipts, lambda item: (item.operation, item.identity)),
                (backup.manifests, lambda item: item.key),
            ):
                require(
                    len({key(item) for item in items}) == len(items),
                    "restore_duplicate_identity",
                )
            for receipt in backup.materials:
                read_material(self._store, receipt)
                state.materials[receipt.key] = receipt
            state.versions = {item.key: item for item in backup.versions}
            state.publications = {item.key: item for item in backup.publications}
            state.occurrences = {
                item.request.occurrence: item.key for item in backup.publications
            }
            require(
                len(state.occurrences) == len(backup.publications),
                "restore_duplicate_occurrence",
            )
            state.admissions = {item.subject: item for item in backup.admissions}
            state.receipts = {
                (item.operation, item.identity): item for item in backup.receipts
            }
            state.manifests = {item.key: item for item in backup.manifests}
            require(
                not state.manifests.keys() & state.materials.keys(),
                "restore_duplicate_material_identity",
            )
            for item in backup.manifests:
                require(
                    item.ledger_binding == backup.ledger_binding
                    and item.source.revision == item.assessment.d1_revision,
                    "recovery_manifest_binding_mismatch",
                )
                require(
                    all(key in state.versions for key in item.assessment.versions),
                    "recovery_manifest_version_missing",
                )
                self._read_manifest(item)
            self._validate_recovery_receipts(state)
            inject(fault, Fault.AFTER_CANONICAL)
            state.index = self._reindex(state, generation, backup.covered_months, fault)
            return state.index.generation

        return self._transaction("restore", generation, backup, mutate, fault)

    @staticmethod
    def _validate_recovery_receipts(state: _State) -> None:
        facts = [
            ("material", content_id(item.key), item, item)
            for item in state.materials.values()
        ]
        facts += [
            ("version", content_id(item.key), item, item)
            for item in state.versions.values()
        ]
        facts += [
            ("publication", content_id(item.key), item.request, item)
            for item in state.publications.values()
        ]
        facts += [
            (
                "admission",
                content_id((item.subject, item.revision)),
                (item.subject, item.revision, item.allowed),
                item,
            )
            for item in state.admissions.values()
        ]
        facts += [
            ("manifest", content_id(item.key), item, item)
            for item in state.manifests.values()
        ]
        require(
            all(item.change > 0 for item in state.admissions.values()),
            "recovery_admission_guard_invalid",
        )
        for operation, identity, payload, result in facts:
            receipt = state.receipts.get((operation, identity))
            require(
                receipt is not None
                and receipt.payload_digest == content_id(payload)
                and receipt.result_digest == content_id(result),
                "recovery_receipt_mismatch",
            )
        # Reject receipts claiming missing material/version/publication facts.
        actual = {(operation, identity) for operation, identity, _, _ in facts}
        require(
            all(
                operation not in {"material", "version", "publication", "manifest"}
                or (operation, identity) in actual
                for operation, identity in state.receipts
            ),
            "recovery_receipt_orphan",
        )

    def _validate_coverage_projection(
        self, state: _State, unit: PublicationUnit, now: datetime
    ) -> tuple[MarketVersion, ...]:
        """Offline global audit, including strict subranges and unpublished facts.

        Canonical reconciliation deliberately scans the in-memory substitute. This
        is not the bounded D4 read path or a qualified database query plan.
        """
        slots = tuple(
            SlotKey(unit.definition, unit.instrument, item.point.day)
            for item in unit.expand()
        )
        expected = {slot: [] for slot in slots}
        relevant = {}
        for version in state.versions.values():
            overlap = tuple(slot for slot in version_slots(version) if slot in expected)
            if overlap:
                relevant[version.key] = version
                for slot in overlap:
                    expected[slot].append(candidate(version))
        view = self.reader.open(state.index.generation.ledger_binding)
        records = view.candidates(slots)
        require(
            records
            == tuple(
                tuple(
                    sorted(
                        expected[slot], key=lambda item: canonical_bytes(item.version)
                    )
                )
                for slot in slots
            ),
            "coverage_candidates_inconsistent",
        )
        keys = tuple(sorted(relevant, key=canonical_bytes))
        view.versions(keys)
        # Validates full chains and count/digest/change.
        histories = view.histories(keys)
        active = set()
        for key, history in zip(keys, histories, strict=True):
            canonical_history = tuple(
                sorted(
                    (
                        event
                        for event in state.publications.values()
                        if event.request.version == key
                    ),
                    key=lambda event: event.decision_at.astimezone(UTC),
                )
            )
            require(history == canonical_history, "coverage_history_inconsistent")
            self._validate_version(state, relevant[key])
            view.admission_guards(key)  # Validate facts even for inactive candidates.
            if active_at(history, now):
                require(
                    _same_coverage_scope(relevant[key].assessment.unit, unit),
                    "coverage_scope_mismatch",
                )
                active.add(key)
                view.content(key)  # Independent readback and current service guards.
        require(
            all(
                sum(item.version in active for item in group) == 1 for group in records
            ),
            "coverage_candidate_not_unique",
        )
        return tuple(relevant[key] for key in keys if key in active)

    def coverage_manifest(self, manifest: CoverageManifest) -> MarketCoverageManifest:
        """All D1 axes remain independent; partial unit success cannot lift globals."""
        with self._lock:
            state = self._state
            selected = set()
            require(self.available, "ledger_unavailable")
            missing = []
            material_ok = True
            published = True
            now = self._clock()
            entries = {entry.point: entry for entry in manifest.entries}
            for unit in manifest.plan.units:
                points = {item.point for item in unit.expand()}
                matches = tuple(
                    version
                    for version in state.versions.values()
                    if _same_coverage_scope(version.assessment.unit, unit)
                    and any(
                        item.point in points for item in version.assessment.evidence
                    )
                )
                if not matches:
                    missing.append(unit.identity)
                    material_ok = published = False
                    continue
                # Retained history and unpublished siblings do not compete for
                # current coverage. Reconcile all candidates, then select the
                # verified unique active Version at each natural-date slot.
                try:
                    sources = self._validate_coverage_projection(state, unit, now)
                except Exception:
                    published = False
                    # Material readback remains a separate axis even when no
                    # candidate has been published or the projection is broken.
                    sources = matches
                explained = set()
                for version in sources:
                    try:
                        self._validate_version(state, version)
                    except Exception:
                        material_ok = False
                        continue
                    selected.add(version.key)
                    explained.update(
                        item.point
                        for item in version.assessment.evidence
                        if item.point in points
                        and entries.get(item.point) is not None
                        and entries[item.point].evidence == item
                        and entries[item.point].classification == item.classification
                    )
                if explained != points:
                    missing.append(unit.identity)
                    material_ok = False
            material = (
                CoverageStatus.COMPLETE if material_ok else CoverageStatus.PARTIAL
            )
            publication = (
                CoverageStatus.COMPLETE if published else CoverageStatus.PARTIAL
            )
            complete = (
                material_ok
                and published
                and not manifest.extras
                and not manifest.duplicates
                and all(
                    value == CoverageStatus.COMPLETE
                    for value in (
                        manifest.axes.member_enumeration,
                        manifest.axes.lifecycle_calendar,
                        manifest.axes.bar_obligations,
                    )
                )
            )
            return MarketCoverageManifest(
                manifest.revision,
                tuple(sorted(selected, key=canonical_bytes)),
                tuple(missing),
                material,
                publication,
                CoverageStatus.COMPLETE if complete else CoverageStatus.UNKNOWN,
            )

    def _read_manifest(self, material: MarketManifestMaterial) -> bytes:
        require(len(material.payload) <= 8 * 1024 * 1024, "manifest_capacity_exceeded")
        try:
            raw = self._store.read(material.locator, len(material.payload))
        except Exception:
            raise ContractError("manifest_read_unavailable") from None
        require(raw == material.payload, "manifest_readback_mismatch")
        return raw

    def freeze_manifest(
        self, key: Key, source: CoverageManifest, fault: Fault | None = None
    ) -> CommitReceipt:
        """Immutable global assessment Material; no new Publication is implied."""
        require(key.kind == Kind.MATERIAL, "manifest_material_key_required")
        with self._lock:
            previous = self._state.manifests.get(key)
            if previous is not None:
                require(previous.source == source, "same_identity_different_content")
                return self._state.receipts[("manifest", content_id(key))]
            material = MarketManifestMaterial(
                key,
                source,
                self.coverage_manifest(source),
                self._clock(),
                self._state.index.generation.ledger_binding,
                self._state.index.generation.generation,
                "",
            )
            material = replace(
                material, locator="memory://market-manifest/" + material.digest
            )

            def mutate(state):
                require(key not in state.materials, "material_identity_conflict")
                require(
                    len(material.payload) <= 8 * 1024 * 1024,
                    "manifest_capacity_exceeded",
                )
                try:
                    self._store.put(material.locator, material.payload)
                except ContractError:
                    raise
                except Exception:
                    raise ContractError("manifest_write_unavailable") from None
                self._read_manifest(material)
                state.manifests[key] = material
                inject(fault, Fault.AFTER_CANONICAL)
                return material

            return self._transaction(
                "manifest", content_id(key), material, mutate, fault
            )

    def read_manifest(self, key: Key) -> MarketManifestMaterial:
        with self._lock:
            require(self.available, "ledger_unavailable")
            require(key in self._state.manifests, "manifest_not_found")
            material = self._state.manifests[key]
            self._read_manifest(material)
            return material
