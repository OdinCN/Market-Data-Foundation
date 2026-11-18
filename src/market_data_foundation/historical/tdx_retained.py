"""Explicit admission of qualified, digest-bound receipts through normal jobs.

This source performs no network requests and never changes receipt clocks or
request identities. DailyJobs supplies the new admission clock; PgStore supplies
the formal publication clock, including normal correction/as-of semantics.
"""

import json
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_source import verify_wire
from market_data_foundation.runtime.tdx_watchlist import V100_DRIVER_SDK_RELEASE


class RetainedDailySource:
    def __init__(self, manifest, *, endpoint, dev_opt_in=False):
        require(dev_opt_in is True, "tdx_retained_dev_opt_in_required")
        require(manifest["endpoint"] == endpoint, "tdx_retained_endpoint")
        self.rows = {}
        self.requests = 0
        counts = set()
        for proof in manifest["receipts"]:
            path = Path(proof["path"])
            raw = path.read_bytes()
            require(sha256(raw).hexdigest() == proof["sha256"], "tdx_retained_digest")
            observation = Path(proof["observation_path"]).read_bytes()
            require(
                sha256(observation).hexdigest() == proof["observation_sha256"],
                "tdx_retained_observation_digest",
            )
            row, obs = json.loads(raw), json.loads(observation)
            request = row["request"]
            require(
                row["classification"] == "retained_raw"
                and row["peer"] == endpoint
                and row["sdk_release"] == V100_DRIVER_SDK_RELEASE
                and request["operation"] == "GetKline"
                and request["count"] > 0
                and obs["success"] is True
                and obs["mode"] == "live"
                and obs["provider"] == "tdx-v100-primary"
                and obs["capability"] == "bars.daily"
                and obs["implementation_version"] == V100_DRIVER_SDK_RELEASE
                and obs["fixture_id"] == "p0-" + request["id"]
                and f"{obs['endpoint']['host']}:{obs['endpoint']['port']}" == endpoint
                and datetime.fromisoformat(row["requested_at"])
                <= datetime.fromisoformat(row["received_at"])
                <= datetime.now(UTC),
                "tdx_retained_qualification",
            )
            verify_wire(row)
            key = (request["market"], request["code"], request["start"])
            require(key not in self.rows, "tdx_retained_duplicate_page")
            counts.add(request["count"])
            self.rows[key] = dict(
                path=str(path), sha256=proof["sha256"], row=row, retained=True
            )
        require(bool(self.rows) and len(counts) == 1, "tdx_retained_page_size")
        self.page_size = counts.pop()

    def request(self, operation, market, code="", start=0, count=0):
        require(operation == "GetKline", "tdx_retained_operation")
        require(count == self.page_size, "tdx_retained_request_count")
        require((market, code, start) in self.rows, "tdx_retained_page_missing")
        return self.rows[(market, code, start)]

    def close(self):
        pass
