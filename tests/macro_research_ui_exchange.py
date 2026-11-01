"""Real Python router/serializer HTTP for UI research-field contract tests.

Synthetic sources only; this is not a provider/runtime listener. Standard
TestClient is intentionally used without sandbox socket/event-loop workarounds.
"""

import json
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from fastapi import FastAPI

from market_data_foundation.web.cpi_timeseries_api import timeseries_router
from market_data_foundation.web.retail_timeseries import (
    RetailTimeSeriesHost,
)
from market_data_foundation.web.retail_timeseries import (
    timeseries_router as retail_router,
)
from tests.test_macro_research_fields import PROFILES, make_host
from tests.test_ppi_timeseries_web import client
from tests.test_retail_timeseries_backend import batch, service


def forbidden(*args, **kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with TemporaryDirectory(prefix="mdf-research-wire-") as directory:
    app = FastAPI()
    sources = []
    for profile in PROFILES:
        host, source, _ = make_host(profile, Path(directory) / profile.key)
        host.submit(
            {
                "request_id": "12345678-1234-4234-8234-123456789abc",
                "action": "update",
                "start": "2026-09",
                "end": "2026-09",
            }
        )
        sources.append(source)
        app.include_router(timeseries_router(host))
    retail = service()
    retail.store.save_batch("synthetic", batch())
    app.include_router(
        retail_router(
            RetailTimeSeriesHost(retail, query_allowed=True, allow_source=False),
            authorize=lambda _: True,
        )
    )
    with client(app) as http:
        for line in sys.stdin:
            message = json.loads(line)
            response = http.request(message.get("method", "GET"), message["path"])
            print(
                json.dumps(
                    {
                        "status": response.status_code,
                        "body": response.json(),
                        "text": response.text,
                        "effects": {
                            "source": sum(len(s.calls) for s in sources),
                            "raw": 0,
                            "jobs": 4,
                        },
                    }
                ),
                flush=True,
            )
