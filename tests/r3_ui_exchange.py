"""A1 Vitest JSON-lines bridge. Actual ASGI app, no listener or external I/O."""

import json
import socket
import sys
from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient

from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.management import InstanceRecord, ManagementHost
from market_data_foundation.web.source_catalog import DirectoryQuery, directory


def forbidden(*_args, **_kwargs):
    raise AssertionError("network_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
scenario = sys.argv[1]
host = None
if scenario != "catalog":
    now = datetime(2026, 10, 24, tzinfo=UTC)
    observed = now - timedelta(seconds=61 if scenario == "expired" else 10)
    record = InstanceRecord(
        identity="fixture.r3.owner.instance",
        catalog_identity=directory(DirectoryQuery()).entries[0].identity,
        ownership="OWNER_MANAGED",
        endpoint_ref="fixture.public.endpoint",
        observed_at=observed.isoformat(),
        expires_at=(observed + timedelta(seconds=60)).isoformat(),
        state="AVAILABLE",
    )
    host = ManagementHost(lambda: (record,), "OFFLINE_FIXTURE", lambda: now)

with TestClient(create_app(management_host=host)) as client:
    for line in sys.stdin:
        response = client.get(json.loads(line)["path"])
        print(
            json.dumps({"status": response.status_code, "body": response.json()}),
            flush=True,
        )
