"""Actual product job routers + isolated durable journals; no business execution.

The fixture never imports frontend submission code. Its synthetic host admits a
bounded command and persists through the normal JobJournal. Socket and PG access
are prohibited; dedicated /tmp roots only. Legacy adapters use their existing
separate ASGI rigs rather than pretending to accept another protocol.
"""

import atexit
import json
import shutil
import signal
import socket
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import psycopg
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical import bls_timeseries_profile as profiles
from market_data_foundation.historical.claims_contract import SERIES_IDS as CLAIMS_IDS
from market_data_foundation.historical.gdp_contract import SERIES_IDS as GDP_IDS
from market_data_foundation.web.cpi_timeseries_api import (
    timeseries_router as monthly_router,
)
from market_data_foundation.web.cpi_timeseries_jobs import JobJournal, WebError
from market_data_foundation.web.financial_daily_api import (
    FinancialOptions,
    financial_router,
)
from market_data_foundation.web.gdp_api import timeseries_router as period_router
from market_data_foundation.web.package_catalog import PACKAGES
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from market_data_foundation.web.source_catalog import DirectoryQuery, directory
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.test_tdx_p0 import Source, Store


def forbidden(*_, **__):
    raise AssertionError("external_io_forbidden")


for operation in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, operation, forbidden)
socket.create_connection = psycopg.connect = forbidden
family = sys.argv[1]
root = Path(tempfile.mkdtemp(prefix="mdf-submit-matrix-"))
atexit.register(shutil.rmtree, root, ignore_errors=True)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))


class Host:
    def __init__(self, key):
        self.profile = getattr(
            profiles, key.upper() + "_PROFILE", SimpleNamespace(key=key)
        )
        self.prefix = f"/api/r5/{key}/timeseries"
        self.journal = JobJournal(root / key, profile=self.profile)
        self.key = key

    def _read_allowed(self):
        pass

    def status(self):
        result = dict(
            latest_months={},
            observation_count=0,
            revision_count=0,
            batch_count=0,
            source_allowed=True,
            web_capabilities=dict(query=True, collect=True),
            initialization=dict(
                start="2026-09",
                end="2026-09",
                months=1,
                available=True,
                preset_id="isolated",
                anchor_basis="confirmed_local_available",
            ),
        )
        if self.key == "gdp":
            result.update(
                latest_quarters={k: None for k in GDP_IDS},
                initialization=dict(
                    start="2026-Q3",
                    end="2026-Q3",
                    quarters=1,
                    available=True,
                    anchor_basis="previous_complete_quarter",
                    preset_id="isolated",
                ),
                collection_scope=dict(max_quarters=120, series="all"),
            )
        if self.key == "claims":
            result.update(
                latest_weeks={k: "2026-10-31" for k in CLAIMS_IDS},
                initialization=dict(
                    start="2026-10-31",
                    end="2026-10-31",
                    years=20,
                    available=True,
                    preset_id="isolated",
                ),
            )
        return result

    def executions(self):
        return dict(
            executions=[
                dict(j, id=j["job_id"], origin="manual") for j in self.journal.list()
            ]
        )

    def submit(self, command, *, on_started=None):
        if set(command) != {"request_id", "action", "start", "end"}:
            raise WebError("invalid_job_shape")
        identity = str(UUID(command["request_id"]))
        self.journal.directory.mkdir(parents=True, exist_ok=True)
        existing = self.journal.read(identity)
        if existing:
            if any(existing[k] != command[k] for k in command):
                raise WebError("identity_conflict", 409)
            return existing
        now = datetime.now(UTC).isoformat()
        job = dict(
            command,
            job_id=identity,
            asset=self.key,
            origin="manual",
            state="running",
            created_at=now,
            started_at=now,
            finished_at=None,
            classification=None,
            result=None,
        )
        self.journal.save(job)
        if on_started:
            on_started(job)
        return job


app = FastAPI()
if family in {"tdx", "roster"}:
    runtime = DailyRuntime(
        TdxOptions(
            root / "tdx",
            Path("/unused"),
            "fixture.invalid",
            True,
            origin=CANDIDATE_ORIGIN,
        ),
        Store(),
        lambda _: Source(root, length=3),
    )
    runtime.jobs.launch = runtime.jobs.run
    app.include_router(tdx_router(runtime))
    app.include_router(roster_router(runtime))
    app.state.mdf_dev_read_origin = CANDIDATE_ORIGIN
elif family == "financial":
    host = Host("financial")
    # Financial jobs retain their distinct nested command and result envelope.
    original = host.submit

    def submit(command, *, on_started=None):
        job = original(command)
        nested = dict(
            job, command=command, steps=[], provider_requests=0, state="pending"
        )
        host.journal.save(nested)
        if on_started:
            on_started(nested)
        return nested

    host.submit = submit
    app.include_router(
        financial_router(
            FinancialOptions(root, allow_read=True, allow_collect=True), jobs=host
        )
    )
else:
    host = Host(family)
    if family in {"cpi", "ppi", "pce", "employment", "consumption", "jolts"}:
        app.include_router(monthly_router(host))
    else:
        app.include_router(period_router(host, prefix=host.prefix, family=family))

with TestClient(
    app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
) as client:
    for line in sys.stdin:
        req = json.loads(line)
        if req.get("control") == "inventory":
            out = dict(
                status=200,
                body=[
                    dict(
                        package=p.package_id,
                        capability=c.capability_id,
                        asset=c.definition_ref.model_dump(),
                        title=c.title,
                    )
                    for p in PACKAGES
                    for c in p.capabilities
                ],
            )
        elif req.get("control") == "directory":
            out = dict(status=200, body=directory(DirectoryQuery()).model_dump())
        elif req.get("control") == "stored":
            paths = list(root.rglob("*.json"))
            out = dict(
                status=200,
                body=dict(
                    records=[
                        json.loads(p.read_text())
                        for p in paths
                        if p.name not in {"base.json"}
                    ]
                ),
            )
        else:
            response = client.request(
                req.get("method", "GET"),
                req["path"],
                json=req.get("body"),
                headers={"Origin": CANDIDATE_ORIGIN},
            )
            out = dict(status=response.status_code, body=response.json())
        print(json.dumps(out), flush=True)
