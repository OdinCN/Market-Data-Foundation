"""Offline daily material contract; a receipt alone is never canonical evidence.

Only the synthetic JSON decoder is supplied. Provider decoders must implement the
explicit port and bind their own field/transform evidence; no TDX unit inference.
"""

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
from typing import Protocol

from market_data_foundation.canonical.model import (
    ContractError,
    Key,
    Kind,
    instant,
    require,
)
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    PointEvidence,
)
from market_data_foundation.domain_contracts.market_planning import (
    DailyDefinition,
    GridPoint,
    PublicationUnit,
)
from market_data_foundation.domain_contracts.market_universe import (
    EvidenceMode,
    InstrumentIdentity,
    canonical_bytes,
    content_id,
    frozen_tuple,
    text_fields,
)
from market_data_foundation.qualification.contracts import Capability


def byte_digest(raw: bytes) -> str:
    return sha256(raw).hexdigest()


@dataclass(frozen=True)
class RowLocator:
    identity: str
    page_identity: str
    offset: int
    length: int
    digest: str

    def __post_init__(self) -> None:
        text_fields(self.identity, self.page_identity, self.digest)
        require(type(self.offset) is int and self.offset >= 0, "invalid_row_offset")
        require(type(self.length) is int and self.length > 0, "invalid_row_length")


@dataclass(frozen=True)
class MaterialReceipt:
    key: Key
    job: str
    shard: str
    attempt: str
    request: str
    source: Key
    binding: Key
    mapping_revision: str
    definition: Key
    instrument: InstrumentIdentity
    capability: Capability
    producer_operation: str
    upstream_revision: str
    decode_revision: str
    request_fields: tuple[tuple[str, str], ...]
    mode: EvidenceMode
    digest: str
    byte_length: int
    locator: str
    pages: tuple[str, ...]
    rows: tuple[RowLocator, ...]
    obtained_at: datetime | None
    received_at: datetime | None
    known_at: datetime | None
    exception_classification: str | None = None
    effect: str = "CONFIRMED"
    material_kind: str = "raw"
    fixture_id: str = ""
    requested_at: datetime | None = None

    def __post_init__(self) -> None:
        require(
            self.key.kind == Kind.MATERIAL
            and self.source.kind == Kind.SOURCE
            and self.binding.kind == Kind.BINDING
            and self.definition.kind == Kind.DEFINITION,
            "material_receipt_key_mismatch",
        )
        text_fields(
            self.job,
            self.shard,
            self.attempt,
            self.request,
            self.mapping_revision,
            self.capability,
            self.producer_operation,
            self.upstream_revision,
            self.decode_revision,
            self.digest,
            self.locator,
            self.fixture_id,
        )
        require(
            isinstance(self.capability, Capability), "canonical_capability_required"
        )
        require(isinstance(self.mode, EvidenceMode), "evidence_mode_required")
        require(self.mode != EvidenceMode.LIVE, "offline_material_only")
        require(
            type(self.byte_length) is int and self.byte_length > 0,
            "invalid_material_length",
        )
        for values in (self.pages, self.rows, self.request_fields):
            frozen_tuple(values)
        require(
            bool(self.pages) and len(set(self.pages)) == len(self.pages),
            "unique_page_sequence_required",
        )
        text_fields(*self.pages)
        require(
            len(dict(self.request_fields)) == len(self.request_fields),
            "duplicate_request_field",
        )
        require(
            len({row.identity for row in self.rows}) == len(self.rows),
            "duplicate_row_locator",
        )
        require(
            all(
                row.page_identity in self.pages
                and row.offset + row.length <= self.byte_length
                for row in self.rows
            ),
            "row_outside_material",
        )
        require(self.effect in ("CONFIRMED", "UNKNOWN", "FAILED"), "invalid_effect")
        require(
            self.exception_classification is None
            or re.fullmatch(r"[A-Z][A-Z0-9_]{0,79}", self.exception_classification)
            is not None,
            "exception_classification_required",
        )
        require(self.material_kind in ("raw", "derived"), "invalid_material_kind")
        for value in (
            self.requested_at,
            self.obtained_at,
            self.received_at,
            self.known_at,
        ):
            if value is not None:
                instant(value)


class MaterialBytes(Protocol):
    """No provider fallback; independent bounded read after put is mandatory."""

    def put(self, locator: str, raw: bytes) -> None: ...
    def read(self, locator: str, maximum: int) -> bytes: ...


class MemoryMaterialBytes:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, locator: str, raw: bytes) -> None:
        require(type(raw) is bytes, "immutable_bytes_required")
        require(
            locator not in self._blobs or self._blobs[locator] == raw,
            "material_locator_conflict",
        )
        self._blobs[locator] = raw

    def read(self, locator: str, maximum: int) -> bytes:
        require(locator in self._blobs, "material_unavailable")
        raw = self._blobs[locator]
        require(len(raw) <= maximum, "material_read_capacity_exceeded")
        return raw


def read_material(store: MaterialBytes, receipt: MaterialReceipt) -> bytes:
    try:
        raw = store.read(receipt.locator, receipt.byte_length)
    except Exception:
        raise ContractError("material_read_unavailable") from None
    require(
        type(raw) is bytes
        and len(raw) == receipt.byte_length
        and byte_digest(raw) == receipt.digest,
        "material_readback_mismatch",
    )
    for row in receipt.rows:
        require(
            byte_digest(raw[row.offset : row.offset + row.length]) == row.digest,
            "row_digest_mismatch",
        )
    return raw


@dataclass(frozen=True)
class FieldSemantics:
    source_field: str
    native_type: str
    native_unit: str
    target_field: str
    target_unit: str
    status: str
    source_ref: str
    transform_ref: str
    clock_semantics: str = "UNKNOWN"

    def __post_init__(self) -> None:
        text_fields(
            self.source_field,
            self.native_type,
            self.native_unit,
            self.target_field,
            self.target_unit,
            self.status,
            self.source_ref,
            self.transform_ref,
            self.clock_semantics,
        )
        require(
            self.status in ("EXPLICIT", "UNKNOWN", "CONFLICT"),
            "invalid_semantic_status",
        )


@dataclass(frozen=True)
class NormalizationContract:
    source: Key
    binding: Key
    mapping_revision: str
    daily: DailyDefinition
    capability: Capability
    producer_operation: str
    upstream_revision: str
    decoder_revision: str
    request_fields: tuple[tuple[str, str], ...]
    fields: tuple[FieldSemantics, ...]
    material_kind: str = "raw"

    def __post_init__(self) -> None:
        frozen_tuple(self.fields)
        frozen_tuple(self.request_fields)
        require(
            isinstance(self.capability, Capability)
            and self.capability == Capability.BARS_DAILY,
            "daily_capability_required",
        )
        require(self.material_kind in ("raw", "derived"), "invalid_material_kind")
        require(
            len({item.target_field for item in self.fields}) == len(self.fields),
            "duplicate_target_field",
        )

    def matches(self, receipt: MaterialReceipt) -> bool:
        return (
            receipt.source,
            receipt.binding,
            receipt.mapping_revision,
            receipt.definition,
            receipt.capability,
            receipt.producer_operation,
            receipt.upstream_revision,
            receipt.decode_revision,
            receipt.request_fields,
            receipt.material_kind,
        ) == (
            self.source,
            self.binding,
            self.mapping_revision,
            self.daily.definition,
            self.capability,
            self.producer_operation,
            self.upstream_revision,
            self.decoder_revision,
            self.request_fields,
            self.material_kind,
        )

    def require_semantics(self) -> None:
        expected = {
            "open": self.daily.price_unit,
            "high": self.daily.price_unit,
            "low": self.daily.price_unit,
            "close": self.daily.price_unit,
        }
        if self.daily.volume_unit != "not_applicable":
            expected.update(
                volume=self.daily.volume_unit, amount=self.daily.amount_unit
            )
        require(
            {item.target_field: item.target_unit for item in self.fields} == expected
            and all(
                item.status == "EXPLICIT"
                and item.native_unit not in ("UNKNOWN", "CONFLICT")
                for item in self.fields
            ),
            "economic_semantics_unqualified",
        )


@dataclass(frozen=True)
class DailyBar:
    day: date
    open: str
    high: str
    low: str
    close: str
    volume: str | None
    amount: str | None

    def __post_init__(self) -> None:
        require(type(self.day) is date, "daily_local_date_required")
        require(
            all(
                type(value) is str
                for value in (self.open, self.high, self.low, self.close)
            ),
            "decimal_text_required",
        )
        values = []
        for value in (
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.amount,
        ):
            if value is None:
                continue
            require(type(value) is str, "decimal_text_required")
            try:
                number = Decimal(value)
            except InvalidOperation:
                require(False, "invalid_decimal")
            require(number.is_finite() and number >= 0, "invalid_daily_number")
            values.append(number)
        opening, high, low, close = values[:4]
        require(
            low <= min(opening, close) <= max(opening, close) <= high, "invalid_ohlc"
        )


@dataclass(frozen=True)
class DailyRowFrame:
    """Physical row discovered from full material framing, not receipt locators."""

    page_ordinal: int
    offset: int
    length: int

    def __post_init__(self) -> None:
        require(
            type(self.page_ordinal) is int and self.page_ordinal >= 0,
            "invalid_row_page_ordinal",
        )
        require(type(self.offset) is int and self.offset >= 0, "invalid_row_offset")
        require(type(self.length) is int and self.length > 0, "invalid_row_length")


class DailyDecoder(Protocol):
    contract: NormalizationContract

    def enumerate_rows(
        self, raw: bytes, receipt: MaterialReceipt
    ) -> tuple[DailyRowFrame, ...]:
        """Validate all framing/bytes and enumerate rows without using receipt.rows.

        Return physical order and page ordinals; explicit non-row evidence must
        have its own validated framing. Unparsed bytes cannot imply no rows.
        """
        ...

    def decode(self, raw_row: bytes, receipt: MaterialReceipt) -> DailyBar: ...


class SyntheticDailyDecoder:
    """Synthetic JSON pages separated by LF; no empty pages or trailing LF.

    Each page is one daily row or an explicit {"evidence": nonempty_text} object.
    Page labels come from the receipt's ordered pages, never its row locators.
    This invented framing cannot qualify retained TDX rows.
    """

    def __init__(self, contract: NormalizationContract) -> None:
        self.contract = contract

    @staticmethod
    def _object(raw_page: bytes) -> dict:
        def unique_fields(pairs):
            require(len(dict(pairs)) == len(pairs), "duplicate_daily_field")
            return dict(pairs)

        try:
            value = json.loads(raw_page, object_pairs_hook=unique_fields)
        except (TypeError, ValueError) as error:
            if isinstance(error, ContractError):
                raise
            raise ContractError("invalid_daily_row_encoding") from None
        require(type(value) is dict, "invalid_daily_row_schema")
        return value

    def enumerate_rows(
        self, raw: bytes, receipt: MaterialReceipt
    ) -> tuple[DailyRowFrame, ...]:
        require(receipt.mode == EvidenceMode.SYNTHETIC, "synthetic_decoder_only")
        pages = raw.split(b"\n")
        require(
            len(pages) == len(receipt.pages) and all(pages),
            "material_page_framing_mismatch",
        )
        frames = []
        offset = 0
        for ordinal, page in enumerate(pages):
            value = self._object(page)
            if set(value) == {"evidence"}:
                text_fields(value["evidence"])
            else:
                self.decode(page, receipt)
                frames.append(DailyRowFrame(ordinal, offset, len(page)))
                require(len(frames) <= 127, "lineage_capacity_exceeded")
            offset += len(page) + 1
        return tuple(frames)

    def decode(self, raw_row: bytes, receipt: MaterialReceipt) -> DailyBar:
        require(receipt.mode == EvidenceMode.SYNTHETIC, "synthetic_decoder_only")
        try:
            value = self._object(raw_row)
            require(
                set(value)
                == {"day", "open", "high", "low", "close", "volume", "amount"},
                "invalid_daily_row_schema",
            )
            value["day"] = date.fromisoformat(value["day"])
            return DailyBar(**value)
        except (TypeError, KeyError, ValueError) as error:
            # Do not persist raw decoder exceptions or credential-bearing input.
            if isinstance(error, ContractError):
                raise
            raise ContractError("invalid_daily_row_encoding") from None


@dataclass(frozen=True)
class DailyAssessment:
    unit: PublicationUnit
    contract: NormalizationContract
    materials: tuple[MaterialReceipt, ...]
    evidence: tuple[PointEvidence, ...]
    bars: tuple[DailyBar, ...]
    evaluated_at: datetime
    rule_revision: str = "market.daily.assessment.d3.r1"

    @property
    def digest(self) -> str:
        return content_id(self)

    @property
    def content(self) -> bytes:
        return canonical_bytes((self.unit, self.bars, self.evidence))


def assess_daily(
    unit: PublicationUnit,
    receipts: tuple[MaterialReceipt, ...],
    evidence: tuple[PointEvidence, ...],
    decoder: DailyDecoder,
    store: MaterialBytes,
    evaluated_at: datetime,
) -> DailyAssessment:
    """Fail closed; raw materials remain retained when economics are UNKNOWN."""
    instant(evaluated_at)
    frozen_tuple(receipts)
    frozen_tuple(evidence)
    contract = decoder.contract
    require(unit.definition == contract.daily.definition, "definition_mismatch")
    contract.require_semantics()
    require(
        bool(receipts) and len({r.key for r in receipts}) == len(receipts),
        "unique_materials_required",
    )
    require(
        len(receipts) + sum(len(r.rows) for r in receipts) <= 128,
        "lineage_capacity_exceeded",
    )
    require(
        sum(r.byte_length for r in receipts) <= 8 * 1024 * 1024,
        "material_validation_capacity_exceeded",
    )
    raw = {}
    decoded = {}
    seen_days = set()
    for receipt in receipts:
        require(contract.matches(receipt), "receipt_contract_mismatch")
        require(receipt.instrument == unit.instrument, "receipt_instrument_mismatch")
        require(
            receipt.effect == "CONFIRMED" and receipt.exception_classification is None,
            "material_effect_unknown",
        )
        require(
            receipt.known_at is not None
            and receipt.known_at.astimezone(UTC) <= evaluated_at.astimezone(UTC),
            "material_known_at_unknown_or_future",
        )
        raw[receipt.key] = read_material(store, receipt)
        frames = decoder.enumerate_rows(raw[receipt.key], receipt)
        frozen_tuple(frames)
        require(len(frames) == len(receipt.rows), "material_row_inventory_mismatch")
        for frame, row in zip(frames, receipt.rows, strict=True):
            require(
                frame.page_ordinal < len(receipt.pages)
                and frame.offset + frame.length <= receipt.byte_length,
                "row_outside_material",
            )
            require(
                (row.page_identity, row.offset, row.length, row.digest)
                == (
                    receipt.pages[frame.page_ordinal],
                    frame.offset,
                    frame.length,
                    byte_digest(
                        raw[receipt.key][frame.offset : frame.offset + frame.length]
                    ),
                ),
                "material_row_inventory_mismatch",
            )
        for row in receipt.rows:
            bar = decoder.decode(
                raw[receipt.key][row.offset : row.offset + row.length], receipt
            )
            decoded[(receipt.key, row.identity)] = bar
            if unit.window.contains(bar.day):
                require(bar.day not in seen_days, "duplicate_daily_source_row")
                seen_days.add(bar.day)
    require(
        tuple(item.point for item in evidence)
        == tuple(item.point for item in unit.expand()),
        "coverage_partition_mismatch",
    )
    by_key = {r.key: r for r in receipts}
    bars = []
    used = set()
    for item in evidence:
        require(
            item.classification
            not in (
                CoverageClass.QUALITY_REJECTED,
                CoverageClass.COLLECTION_FAILED,
                CoverageClass.MISSING_EXPECTED,
                CoverageClass.UNKNOWN,
            ),
            "coverage_not_publishable",
        )
        # Every explicit reference must be in the independently read receipts,
        # including calendar/lifecycle/no-trade evidence without a daily row.
        if item.material is not None:
            require(item.material in by_key, "lineage_material_missing")
        if item.classification != CoverageClass.OBSERVED_VALID:
            continue  # Explicit D1 evidence; never infer a closure from missing rows.
        require(item.material in by_key, "lineage_material_missing")
        receipt = by_key[item.material]
        require(
            receipt.shard in item.original_shards
            and receipt.attempt in item.attempt_identities
            and receipt.request in item.request_identities,
            "lineage_request_mismatch",
        )
        rows = {row.identity: row for row in receipt.rows}
        require(item.source_row_locator in rows, "lineage_row_missing")
        row = rows[item.source_row_locator]
        require((receipt.key, row.identity) not in used, "duplicate_source_row")
        used.add((receipt.key, row.identity))
        bar = decoded[(receipt.key, row.identity)]
        require(GridPoint(unit.instrument, bar.day) == item.point, "row_scope_mismatch")
        require(
            (bar.volume is None and bar.amount is None)
            if contract.daily.volume_unit == "not_applicable"
            else (bar.volume is not None and bar.amount is not None),
            "daily_units_or_missing_fields",
        )
        bars.append(bar)
    require(
        used == {key for key, bar in decoded.items() if unit.window.contains(bar.day)},
        "unaccounted_source_row",
    )
    result = DailyAssessment(
        unit, contract, receipts, evidence, tuple(bars), evaluated_at
    )
    require(len(result.content) <= 65536, "version_content_capacity_exceeded")
    return result
