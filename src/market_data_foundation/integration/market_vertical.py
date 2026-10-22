"""Explicit offline D2 -> D3 operations. Importing or reading never starts work.

The caller owns the journal, bytes store, clock evidence and registered decoder.
There is no provider, filesystem discovery, storage fallback or automatic publish.
"""

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Protocol

from market_data_foundation.canonical.market_material import (
    DailyDecoder,
    MaterialBytes,
    MaterialReceipt,
    RowLocator,
    assess_daily,
    byte_digest,
)
from market_data_foundation.canonical.market_persistence import (
    CommitReceipt,
    Verification,
    required_admissions,
)
from market_data_foundation.canonical.market_publication import (
    MarketPublicationRequest,
    MarketVersion,
)
from market_data_foundation.canonical.model import Key, Kind, require
from market_data_foundation.domain_contracts.market_coverage import (
    PointEvidence,
    classify_coverage,
)
from market_data_foundation.domain_contracts.market_planning import (
    Plan,
    PublicationUnit,
    plan_job,
)
from market_data_foundation.domain_contracts.market_universe import (
    EvidenceMode,
    canonical_bytes,
    content_id,
    frozen_tuple,
)
from market_data_foundation.historical.market_daily_journal import page_metadata
from market_data_foundation.historical.market_daily_source import (
    DailyError,
    Failure,
    Lookup,
    LookupStatus,
    OfflineBinding,
    Page,
    PageRequest,
)
from market_data_foundation.historical.market_daily_worker import DailyRunner


class MarketWriter(Protocol):
    """Storage injection; transactions/verification remain the writer's contract."""

    def retain(self, receipt: MaterialReceipt, raw: bytes) -> CommitReceipt: ...
    def commit_version(self, version: MarketVersion) -> CommitReceipt: ...
    def publish(self, request: MarketPublicationRequest) -> CommitReceipt: ...
    def verify(self, operation: str, identity: str, payload: str) -> Verification: ...


@dataclass(frozen=True)
class IntakeLayout:
    """Explicit byte framing and request facts supplied by the capability adapter.

    One D2 response is one D3 page. Offsets refer to unmodified D2 raw bytes.
    requested_at is the original acquisition fact, NOT the offline replay clock.
    """

    rows: tuple[RowLocator, ...]
    request_fields: tuple[tuple[str, str], ...]
    fixture_id: str
    requested_at: datetime | None
    request_clock_evidence: str


@dataclass(frozen=True)
class IntakeReceipt:
    material: MaterialReceipt
    response_identity: str
    request_schema: str
    page_metadata_digest: str
    source_time: datetime | None
    retained_reference: str | None
    request_clock_evidence: str
    commit: CommitReceipt


def response_identity(page: Page) -> str:
    return byte_digest(page.raw + page_metadata(page).encode())


def accept_original(worker: DailyRunner, request: PageRequest, result: Lookup):
    """Preflight identity before D2's explicit reconciliation write boundary.

    D2 itself detects a foreign FOUND page only after reopening the attempt and
    observing the response. Keep that shared implementation unchanged, but reject
    cross-identity input before it can mutate the original journal here.
    """
    validate_frozen_plan(worker.plan)
    worker.journal.check_intent(worker.plan, worker.contract)
    require(worker.source.binding == worker.binding, "offline_binding_changed")
    require(
        canonical_bytes(request)
        == canonical_bytes(worker.request(request.shard, request.page_index))
        and result.request_identity == request.identity,
        "original_request_required",
    )
    if result.page is not None:
        page, binding = result.page, worker.binding
        require(
            page.request_identity == request.identity
            and (page.producer_operation, page.upstream_revision, page.decode_revision)
            == (
                binding.producer_operation,
                binding.upstream_revision,
                binding.decode_revision,
            )
            and (
                binding.mode != EvidenceMode.RETAINED
                or page.retained_reference in binding.retained_materials
            ),
            "original_response_required",
        )
    receipt = worker.journal.receipt(request)
    if receipt is not None:
        original = worker.journal.read_material(receipt)
        require(
            result.status == LookupStatus.FOUND
            and original.raw == result.page.raw
            and page_metadata(original) == page_metadata(result.page),
            "original_response_conflict",
        )
        # D2 uses dataclass equality on this branch. Equivalent UTC instants in
        # a DST fold can compare unequal across zones; only after exact metadata
        # validation, reuse the original journal value without changing identity.
        result = replace(result, page=original)
    return worker.accept_reconciliation(request, result)


def retain_page(
    worker: DailyRunner,
    request: PageRequest,
    layout: IntakeLayout,
    writer: MarketWriter,
) -> IntakeReceipt:
    """Read the original durable D2 receipt, then explicitly retain exact bytes.

    UNKNOWN/REJECTED receipts can be retained but cannot pass D3 assessment.
    No fetch, reconcile, retry, admission, Version or Publication is implicit.
    """
    validate_frozen_plan(worker.plan)
    worker.journal.check_intent(worker.plan, worker.contract)
    require(worker.source.binding == worker.binding, "offline_binding_changed")
    require(
        canonical_bytes(request)
        == canonical_bytes(worker.request(request.shard, request.page_index)),
        "original_request_required",
    )
    original = worker.journal.receipt(request)
    require(original is not None, "d2_material_receipt_required")
    page = worker.journal.read_material(original)
    response = response_identity(page)
    attempt = worker.journal.attempt(request)
    require(
        attempt is not None and attempt["response_identity"] == response,
        "d2_response_identity_mismatch",
    )
    require(bool(layout.request_clock_evidence), "request_clock_evidence_required")
    require(
        tuple(row.identity for row in layout.rows)
        == tuple(row.locator for row in page.rows)
        and all(row.page_identity == response for row in layout.rows),
        "d2_row_inventory_mismatch",
    )
    binding = worker.binding
    route = request.shard.route
    receipt = MaterialReceipt(
        key=Key(Kind.MATERIAL, content_id(("i1.d2.material.r1", request.identity))),
        job=request.shard.job_identity,
        shard=request.shard.identity,
        attempt=request.attempt_identity,
        request=request.identity,
        source=route.source,
        binding=route.binding,
        mapping_revision=route.mapping_revision,
        definition=route.daily.definition,
        instrument=request.shard.instrument,
        capability=binding.capability,
        producer_operation=page.producer_operation,
        upstream_revision=page.upstream_revision,
        decode_revision=page.decode_revision,
        request_fields=layout.request_fields,
        mode=binding.mode,
        digest=original.digest,
        byte_length=original.byte_length,
        locator="mdf-i1://sha256/" + original.digest,
        pages=(response,),
        rows=layout.rows,
        requested_at=layout.requested_at,
        obtained_at=page.obtained_at,
        received_at=page.received_at,
        known_at=page.known_at,
        exception_classification=(
            original.classification.value if original.classification else None
        ),
        effect=(
            "CONFIRMED"
            if original.state == "SUCCEEDED"
            else "FAILED"
            if original.state == "REJECTED"
            else "UNKNOWN"
        ),
        material_kind=page.material_kind,
        fixture_id=layout.fixture_id,
    )
    commit = writer.retain(receipt, page.raw)
    return IntakeReceipt(
        receipt,
        response,
        request.schema_revision,
        byte_digest(original.metadata.encode()),
        page.source_time,
        page.retained_reference,
        layout.request_clock_evidence,
        commit,
    )


def validate_frozen_plan(plan: Plan) -> None:
    """Reuse D1's full derivation, exactly as D2 does, without I/O."""
    require(
        isinstance(plan, Plan) and plan == plan_job(plan.spec),
        "exact_frozen_d1_plan_required",
    )


def validate_plan_evidence(
    plan: Plan,
    unit: PublicationUnit,
    receipts: tuple[MaterialReceipt, ...],
    evidence: tuple[PointEvidence, ...],
) -> None:
    """Check the original frozen scope and defer calendar/lifecycle rules to D1.

    D1 can explain absent structural points. I1 requires explicit evidence for
    every date in this unit instead; never use the manifest to fill missing rows.
    """
    validate_frozen_plan(plan)
    require(unit in plan.units, "frozen_plan_unit_required")
    frozen_tuple(receipts)
    frozen_tuple(evidence)
    require(
        tuple(e.point for e in evidence) == tuple(r.point for r in unit.expand()),
        "coverage_partition_mismatch",
    )
    # Revalidate values loaded from storage, including missing references or a
    # lost NO_TRADE contract that a deserializer may not have checked.
    for item in evidence:
        replace(item)
    shards = {s.identity: s for s in plan.shards}
    require(bool(receipts), "unique_materials_required")
    for receipt in receipts:
        shard = shards.get(receipt.shard)
        require(
            receipt.job == plan.spec.identity
            and shard is not None
            and shard.instrument == unit.instrument == receipt.instrument
            and shard.window.overlaps(unit.window)
            and shard.route.daily.definition == unit.definition == receipt.definition
            and shard.route.source == receipt.source
            and shard.route.binding == receipt.binding
            and shard.route.mapping_revision == receipt.mapping_revision,
            "lineage_scope_mismatch",
        )
    manifest = classify_coverage(plan, evidence)
    require(
        not manifest.extras and not manifest.duplicates, "coverage_partition_mismatch"
    )


def prepare_version(
    key: Key,
    unit: PublicationUnit,
    receipts: tuple[MaterialReceipt, ...],
    evidence: tuple[PointEvidence, ...],
    decoder: DailyDecoder,
    store: MaterialBytes,
    evaluated_at: datetime,
    frozen_at: datetime,
    *,
    plan: Plan,
) -> MarketVersion:
    """Validate frozen D1 evidence then assess; grants no admission or commit."""
    validate_plan_evidence(plan, unit, receipts, evidence)
    assessment = assess_daily(unit, receipts, evidence, decoder, store, evaluated_at)
    version = MarketVersion(key, assessment, frozen_at, ("pending-subjects",))
    return replace(version, admission_subjects=required_admissions(version))


def verify_original(writer: MarketWriter, value) -> Verification:
    """Read-only verification of the value frozen BEFORE the original commit.

    D3 retains a pure MaterialReceipt. I3 callers explicitly freeze and retain
    (MaterialReceipt, physical level), e.g. via writer.material_intent(). Never
    infer a level from returned ACKs, stored facts, material_kind or a locator.
    """
    original = value
    if type(value) is tuple:
        require(
            len(value) == 2 and isinstance(value[0], MaterialReceipt),
            "supported_commit_value_required",
        )
        original, level = value
        require(
            type(level) is str
            and level in ("raw", "source-projection", "status", "metadata"),
            "material_level",
        )
    require(
        isinstance(
            original, (MaterialReceipt, MarketVersion, MarketPublicationRequest)
        ),
        "supported_commit_value_required",
    )
    operation = (
        "material"
        if isinstance(original, MaterialReceipt)
        else "version"
        if isinstance(original, MarketVersion)
        else "publication"
    )
    return writer.verify(operation, content_id(original.key), content_id(value))


@dataclass(frozen=True)
class RetainedPage:
    """Named, already-loaded evidence. No URI is opened by this module."""

    request: PageRequest
    page: Page
    raw_digest: str
    metadata_digest: str


class RetainedPageSource:
    """Bounded D2 offline replay of explicitly named immutable pages only."""

    def __init__(self, binding: OfflineBinding, pages: tuple[RetainedPage, ...]):
        require(binding.mode == EvidenceMode.RETAINED, "retained_binding_required")
        require(type(pages) is tuple, "immutable_retained_manifest_required")
        require(len(pages) <= 1000, "retained_manifest_capacity_exceeded")
        self.binding = binding
        self._pages = {}
        total_bytes = total_metadata = 0
        for retained in pages:
            request, page = retained.request, retained.page
            require(
                request.shard.job_identity == pages[0].request.shard.job_identity
                and request.budget == pages[0].request.budget,
                "retained_manifest_job_mismatch",
            )
            route = request.shard.route
            require(
                request.identity == page.request_identity
                and (
                    route.source,
                    route.binding,
                    route.mapping_revision,
                    route.implementation_identity,
                    route.endpoint_contract,
                )
                == (
                    binding.source,
                    binding.binding,
                    binding.mapping_revision,
                    binding.implementation_identity,
                    binding.endpoint_contract,
                )
                and request.schema_revision == binding.schema_revision
                and (
                    page.producer_operation,
                    page.upstream_revision,
                    page.decode_revision,
                )
                == (
                    binding.producer_operation,
                    binding.upstream_revision,
                    binding.decode_revision,
                )
                and page.retained_reference in binding.retained_materials,
                "retained_original_identity_mismatch",
            )
            require(
                len(page.raw) <= request.budget.max_response_bytes
                and byte_digest(page.raw) == retained.raw_digest
                and byte_digest(page_metadata(page).encode())
                == retained.metadata_digest,
                "retained_integrity_mismatch",
            )
            total_bytes += len(page.raw)
            total_metadata += len(page_metadata(page).encode())
            require(
                len(pages) <= request.budget.max_requests
                and total_bytes <= request.budget.max_material_bytes
                and total_metadata <= 16 * 1024 * 1024,
                "retained_manifest_capacity_exceeded",
            )
            require(request.identity not in self._pages, "duplicate_retained_request")
            self._pages[request.identity] = retained

    def fetch(self, request, charge):
        retained = self._pages.get(request.identity)
        if retained is None:
            # A missing retained input cannot prove the original acquisition
            # was absent, even though this offline replay made no network call.
            raise DailyError(Failure.UNKNOWN)
        require(
            canonical_bytes(retained.request) == canonical_bytes(request),
            "retained_original_identity_mismatch",
        )
        return retained.page

    def lookup_original(self, request):
        retained = self._pages.get(request.identity)
        return Lookup(
            request.identity,
            LookupStatus.FOUND if retained else LookupStatus.UNKNOWN,
            "explicit-retained-manifest-r1",
            retained.page if retained else None,
        )
