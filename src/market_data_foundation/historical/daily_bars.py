"""Bounded P4 replay for raw daily bars; no provider I/O or active binding."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    DataRun,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    EvidenceRequirement,
    FiniteScope,
    Grade,
    OutputMapping,
    Publication,
    PublicationRequest,
    RunKind,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
    Transform,
    digest,
    require,
)
from market_data_foundation.domain_contracts.dense_market import (
    RAW_DAILY_BARS,
    require_bounded_universe,
    require_explicit_daily_units,
)
from market_data_foundation.qualification.tdx_semantics import daily_rows

TDX_R1_DAILY_SOURCE = SourceOperationRef(
    identity="mdf.source-candidate.tdx-r1.bars.daily",
    contract_revision="tdx-r1",
    owner="market-data-foundation",
    product="tdx-python",
    operation="bars.daily",
    projection_revision="tdx-r1-daily-rows-v1",
    variant="tdx-r1-qualified-raw-daily",
    build_revision="0.1.6",
    registry_revision=None,
    provider_app_revision=None,
    access_effect="read",
    acquisition_mode="historical_query",
    scope=Scope(),
    temporal_contract=(
        "source-qualified local trading date; no absolute UTC event-time claim"
    ),
    completeness_contract=(
        "bounded explicit symbol/date window; observed rows only; "
        "no session/universe completeness claim"
    ),
    pagination_contract=(
        "offline replay only; TDX R1 native multi-page global ordering is not asserted"
    ),
    freshness_contract="historical offline replay; no live freshness claim",
    evidence_capability=frozenset({Grade.PROJECTED}),
)

TDX_R1_DAILY_BINDING = SourceBindingRevision(
    identity="mdf.binding-candidate.tdx-r1.bars.daily.raw",
    revision="p4-r1",
    source=TDX_R1_DAILY_SOURCE.key,
    mappings=(
        OutputMapping(
            identity="raw-daily-bars",
            definition=RAW_DAILY_BARS.key,
            semantic_revision="p3-r1",
            temporal_revision="local-trading-date-v1",
            scope=Scope(),
            evidence=EvidenceRequirement(allowed=frozenset({Grade.PROJECTED})),
        ),
    ),
    compatibility_policy="exact_tdx_r1_qualification",
    limitations=(
        "explicit_endpoint_required_for_future_live_use",
        "redial_false_for_recommended_tdx_python_candidate",
        "complete_a_share_universe_not_admitted",
        "native_multi_page_global_order_not_asserted",
        "lot_precision_loss_not_recoverable",
        "local_date_is_not_absolute_utc_event_time",
    ),
    silent_fallback=False,
)


@dataclass(frozen=True)
class TdxStockDailyRawRow:
    """Exact fields consumed from the qualified tdx-python daily-row shape."""

    time: str
    open: int | float
    high: int | float
    low: int | float
    close: int | float
    volume: int | float
    amount: int | float

    def __post_init__(self) -> None:
        require(bool(self.time), "tdx_daily_time_required")
        for value in (
            self.open,
            self.high,
            self.low,
            self.close,
            self.volume,
            self.amount,
        ):
            require(type(value) in {int, float}, "tdx_daily_numeric_type")

    def as_mapping(self) -> dict[str, int | float | str]:
        return {
            "time": self.time,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "amount": self.amount,
        }


@dataclass(frozen=True)
class TdxDailyReplayBatch:
    fixture_id: str
    security_id: str
    identity_namespace: str
    requested_start: date
    requested_end: date
    rows: tuple[TdxStockDailyRawRow, ...]
    instrument_kind: str = "stock"
    membership_completeness: str = "bounded_explicit_membership"
    price_unit: str = "CNY"
    volume_unit: str = "shares"
    amount_unit: str = "CNY"
    adjustment: str = "none"

    def __post_init__(self) -> None:
        require(bool(self.fixture_id and self.security_id), "replay_identity_required")
        require(self.requested_start <= self.requested_end, "replay_window_reversed")
        require(bool(self.rows), "replay_rows_required")
        require(
            self.identity_namespace == "provider_suffix",
            "tdx_replay_identity_namespace_not_admitted",
        )
        require(
            self.instrument_kind == "stock", "tdx_replay_instrument_kind_not_admitted"
        )
        require(self.adjustment == "none", "raw_replay_adjustment_not_admitted")


@dataclass(frozen=True)
class RawDailyReplayPlan:
    source: SourceOperationRef
    binding: SourceBindingRevision
    run: DataRun
    material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes


def _normalized_rows(batch: TdxDailyReplayBatch) -> tuple[dict[str, object], ...]:
    require_bounded_universe(
        (batch.security_id,),
        completeness=batch.membership_completeness,
        identity_namespace=batch.identity_namespace,
    )
    require_explicit_daily_units(
        price_unit=batch.price_unit,
        volume_unit=batch.volume_unit,
        amount_unit=batch.amount_unit,
    )
    rows = daily_rows(
        "tdx-python",
        [row.as_mapping() for row in batch.rows],
        kind="stock",
    )
    normalized: list[dict[str, object]] = []
    for row in rows:
        local_day = date.fromisoformat(str(row["date"]))
        require(
            batch.requested_start <= local_day <= batch.requested_end,
            "replay_observation_outside_requested_window",
        )
        normalized.append(
            {
                "security_id": batch.security_id,
                "identity_namespace": batch.identity_namespace,
                "local_trading_date": local_day.isoformat(),
                "open": row["open"],
                "high": row["high"],
                "low": row["low"],
                "close": row["close"],
                "volume": row["volume"],
                "amount": row["amount"],
                "adjustment": "none",
            }
        )
    return tuple(normalized)


def replay_content(batch: TdxDailyReplayBatch) -> bytes:
    rows = _normalized_rows(batch)
    payload = {
        "schema": "mdf.p4.raw-daily-bars-replay.v1",
        "fixture_id": batch.fixture_id,
        "dataset_definition": (f"{RAW_DAILY_BARS.identity}@{RAW_DAILY_BARS.revision}"),
        "source_candidate": (
            f"{TDX_R1_DAILY_SOURCE.identity}@{TDX_R1_DAILY_SOURCE.contract_revision}"
        ),
        "binding_candidate": (
            f"{TDX_R1_DAILY_BINDING.identity}@{TDX_R1_DAILY_BINDING.revision}"
        ),
        "security_id": batch.security_id,
        "identity_namespace": batch.identity_namespace,
        "requested_local_date_range": [
            batch.requested_start.isoformat(),
            batch.requested_end.isoformat(),
        ],
        "observed_local_dates": [row["local_trading_date"] for row in rows],
        "units": {
            "price": batch.price_unit,
            "volume": batch.volume_unit,
            "amount": batch.amount_unit,
        },
        "adjustment": batch.adjustment,
        "rows": list(rows),
    }
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def build_replay_plan(
    batch: TdxDailyReplayBatch,
    *,
    run_identity: str,
    material_identity: str,
    version_identity: str,
    publication_identity: str,
    publication_occurrence: str,
    started_at: datetime,
    finished_at: datetime,
    frozen_at: datetime,
) -> RawDailyReplayPlan:
    require(started_at <= finished_at <= frozen_at, "replay_time_order")
    content = replay_content(batch)
    transform = Transform(
        identity="mdf.p4.raw-daily-bars.offline-replay",
        revision="p4-r1",
        parameters=(
            ("fixture_id", batch.fixture_id),
            (
                "source_candidate",
                f"{TDX_R1_DAILY_SOURCE.identity}@"
                f"{TDX_R1_DAILY_SOURCE.contract_revision}",
            ),
            (
                "binding_candidate",
                f"{TDX_R1_DAILY_BINDING.identity}@{TDX_R1_DAILY_BINDING.revision}",
            ),
            ("normalizer", "tdx-r1-daily-rows-v1"),
            ("security_id", batch.security_id),
            ("identity_namespace", batch.identity_namespace),
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
        trigger="offline_tdx_r1_qualification_replay",
    )
    material = EvidenceMaterial(
        identity=material_identity,
        grade=Grade.NORMALIZED,
        digest=digest(content),
        scope=Scope(),
        produced_by=run.key,
        controlled_ref=f"fixture://p4/raw-daily-bars/{batch.fixture_id}",
        durable=False,
        replayable=False,
        named_class="tdx_r1_offline_normalized_replay",
    )
    rows = _normalized_rows(batch)
    members = tuple(
        f"{batch.identity_namespace}:{batch.security_id}:{row['local_trading_date']}"
        for row in rows
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
        completeness="bounded_observed",
    )
    publication_request = PublicationRequest(
        identity=publication_identity,
        occurrence=publication_occurrence,
        version=version.key,
        rule_revision="p4-raw-daily-bars-replay-r1",
        decision=Decision.PUBLISH,
        scope=Scope(),
        evidence=(material.key,),
        claimed_grades=frozenset({Grade.NORMALIZED}),
        reason="bounded-offline-raw-daily-bars-replay",
        required_completeness="bounded_observed",
    )
    return RawDailyReplayPlan(
        source=TDX_R1_DAILY_SOURCE,
        binding=TDX_R1_DAILY_BINDING,
        run=run,
        material=material,
        version=version,
        publication_request=publication_request,
        content=content,
    )


def persist_replay_before_publication(
    kernel: CanonicalKernel, plan: RawDailyReplayPlan
) -> DatasetVersion:
    """Persist deterministic replay records; caller controls ledger commit boundary."""

    for record in (
        plan.source,
        RAW_DAILY_BARS,
        plan.binding,
        plan.run,
        plan.material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish_replay(kernel: CanonicalKernel, plan: RawDailyReplayPlan) -> Publication:
    return kernel.publish(plan.publication_request)
