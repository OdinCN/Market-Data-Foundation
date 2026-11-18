"""Run the independent exchange evidence refresh on the MDF DEV host."""

import argparse
import fcntl
import json
from datetime import UTC, datetime, time
from pathlib import Path
from tempfile import mkdtemp
from zoneinfo import ZoneInfo

import httpx
import psycopg

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.tdx_p0_exchange_daily import (
    ExchangeOriginals,
    classification,
    collect_manifest,
)
from market_data_foundation.historical.tdx_p0_exchange_import import (
    import_directory,
    publish_roster,
)
from market_data_foundation.historical.tdx_p0_jobs import atomic, strategy_slot_id
from market_data_foundation.storage.tdx_p0 import PgStore

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)


def read_record(directory, day, action):
    identity = strategy_slot_id(day, action)
    path = directory / (identity + ".json")
    if not path.exists():
        return path, None
    record = json.loads(path.read_bytes())
    require(
        record["request_id"] == identity
        and record["command"]["end"] == day
        and record["command"]["action"] == action
        and record["origin"] == "periodic",
        "tdx_exchange_periodic_job_identity",
    )
    return path, record


def refresh(args, store, at):
    local = at.astimezone(ZoneInfo("Asia/Shanghai"))
    day = local.date()
    day_text = day.isoformat()
    status = {"day": day_text, "checked_at": at.isoformat()}
    calendars = {
        row["market"]: row["is_open"]
        for row in store.strategy_calendar(as_of=at)
        if row["day"] == day_text and datetime.fromisoformat(row["available_at"]) <= at
    }
    if set(calendars) != {0, 1, 2}:
        return dict(status, state="CALENDAR_PENDING")
    if not all(calendars.values()) or local.time() < time(16, 5):
        return dict(status, state="OUTSIDE_TRADING_DAY_WINDOW")
    existing = store.universe(as_of=at, day=day)
    details = (existing or {}).get("details", {})
    ready = details.get("exchange_refresh", {})
    if (
        details.get("roster_kind") == "exchange_lifecycle"
        and details.get("valid_through") == day_text
        and {
            m["market"]
            for m in details.get("markets", [])
            if market_complete(m, day_text)
        }
        == {0, 1, 2}
        and not ready.get("pending_markets")
    ):
        return dict(
            status, state="ALREADY_PUBLISHED", snapshot_id=existing["snapshot_id"]
        )
    job_path, roster = read_record(args.jobs_dir, day_text, "universe")
    _, daily = read_record(args.jobs_dir, day_text, "update")
    if (
        not roster
        or roster["state"] != "COMPLETE"
        or not daily
        or daily["state"] not in {"COMPLETE", "PARTIAL"}
    ):
        return dict(status, state="TDX_JOBS_PENDING")
    require(
        daily.get("roster_binding", {}).get("request_id") == roster["request_id"]
        and daily.get("roster_binding", {}).get("snapshot_id") == roster["snapshot_id"],
        "tdx_exchange_roster_binding",
    )
    provider, proof = import_directory(store, roster["snapshot_id"], job_path)
    # Keep a same-day census verified on an earlier attempt even if another
    # official source has since become unavailable.
    if ready.get("day") == day_text:
        provider["historical_members"] = existing["members"]
    positive = store.positive_daily_keys(day, as_of=at)
    directory = Path(mkdtemp(prefix=f"{day_text}-", dir=args.artifact_dir))
    reference = json.loads(args.reference_manifest.read_bytes())
    with httpx.Client(
        headers={"User-Agent": USER_AGENT},
        timeout=httpx.Timeout(15, connect=5),
        follow_redirects=False,
    ) as client:
        manifest = collect_manifest(
            day,
            reference,
            provider,
            positive,
            ExchangeOriginals(directory, client).get,
        )
    previous_pending = set(ready.get("pending_markets", []))
    pending = set(manifest["exchange_refresh"]["pending_markets"])
    manifest["exchange_refresh"].update(
        roster_job=roster["request_id"],
        daily_job=daily["request_id"],
    )
    path = directory / "manifest.json"
    atomic(path, manifest)
    result = dict(
        status,
        manifest=str(path),
        markets=manifest["exchange_refresh"],
        roster_snapshot=roster["snapshot_id"],
    )
    if len(pending) == 3 or (
        ready.get("day") == day_text and not pending < previous_pending
    ):
        # Only publish a strictly more complete retry for the same trading date.
        return dict(result, state="EVIDENCE_PENDING")
    saved = publish_roster(store, path.read_bytes(), provider, proof)
    return dict(
        result,
        state="PARTIAL" if pending else "COMPLETE",
        snapshot_id=saved["snapshot_id"],
        available_at=saved["available_at"].isoformat(),
    )


def market_complete(market, day):
    return market.get(
        "enumeration"
    ) == "EXCHANGE_CURRENT_AND_TERMINATED" and market.get(
        "enumeration_from"
    ) <= day <= market.get("enumeration_through")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs-dir", required=True, type=Path)
    parser.add_argument("--artifact-dir", required=True, type=Path)
    parser.add_argument("--reference-manifest", required=True, type=Path)
    parser.add_argument("--dev-opt-in", action="store_true")
    args = parser.parse_args()
    require(args.dev_opt_in, "tdx_dev_exchange_refresh_opt_in_required")
    args.artifact_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (args.artifact_dir / "refresh.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(json.dumps({"state": "ALREADY_RUNNING"}))
            return
        at = datetime.now(UTC)
        try:
            result = refresh(args, PgStore(), at)
        except (ContractError, OSError, ValueError, KeyError, psycopg.Error) as error:
            result = {
                "state": "FAILED",
                "classification": classification(error),
                "checked_at": at.isoformat(),
            }
        atomic(args.artifact_dir / "latest.json", result)
        print(json.dumps(result, ensure_ascii=False))
        if result["state"] == "FAILED":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
