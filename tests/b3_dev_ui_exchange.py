"""Offline UI → real ASGI Host → PG protocol double, never a TCP/PG proof."""

import json
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from fastapi.testclient import TestClient

from market_data_foundation.web.cpi_dev_host import ORIGIN, candidate_spec, job_identity
from market_data_foundation.web.cpi_jobs import CpiManualWorker
from tests.b3_dev_fixture import fixture


def forbidden(*args, **kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with TemporaryDirectory(prefix="mdf-b3-dev-offline-") as directory:
    server, rig, app = fixture(Path(directory))
    with TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234)) as client:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("control") == "run":
                if message.get("unknown"):

                    def fail_commit():
                        raise OSError("private offline commit")

                    rig.engine.commit = fail_commit
                result = CpiManualWorker(
                    "offline-worker",
                    rig.engine,
                    candidate_spec(),
                    lambda _: None,
                    rig.acquirer,
                ).process(job_identity(), enabled=True)
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
                source=rig.transport.calls,
                raw=rig.store.create_attempts,
                jobs=len(server.data["jobs"]),
            )
            print(json.dumps(response), flush=True)
