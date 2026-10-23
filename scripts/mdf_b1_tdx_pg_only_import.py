"""R2 frozen B1 offline rehearsal with request clocks; no DSN or live write mode."""

import argparse
import json

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.integration.tdx_daily_ingest import offline_rehearsal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline-frozen", action="store_true", required=True)
    parser.parse_args()
    try:
        report = offline_rehearsal()
    except (ContractError, OSError, ValueError, KeyError, TypeError) as exc:
        # Classification only, no raw exception, credentials or wire in output.
        print(json.dumps({"status": "STOP", "classification": type(exc).__name__}))
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
