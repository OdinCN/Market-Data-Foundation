"""Offline UI→PCE API→journal/worker/Reader contract; no network or live store."""

import json
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from fastapi.testclient import TestClient
from pytest import MonkeyPatch
from test_b3_pce_dev import fail_initial, setup
from test_bea_pce_vertical import candidate, publish

from market_data_foundation.historical.pce_pg_binding import (
    backfill_identity,
    job_identity,
)
from market_data_foundation.web.pce_dev_host import ORIGIN


def forbidden(*_args, **_kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with (
    TemporaryDirectory(prefix="mdf-pce-native-ui-") as directory,
    MonkeyPatch.context() as patch,
):
    server, factory, engine, store, app = setup.__wrapped__(Path(directory), patch)
    backfill = len(sys.argv) > 1 and sys.argv[1].startswith("backfill")
    execution_id = backfill_identity() if backfill else job_identity()
    if backfill:
        fail_initial(
            factory,
            "outcome_unknown" if sys.argv[1] == "backfill-unknown" else "failed",
        )
        # Hypothetical storage for interaction tests only. The actual inherited
        # UNIQUE(scope) blocker is tested separately, never presented as real PG.
        server.successor_supported = sys.argv[1] != "backfill-blocked"
    # An existing publication is independent of the new, unsubmitted Job.
    publish(
        ledger=engine.kernel.ledger, retained=candidate("older-pce"), clock=engine.clock
    )
    source_calls = 0

    def acquire():
        global source_calls
        source_calls += 1
        return candidate(execution_id)

    with TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234)) as client:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("control") == "run":
                result = engine.run(
                    execution_id,
                    enabled=True,
                    preflight=lambda _: None,
                    acquire=acquire,
                )
                response = dict(status=200, body={"state": result["state"]})
            else:
                r = client.request(
                    message.get("method", "GET"),
                    message["path"],
                    headers={"Origin": ORIGIN},
                    **({"json": message["body"]} if "body" in message else {}),
                )
                response = dict(status=r.status_code, body=r.json())
            response["effects"] = dict(
                source=source_calls,
                raw=len(store._objects),
                jobs=len(server.data["jobs"]),
            )
            print(json.dumps(response), flush=True)
