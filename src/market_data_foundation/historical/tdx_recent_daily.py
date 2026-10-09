"""Bounded P6 count-30 candidate; historical exact-two identities stay unchanged."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
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
from market_data_foundation.historical.daily_bars import (
    TDX_R1_DAILY_BINDING,
    TDX_R1_DAILY_SOURCE,
)
from market_data_foundation.qualification.tdx_semantics import daily_rows

EXPECTED_PROBE_SHA256 = (
    "116e44f1de4c4ddfe66c7ac8ca11da8ab2ed06239956a42697c5c414f82f95cd"
)
EXPECTED_GUARD_SHA256 = (
    "61b72db8c46254e7be4b3d718c2e8325f956dd3d55fb35d29d8a9ecf1d4b486e"
)
EXPECTED_ENDPOINT = ("139.9.52.158", 7709)
EXPECTED_FIXTURE = "mdf-p6-real-cognition-r1-recent-count30"
EXPECTED_QUALIFICATION_LABEL = "600000.XSHG"
SOURCE_REQUEST_SYMBOL = "sh600000"
CANONICAL_SECURITY_ID = "600000.SH"
CANONICAL_IDENTITY_NAMESPACE = "provider_suffix"


RECENT_SOURCE = replace(
    TDX_R1_DAILY_SOURCE,
    identity="mdf.source-candidate.tdx.p6.recent30",
    contract_revision="p6-recent-r1",
    operation='get_kline(9,"sh600000",0,30)',
    variant="stock-raw-count30-exact-one",
    pagination_contract="one request, offset=0, count=30; no additional pages",
    freshness_contract="observed at request; source event timestamp unknown",
)
RECENT_BINDING = replace(
    TDX_R1_DAILY_BINDING,
    identity="mdf.binding-candidate.tdx.p6.recent30.raw",
    revision="p6-recent-r1",
    source=RECENT_SOURCE.key,
    compatibility_policy="exact_one_p6_count30_request",
)
EXPECTED_REQUEST = {
    "api": "get_kline",
    "period": 9,
    "symbol": "sh600000",
    "offset": 0,
    "count": 30,
    "redial": False,
    "dial_ms": 2000,
}


@dataclass(frozen=True)
class RecentReceipt:
    observation: Mapping[str, Any]
    raw_projection: bytes
    guard_result: Mapping[str, Any]
    probe_sha256: str
    guard_sha256: str
    artifact_ref: str
    guarded_started_at: datetime
    guarded_finished_at: datetime

    def __post_init__(self) -> None:
        require(bool(self.artifact_ref), "live_artifact_ref_required")
        _instant(self.guarded_started_at)
        _instant(self.guarded_finished_at)
        require(
            self.guarded_finished_at >= self.guarded_started_at,
            "live_guard_time_reversal",
        )


@dataclass(frozen=True)
class RecentPlan:
    run: DataRun
    acquisition: AcquisitionOccurrence
    material: EvidenceMaterial
    version: DatasetVersion
    publication_request: PublicationRequest
    content: bytes
    returned_rows: int
    excluded_current_day_rows: int


def _instant(value: datetime) -> None:
    require(
        value.tzinfo is not None and value.utcoffset() is not None, "naive_datetime"
    )


def _parse_observed_at(value: object) -> datetime:
    require(isinstance(value, str) and value, "live_observed_at_missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _instant(parsed)
    return parsed.astimezone(UTC)


def _exact_dict(actual: object, expected: Mapping[str, object], code: str) -> None:
    require(isinstance(actual, Mapping), code)
    for key, value in expected.items():
        require(actual.get(key) == value, code)


def _validate_guard(receipt: RecentReceipt) -> None:
    require(receipt.probe_sha256 == EXPECTED_PROBE_SHA256, "live_probe_source_drift")
    require(receipt.guard_sha256 == EXPECTED_GUARD_SHA256, "live_guard_source_drift")
    guard = receipt.guard_result
    require(guard.get("stop") is None, "live_guard_stop")
    require(guard.get("process_exit") == 0, "live_probe_process_failed")
    hosts = guard.get("hosts")
    require(hosts == [EXPECTED_ENDPOINT[0]], "live_guard_host_scope_mismatch")
    limits = guard.get("limits")
    require(isinstance(limits, Mapping), "live_guard_limits_missing")
    require(limits.get("connect_attempts") == 2, "live_guard_attempt_limit_mismatch")
    require(
        limits.get("connect_attempts_per_phase") == 2,
        "live_guard_phase_attempt_limit_mismatch",
    )
    require(limits.get("wall_seconds") == 15, "live_guard_wall_limit_mismatch")
    require(limits.get("phase_seconds") == 15, "live_guard_phase_wall_limit_mismatch")
    peers = guard.get("established_peers")
    require(isinstance(peers, list) and bool(peers), "live_guard_peer_missing")
    require(
        all(
            isinstance(item, Mapping)
            and item.get("peer") == [EXPECTED_ENDPOINT[0], EXPECTED_ENDPOINT[1]]
            for item in peers
        ),
        "live_guard_peer_mismatch",
    )
    events = guard.get("events")
    require(isinstance(events, list), "live_guard_events_missing")
    sent_connects = 0
    for event in events:
        require(isinstance(event, Mapping), "live_guard_event_invalid")
        if event.get("syscall") != "connect":
            continue
        target = event.get("target")
        require(
            target == [EXPECTED_ENDPOINT[0], EXPECTED_ENDPOINT[1]],
            "live_guard_endpoint_violation",
        )
        require(event.get("sent") is True, "live_guard_unsent_connect")
        sent_connects += 1
    require(1 <= sent_connects <= 2, "live_guard_connect_count_invalid")


def _validate_observation(receipt: RecentReceipt) -> datetime:
    observation = receipt.observation
    require(observation.get("request") == EXPECTED_REQUEST, "recent_request_mismatch")
    expected_scalars = {
        "schema_version": 2,
        "provider": "TDX",
        "implementation": "tdx-python",
        "implementation_version": "0.1.6",
        "python": "3.14.8",
        "mode": "live",
        "capability": "bars.daily",
        "fixture_id": EXPECTED_FIXTURE,
        "success": True,
        "exception": None,
    }
    for key, value in expected_scalars.items():
        require(observation.get(key) == value, f"live_observation_{key}_mismatch")

    attempted = observation.get("attempted_endpoint")
    _exact_dict(
        attempted,
        {"host": EXPECTED_ENDPOINT[0], "port": EXPECTED_ENDPOINT[1]},
        "live_attempted_endpoint_mismatch",
    )
    endpoint = observation.get("endpoint")
    require(isinstance(endpoint, Mapping), "live_endpoint_missing")
    require(endpoint.get("host") is None, "live_unproven_actual_endpoint")
    require(
        bool(endpoint.get("unobservable_reason")),
        "live_unobservable_endpoint_reason_required",
    )

    coverage = observation.get("coverage")
    _exact_dict(
        coverage,
        {
            "requested_symbols": [EXPECTED_QUALIFICATION_LABEL],
            "returned_symbols": [EXPECTED_QUALIFICATION_LABEL],
            "row_count": len(json.loads(receipt.raw_projection)),
            "complete": False,
            "missing_fields": [],
        },
        "live_coverage_mismatch",
    )
    pagination = observation.get("pagination")
    _exact_dict(
        pagination,
        {"pages": 1, "page_size": 30, "exhausted": None},
        "live_pagination_mismatch",
    )
    units = observation.get("units")
    require(
        units == {"price": "CNY", "amount": "CNY", "volume": "shares"},
        "live_units_mismatch",
    )
    semantics = observation.get("semantics")
    require(isinstance(semantics, Mapping), "live_semantics_missing")
    require(semantics.get("adjustment") == "none", "live_adjustment_not_raw")
    require(
        semantics.get("timezone") == "Asia/Shanghai local date",
        "live_time_semantics_mismatch",
    )
    require(
        str(semantics.get("retry", "")).startswith("no harness retry"),
        "live_retry_semantics_mismatch",
    )
    observed_at = _parse_observed_at(observation.get("observed_at"))
    require(
        receipt.guarded_started_at <= observed_at <= receipt.guarded_finished_at,
        "live_observation_outside_guard_window",
    )
    return observed_at


def _decode_raw(
    receipt: RecentReceipt,
) -> tuple[tuple[dict[str, object], ...], int, int]:
    try:
        payload = json.loads(receipt.raw_projection)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("invalid live raw projection") from error
    require(
        isinstance(payload, list) and 1 <= len(payload) <= 30,
        "live_raw_row_count_mismatch",
    )
    required = {"time", "open", "high", "low", "close", "volume", "amount"}
    require(
        all(isinstance(row, Mapping) and set(row) == required for row in payload),
        "live_raw_shape_mismatch",
    )
    for row in payload:
        require(isinstance(row["time"], str), "recent_time_invalid")
        local = datetime.fromisoformat(row["time"])
        require(local.tzinfo is None, "recent_time_timezone_unproven")
        require(
            all(type(row[k]) in {int, float} for k in required - {"time"}),
            "recent_numeric_type_invalid",
        )
    normalized = daily_rows("tdx-python", payload, kind="stock")
    today = (
        receipt.guarded_started_at.astimezone(ZoneInfo("Asia/Shanghai"))
        .date()
        .isoformat()
    )
    require(all(row["date"] <= today for row in normalized), "recent_future_day")
    rows = [row for row in normalized if row["date"] < today]
    require(bool(rows), "recent_no_completed_day")
    dates = receipt.observation.get("date_range")
    _exact_dict(
        dates,
        {
            "observed_start": normalized[0]["date"],
            "observed_end": normalized[-1]["date"],
        },
        "recent_date_range_mismatch",
    )
    content_rows = tuple(
        {
            "security_id": CANONICAL_SECURITY_ID,
            "identity_namespace": CANONICAL_IDENTITY_NAMESPACE,
            "local_trading_date": row["date"],
            "open": row["open"],
            "high": row["high"],
            "low": row["low"],
            "close": row["close"],
            "volume": row["volume"],
            "amount": row["amount"],
            "adjustment": "none",
        }
        for row in rows
    )
    return content_rows, len(payload), len(normalized) - len(rows)


def build_recent_plan(
    receipt: RecentReceipt,
    *,
    run_identity: str,
    acquisition_identity: str,
    material_identity: str,
    version_identity: str,
    publication_identity: str,
    publication_occurrence: str,
    frozen_at: datetime,
) -> RecentPlan:
    _validate_guard(receipt)
    _instant(frozen_at)
    require(frozen_at >= receipt.guarded_finished_at, "freeze_before_obtained")
    require(
        receipt.guarded_finished_at - receipt.guarded_started_at
        <= timedelta(seconds=15),
        "live_wall_ceiling",
    )
    requested_at = _validate_observation(receipt)
    rows, returned_rows, excluded_rows = _decode_raw(receipt)

    transform = Transform(
        identity="mdf.p6.tdx.recent30.normalize",
        revision="p6-recent-r1",
        parameters=(
            ("source_request_symbol", SOURCE_REQUEST_SYMBOL),
            ("canonical_security_id", CANONICAL_SECURITY_ID),
            ("identity_namespace", CANONICAL_IDENTITY_NAMESPACE),
            ("period", "9"),
            ("start", "0"),
            ("count", "30"),
            ("normalizer", "tdx-r1-daily-rows-v1"),
            ("probe_sha256", receipt.probe_sha256),
            ("guard_sha256", receipt.guard_sha256),
        ),
    )
    run = DataRun(
        identity=run_identity,
        kind=RunKind.COLLECT,
        requested_scope=Scope(),
        started_at=receipt.guarded_started_at,
        finished_at=receipt.guarded_finished_at,
        result="success",
        binding=RECENT_BINDING.key,
        transform=transform,
        trigger="exact_one_guarded_tdx_daily_probe",
        budget=1,
    )
    acquisition = AcquisitionOccurrence(
        identity=acquisition_identity,
        run=run.key,
        request_identity="mdf-p6-real-cognition-r1-sh600000-day-start0-count30",
        source=RECENT_SOURCE.key,
        binding=RECENT_BINDING.key,
        mapping="raw-daily-bars",
        scope=Scope(),
        parameters=(
            ("source_request_symbol", SOURCE_REQUEST_SYMBOL),
            ("canonical_security_id", CANONICAL_SECURITY_ID),
            ("identity_namespace", CANONICAL_IDENTITY_NAMESPACE),
            ("attempted_endpoint", f"{EXPECTED_ENDPOINT[0]}:{EXPECTED_ENDPOINT[1]}"),
            (
                "guard_established_peer",
                f"{EXPECTED_ENDPOINT[0]}:{EXPECTED_ENDPOINT[1]}",
            ),
            ("period", "9"),
            ("start", "0"),
            ("count", "30"),
            ("redial", "false"),
            ("qualification_fixture_label", EXPECTED_QUALIFICATION_LABEL),
        ),
        requested_at=requested_at,
        obtained_at=receipt.guarded_finished_at,
        source_observed_at=None,
        source_frame_identity=None,
        result_mode="historical_query",
        completeness="bounded_up_to_30_returned_rows",
        freshness="bounded-recent-at-request; source-event-time-unproven",
        terminal_result="success",
        external_receipt=receipt.artifact_ref,
        provider_data_attempts=1,
        pagination="one-page-count-30; exhausted=unknown",
        recovery_summary="none; redial=false; no MDF retry",
    )
    material = EvidenceMaterial(
        identity=material_identity,
        grade=Grade.PROJECTED,
        digest=digest(receipt.raw_projection),
        scope=Scope(),
        acquired_from=acquisition.key,
        controlled_ref=f"{receipt.artifact_ref}/raw.json",
        durable=False,
        replayable=False,
        named_class="tdx_python_sdk_daily_projection",
    )
    content_payload = {
        "schema": "mdf.p6.tdx-recent30.v1",
        "source": (f"{RECENT_SOURCE.identity}@{RECENT_SOURCE.contract_revision}"),
        "binding": (f"{RECENT_BINDING.identity}@{RECENT_BINDING.revision}"),
        "acquisition_id": acquisition.identity,
        "source_request_symbol": SOURCE_REQUEST_SYMBOL,
        "canonical_security_id": CANONICAL_SECURITY_ID,
        "identity_namespace": CANONICAL_IDENTITY_NAMESPACE,
        "qualification_fixture_label": EXPECTED_QUALIFICATION_LABEL,
        "attempted_endpoint": f"{EXPECTED_ENDPOINT[0]}:{EXPECTED_ENDPOINT[1]}",
        "sdk_endpoint": "unobservable_by_tdx_python_api",
        "guard_established_peer": f"{EXPECTED_ENDPOINT[0]}:{EXPECTED_ENDPOINT[1]}",
        "units": {"price": "CNY", "volume": "shares", "amount": "CNY"},
        "adjustment": "none",
        "rows": list(rows),
        "coverage": {
            "returned_rows": returned_rows,
            "valid_completed_rows": len(rows),
            "excluded_current_day_rows": excluded_rows,
            "requested_count": 30,
            "missing_requested_rows": 30 - returned_rows,
            "session_completeness": "unproven; no gap filling or suspension inference",
        },
        "limitations": [
            "bounded sample; no continuity or universe coverage claim",
            "current local day conservatively excluded regardless of time",
            "local date is not source event time or MDF usable time",
            "lot precision loss unrecoverable",
            "local retained evidence only; durability/replayability not promoted",
            "no calendar/session-completeness verification",
        ],
    }
    content = json.dumps(
        content_payload, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode()
    version = DatasetVersion(
        identity=version_identity,
        definition=RAW_DAILY_BARS.key,
        finite_scope=FiniteScope(
            tuple(
                f"{CANONICAL_IDENTITY_NAMESPACE}:{CANONICAL_SECURITY_ID}:"
                f"{row['local_trading_date']}"
                for row in rows
            )
        ),
        scope=Scope(),
        frozen_at=frozen_at,
        run=run.key,
        content_digest=digest(content),
        binding=RECENT_BINDING.key,
        mapping="raw-daily-bars",
        acquisitions=(acquisition.key,),
        materials=(material.key,),
        transform=transform,
        completeness="bounded_observed",
    )
    publication_request = PublicationRequest(
        identity=publication_identity,
        occurrence=publication_occurrence,
        version=version.key,
        rule_revision="p6-tdx-recent30-r1",
        decision=Decision.PUBLISH,
        scope=Scope(),
        evidence=(material.key,),
        claimed_grades=frozenset({Grade.PROJECTED}),
        reason="exact-one-guarded-tdx-recent30",
        required_completeness="bounded_observed",
    )
    return RecentPlan(
        run=run,
        acquisition=acquisition,
        material=material,
        version=version,
        publication_request=publication_request,
        content=content,
        returned_rows=returned_rows,
        excluded_current_day_rows=excluded_rows,
    )


def persist_recent_plan(kernel: CanonicalKernel, plan: RecentPlan) -> DatasetVersion:
    for record in (
        RECENT_SOURCE,
        RAW_DAILY_BARS,
        RECENT_BINDING,
        plan.run,
        plan.acquisition,
        plan.material,
    ):
        kernel.add(record)
    return kernel.persist_version(plan.version, plan.content)


def publish_recent_plan(kernel: CanonicalKernel, plan: RecentPlan) -> Publication:
    return kernel.publish(plan.publication_request)
