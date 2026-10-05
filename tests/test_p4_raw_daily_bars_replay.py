import json
from datetime import UTC, date, datetime, timedelta

import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, Kind, RunKind
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.daily_bars import (
    TDX_R1_DAILY_BINDING,
    TDX_R1_DAILY_SOURCE,
    TdxDailyReplayBatch,
    TdxStockDailyRawRow,
    build_replay_plan,
    persist_replay_before_publication,
    publish_replay,
    replay_content,
)

BASE = datetime(2026, 10, 5, 0, 0, tzinfo=UTC)


def raw(day: str, *, close: int = 10500) -> TdxStockDailyRawRow:
    return TdxStockDailyRawRow(
        time=f"{day}T15:00:00",
        open=10000,
        high=11000,
        low=9000,
        close=close,
        volume=100,
        amount=105000000,
    )


def batch(**changes) -> TdxDailyReplayBatch:
    values = {
        "fixture_id": "tdx-r1-bars-daily-offline-1",
        "security_id": "600000.SH",
        "identity_namespace": "provider_suffix",
        "requested_start": date(2024, 1, 2),
        "requested_end": date(2024, 1, 5),
        "rows": (raw("2024-01-02"), raw("2024-01-04", close=10600)),
    }
    values.update(changes)
    return TdxDailyReplayBatch(**values)


def plan(candidate: TdxDailyReplayBatch | None = None):
    return build_replay_plan(
        candidate or batch(),
        run_identity="p4-bars-replay-run-1",
        material_identity="p4-bars-replay-material-1",
        version_identity="p4-bars-replay-version-1",
        publication_identity="p4-bars-replay-publication-1",
        publication_occurrence="p4-bars-replay-occurrence-1",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def test_tdx_r1_source_binding_candidate_preserves_p2_limits() -> None:
    assert TDX_R1_DAILY_SOURCE.product == "tdx-python"
    assert TDX_R1_DAILY_SOURCE.build_revision == "0.1.6"
    assert TDX_R1_DAILY_SOURCE.operation == "bars.daily"
    assert TDX_R1_DAILY_SOURCE.registry_revision is None
    assert TDX_R1_DAILY_BINDING.silent_fallback is False
    assert "complete_a_share_universe_not_admitted" in TDX_R1_DAILY_BINDING.limitations
    assert (
        "native_multi_page_global_order_not_asserted"
        in TDX_R1_DAILY_BINDING.limitations
    )


def test_replay_normalizes_tdx_r1_stock_rows_and_preserves_gap() -> None:
    payload = json.loads(replay_content(batch()))
    assert payload["units"] == {"price": "CNY", "volume": "shares", "amount": "CNY"}
    assert payload["adjustment"] == "none"
    assert payload["observed_local_dates"] == ["2024-01-02", "2024-01-04"]
    assert "2024-01-03" not in payload["observed_local_dates"]
    first = payload["rows"][0]
    assert first["open"] == 10
    assert first["high"] == 11
    assert first["low"] == 9
    assert first["close"] == 10.5
    assert first["volume"] == 10000
    assert first["amount"] == 105000


def test_replay_rejects_complete_universe_namespace_unit_and_adjustment_claims() -> (
    None
):
    with pytest.raises(ContractError, match="universe_completeness_not_admitted"):
        replay_content(batch(membership_completeness="complete_a_share"))
    with pytest.raises(
        ContractError, match="tdx_replay_identity_namespace_not_admitted"
    ):
        replay_content(batch(security_id="920000.XBSE", identity_namespace="mic"))
    with pytest.raises(ContractError, match="invalid_security_identity"):
        replay_content(
            batch(security_id="920000.XBSE", identity_namespace="provider_suffix")
        )
    with pytest.raises(ContractError, match="tdx_replay_instrument_kind_not_admitted"):
        replay_content(batch(instrument_kind="index"))
    with pytest.raises(ContractError, match="daily_bar_volume_unit_not_admitted"):
        replay_content(batch(volume_unit="lots"))
    with pytest.raises(ContractError, match="raw_replay_adjustment_not_admitted"):
        batch(adjustment="qfq")


def test_replay_rejects_observations_outside_requested_window() -> None:
    with pytest.raises(
        ContractError, match="replay_observation_outside_requested_window"
    ):
        replay_content(
            batch(
                requested_start=date(2024, 1, 3),
                rows=(raw("2024-01-02"), raw("2024-01-04")),
            )
        )


@pytest.mark.parametrize(
    "rows",
    [
        (raw("2024-01-02"), raw("2024-01-02")),
        (raw("2024-01-04"), raw("2024-01-02")),
    ],
)
def test_replay_rejects_duplicate_or_nonascending_daily_content(rows) -> None:
    with pytest.raises(ValueError, match="duplicate|ascending"):
        replay_content(batch(rows=rows))


def test_replay_plan_is_offline_and_does_not_fabricate_acquisition() -> None:
    candidate = plan()
    assert candidate.run.kind == RunKind.REPROCESS
    assert candidate.run.binding is None
    assert candidate.version.binding is None
    assert candidate.version.mapping is None
    assert candidate.version.acquisitions == ()
    assert candidate.version.inputs == ()
    assert candidate.version.materials == (candidate.material.key,)
    assert candidate.material.produced_by == candidate.run.key
    assert candidate.material.acquired_from is None
    assert candidate.material.grade.value == "mdf_normalized"
    assert all(key.kind != Kind.ACQUISITION for key in candidate.version.acquisitions)
    parameters = dict(candidate.run.transform.parameters)
    assert parameters["source_candidate"] == (
        f"{candidate.source.identity}@{candidate.source.contract_revision}"
    )
    assert parameters["binding_candidate"] == (
        f"{candidate.binding.identity}@{candidate.binding.revision}"
    )


def test_replay_roundtrips_in_memory_with_candidate_contracts_and_publication() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    candidate = plan()

    stored = persist_replay_before_publication(kernel, candidate)
    assert stored == candidate.version
    assert ledger.get(candidate.source.key) == candidate.source
    assert ledger.get(candidate.binding.key) == candidate.binding
    assert ledger.get_content(candidate.version.key) == candidate.content

    publication = publish_replay(kernel, candidate)
    assert publication.request.version == candidate.version.key
    assert kernel.eligible(candidate.version, BASE + timedelta(seconds=4))

    restored = InMemoryLedger.from_snapshot(ledger.snapshot())
    assert restored.get(candidate.version.key) == candidate.version
    assert restored.get_content(candidate.version.key) == candidate.content
    assert (
        restored.publication_by_occurrence(candidate.publication_request.occurrence)
        == publication
    )


def test_replay_finite_scope_is_exact_observed_symbol_date_membership() -> None:
    candidate = plan()
    assert candidate.version.finite_scope.members == (
        "provider_suffix:600000.SH:2024-01-02",
        "provider_suffix:600000.SH:2024-01-04",
    )
    payload = json.loads(candidate.content)
    assert [row["local_trading_date"] for row in payload["rows"]] == [
        "2024-01-02",
        "2024-01-04",
    ]


def test_postgres_schema_allowlist_adds_only_the_bounded_p4_namespace() -> None:
    assert (
        validate_schema("mdf_p4_raw_daily_bars_replay_r1")
        == "mdf_p4_raw_daily_bars_replay_r1"
    )
    assert (
        validate_schema("mdf_p3_canonical_kernel_r1_correction_001")
        == "mdf_p3_canonical_kernel_r1_correction_001"
    )
    for unsafe in (
        "mdf_p4_raw_daily_bars_replay_r1_extra",
        "mdf_p4_raw_daily_bars_replay_r1;drop",
        "public",
    ):
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            validate_schema(unsafe)
