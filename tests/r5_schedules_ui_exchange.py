"""Host-only offline UI→ASGI→CPI worker bridge; no product execution endpoint."""

import json
import socket
import sys
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from dataclasses import replace  # noqa: E402
from datetime import timedelta  # noqa: E402

import psycopg  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from market_data_foundation.web.cpi_api import create_app  # noqa: E402
from market_data_foundation.web.cpi_schedules import CpiSchedulesHost  # noqa: E402
from tests.r5_fixture import ReadOnlySnapshot  # noqa: E402
from tests.r5_jobs_fixture import jobs_host  # noqa: E402
from tests.test_cpi_scheduling import setup  # noqa: E402


def forbidden(*_args, **_kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with TemporaryDirectory(prefix="mdf-r5-i5-synthetic-") as directory:
    rig, store, worker, transport, clock = setup(Path(directory))
    trusted = replace(jobs_host(rig), clock=lambda: clock[0])

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(rig.ledger)

    app = create_app(
        scope,
        evidence_mode="OFFLINE_FIXTURE",
        jobs_host=trusted,
        schedules_host=CpiSchedulesHost(
            trusted, store, rig.acquirer.history_qualification
        ),
    )
    with TestClient(app) as client:
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("control") == "tick":
                clock[0] += timedelta(seconds=2)
                transport.failure = message.get("failure", False)
                if message.get("crash_after_child"):
                    run = rig.engine.run

                    def crash_after_success(*args, **kwargs):
                        clock[0] += timedelta(seconds=5)
                        child = run(*args, **kwargs)
                        assert child["state"] == "succeeded"
                        raise KeyboardInterrupt()

                    with patch.object(rig.engine, "run", crash_after_success):
                        try:
                            worker.tick(message["plan"], enabled=True)
                        except KeyboardInterrupt:
                            pass
                        else:
                            raise AssertionError("child_success_crash_not_reached")
                else:
                    worker.tick(message["plan"], enabled=True)
                result = {"status": 200, "body": {"status": "OK"}}
            else:
                response = client.request(
                    message.get("method", "GET"),
                    message["path"],
                    **({"json": message["body"]} if "body" in message else {}),
                )
                result = {"status": response.status_code, "body": response.json()}
            result["effects"] = {
                "source": len(transport.calls),
                "raw": rig.store.create_attempts,
            }
            print(json.dumps(result), flush=True)
