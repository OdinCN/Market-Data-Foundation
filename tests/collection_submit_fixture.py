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
from datetime import UTC, date, datetime, timedelta
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
from market_data_foundation.web.m3_query_api import timeseries_router as m3_router
from market_data_foundation.web.package_catalog import PACKAGES
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from market_data_foundation.web.retail_timeseries import (
    timeseries_router as retail_router,
)
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

    def strategy(self):
        return dict(
            exists=True,
            enabled=False,
            history_start="2000-Q1"
            if self.key == "gdp"
            else "2000-01-01"
            if self.key == "claims"
            else "2000-01",
            worker=dict(state="paused", last_heartbeat_at=None),
            next_due=None,
            policy=dict(timezone="Asia/Shanghai"),
        )

    def schedule(self):
        return dict(calendar_status="verified", targets=[])

    def executions(self):
        return dict(
            executions=[
                dict(j, id=j["job_id"], origin=j.get("origin", "manual"))
                for j in self.journal.list()
            ]
        )

    def jobs(self):
        return dict(jobs=self.journal.list())

    def submit(self, command, *, on_started=None, resume=False):
        if set(command) != {"request_id", "action", "start", "end"}:
            raise WebError("invalid_job_shape")
        identity = str(UUID(command["request_id"]))
        self.journal.directory.mkdir(parents=True, exist_ok=True)
        existing = self.journal.read(identity)
        if existing:
            if any(existing[k] != command[k] for k in command):
                raise WebError("identity_conflict", 409)
            if resume:
                existing.update(
                    state="running",
                    started_at=datetime.now(UTC).isoformat(),
                    finished_at=None,
                    classification=None,
                )
                self.journal.save(existing)
                if on_started:
                    on_started(existing)
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

    def resume(identity, *, on_started=None):
        job = host.journal.read(identity)
        job.update(
            state="pending", finished_at=None, updated_at=datetime.now(UTC).isoformat()
        )
        job["resume_count"] = job.get("resume_count", 0) + 1
        host.journal.save(job)
        if on_started:
            on_started(job)
        return job

    host.resume = resume
    app.include_router(
        financial_router(
            FinancialOptions(
                root,
                allow_read=True,
                allow_collect=True,
                allow_strategy=True,
                state_directory=root / "strategy",
                window_path=root / "absent-window.json",
            ),
            jobs=host,
        )
    )
else:
    host = Host(family)
    if family in {"cpi", "ppi", "pce", "employment", "consumption", "jolts"}:
        app.include_router(monthly_router(host))
    elif family == "m3":
        app.include_router(m3_router(host))
    elif family == "retail":
        app.include_router(retail_router(host, authorize=lambda _: True))
    else:
        app.include_router(period_router(host, prefix=host.prefix, family=family))

held = {}

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
        elif req.get("control") == "clone":
            journal = runtime.jobs if family in {"tdx", "roster"} else host.journal
            job = dict(journal.read(req["id"]))
            for field in ("request_id", "job_id", "id"):
                if field in job:
                    job[field] = req["target_id"]
            job["origin"] = req.get("origin")
            if "created_at" in req:
                job["created_at"] = req["created_at"]
            journal.save(job)
            out = dict(status=200, body=job)
        elif req.get("control") == "phase":
            identity, phase = req["id"], req["phase"]
            if identity in held:
                held.pop(identity).__exit__(None, None, None)
            journal = runtime.jobs if family in {"tdx", "roster"} else host.journal
            job = journal.read(identity)
            state = {
                "active": "running",
                "unknown": "unknown",
                "complete": "complete",
                "failed": "failed",
            }[phase]
            if family in {"tdx", "roster"}:
                state = {
                    "running": "RUNNING",
                    "unknown": "PARTIAL",
                    "complete": "COMPLETE",
                    "failed": "FAILED",
                }[state]
            if phase == "active":
                lock = (
                    runtime.jobs.lock(identity)
                    if family in {"tdx", "roster"}
                    else host.journal.execution()
                )
                lock.__enter__()
                held[identity] = lock
            clock = datetime.now(UTC).isoformat()
            job.update(
                state=state,
                updated_at=clock,
                finished_at=clock if phase in {"complete", "failed"} else None,
                classification="commit_outcome_unknown"
                if phase == "unknown" and family in {"tdx", "roster"}
                else None,
            )
            if family == "financial":
                job["steps"] = []
            if phase == "complete" and family == "gdp":
                start_year, start_q = job["start"].split("-Q")
                end_year, end_q = job["end"].split("-Q")
                expected = (
                    (int(end_year) - int(start_year)) * 4
                    + int(end_q)
                    - int(start_q)
                    + 1
                ) * len(GDP_IDS)
                job["result"] = dict(
                    coverage=dict(
                        expected=expected,
                        available=expected,
                        official_null=0,
                        missing=0,
                    ),
                    readback=dict(verified=True, as_of_basis="available"),
                )
            if phase == "complete" and family == "claims":
                day, end = (
                    date.fromisoformat(job["start"]),
                    date.fromisoformat(job["end"]),
                )
                weeks = []
                while day <= end:
                    if day.weekday() == 5:
                        weeks.append(day.isoformat())
                    day += timedelta(days=1)
                expected = len(weeks) * len(CLAIMS_IDS)
                job["result"] = dict(
                    coverage=dict(
                        expected=expected,
                        available=expected,
                        missing_rows=0,
                        provider_missing_values=0,
                    ),
                    readback=dict(verified=True, as_of_basis="available"),
                    series_coverage={
                        key: dict(
                            expected=len(weeks),
                            available=len(weeks),
                            latest_week=weeks[-1] if weeks else None,
                            missing_weeks=[],
                            missing_weeks_total=0,
                            missing_weeks_truncated=False,
                            trailing_missing_weeks=[],
                            trailing_missing_weeks_total=0,
                            trailing_missing_weeks_truncated=False,
                        )
                        for key in CLAIMS_IDS
                    },
                )
            journal.save(job)
            out = dict(status=200, body=job)
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
                headers={"Origin": CANDIDATE_ORIGIN, **req.get("headers", {})},
            )
            out = dict(status=response.status_code, body=response.json())
        print(json.dumps(out), flush=True)
