"""Offline R8 interaction bridge; real native API, fake PG and synthetic source."""

import json
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from test_cpi_failed_init_backfill_r8 import client, setup_backfill

from market_data_foundation.historical.cpi_pg_binding import BACKFILL_JOB
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, ORIGIN


def forbidden(*args, **kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with TemporaryDirectory(prefix="mdf-r8-ui-") as directory:
    scenario = sys.argv[1]
    origin = CANDIDATE_ORIGIN if scenario == "readonly" else ORIGIN
    server, rig, worker, app = setup_backfill(
        Path(directory), migrated=scenario != "migration", origin=origin
    )
    with client(app, origin) as c:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("control") == "run":
                result = worker.process(BACKFILL_JOB, enabled=True)
                response = dict(status=200, body={"state": result["state"]})
            else:
                r = c.request(
                    message.get("method", "GET"),
                    message["path"],
                    headers={"Origin": origin},
                    **({"json": message["body"]} if "body" in message else {}),
                )
                response = dict(status=r.status_code, body=r.json())
            response["effects"] = dict(
                source=rig.transport.calls,
                raw=rig.store.create_attempts,
                jobs=len(server.data["jobs"]),
            )
            print(json.dumps(response), flush=True)
