"""JSON-lines ASGI test bridge for real UI→API→domain tests, without listeners."""

import json
import socket
import sys
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fastapi.testclient import TestClient  # noqa: E402

from market_data_foundation.web.cpi_api import create_app  # noqa: E402
from tests.r5_fixture import (  # noqa: E402
    ReadOnlySnapshot,
    fixture_app,
    fixture_ledger,
)
from tests.r5_observability_fixture import fixture_host  # noqa: E402


def forbidden(*_args, **_kwargs):
    raise AssertionError("network_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
scenario = sys.argv[1]
if scenario.startswith("observability"):
    ledger, _, _ = fixture_ledger()

    @contextmanager
    def scope():
        yield ReadOnlySnapshot(ledger)

    host = (
        None
        if scenario == "observability_unconfigured"
        else fixture_host(
            datetime.now(UTC) - timedelta(seconds=61)
            if scenario == "observability_stale"
            else None
        )
    )
    app = create_app(scope, evidence_mode="OFFLINE_FIXTURE", observability_host=host)
elif scenario in {"content_storage_fault", "scope_exit_fault"}:
    ledger, _, _ = fixture_ledger()

    class FaultingContentSnapshot(ReadOnlySnapshot):
        def get_content(self, key):
            raise OSError("private-sensitive-dsn")

    @contextmanager
    def scope():
        try:
            yield (
                FaultingContentSnapshot(ledger)
                if scenario == "content_storage_fault"
                else ReadOnlySnapshot(ledger)
            )
        finally:
            if scenario == "scope_exit_fault":
                raise OSError("private-sensitive-dsn")

    app = create_app(scope, evidence_mode="OFFLINE_FIXTURE")
else:
    app = fixture_app(scenario)

with TestClient(app) as client:
    for line in sys.stdin:
        request = json.loads(line)
        response = client.get(request["path"])
        print(
            json.dumps({"status": response.status_code, "body": response.json()}),
            flush=True,
        )
