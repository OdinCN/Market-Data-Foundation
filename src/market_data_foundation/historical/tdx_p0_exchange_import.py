"""Prepare and publish DEV exchange evidence through the existing roster store."""

import json
from datetime import UTC, datetime
from hashlib import sha256

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_roster import build_roster


def import_directory(store, snapshot_id, job_path=None):
    provider = store.universe(snapshot_id=snapshot_id)
    require(provider is not None, "tdx_provider_snapshot_required")
    directory_proof = None
    if job_path:
        job_raw = job_path.read_bytes()
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
            path=str(job_path),
            sha256=sha256(job_raw).hexdigest(),
            request_id=job["request_id"],
        )
    else:
        require(
            not provider["details"].get("directory_snapshot"),
            "tdx_provider_directory_job_required",
        )
    return provider, directory_proof


def publish_roster(store, raw, provider, directory_proof):
    manifest = json.loads(raw)
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
    return store.universe(snapshot_id=snapshot["snapshot_id"])
