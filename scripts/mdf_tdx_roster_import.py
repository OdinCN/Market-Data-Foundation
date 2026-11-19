"""Explicit DEV import of a digest-bound manifest of retained exchange originals."""

import argparse
import json
from pathlib import Path

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_exchange_import import (
    import_directory,
    publish_roster,
)
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
    store = PgStore()
    provider, proof = import_directory(store, args.provider_snapshot, args.provider_job)
    saved = publish_roster(store, raw, provider, proof)
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
