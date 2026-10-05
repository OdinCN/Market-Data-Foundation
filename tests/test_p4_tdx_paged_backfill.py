import hashlib
import json
from datetime import UTC, date, datetime, timedelta

import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, RunKind
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import tdx_paged_backfill as paged

BASE = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)


def raw(day: date, value: int) -> dict[str, object]:
    base = 10000 + value % 100
    return {
        "time": f"{day.isoformat()}T15:00:00",
        "open": base,
        "high": base + 100,
        "low": base - 100,
        "close": base + 20,
        "volume": 100 + value,
        "amount": (base + 20) * (100 + value) * 1000,
    }


def synthetic_source_pages() -> tuple[list[dict[str, object]], ...]:
    start = date(2000, 1, 1)
    chronological = [raw(start + timedelta(days=i), i) for i in range(paged.TOTAL_ROWS)]
    oldest_to_newest = tuple(
        chronological[i : i + paged.PAGE_SIZE]
        for i in range(0, paged.TOTAL_ROWS, paged.PAGE_SIZE)
    )
    return tuple(reversed(oldest_to_newest))


def synthetic_payloads() -> tuple[bytes, bytes, bytes]:
    pages = synthetic_source_pages()
    history = [row for page in pages for row in page]
    page0 = pages[0][-2:]
    page2 = pages[0][-4:-2]

    def encode(value):
        return json.dumps(value, separators=(",", ":")).encode()

    return encode(history), encode(page0), encode(page2)


def receipt(monkeypatch) -> paged.RetainedPagedHistoryReceipt:
    history, page0, page2 = synthetic_payloads()
    monkeypatch.setattr(paged, "HISTORY_SHA256", hashlib.sha256(history).hexdigest())
    monkeypatch.setattr(paged, "PAGE0_SHA256", hashlib.sha256(page0).hexdigest())
    monkeypatch.setattr(paged, "PAGE2_SHA256", hashlib.sha256(page2).hexdigest())
    return paged.RetainedPagedHistoryReceipt(
        history_bytes=history,
        page0_bytes=page0,
        page2_bytes=page2,
        history_ref="fixture://tdx-r1/history",
        page0_ref="fixture://tdx-r1/page0",
        page2_ref="fixture://tdx-r1/page2",
    )


def plan(monkeypatch):
    return paged.build_paged_backfill_plan(
        receipt(monkeypatch),
        run_identity="p4-paged-run-1",
        material_identity="p4-paged-material-1",
        version_identity="p4-paged-version-1",
        publication_identity="p4-paged-publication-1",
        publication_occurrence="p4-paged-occurrence-1",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def test_bound_retained_hash_constants_match_p2_evidence() -> None:
    assert paged.HISTORY_SHA256 == (
        "40037649d3d6b3942940b463e99caed00d70473cb938baccf1f657214f586a9d"
    )
    assert paged.PAGE0_SHA256 == (
        "c9789df215f7ab1d2822997e97cadcf40f267aca070ec46ad0b4347fc3cd81eb"
    )
    assert paged.PAGE2_SHA256 == (
        "1fcea41890dd262c90b354f52563b844baeaee0ab90abc8bac44aba1194096a3"
    )


def test_six_pages_validate_newest_first_and_canonical_reorder() -> None:
    pages = synthetic_source_pages()
    history = [row for page in pages for row in page]
    merge = paged.validate_and_merge_pages(history, pages[0][-2:], pages[0][-4:-2])

    assert len(merge.pages) == 6
    assert [p.offset for p in merge.pages] == list(paged.PAGE_OFFSETS)
    assert all(p.rows == 800 for p in merge.pages)
    assert all(p.source_row_start == 0 for p in merge.pages)
    assert all(p.source_row_end == 799 for p in merge.pages)
    assert [(p.canonical_row_start, p.canonical_row_end) for p in merge.pages] == [
        (4000, 4799),
        (3200, 3999),
        (2400, 3199),
        (1600, 2399),
        (800, 1599),
        (0, 799),
    ]
    coverage = sorted(
        index
        for page in merge.pages
        for index in range(page.canonical_row_start, page.canonical_row_end + 1)
    )
    assert coverage == list(range(4800))
    assert merge.pages[0].canonical_row_start + 798 == 4798
    assert merge.pages[0].canonical_row_start + 799 == 4799
    assert len(merge.canonical_rows) == 4800

    dates = [row["local_trading_date"] for row in merge.canonical_rows]
    assert dates == sorted(dates)
    assert len(dates) == len(set(dates))
    assert dates[0] == "2000-01-01"
    assert dates[-1] == (date(2000, 1, 1) + timedelta(days=4799)).isoformat()

    manifest = json.loads(merge.validation_manifest)
    assert manifest["source_page_order"] == "newest_page_first"
    assert manifest["canonical_reorder"] == "reverse_page_sequence_preserve_row_order"
    assert manifest["provider_exhausted"] is None
    assert manifest["complete_history_claim"] is False
    assert manifest["completeness"] == paged.COMPLETENESS
    assert manifest["pages"][0]["source_row_start"] == 0
    assert manifest["pages"][0]["source_row_end"] == 799
    assert manifest["pages"][0]["canonical_row_start"] == 4000
    assert manifest["pages"][0]["canonical_row_end"] == 4799
    assert manifest["pages"][5]["canonical_row_start"] == 0
    assert manifest["pages"][5]["canonical_row_end"] == 799


def test_page0_and_page2_witnesses_must_match_newest_page_tail() -> None:
    pages = synthetic_source_pages()
    history = [row for page in pages for row in page]

    bad_page0 = [dict(row) for row in pages[0][-2:]]
    bad_page0[-1]["close"] = int(bad_page0[-1]["close"]) + 10
    with pytest.raises(ContractError, match="paged_page0_witness_mismatch"):
        paged.validate_and_merge_pages(history, bad_page0, pages[0][-4:-2])

    bad_page2 = [dict(row) for row in pages[0][-4:-2]]
    bad_page2[0]["close"] = int(bad_page2[0]["close"]) + 10
    with pytest.raises(ContractError, match="paged_page2_witness_mismatch"):
        paged.validate_and_merge_pages(history, pages[0][-2:], bad_page2)


def test_page_row_order_is_validated_before_reorder() -> None:
    pages = [list(page) for page in synthetic_source_pages()]
    pages[0][0], pages[0][1] = pages[0][1], pages[0][0]
    history = [row for page in pages for row in page]
    with pytest.raises(ValueError, match="ascending"):
        paged.validate_and_merge_pages(history, pages[0][-2:], pages[0][-4:-2])


def test_duplicate_or_overlapping_dates_across_pages_fail_closed() -> None:
    pages = [list(page) for page in synthetic_source_pages()]
    pages[1][-1] = dict(pages[1][-1])
    pages[1][-1]["time"] = pages[0][0]["time"]
    history = [row for page in pages for row in page]
    with pytest.raises(
        ContractError,
        match="paged_duplicate_date_across_pages|paged_source_page_sequence_not_newest_first",
    ):
        paged.validate_and_merge_pages(history, pages[0][-2:], pages[0][-4:-2])


def test_wrong_source_page_sequence_is_not_hidden_by_global_sort() -> None:
    pages = list(synthetic_source_pages())
    pages[0], pages[1] = pages[1], pages[0]
    history = [row for page in pages for row in page]
    with pytest.raises(
        ContractError, match="paged_source_page_sequence_not_newest_first"
    ):
        paged.validate_and_merge_pages(history, pages[0][-2:], pages[0][-4:-2])


def test_exact_4800_row_shape_is_required() -> None:
    pages = synthetic_source_pages()
    history = [row for page in pages for row in page][:-1]
    with pytest.raises(ContractError, match="paged_history_row_count_mismatch"):
        paged.validate_and_merge_pages(history, pages[0][-2:], pages[0][-4:-2])


def test_retained_hash_drift_rejects_before_semantic_merge(monkeypatch) -> None:
    candidate = receipt(monkeypatch)
    changed = paged.RetainedPagedHistoryReceipt(
        history_bytes=candidate.history_bytes + b" ",
        page0_bytes=candidate.page0_bytes,
        page2_bytes=candidate.page2_bytes,
        history_ref=candidate.history_ref,
        page0_ref=candidate.page0_ref,
        page2_ref=candidate.page2_ref,
    )
    with pytest.raises(ContractError, match="paged_history_sha_drift"):
        paged.merge_retained_receipt(changed)


def test_plan_is_offline_reprocess_without_binding_or_acquisition(monkeypatch) -> None:
    candidate = plan(monkeypatch)
    assert candidate.run.kind == RunKind.REPROCESS
    assert candidate.run.binding is None
    assert candidate.version.binding is None
    assert candidate.version.mapping is None
    assert candidate.version.acquisitions == ()
    assert candidate.material.produced_by == candidate.run.key
    assert candidate.material.acquired_from is None
    assert candidate.version.materials == (candidate.material.key,)
    assert candidate.version.completeness == paged.COMPLETENESS

    payload = json.loads(candidate.content)
    assert payload["security_id"] == "600000.SH"
    assert payload["identity_namespace"] == "provider_suffix"
    assert payload["adjustment"] == "none"
    assert payload["provider_exhausted"] is None
    assert payload["complete_history_claim"] is False
    assert payload["completeness"] == paged.COMPLETENESS
    assert payload["pages"][0]["source_row_start"] == 0
    assert payload["pages"][0]["source_row_end"] == 799
    assert payload["pages"][0]["canonical_row_start"] == 4000
    assert payload["pages"][0]["canonical_row_end"] == 4799
    assert payload["pages"][5]["canonical_row_start"] == 0
    assert payload["pages"][5]["canonical_row_end"] == 799
    assert len(payload["rows"]) == 4800


def test_paged_backfill_roundtrips_and_publishes_in_memory(monkeypatch) -> None:
    candidate = plan(monkeypatch)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))

    stored = paged.persist_paged_backfill_before_publication(kernel, candidate)
    assert stored == candidate.version
    assert ledger.get(candidate.run.key) == candidate.run
    assert ledger.get(candidate.material.key) == candidate.material
    assert ledger.get_content(candidate.version.key) == candidate.content

    publication = paged.publish_paged_backfill(kernel, candidate)
    assert publication.request.version == candidate.version.key
    assert kernel.eligible(candidate.version, BASE + timedelta(seconds=4))

    restored = InMemoryLedger.from_snapshot(ledger.snapshot())
    assert restored.get(candidate.version.key) == candidate.version
    assert restored.get_content(candidate.version.key) == candidate.content
    assert (
        restored.publication_by_occurrence(candidate.publication_request.occurrence)
        == publication
    )


def test_postgres_allowlist_adds_only_exact_paged_backfill_namespace() -> None:
    assert (
        validate_schema("mdf_p4_tdx_daily_bars_paged_backfill_r1")
        == "mdf_p4_tdx_daily_bars_paged_backfill_r1"
    )
    for unsafe in (
        "mdf_p4_tdx_daily_bars_paged_backfill_r1_extra",
        "mdf_p4_tdx_daily_bars_paged_backfill_r1;drop",
    ):
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            validate_schema(unsafe)
