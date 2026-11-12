"""Read-only frozen B1 intake; delegates admission to delivered evidence code."""

import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import unquote, urlparse
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import require
from market_data_foundation.qualification.tdx_market_evidence import admit_line
from market_data_foundation.qualification.tdx_market_plan import (
    Plan,
    bounded_read,
    encoded,
)
from market_data_foundation.storage.tdx_daily_pg import (
    PRIMARY_SOURCE,
    Bar,
    Batch,
)

FROZEN_ROOT = Path(
    "/srv/market-data-foundation-control/worktrees/"
    "mdf-b1-tdx-eod-batch-20261023-r1/artifacts"
)
HOST_OUTPUTS = Path(
    "/srv/market-data-foundation-control/transactions/"
    "MDF-B1-TDX-EOD-PRODUCTION-MATRIX-20261023-R1/outputs"
)
HOST_POSTFLIGHT_SHA = "9b9def717fa7d7f3bba1a5650d09f53d980b31da584ab200456ceecee96aa1a3"
HOST_RECEIPTS = {
    "six": "a891a542a9ed2bb97879a7918a590b8a6035e5ecba3ac50f536b86222efcd28d",
    "thirtytwo": "76657305cca2f266c2c960bd3571db4fe2542e4e2c5cfd33b19811a5f25e2102",
}
QUALIFICATIONS = {
    "category_identity": "UNKNOWN",
    "universe_calendar": "UNKNOWN",
    "source_event_time": "UNKNOWN",
    "adjustment": "UNKNOWN",
    "historical_exhaustion": "UNKNOWN",
    "D1_publication": "UNKNOWN",
    "price_amount_unit": "SDK native signed integer, 0.001 yuan",
    "volume_unit": "SDK native integer, hand; Type9 no /100",
}


def read_checked(path, expected, limit=4 * 1024 * 1024):
    raw = bounded_read(path, limit)
    require(sha256(raw).hexdigest() == expected, "host_digest_mismatch")
    return raw


def native_bar(market, instrument, native):
    """Native daily projection only; Last never enters the economic fact."""
    clock = datetime.fromisoformat(native["Time"])
    require(
        # Receipts may use fixed Beijing time or the historical Shanghai civil offset.
        clock.utcoffset()
        in (timedelta(hours=8), ZoneInfo("Asia/Shanghai").utcoffset(clock))
        and (clock.hour, clock.minute, clock.second, clock.microsecond)
        == (15, 0, 0, 0),
        "sdk_date_projection_invalid",
    )
    return Bar(
        market,
        instrument,
        clock.date(),
        tuple(native[k] for k in ("Open", "High", "Low", "Close", "Volume", "Amount")),
    )


def frozen_batch(stage, *, known_at=None):
    """Exact named roots and pinned Host receipts only; no Provider transport.

    Completeness means every requested 32-row window returned and passed quality,
    not full history exhaustion or official calendar/category qualification.
    """
    require(stage in HOST_RECEIPTS, "frozen_stage_only")
    known_at = known_at or datetime.now(UTC)
    root = FROZEN_ROOT / f"b1-go-r7-{stage}-live"
    receipt = json.loads(
        read_checked(
            HOST_OUTPUTS / f"B1_GO_{stage.upper()}_R7_RETURN.json",
            HOST_RECEIPTS[stage],
            65536,
        )
    )
    postflight = json.loads(
        read_checked(
            HOST_OUTPUTS / "B1_REAL_SOURCE_POSTFLIGHT_R8.json",
            HOST_POSTFLIGHT_SHA,
            65536,
        )
    )["runs"][stage.upper()]
    plan_raw = read_checked(root / "plan.json", postflight["plan_sha256"], 65536)
    plan = Plan.load(plan_raw)
    ledger = json.loads(
        read_checked(
            root / "effect-ledger.json",
            receipt["effect_ledger_sha256"],
            65536,
        )
    )
    require(
        ledger["plan_revision"] == plan.revision == postflight["plan_sha256"],
        "plan_identity",
    )
    require(
        ledger["source"] == "BOUNDED_READ_COMPLETE"
        and ledger["source_exit"] == 0
        and ledger["source_effect"] == receipt["source_effect"]
        and ledger["source_effect"]["stage"] == "COMPLETE"
        and ledger["source_effect"]["total_effect"] == "EXACT"
        and ledger["source_effect"]["unconfirmed_stdout_bytes"] == 0,
        "source_incomplete",
    )
    require(plan.run_id == receipt["run_id"] == postflight["run_id"], "run_identity")
    # Every ledger artifact, including MConnect, is verified in place. No copies.
    for material in ledger["materials"]:
        parsed = urlparse(material["uri"])
        path = Path(unquote(parsed.path))
        require(
            parsed.scheme == "file"
            and not parsed.netloc
            and path.parent == root
            and not path.is_symlink(),
            "ledger_path_scope",
        )
        raw = read_checked(path, material["sha256"])
        require(len(raw) == material["bytes"], "ledger_byte_mismatch")
    lines = read_checked(
        root / "source-stdout.jsonl", receipt["source_stdout_sha256"]
    ).splitlines()
    require(len(lines) == len(plan.requests) + 1, "line_count_invalid")
    bars, evidence, clocks = [], {}, []
    for offset, line in enumerate(lines):
        index = offset - 1
        row, sent, wire = admit_line(line, plan, index, known_at=known_at)
        require(row["classification"] == "retained_raw", "source_line_failed")
        require(
            sent == bounded_read(root / f"source-{offset}-request.bin", 8192)
            and wire
            == bounded_read(root / f"source-{offset}-response.bin", 4 * 1024 * 1024),
            "wire_file_drift",
        )
        if index < 0:
            continue
        request = plan.requests[index]
        require(
            request.operation == "GetKline"
            and request.start == 0
            and request.count == 32
            and plan.mode == "after_close",
            "frozen_request_scope",
        )
        assessment = row["daily_assessment"]
        require(
            not assessment["issues"] and assessment["row_count"] == 32,
            "incomplete_or_invalid_page",
        )
        symbol = ("sh" if request.market else "sz") + request.code
        host = postflight["symbols"][symbol]
        proof = {
            "request_index": index,
            "received_at": row["received_at"],
            "line_sha256": sha256(line).hexdigest(),
            "wire_sha256": sha256(wire).hexdigest(),
            "request_sha256": sha256(sent).hexdigest(),
            "projection_sha256": sha256(encoded(row["source_projection"])).hexdigest(),
        }
        require(
            proof["line_sha256"] == host["line_sha256"]
            and proof["wire_sha256"] == host["original_wire_sha256"]
            and proof["projection_sha256"] == host["projection_sha256"],
            "host_request_evidence_mismatch",
        )
        scope = f"{request.market}:{request.code}"
        require(scope not in evidence, "ambiguous_request_clock_mapping")
        evidence[scope] = [proof]
        received = datetime.fromisoformat(row["received_at"])
        local = received.astimezone(timezone(timedelta(hours=8)))
        require(
            local.date().isoformat() == plan.observation_day
            and (local.hour, local.minute) >= (15, 5),
            "after_close_required",
        )
        clocks.append(received)
        for native in row["source_projection"]["List"]:
            bars.append(native_bar(request.market, request.code, native))
    require(len(bars) == postflight["total_rows"], "host_row_count")
    batch = Batch(
        plan.run_id,
        PRIMARY_SOURCE,
        max(clocks),
        known_at,
        tuple(bars),
        evidence,
        True,
        True,
        min(b.trading_day for b in bars),
        max(b.trading_day for b in bars),
    )
    batch.validate()
    audit = {
        "stage": stage,
        "rows": len(bars),
        "unique": len({b.key for b in bars}),
        "plan_sha256": sha256(plan_raw).hexdigest(),
        "source_stdout_sha256": receipt["source_stdout_sha256"],
        "effect_ledger_sha256": receipt["effect_ledger_sha256"],
        "host_receipt_sha256": HOST_RECEIPTS[stage],
        "host_postflight_sha256": HOST_POSTFLIGHT_SHA,
        "wire_requests_checked": len(lines),
        "batch_digest": batch.fingerprint,
        "qualifications": QUALIFICATIONS,
    }
    return batch, audit


def offline_rehearsal():
    """Read frozen evidence and run the reference oracle. Never opens PG."""
    from market_data_foundation.storage.tdx_daily_pg import OfflineCurrent

    large, large_audit = frozen_batch("thirtytwo")
    small, small_audit = frozen_batch("six")
    reference = OfflineCurrent()
    first = reference.apply(large)
    overlap = reference.apply(small)
    repeated = reference.apply(
        replace(large, batch_id=large.batch_id + "-offline-repeat")
    )
    retry = reference.apply(large)
    rebuilt = OfflineCurrent.rebuild(reference.snapshot())
    require(rebuilt.snapshot() == reference.snapshot(), "offline_cold_rebuild")
    require(
        len(reference.current) == 1024 and overlap.new == overlap.changed == 0,
        "frozen_overlap_invalid",
    )
    return {
        "mode": "OFFLINE_REFERENCE_ONLY_NOT_PG_SQL_PASS",
        "inputs": [large_audit, small_audit],
        "counts": {
            "first": asdict(first),
            "six_overlap": asdict(overlap),
            "full_repeat": asdict(repeated),
            "full_retry": asdict(retry),
            "current": len(reference.current),
            "corrections": len(reference.corrections),
        },
        "cold_reference_rebuild": True,
        "pg_sql_gate": "PENDING_DEV_AUTHORIZATION",
        "effects": {"pg": 0, "garage": 0, "source": 0, "git_delivery": 0},
    }
