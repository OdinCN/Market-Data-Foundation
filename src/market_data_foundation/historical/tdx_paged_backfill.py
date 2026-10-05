"""Bounded offline merge of retained TDX R1 six-page daily history."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    DataRun,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    FiniteScope,
    Grade,
    Publication,
    PublicationRequest,
    RunKind,
    Scope,
    Transform,
    digest,
    require,
)
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.qualification.tdx_semantics import daily_rows

HISTORY_SHA256 = "40037649d3d6b3942940b463e99caed00d70473cb938baccf1f657214f586a9d"
PAGE0_SHA256 = "c9789df215f7ab1d2822997e97cadcf40f267aca070ec46ad0b4347fc3cd81eb"
PAGE2_SHA256 = "1fcea41890dd262c90b354f52563b844baeaee0ab90abc8bac44aba1194096a3"

PAGE_SIZE = 800
PAGE_OFFSETS = (0, 800, 1600, 2400, 3200, 4000)
PAGE_COUNT = len(PAGE_OFFSETS)
TOTAL_ROWS = PAGE_SIZE * PAGE_COUNT
COMPLETENESS = "bounded_six_page_observed_not_exhaustive"
SECURITY_ID = "600000.SH"
IDENTITY_NAMESPACE = "provider_suffix"


@dataclass(frozen=True)
class RetainedPagedHistoryReceipt:
    history_bytes: bytes
    page0_bytes: bytes
    page2_bytes: bytes
    history_ref: str
    page0_ref: str
    page2_ref: str

    def __post_init__(self) -> None:
        require(bool(self.history_bytes), "paged_history_required")
        require(bool(self.page0_bytes), "paged_page0_required")
        require(bool(self.page2_bytes), "paged_page2_required")
        for ref in (self.history_ref, self.page0_ref, self.page2_ref):
            require(bool(ref), "paged_artifact_ref_required")


@dataclass(frozen=True)
class PageBoundary:
    source_page_index: int
    offset: int
    rows: int
    source_row_start: int
    source_row_end: int
    canonical_row_start: int
    canonical_row_end: int
    observed_start: str
    observed_end: str


@dataclass(frozen=True)
class PagedHistoryMerge:
    pages: tuple[PageBoundary, ...]
    canonical_rows: tuple[dict[str, object], ...]
    validation_manifest: bytes


@dataclass(frozen=True)
class PagedBackfillPlan:
    run: DataRun
    material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes
    validation_manifest: bytes


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _decode_rows(value: bytes, code: str) -> list[Mapping[str, Any]]:
    try:
        payload = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError(code) from error
    require(isinstance(payload, list), code)
    required = {"time", "open", "high", "low", "close", "volume", "amount"}
    require(
        all(isinstance(row, Mapping) and set(row) == required for row in payload),
        code,
    )
    return payload


def _canonical_row(row: Mapping[str, object]) -> dict[str, object]:
    return {
        "security_id": SECURITY_ID,
        "identity_namespace": IDENTITY_NAMESPACE,
        "local_trading_date": row["date"],
        "open": row["open"],
        "high": row["high"],
        "low": row["low"],
        "close": row["close"],
        "volume": row["volume"],
        "amount": row["amount"],
        "adjustment": "none",
    }


def validate_and_merge_pages(
    history_rows: Sequence[Mapping[str, Any]],
    page0_rows: Sequence[Mapping[str, Any]],
    page2_rows: Sequence[Mapping[str, Any]],
) -> PagedHistoryMerge:
    """Validate retained page structure before any canonical reordering."""

    require(len(history_rows) == TOTAL_ROWS, "paged_history_row_count_mismatch")
    require(len(page0_rows) == 2, "paged_page0_row_count_mismatch")
    require(len(page2_rows) == 2, "paged_page2_row_count_mismatch")

    pages: list[tuple[dict[str, object], ...]] = []
    boundaries: list[PageBoundary] = []
    source_dates: list[str] = []

    for page_index, offset in enumerate(PAGE_OFFSETS):
        raw_page = history_rows[offset : offset + PAGE_SIZE]
        require(len(raw_page) == PAGE_SIZE, "paged_chunk_size_mismatch")
        normalized = tuple(
            _canonical_row(row)
            for row in daily_rows("tdx-python", raw_page, kind="stock")
        )
        dates = [str(row["local_trading_date"]) for row in normalized]
        require(len(dates) == len(set(dates)), "paged_duplicate_date_in_page")
        canonical_row_start = (PAGE_COUNT - 1 - page_index) * PAGE_SIZE
        canonical_row_end = canonical_row_start + PAGE_SIZE - 1
        boundaries.append(
            PageBoundary(
                source_page_index=page_index,
                offset=offset,
                rows=len(normalized),
                source_row_start=0,
                source_row_end=PAGE_SIZE - 1,
                canonical_row_start=canonical_row_start,
                canonical_row_end=canonical_row_end,
                observed_start=dates[0],
                observed_end=dates[-1],
            )
        )
        pages.append(normalized)
        source_dates.extend(dates)

    require(
        len(source_dates) == len(set(source_dates)), "paged_duplicate_date_across_pages"
    )

    for newer, older in zip(pages, pages[1:]):
        require(
            str(older[-1]["local_trading_date"]) < str(newer[0]["local_trading_date"]),
            "paged_source_page_sequence_not_newest_first",
        )

    normalized_page0 = tuple(
        _canonical_row(row)
        for row in daily_rows("tdx-python", page0_rows, kind="stock")
    )
    normalized_page2 = tuple(
        _canonical_row(row)
        for row in daily_rows("tdx-python", page2_rows, kind="stock")
    )
    require(normalized_page0 == pages[0][-2:], "paged_page0_witness_mismatch")
    require(normalized_page2 == pages[0][-4:-2], "paged_page2_witness_mismatch")

    canonical_rows = tuple(row for page in reversed(pages) for row in page)
    canonical_dates = [str(row["local_trading_date"]) for row in canonical_rows]
    require(
        canonical_dates == sorted(canonical_dates),
        "paged_canonical_reorder_not_ascending",
    )
    require(
        len(canonical_dates) == len(set(canonical_dates)),
        "paged_canonical_duplicate_date",
    )

    manifest_value = {
        "schema": "mdf.p4.tdx-daily-paged-backfill.validation.v1",
        "security_id": SECURITY_ID,
        "identity_namespace": IDENTITY_NAMESPACE,
        "page_size": PAGE_SIZE,
        "page_offsets": list(PAGE_OFFSETS),
        "source_page_order": "newest_page_first",
        "canonical_reorder": "reverse_page_sequence_preserve_row_order",
        "provider_exhausted": None,
        "complete_history_claim": False,
        "completeness": COMPLETENESS,
        "source_rows": len(source_dates),
        "canonical_rows": len(canonical_rows),
        "pages": [
            {
                "source_page_index": boundary.source_page_index,
                "offset": boundary.offset,
                "rows": boundary.rows,
                "source_row_start": boundary.source_row_start,
                "source_row_end": boundary.source_row_end,
                "canonical_row_start": boundary.canonical_row_start,
                "canonical_row_end": boundary.canonical_row_end,
                "observed_start": boundary.observed_start,
                "observed_end": boundary.observed_end,
            }
            for boundary in boundaries
        ],
    }
    validation_manifest = json.dumps(
        manifest_value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    return PagedHistoryMerge(
        pages=tuple(boundaries),
        canonical_rows=canonical_rows,
        validation_manifest=validation_manifest,
    )


def merge_retained_receipt(receipt: RetainedPagedHistoryReceipt) -> PagedHistoryMerge:
    require(_sha256(receipt.history_bytes) == HISTORY_SHA256, "paged_history_sha_drift")
    require(_sha256(receipt.page0_bytes) == PAGE0_SHA256, "paged_page0_sha_drift")
    require(_sha256(receipt.page2_bytes) == PAGE2_SHA256, "paged_page2_sha_drift")

    history_rows = _decode_rows(receipt.history_bytes, "paged_history_shape_invalid")
    page0_rows = _decode_rows(receipt.page0_bytes, "paged_page0_shape_invalid")
    page2_rows = _decode_rows(receipt.page2_bytes, "paged_page2_shape_invalid")
    return validate_and_merge_pages(history_rows, page0_rows, page2_rows)


def build_paged_backfill_plan(
    receipt: RetainedPagedHistoryReceipt,
    *,
    run_identity: str,
    material_identity: str,
    version_identity: str,
    publication_identity: str,
    publication_occurrence: str,
    started_at: datetime,
    finished_at: datetime,
    frozen_at: datetime,
) -> PagedBackfillPlan:
    require(started_at <= finished_at <= frozen_at, "paged_time_order")
    merge = merge_retained_receipt(receipt)

    transform = Transform(
        identity="mdf.p4.tdx-r1.daily-bars.paged-backfill-merge",
        revision="p4-paged-backfill-r1",
        parameters=(
            ("history_sha256", HISTORY_SHA256),
            ("page0_sha256", PAGE0_SHA256),
            ("page2_sha256", PAGE2_SHA256),
            ("history_ref", receipt.history_ref),
            ("page0_ref", receipt.page0_ref),
            ("page2_ref", receipt.page2_ref),
            ("page_size", str(PAGE_SIZE)),
            ("page_offsets", ",".join(str(v) for v in PAGE_OFFSETS)),
            ("source_page_order", "newest_page_first"),
            ("canonical_reorder", "reverse_page_sequence_preserve_row_order"),
            ("provider_exhausted", "unknown"),
            ("complete_history_claim", "false"),
        ),
    )
    run = DataRun(
        identity=run_identity,
        kind=RunKind.REPROCESS,
        requested_scope=Scope(),
        started_at=started_at,
        finished_at=finished_at,
        result="success",
        transform=transform,
        trigger="retained_tdx_r1_six_page_history_merge",
    )
    material = EvidenceMaterial(
        identity=material_identity,
        grade=Grade.NORMALIZED,
        digest=digest(merge.validation_manifest),
        scope=Scope(),
        produced_by=run.key,
        durable=False,
        replayable=False,
        named_class="tdx_r1_paged_history_merge_validation",
    )
    content_value = {
        "schema": "mdf.p4.tdx-daily-paged-backfill.v1",
        "dataset_definition": f"{RAW_DAILY_BARS.identity}@{RAW_DAILY_BARS.revision}",
        "security_id": SECURITY_ID,
        "identity_namespace": IDENTITY_NAMESPACE,
        "units": {"price": "CNY", "volume": "shares", "amount": "CNY"},
        "adjustment": "none",
        "retained_inputs": {
            "history": {"ref": receipt.history_ref, "sha256": HISTORY_SHA256},
            "page0": {"ref": receipt.page0_ref, "sha256": PAGE0_SHA256},
            "page2": {"ref": receipt.page2_ref, "sha256": PAGE2_SHA256},
        },
        "page_size": PAGE_SIZE,
        "page_offsets": list(PAGE_OFFSETS),
        "source_page_order": "newest_page_first",
        "canonical_reorder": "reverse_page_sequence_preserve_row_order",
        "provider_exhausted": None,
        "complete_history_claim": False,
        "completeness": COMPLETENESS,
        "pages": [
            {
                "source_page_index": p.source_page_index,
                "offset": p.offset,
                "rows": p.rows,
                "source_row_start": p.source_row_start,
                "source_row_end": p.source_row_end,
                "canonical_row_start": p.canonical_row_start,
                "canonical_row_end": p.canonical_row_end,
                "observed_start": p.observed_start,
                "observed_end": p.observed_end,
            }
            for p in merge.pages
        ],
        "rows": list(merge.canonical_rows),
    }
    content = json.dumps(
        content_value, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    members = tuple(
        f"{IDENTITY_NAMESPACE}:{SECURITY_ID}:{row['local_trading_date']}"
        for row in merge.canonical_rows
    )
    version = DatasetVersion(
        identity=version_identity,
        definition=RAW_DAILY_BARS.key,
        finite_scope=FiniteScope(members),
        scope=Scope(),
        frozen_at=frozen_at,
        run=run.key,
        content_digest=digest(content),
        materials=(material.key,),
        transform=transform,
        completeness=COMPLETENESS,
    )
    publication_request = PublicationRequest(
        identity=publication_identity,
        occurrence=publication_occurrence,
        version=version.key,
        rule_revision="p4-tdx-paged-backfill-r1",
        decision=Decision.PUBLISH,
        scope=Scope(),
        evidence=(material.key,),
        claimed_grades=frozenset({Grade.NORMALIZED}),
        reason="bounded-retained-six-page-tdx-history-merge",
        required_completeness=COMPLETENESS,
    )
    return PagedBackfillPlan(
        run=run,
        material=material,
        version=version,
        publication_request=publication_request,
        content=content,
        validation_manifest=merge.validation_manifest,
    )


def persist_paged_backfill_before_publication(
    kernel: CanonicalKernel, plan: PagedBackfillPlan
) -> DatasetVersion:
    for record in (RAW_DAILY_BARS, plan.run, plan.material):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish_paged_backfill(
    kernel: CanonicalKernel, plan: PagedBackfillPlan
) -> Publication:
    return kernel.publish(plan.publication_request)
