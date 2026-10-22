"""Named Host retained quote intake, no live authority and no network calls."""

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import require
from market_data_foundation.runtime.tdx_watchlist import (
    V100_DRIVER_SDK_RELEASE,
    SessionConfig,
    decode_json,
)
from market_data_foundation.runtime.watchlist_read import read_latest_watchlist

from .tdx_market_collect import ArtifactRun
from .tdx_market_evidence import quote_changes
from .tdx_market_plan import Plan, Request, bounded_read, encoded


def intake_quotes(source_dir: Path, output: Path, *, expected_files: dict[str, str]):
    """Read only an explicitly named manifest. Never discover directories or refetch.

    Caller supplies Host-frozen digests for config/report/ACK receipt/every batch.
    Receipt and report readers independently reconstruct acceptance from raw bytes.
    Original Source run identity is retained; this inspection gets a NEW identity.
    """
    require(source_dir.is_absolute(), "named_absolute_retained_directory_required")
    require(
        type(expected_files) is dict and len(expected_files) <= 12, "manifest_budget"
    )
    require(
        {"config.json", "report.json", "read-ready.json"} <= set(expected_files),
        "retained_receipt_manifest_required",
    )
    blobs = {}
    for name, digest in expected_files.items():
        require(Path(name).name == name, "manifest_name_invalid")
        raw = bounded_read(source_dir / name, 1024 * 1024)
        require(
            len(raw) <= 1024 * 1024 and sha256(raw).hexdigest() == digest,
            "retained_manifest_mismatch",
        )
        blobs[name] = raw
    config_values = decode_json(blobs["config.json"])
    for name in ("securities", "endpoints"):
        config_values[name] = tuple(config_values[name])
    config = SessionConfig(**config_values)
    require(
        config.driver_sdk_release == V100_DRIVER_SDK_RELEASE
        and not config.sdk_candidates
        and len(config.endpoints) == 1
        and config.max_failovers == 0
        and 1 <= config.rounds <= 8,
        "retained_configuration_not_supported",
    )
    require(
        set(blobs)
        == {"config.json", "report.json", "read-ready.json"}
        | {f"batch-{n}.json" for n in range(1, config.rounds + 1)},
        "exact_manifest_required",
    )
    report = decode_json(blobs["report.json"])
    require(
        report["session_id"] == config.session_id
        and report["session_complete"] is True
        and report["stop_reason"] == "budget_exhausted"
        and report["go_ack_receipts"] == config.rounds,
        "incomplete_retained_report",
    )
    # Read at current time: old materials can be stale. Replaying original receipt
    # clocks for internal validation does not turn them into current observations.
    inspected_at = datetime.now(UTC)
    receipt = read_latest_watchlist(source_dir, read_at=inspected_at)
    completed = read_latest_watchlist(
        source_dir, read_at=inspected_at, completed_report=True
    )
    for result in (receipt, completed):
        require(
            result["status"] in {"observed", "stale"} and result["batch"] is not None,
            "retained_acceptance_reconstruction_failed",
        )
    require(
        receipt["batch"] == completed["batch"], "retained_acceptance_modes_disagree"
    )
    # Reject any producer rewrite during reconstruction.
    require(
        all(
            bounded_read(source_dir / name, 1024 * 1024) == raw
            for name, raw in blobs.items()
        ),
        "retained_files_changed",
    )
    local_clocks = [
        datetime.fromisoformat(
            decode_json(blobs[f"batch-{n}.json"])["clocks"]["received"]
        ).astimezone(ZoneInfo("Asia/Shanghai"))
        for n in range(1, config.rounds + 1)
    ]
    require(
        all((clock.hour, clock.minute) >= (15, 5) for clock in local_clocks)
        and len({clock.date() for clock in local_clocks}) == 1,
        "retained_after_close_context_required",
    )
    plan = Plan(
        config.session_id,
        config.endpoints[0],
        "0" * 64,
        "0" * 64,
        (Request("GetCount", 1),),
        config.securities,
        source_enabled=False,
        observation_day=local_clocks[0].date().isoformat(),
        quote_rounds=config.rounds,
        quote_period_seconds=int(config.period_seconds),
        quote_wall_seconds=int(config.duration_seconds),
    )
    comparison = quote_changes(
        tuple(blobs[f"batch-{n}.json"] for n in range(1, config.rounds + 1)), plan
    )
    run = ArtifactRun(output)
    copies = {name: run.write(name, raw) for name, raw in blobs.items()}
    run.write("manifest.json", encoded(expected_files))
    # Reconstruct from the exact copied bytes too, before recording intake success.
    for completed_report in (False, True):
        copied = read_latest_watchlist(
            run.path, read_at=inspected_at, completed_report=completed_report
        )
        require(
            copied["status"] == receipt["status"]
            and copied["batch"] == receipt["batch"],
            "copied_acceptance_readback_failed",
        )
    run.write("comparison.json", encoded(comparison))
    summary = {
        "schema": "mdf.i2.retained-quote-intake.v1",
        "inspection_id": uuid4().hex,
        "original_source_run_id": config.session_id,
        "inspected_at": inspected_at.isoformat(),
        "new_provider_requests": 0,
        "original_getquote_calls": report["observed_sdk_getquote_entries"],
        "original_ack_receipts": report["go_ack_receipts"],
        "status_at_inspection": receipt["status"],
        "classification": "RETAINED_AFTER_CLOSE_SNAPSHOT_READBACK",
        "source_event_time": "UNKNOWN",
        "intraday_refresh": "NOT_PROVEN",
        "calendar": "UNKNOWN",
        "universe": "BOUNDED_EXPLICIT_TARGETS_ONLY",
        "source_adoption": "NOT_GRANTED",
        "pg_writes": 0,
        "garage_writes": 0,
        "materials": copies,
    }
    run.write("retained-intake.json", encoded(summary))
    # Current-age metadata only. Price/level content remains in private copied blobs.
    run.write(
        "current-readback.json",
        encoded(
            {
                "batch": receipt["batch"],
                "status": receipt["status"],
                "members": [
                    {
                        key: member[key]
                        for key in (
                            "security",
                            "status",
                            "stale",
                            "local_received_age_seconds",
                            "value_received_at",
                        )
                    }
                    for member in receipt["members"]
                ],
            }
        ),
    )
    return summary
