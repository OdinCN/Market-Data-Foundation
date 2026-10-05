import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError, RunKind
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical.tdx_live_daily import (
    CANONICAL_IDENTITY_NAMESPACE,
    CANONICAL_SECURITY_ID,
    EXPECTED_GUARD_SHA256,
    EXPECTED_PROBE_SHA256,
    GuardedLiveReceipt,
    build_live_plan,
    persist_live_plan_before_publication,
    publish_live_plan,
)

BASE = datetime(2026, 10, 5, 10, 0, tzinfo=UTC)


def raw_bytes() -> bytes:
    return json.dumps(
        [
            {
                "time": "2026-09-29T15:00:00",
                "open": 9150,
                "high": 9250,
                "low": 9090,
                "close": 9180,
                "volume": 805097,
                "amount": 739740992000,
            },
            {
                "time": "2026-09-30T15:00:00",
                "open": 9220,
                "high": 9490,
                "low": 9160,
                "close": 9480,
                "volume": 1474848,
                "amount": 1386209920000,
            },
        ],
        separators=(",", ":"),
    ).encode()


def observation() -> dict:
    return {
        "schema_version": 2,
        "provider": "TDX",
        "implementation": "tdx-python",
        "implementation_version": "0.1.6",
        "python": "3.14.8",
        "mode": "live",
        "capability": "bars.daily",
        "fixture_id": "tdx-r1-common-endpoint-recent-two",
        "observed_at": (BASE + timedelta(milliseconds=100)).isoformat(),
        "success": True,
        "latency_ms": 100.0,
        "endpoint": {
            "host": None,
            "port": None,
            "server_identity": None,
            "unobservable_reason": "Go peer not exposed by Python API",
        },
        "attempted_endpoint": {
            "host": "139.9.52.158",
            "port": 7709,
            "server_identity": None,
            "unobservable_reason": "explicit configured target",
        },
        "coverage": {
            "requested_symbols": ["600000.XSHG"],
            "returned_symbols": ["600000.XSHG"],
            "row_count": 2,
            "complete": False,
            "missing_fields": [],
        },
        "date_range": {
            "requested_start": None,
            "requested_end": None,
            "observed_start": "2026-09-29",
            "observed_end": "2026-09-30",
        },
        "pagination": {"pages": 1, "page_size": 2, "exhausted": None},
        "units": {"price": "CNY", "amount": "CNY", "volume": "shares"},
        "semantics": {
            "origin": "SDK daily response with explicit R1 unit mapping",
            "adjustment": "none",
            "timezone": "Asia/Shanghai local date",
            "volume_precision": (
                "approximate shares reconstructed from truncated native lots"
            ),
            "retry": "no harness retry; pytdxdata may internally retry",
            "discovery": "explicit servers; pytdxdata public probe-disable switch",
        },
        "exception": None,
    }


def guard_result() -> dict:
    return {
        "command": ["probe"],
        "hosts": ["139.9.52.158"],
        "limits": {
            "connect_attempts": 2,
            "connect_attempts_per_phase": 2,
            "wall_seconds": 15,
            "phase_seconds": 15,
        },
        "events": [
            {
                "phase": "import",
                "tid": 1,
                "syscall": "connect",
                "target": ["139.9.52.158", 7709],
                "elapsed": 0.1,
                "sent": True,
                "syscall_return": 0,
            }
        ],
        "established_peers": [{"phase": "import", "peer": ["139.9.52.158", 7709]}],
        "stop": None,
        "process_exit": 0,
    }


def receipt(**changes) -> GuardedLiveReceipt:
    values = {
        "observation": observation(),
        "raw_projection": raw_bytes(),
        "guard_result": guard_result(),
        "probe_sha256": EXPECTED_PROBE_SHA256,
        "guard_sha256": EXPECTED_GUARD_SHA256,
        "artifact_ref": "artifact://p4-tdx-live/run-001",
        "guarded_started_at": BASE,
        "guarded_finished_at": BASE + timedelta(seconds=1),
    }
    values.update(changes)
    return GuardedLiveReceipt(**values)


def plan(candidate: GuardedLiveReceipt | None = None):
    return build_live_plan(
        candidate or receipt(),
        run_identity="p4-tdx-live-run-1",
        acquisition_identity="p4-tdx-live-acquisition-1",
        material_identity="p4-tdx-live-material-1",
        version_identity="p4-tdx-live-version-1",
        publication_identity="p4-tdx-live-publication-1",
        publication_occurrence="p4-tdx-live-occurrence-1",
    )


def test_valid_live_plan_is_one_source_bound_acquisition() -> None:
    candidate = plan()
    assert candidate.run.kind == RunKind.COLLECT
    assert candidate.run.binding == candidate.acquisition.binding
    assert candidate.run.budget == 1
    assert candidate.acquisition.provider_data_attempts == 1
    assert candidate.acquisition.source_observed_at is None
    assert candidate.acquisition.parameters
    assert candidate.version.binding == candidate.acquisition.binding
    assert (
        candidate.version.mapping == candidate.acquisition.mapping == "raw-daily-bars"
    )
    assert candidate.version.acquisitions == (candidate.acquisition.key,)
    assert candidate.version.materials == (candidate.material.key,)
    assert candidate.material.acquired_from == candidate.acquisition.key
    assert candidate.material.produced_by is None

    payload = json.loads(candidate.content)
    assert payload["canonical_security_id"] == CANONICAL_SECURITY_ID
    assert payload["identity_namespace"] == CANONICAL_IDENTITY_NAMESPACE
    assert payload["qualification_fixture_label"] == "600000.XSHG"
    assert payload["source_request_symbol"] == "sh600000"
    assert payload["sdk_endpoint"] == "unobservable_by_tdx_python_api"
    assert payload["guard_established_peer"] == "139.9.52.158:7709"
    assert payload["units"] == {"price": "CNY", "volume": "shares", "amount": "CNY"}
    assert payload["adjustment"] == "none"
    assert [row["local_trading_date"] for row in payload["rows"]] == [
        "2026-09-29",
        "2026-09-30",
    ]


def test_live_plan_roundtrips_and_publishes_in_memory() -> None:
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=2))
    candidate = plan()

    stored = persist_live_plan_before_publication(kernel, candidate)
    assert stored == candidate.version
    assert ledger.get(candidate.acquisition.key) == candidate.acquisition
    assert ledger.get_content(candidate.version.key) == candidate.content

    publication = publish_live_plan(kernel, candidate)
    assert publication.request.version == candidate.version.key
    assert kernel.eligible(candidate.version, BASE + timedelta(seconds=3))

    restored = InMemoryLedger.from_snapshot(ledger.snapshot())
    assert restored.get(candidate.acquisition.key) == candidate.acquisition
    assert restored.get(candidate.version.key) == candidate.version
    assert restored.get_content(candidate.version.key) == candidate.content
    assert (
        restored.publication_by_occurrence(candidate.publication_request.occurrence)
        == publication
    )


@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("implementation", "pytdxdata", "live_observation_implementation_mismatch"),
        (
            "implementation_version",
            "9.9.9",
            "live_observation_implementation_version_mismatch",
        ),
        ("python", "3.15.0", "live_observation_python_mismatch"),
        ("capability", "quote.snapshot", "live_observation_capability_mismatch"),
        ("fixture_id", "other", "live_observation_fixture_id_mismatch"),
        ("success", False, "live_observation_success_mismatch"),
    ],
)
def test_live_observation_identity_mismatch_rejects(field, value, code) -> None:
    obs = observation()
    obs[field] = value
    with pytest.raises(ContractError, match=code):
        plan(receipt(observation=obs))


def test_live_observation_rejects_endpoint_coverage_units_and_adjustment_drift() -> (
    None
):
    obs = observation()
    obs["attempted_endpoint"]["host"] = "1.2.3.4"
    with pytest.raises(ContractError, match="live_attempted_endpoint_mismatch"):
        plan(receipt(observation=obs))

    obs = observation()
    obs["coverage"]["row_count"] = 3
    with pytest.raises(ContractError, match="live_coverage_mismatch"):
        plan(receipt(observation=obs))

    obs = observation()
    obs["coverage"]["requested_symbols"] = ["600000.SH"]
    with pytest.raises(ContractError, match="live_coverage_mismatch"):
        plan(receipt(observation=obs))

    obs = observation()
    obs["units"]["volume"] = "lots"
    with pytest.raises(ContractError, match="live_units_mismatch"):
        plan(receipt(observation=obs))

    obs = observation()
    obs["semantics"]["adjustment"] = "qfq"
    with pytest.raises(ContractError, match="live_adjustment_not_raw"):
        plan(receipt(observation=obs))


def test_live_guard_rejects_source_drift_stop_wrong_endpoint_and_extra_connects() -> (
    None
):
    with pytest.raises(ContractError, match="live_probe_source_drift"):
        plan(receipt(probe_sha256="0" * 64))
    with pytest.raises(ContractError, match="live_guard_source_drift"):
        plan(receipt(guard_sha256="0" * 64))

    guard = guard_result()
    guard["stop"] = "attempt_limit"
    with pytest.raises(ContractError, match="live_guard_stop"):
        plan(receipt(guard_result=guard))

    guard = guard_result()
    guard["established_peers"] = []
    with pytest.raises(ContractError, match="live_guard_peer_missing"):
        plan(receipt(guard_result=guard))

    guard = guard_result()
    guard["established_peers"][0]["peer"] = ["124.70.176.52", 7709]
    with pytest.raises(ContractError, match="live_guard_peer_mismatch"):
        plan(receipt(guard_result=guard))

    guard = guard_result()
    guard["events"][0]["target"] = ["124.70.176.52", 7709]
    with pytest.raises(ContractError, match="live_guard_endpoint_violation"):
        plan(receipt(guard_result=guard))

    guard = guard_result()
    guard["events"] = deepcopy(guard["events"]) * 3
    with pytest.raises(ContractError, match="live_guard_connect_count_invalid"):
        plan(receipt(guard_result=guard))


def test_live_observation_time_must_be_inside_guard_window() -> None:
    obs = observation()
    obs["observed_at"] = (BASE - timedelta(seconds=1)).isoformat()
    with pytest.raises(ContractError, match="live_observation_outside_guard_window"):
        plan(receipt(observation=obs))


@pytest.mark.parametrize(
    "payload",
    [
        [],
        [
            {
                "time": "2026-09-29T15:00:00",
                "open": 9150,
                "high": 9250,
                "low": 9090,
                "close": 9180,
                "volume": 805097,
                "amount": 739740992000,
            }
        ],
    ],
)
def test_live_raw_projection_requires_exactly_two_rows(payload) -> None:
    with pytest.raises(ContractError, match="live_raw_row_count_mismatch"):
        plan(receipt(raw_projection=json.dumps(payload).encode()))


def test_live_raw_projection_rejects_duplicate_or_reversed_dates() -> None:
    payload = json.loads(raw_bytes())
    payload[1]["time"] = payload[0]["time"]
    with pytest.raises(ValueError, match="duplicate"):
        plan(receipt(raw_projection=json.dumps(payload).encode()))

    payload = json.loads(raw_bytes())
    payload.reverse()
    with pytest.raises(ValueError, match="ascending"):
        plan(receipt(raw_projection=json.dumps(payload).encode()))


def test_live_plan_does_not_fabricate_source_event_time_or_mic_identity() -> None:
    candidate = plan()
    assert candidate.acquisition.source_observed_at is None
    payload = json.loads(candidate.content)
    assert payload["canonical_security_id"] == "600000.SH"
    assert payload["identity_namespace"] == "provider_suffix"
    assert payload["qualification_fixture_label"] == "600000.XSHG"
    assert payload["canonical_security_id"] != payload["qualification_fixture_label"]


def test_postgres_allowlist_adds_only_exact_live_admission_namespace() -> None:
    assert (
        validate_schema("mdf_p4_tdx_daily_bars_live_admission_r1")
        == "mdf_p4_tdx_daily_bars_live_admission_r1"
    )
    for unsafe in (
        "mdf_p4_tdx_daily_bars_live_admission_r1_extra",
        "mdf_p4_tdx_daily_bars_live_admission_r1;drop",
    ):
        with pytest.raises(ContractError, match="unsafe_postgres_namespace"):
            validate_schema(unsafe)
