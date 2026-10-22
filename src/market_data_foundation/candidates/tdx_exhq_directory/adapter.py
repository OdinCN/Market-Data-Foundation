"""Receipt-bound current directory assembly; no IO, canonical writes or admission."""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from market_data_foundation.qualification.contracts import Capability

from .schema import (
    DECODE_REVISION,
    IDENTITY_NAMESPACE,
    MAX_DIRECTORY_BYTES,
    MAX_DIRECTORY_PAGES,
    MAX_DIRECTORY_ROWS,
    MAX_PAYLOAD_BYTES,
    SCHEMA_REVISION,
    SDK_COMMIT,
    SDK_VERSION,
    EvidenceMode,
    Operation,
    content_hash,
    digest,
    require,
    text,
    timestamp,
    uint,
)
from .wire import (
    ExInstrument,
    ExMarket,
    PageRequest,
    decode_count,
    decode_instruments,
    decode_markets,
)


@dataclass(frozen=True)
class CaptureContext:
    source: str
    binding: str
    mapping_revision: str
    capture_id: str
    mode: EvidenceMode
    # The actual downstream Go binary hash is not inferred from a Git tag.
    producer_derived_sha256: str | None = None

    def __post_init__(self) -> None:
        for value in (
            self.source,
            self.binding,
            self.mapping_revision,
            self.capture_id,
        ):
            text(value)
        require(type(self.mode) is EvidenceMode, "offline_evidence_mode_required")
        if self.producer_derived_sha256 is not None:
            _sha256(self.producer_derived_sha256)


def _sha256(value: str) -> None:
    require(
        type(value) is str
        and len(value) == 64
        and all(char in "0123456789abcdef" for char in value),
        "invalid_sha256",
    )


@dataclass(frozen=True)
class OfflineReceipt:
    context: CaptureContext
    operation: Operation
    payload: bytes = field(repr=False)
    expected_sha256: str
    locator: str
    obtained_at: datetime | None = None
    received_at: datetime | None = None
    known_at: datetime | None = None
    request: PageRequest | None = None
    sdk_commit: str = SDK_COMMIT

    def __post_init__(self) -> None:
        require(type(self.context) is CaptureContext, "capture_context_required")
        require(type(self.operation) is Operation, "operation_required")
        require(type(self.payload) is bytes, "immutable_payload_bytes_required")
        require(len(self.payload) <= MAX_PAYLOAD_BYTES, "payload_budget_exceeded")
        _sha256(self.expected_sha256)
        require(
            digest(self.payload) == self.expected_sha256, "material_digest_mismatch"
        )
        require(self.sdk_commit == SDK_COMMIT, "unsupported_sdk_commit")
        text(self.locator)
        require(
            (
                self.operation == Operation.INSTRUMENTS
                and type(self.request) is PageRequest
            )
            or (self.operation != Operation.INSTRUMENTS and self.request is None),
            "operation_request_mismatch",
        )
        clocks = (self.obtained_at, self.received_at, self.known_at)
        for clock in clocks:
            timestamp(clock)
        known_clocks = tuple(
            clock.astimezone(UTC) for clock in clocks if clock is not None
        )
        require(
            all(left <= right for left, right in zip(known_clocks, known_clocks[1:])),
            "receipt_clock_order",
        )

    @property
    def material(self) -> MaterialReceipt:
        return MaterialReceipt(
            self.context,
            self.operation,
            self.request,
            self.expected_sha256,
            len(self.payload),
            self.locator,
            self.obtained_at,
            self.received_at,
            self.known_at,
        )


@dataclass(frozen=True)
class MaterialReceipt:
    """D2/D3 integration value, not a canonical Material key or publication."""

    context: CaptureContext
    operation: Operation
    request: PageRequest | None
    raw_sha256: str
    byte_length: int
    locator: str
    obtained_at: datetime | None
    received_at: datetime | None
    known_at: datetime | None
    sdk_version: str = SDK_VERSION
    sdk_commit: str = SDK_COMMIT
    decode_revision: str = DECODE_REVISION
    source_published_at: None = None
    capability: Capability = Capability.SECURITY_UNIVERSE

    @property
    def identity(self) -> str:
        return content_hash(self)


@dataclass(frozen=True)
class RowLocator:
    receipt_identity: str
    raw_sha256: str
    row_index: int
    byte_offset: int
    byte_length: int = 64


@dataclass(frozen=True)
class UnknownEconomics:
    currency: None = None
    lot: None = None
    multiplier: None = None
    expiry: None = None
    calendar_revision: None = None
    unit: None = None
    source_rights: str = "UNKNOWN"
    status: str = "UNKNOWN"


@dataclass(frozen=True)
class DirectoryMapping:
    """Source identity association only; code reuse across time stays unresolved."""

    namespace: str
    instrument_id: str
    source: str
    binding: str
    mapping_revision: str
    market: int
    category: int
    code: str
    market_record: ExMarket | None
    association_status: str
    instrument_evidence: RowLocator
    market_evidence: RowLocator | None
    known_at: datetime | None
    economics: UnknownEconomics = UnknownEconomics()
    valid_from: None = None
    valid_until: None = None
    membership_mode: str = "snapshot"
    qualification: str = "UNKNOWN"

    @property
    def key(self) -> tuple[int, int, str]:
        return self.market, self.category, self.code


@dataclass(frozen=True)
class DirectorySnapshot:
    context: CaptureContext
    total_count: int
    market_declared_count: int
    skipped_market_slots: tuple[int, ...]
    markets: tuple[ExMarket, ...]
    instruments: tuple[ExInstrument, ...]
    mappings: tuple[DirectoryMapping, ...]
    materials: tuple[MaterialReceipt, ...]
    known_at: datetime | None
    schema_revision: str = SCHEMA_REVISION
    membership_mode: str = "snapshot"
    completeness: str = "bounded_count_and_pages_consistent"
    qualification: str = "UNKNOWN"

    @property
    def directory_hash(self) -> str:
        """Includes exact page/material identity, provenance, order and UNKNOWNs."""
        return content_hash(self)

    @property
    def market_table_hash(self) -> str:
        return self.materials[0].raw_sha256

    @property
    def mapping_hash(self) -> str:
        return content_hash(self.mappings)

    @property
    def unresolved_associations(self) -> tuple[DirectoryMapping, ...]:
        return tuple(
            mapping
            for mapping in self.mappings
            if mapping.association_status != "EXACT_RESPONSE_MARKET_CATEGORY"
        )

    def mapping_for(self, market: int, category: int, code: str) -> DirectoryMapping:
        # No numeric coercion, stripping, padding, aliases or static-table fallback.
        uint(market, 8, "market")
        uint(category, 8, "category")
        text(code)
        matches = tuple(m for m in self.mappings if m.key == (market, category, code))
        require(len(matches) == 1, "exact_mapping_not_found")
        return matches[0]


def _row_locator(receipt: OfflineReceipt, header: int, index: int) -> RowLocator:
    return RowLocator(
        receipt.material.identity, receipt.expected_sha256, index, header + index * 64
    )


def build_directory(
    markets: OfflineReceipt,
    count_before: OfflineReceipt,
    pages: tuple[OfflineReceipt, ...],
    count_after: OfflineReceipt,
) -> DirectorySnapshot:
    """Assemble a frozen explicitly retained/synthetic capture, with count bookends.

    Equal counts cannot prove server atomicity or historical membership. A missing
    exact market/category association is retained as UNKNOWN, never filled by enum.
    """
    require(type(pages) is tuple, "immutable_pages_required")
    require(len(pages) <= MAX_DIRECTORY_PAGES, "page_budget_exceeded")
    receipts = (markets, count_before, *pages, count_after)
    require(
        all(type(r) is OfflineReceipt for r in receipts), "offline_receipt_required"
    )
    require(
        markets.operation == Operation.MARKETS
        and count_before.operation == count_after.operation == Operation.COUNT
        and all(r.operation == Operation.INSTRUMENTS for r in pages),
        "operation_sequence_mismatch",
    )
    require(
        all(r.context == markets.context for r in receipts), "mixed_capture_context"
    )
    require(
        len({r.locator for r in receipts}) == len(receipts),
        "duplicate_material_locator",
    )
    require(
        sum(len(r.payload) for r in receipts) <= MAX_DIRECTORY_BYTES,
        "directory_byte_budget_exceeded",
    )
    # Known per-operation clocks must agree with the declared capture sequence.
    for name in ("obtained_at", "received_at", "known_at"):
        clocks = tuple(
            getattr(r, name).astimezone(UTC)
            for r in receipts
            if getattr(r, name) is not None
        )
        require(
            all(left <= right for left, right in zip(clocks, clocks[1:])),
            "capture_clock_order",
        )
    table = decode_markets(markets.payload)
    total = decode_count(count_before.payload)
    require(total == decode_count(count_after.payload), "count_drift")
    require(total <= MAX_DIRECTORY_ROWS, "directory_row_budget_exceeded")
    market_by_key = {row.key: row for row in table.rows}
    instruments = []
    mappings = []
    seen = set()
    position = 0
    known_at = (
        max((r.known_at for r in receipts), key=lambda value: value.astimezone(UTC))
        if all(r.known_at is not None for r in receipts)
        else None
    )
    for receipt in pages:
        request = receipt.request
        require(request.count > 0, "zero_count_page")
        require(position < total, "unexpected_page_after_total")
        require(request.start == position, "page_gap_overlap_or_order")
        page = decode_instruments(receipt.payload, request)
        require(
            page.declared_count == min(request.count, total - position),
            "short_page_or_count_mismatch",
        )
        for row in page.rows:
            require(row.key not in seen, "duplicate_instrument_identity")
            seen.add(row.key)
            market = market_by_key.get((row.market, row.category))
            mapping = DirectoryMapping(
                namespace=IDENTITY_NAMESPACE,
                instrument_id=content_hash(
                    (IDENTITY_NAMESPACE, markets.context.source, *row.key)
                ),
                source=markets.context.source,
                binding=markets.context.binding,
                mapping_revision=markets.context.mapping_revision,
                market=row.market,
                category=row.category,
                code=row.code,
                market_record=market,
                association_status=(
                    "EXACT_RESPONSE_MARKET_CATEGORY"
                    if market is not None
                    else "UNKNOWN_MARKET_CATEGORY"
                ),
                instrument_evidence=_row_locator(receipt, 6, row.row_index),
                market_evidence=(
                    _row_locator(markets, 2, market.row_index) if market else None
                ),
                known_at=known_at,
            )
            instruments.append(row)
            mappings.append(mapping)
        position += page.declared_count
    require(position == total, "incomplete_directory_pages")
    return DirectorySnapshot(
        markets.context,
        total,
        table.declared_count,
        table.skipped_slots,
        table.rows,
        tuple(instruments),
        tuple(mappings),
        tuple(receipt.material for receipt in receipts),
        known_at,
    )
