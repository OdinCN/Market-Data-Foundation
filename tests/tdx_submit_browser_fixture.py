"""Isolated TDX job routes for the deployed browser's submit-flow test."""

import json
import sys
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.tdx_p0_contract import ORIGIN
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.test_tdx_p0 import Source, Store


def main():
    directory = Path(sys.argv[1])
    req = json.load(sys.stdin)
    runtime = DailyRuntime(
        TdxOptions(directory, Path("/unused"), "fixture.invalid", allow_collect=True),
        store=Store(),
        source_factory=lambda _: Source(directory, length=3),
    )
    # Exercise the writer and a later GET against the same isolated job journal.
    runtime.jobs.launch = runtime.jobs.run
    app = FastAPI()
    app.include_router(tdx_router(runtime))
    app.include_router(roster_router(runtime))
    app.state.mdf_dev_read_origin = ORIGIN
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        response = client.request(
            req["method"], req["path"], json=req.get("body"), headers={"Origin": ORIGIN}
        )
    print(json.dumps({"status": response.status_code, "body": response.json()}))


if __name__ == "__main__":
    main()
