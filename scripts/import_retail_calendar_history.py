"""Calendar-only Census maintenance; no DB, jobs, strategy changes or data requests."""

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path

from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.retail_release_scheduler import (
    RetailReleaseScheduler,
    calendar_fetcher,
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--year", type=int, action="append", required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--html", type=Path)
    mode.add_argument(
        "--fetch", action="store_true", help="explicit calendar HTTPS opt-in"
    )
    parser.add_argument(
        "--acquired-at", help="actual HTML acquisition time, with offset"
    )
    parser.add_argument(
        "--request-control", type=Path, help="existing shared Census budget"
    )
    args = parser.parse_args()
    if not args.state.is_file():
        parser.error("existing retail schedule required; preserve its original path")
    if args.html and (len(args.year) != 1 or not args.acquired_at):
        parser.error("offline import requires one year and actual --acquired-at")
    if args.fetch and (not args.request_control or not args.request_control.is_file()):
        parser.error("fetch requires the existing shared Census --request-control file")
    scheduler = RetailReleaseScheduler(None, args.state)
    scheduler._read()  # Validate asset binding before any HTTP or mutation.
    if args.fetch:
        scheduler.history_calendar_fetcher = calendar_fetcher(
            CpiRequestControl(args.request_control)
        )
        for year in args.year:
            result = scheduler.refresh_history_calendars(years=[year])
            cache = result["calendar"].get("history_calendars", {}).get(str(year), {})
            # One bounded retry for normal shared admission spacing; other
            # cooldowns and failures remain durable for the operator/worker.
            if cache.get("error") == "census_retail_request_spacing":
                delay = (
                    datetime.fromisoformat(cache["next_refresh_at"]) - datetime.now(UTC)
                ).total_seconds()
                if 0 <= delay <= 2:
                    time.sleep(delay + 0.01)
                    result = scheduler.refresh_history_calendars(years=[year])
    else:
        result = scheduler.import_history_calendar(
            args.html.read_bytes(),
            datetime.fromisoformat(args.acquired_at),
            year=args.year[0],
        )
    caches = result["calendar"].get("history_calendars", {})
    errors = {str(y): caches.get(str(y), {}).get("error") for y in args.year}
    print(
        json.dumps(
            {
                "calendar_publications": len(result["calendar_publications"]),
                "targets": len(result["targets"]),
                "errors": errors,
            }
        )
    )
    return 1 if any(errors.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
