"""Read-only real routers over an explicitly copied isolated job journal."""

import json
import socket
import sys
from pathlib import Path

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.tdx_p0_jobs import DailyJobs
from market_data_foundation.historical.tdx_p0_strategy import DailyStrategy
from market_data_foundation.web.packages import create_package_app
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.d01_package_fixture import fixture_host
from tests.test_tdx_p0 import Store


def forbidden(*_, **__):
    raise AssertionError("external_io_forbidden")


root = Path(sys.argv[1]).resolve()
if not (root / "fixture-only.json").is_file():
    raise ValueError("explicit_copied_journal_required")
for name in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, name, forbidden)
socket.create_connection = psycopg.connect = forbidden
runtime = DailyRuntime(
    TdxOptions(root / "runtime", Path("/unused"), "unused"), Store(), forbidden
)
runtime.jobs = DailyJobs(root / "journal", Store(), forbidden)
runtime.strategy = DailyStrategy(runtime.jobs, directory=root / "strategy")
app = FastAPI()
app.include_router(tdx_router(runtime))
app.include_router(roster_router(runtime))
host = fixture_host(root / "packages", initialize=not (root / "packages").exists())
with TestClient(create_package_app(host)) as packages, TestClient(app) as client:
    current = packages.get("/api/r5/packages/tdx").json()
    saved = packages.put(
        "/api/r5/packages/tdx",
        json=dict(
            expected_revision=current["config_revision"],
            enabled_capability_ids=["daily_native", "stock_roster"],
            source_changes=[],
        ),
    )
    # Fresh fixtures use the normal initial revision, rather than an assumed id.
    if saved.status_code != 200:
        raise ValueError("isolated_package_setup_failed")
    for line in sys.stdin:
        command = json.loads(line)
        if command.get("method", "GET") != "GET":
            print(
                json.dumps(
                    dict(status=405, body=dict(classification="fixture_read_only"))
                ),
                flush=True,
            )
            continue
        path = command["path"]
        response = (
            packages
            if path.startswith(("/api/r5/sources", "/api/r5/packages"))
            else client
        ).get(path)
        print(
            json.dumps(dict(status=response.status_code, body=response.json())),
            flush=True,
        )
