"""Admit retained qualified daily receipts as an ordinary tracked DEV update."""

import argparse
import json
from hashlib import sha256
from pathlib import Path

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_jobs import DailyJobs
from market_data_foundation.historical.tdx_retained import RetainedDailySource
from market_data_foundation.storage.tdx_p0 import PgStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--state-directory", required=True, type=Path)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--dev-opt-in", action="store_true")
    parser.add_argument("--workers", type=int, default=1)
    args = parser.parse_args()
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)

    def source(_):
        return RetainedDailySource(
            manifest, endpoint=args.endpoint, dev_opt_in=args.dev_opt_in
        )

    validated = source(None)
    command = manifest["command"]
    require(command["action"] == "update", "tdx_retained_update_required")
    if command.get("instruments") is not None:
        require(
            set(command["instruments"]) == {f"{m}:{c}" for m, c, _ in validated.rows},
            "tdx_retained_selection",
        )
    jobs = DailyJobs(
        args.state_directory / "jobs", PgStore(), source, workers=args.workers
    )
    admission = dict(
        mode="retained_qualified_receipts",
        manifest_sha256=sha256(raw).hexdigest(),
        receipt_count=len(validated.rows),
        endpoint=args.endpoint,
    )
    jobs.submit(command, launch=False, origin="manual", admission=admission)
    record = jobs.run(command["request_id"])
    print(json.dumps(record, default=str, ensure_ascii=False))
    require(
        record is not None and record["state"] == "COMPLETE",
        "tdx_retained_job_incomplete",
    )


if __name__ == "__main__":
    main()
