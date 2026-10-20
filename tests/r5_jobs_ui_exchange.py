"""Host-only JSON lines bridge. No listener; Worker actions are test controls."""

import json
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from market_data_foundation.web.cpi_jobs import Operator  # noqa: E402
from tests.r5_jobs_fixture import Rig, jobs_app, worker  # noqa: E402


def forbidden(*_args, **_kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with TemporaryDirectory(prefix="mdf-r5-i4-synthetic-") as directory:
    rig = Rig(Path(directory))
    scenario = sys.argv[1]
    if scenario == "unbound":
        app = jobs_app(rig, operator=None)
    elif scenario == "readonly":
        app = jobs_app(rig, operator=Operator("readonly-operator", False))
    else:
        app = jobs_app(rig)
    with TestClient(app) as client:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("control") == "run":
                # This operation is ONLY the offline bridge protocol; no API/CLI.
                def preflight(_spec):
                    if message.get("failure"):
                        raise OSError("private-worker-detail")

                worker(rig, preflight=preflight).process(message["job"], enabled=True)
                result = {"status": 200, "body": {"status": "OK"}}
            else:
                response = client.request(
                    message.get("method", "GET"),
                    message["path"],
                    **({"json": message["body"]} if "body" in message else {}),
                )
                result = {"status": response.status_code, "body": response.json()}
            result["effects"] = {
                "source": rig.transport.calls,
                "raw": rig.store.create_attempts,
                "jobs": len(rig.journal.list_jobs()),
            }
            print(json.dumps(result), flush=True)
