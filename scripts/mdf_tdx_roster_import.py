"""Explicit DEV import of a digest-bound manifest of retained exchange originals."""

import argparse
import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_roster import build_roster
from market_data_foundation.storage.tdx_p0 import PgStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--provider-snapshot", required=True)
    parser.add_argument("--provider-job", type=Path)
    parser.add_argument("--dev-opt-in", action="store_true")
    args = parser.parse_args()
    require(args.dev_opt_in, "tdx_dev_roster_import_opt_in_required")
    raw = args.manifest.read_bytes()
    manifest = json.loads(raw)
    store = PgStore()
    provider = store.universe(snapshot_id=args.provider_snapshot)
    require(provider is not None, "tdx_provider_snapshot_required")
    directory_proof = None
    if args.provider_job:
        job_raw = args.provider_job.read_bytes()
        job = json.loads(job_raw)
        require(
            job["snapshot_id"] == provider["snapshot_id"]
            and job["state"] == "COMPLETE"
            and job["command"]["action"] == "universe"
            and {s["market"] for s in job["steps"]} == {0, 1, 2}
            and all(s["state"] == "COMPLETE" for s in job["steps"]),
            "tdx_provider_directory_job",
        )
        provider = dict(
            provider,
            historical_members=provider["members"],
            members=[m for s in job["steps"] for m in s["members"]],
        )
        directory_proof = dict(
            path=str(args.provider_job),
            sha256=sha256(job_raw).hexdigest(),
            request_id=job["request_id"],
        )
    else:
        require(
            not provider["details"].get("directory_snapshot"),
            "tdx_provider_directory_job_required",
        )
    snapshot = build_roster(manifest, provider)
    if directory_proof:
        snapshot["details"]["provider_directory_job"] = directory_proof
    snapshot.update(
        snapshot_id="exchange-"
        + sha256(raw + b"\0" + provider["snapshot_id"].encode()).hexdigest()[:32],
        observed_at=max(s["received_at"] for s in manifest["sources"]),
        available_at=datetime.now(UTC).isoformat(),
    )
    store.save_universe(snapshot)
    saved = store.universe(snapshot_id=snapshot["snapshot_id"])
    print(
        json.dumps(
            dict(
                snapshot_id=saved["snapshot_id"],
                available_at=saved["available_at"].isoformat(),
                members=len(saved["members"]),
                markets=saved["details"]["markets"],
            ),
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
