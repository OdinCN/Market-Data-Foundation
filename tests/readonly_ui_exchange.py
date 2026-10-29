"""JSON-lines UI bridge to the real readonly Host and synthetic PG wire rows."""

import json
import socket
import sys
import tempfile
from pathlib import Path

import psycopg
from fastapi.testclient import TestClient
from pytest import MonkeyPatch

from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_readonly_host import setup


def forbidden(*args, **kwargs):
    raise AssertionError("external_io_forbidden")


for name in ["connect", "connect_ex", "bind", "listen"]:
    setattr(socket.socket, name, forbidden)
socket.create_connection = forbidden
psycopg.connect = forbidden
scenario = sys.argv[1] if len(sys.argv) > 1 else "default"
if scenario not in {"default", "storage_failure", "partial", "config_save"}:
    raise ValueError("unknown_test_scenario")
with (
    tempfile.TemporaryDirectory(prefix="mdf-readonly-ui-") as directory,
    MonkeyPatch.context() as patch,
):
    app, runtime, connections, ledgers, _, _ = setup(
        Path(directory),
        patch,
        scenario="execute" if scenario == "storage_failure" else scenario,
        enabled_caps=("cpi",) if scenario == "storage_failure" else (),
        package_config_save=scenario == "config_save",
    )
    with TestClient(
        app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234)
    ) as client:
        for line in sys.stdin:
            try:
                message = json.loads(line)
                response = client.request(
                    message["method"],
                    message["path"],
                    json=message.get("body"),
                    headers={"Origin": CANDIDATE_ORIGIN}
                    if message["method"] == "PUT"
                    else {},
                )
                assert (
                    ledgers[0].snapshot() == ledgers[1]
                    and ledgers[2].snapshot() == ledgers[3]
                )
                result = {
                    "status": response.status_code,
                    "body": response.json(),
                    "effects": {
                        "source": 0,
                        "jobs": 0,
                        "versions": 0,
                        "publications": 0,
                    },
                }
            except Exception:
                result = {
                    "status": 503,
                    "body": {
                        "status": "UNKNOWN",
                        "classification": "test_bridge_failed",
                    },
                }
            print(json.dumps(result, ensure_ascii=False), flush=True)
