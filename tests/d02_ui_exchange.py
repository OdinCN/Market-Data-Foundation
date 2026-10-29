"""JSON-lines in-process D02 bridge; synthetic data and no network/listeners.

stdin: {scenario?, method?, path, body?}; stdout: {status, body, effects}.
Each line uses a fresh temporary synthetic store. No real configuration is read.
"""

import json
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

DEFAULT_SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "local_hit"
WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE))
sys.path.insert(0, str(WORKTREE / "tests"))
sys.path.insert(0, str(WORKTREE / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from tests.d02_query_fixture import effects, fixture  # noqa: E402


def forbidden(*_args, **_kwargs):
    raise AssertionError("network_forbidden")


for name in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, name, forbidden)
socket.create_connection = forbidden


def exchange(command):
    with TemporaryDirectory(prefix="mdf-d02-") as root:
        app, state = fixture(Path(root), command.get("scenario", DEFAULT_SCENARIO))
        with TestClient(app) as client:
            response = client.request(
                command.get("method", "GET"), command["path"], json=command.get("body")
            )
        return {
            "status": response.status_code,
            "body": response.json(),
            "effects": effects(state),
        }


def main():
    for line in sys.stdin:
        try:
            result = exchange(json.loads(line))
        except Exception:
            result = {
                "status": 422,
                "body": {
                    "status": "REJECTED",
                    "classification": "invalid_test_bridge_request",
                },
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
