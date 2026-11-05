"""Explicit, single-project DEV financial daily worker (never starts on a GET)."""

import argparse
import sys
import threading
import time
from pathlib import Path

from market_data_foundation.web.financial_daily_jobs import FinancialJobs
from market_data_foundation.web.financial_daily_schedule import FinancialSchedule
from market_data_foundation.web.worker_control import read_window


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--opt-in-financial-worker", action="store_true")
    parser.add_argument("--allow-collect", action="store_true")
    parser.add_argument("--state-directory", type=Path, required=True)
    parser.add_argument("--job-directory", type=Path, required=True)
    parser.add_argument("--window-state", type=Path, required=True)
    args = parser.parse_args()
    if not args.opt_in_financial_worker or not args.allow_collect:
        parser.error("explicit DEV worker and source-write opt-in required")
    if sys.version_info[:3] != (3, 14, 8):
        parser.error("qualified Python baseline required")
    read_window(args.window_state)
    schedule = FinancialSchedule(args.state_directory)
    jobs = FinancialJobs(args.job_directory)
    state = {"value": "idle", "classification": None}
    stopped = threading.Event()

    def heartbeat():
        while not stopped.is_set():
            schedule.heartbeat(state["value"], classification=state["classification"])
            stopped.wait(5)

    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    try:
        while True:
            read_window(args.window_state)
            state.update(
                value="running" if schedule.read()["enabled"] else "paused",
                classification=None,
            )
            try:
                result = schedule.tick(jobs)
                state.update(value="paused" if result["state"] == "paused" else "idle")
            except Exception as error:
                state.update(
                    value="error",
                    classification=getattr(
                        error, "classification", "financial_worker_check_failed"
                    ),
                )
            schedule.heartbeat(state["value"], classification=state["classification"])
            time.sleep(5)
    finally:
        stopped.set()
        thread.join(timeout=6)
        schedule.heartbeat("stopped")


if __name__ == "__main__":
    main()
