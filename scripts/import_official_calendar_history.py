"""Explicit local BLS calendar evidence import; never downloads or collects data."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", choices=("cpi", "ppi"), required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--html", type=Path, required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--acquired-at", required=True)
    args = parser.parse_args()
    # Require an existing asset-bound scheduler file, preserving its baseline.
    state = json.loads(args.state.read_text())
    scheduler = CpiReleaseScheduler(
        None,
        args.state,
        profile=CPI_PROFILE if args.asset == "cpi" else PPI_PROFILE,
        history_start=state["history_start"],
        calendar_fetcher=None,
        reference_fetcher=None,
        signal_fetcher=None,
    )
    result = scheduler.import_display_history(
        args.html.read_bytes(),
        datetime.fromisoformat(args.acquired_at),
        source=args.source,
        start=args.start,
        end=args.end,
    )
    print(
        json.dumps(
            {
                "calendar_publications": len(result["calendar_publications"]),
                "targets": len(result["targets"]),
            }
        )
    )


if __name__ == "__main__":
    main()
