"""JSON-lines, in-process D-01 UI bridge using the actual product router.

stdin: {store_dir, method, path, body?, scenario?}; stdout: {status, body}.
store_dir must be a dedicated mdf-d01-* or d01-* directory under /tmp or this
worktree's artifacts. A missing directory or an existing empty test directory is
explicitly provisioned; existing configuration is reopened across processes.
Default evidence is synthetic: retained readable PPI, BLS configured, BEA missing.
The optional scenario selects only the named synthetic permission/history cases.
No network listeners, runtime configuration, sources, Jobs or Workers are used.
"""

import json
import socket
import sys
import tempfile
from pathlib import Path

WORKTREE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WORKTREE))
sys.path.insert(0, str(WORKTREE / "src"))

from fastapi.testclient import TestClient  # noqa: E402

from market_data_foundation.web.packages import create_package_app  # noqa: E402
from tests.d01_package_fixture import fixture_host  # noqa: E402


def forbidden(*args, **kwargs):
    raise AssertionError("network_forbidden")


for name in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, name, forbidden)
socket.create_connection = forbidden

SCENARIOS = {
    "default",
    "catalog_denied",
    "read_only",
    "write_unknown",
    "history_unknown",
    "metadata_denied",
    "metadata_unknown",
    "read_denied",
    "read_unknown",
    "unpublished",
}


def exchange(command):
    root = Path(command["store_dir"])
    resolved = root.resolve()
    if root.is_symlink() or not root.is_absolute() or root != resolved:
        raise ValueError("temporary_test_directory_required")
    bases = (Path(tempfile.gettempdir()).resolve(), WORKTREE / "artifacts")
    if not any(root.is_relative_to(base) and root != base for base in bases):
        raise ValueError("temporary_test_directory_required")
    if not root.name.startswith(("mdf-d01-", "d01-")):
        raise ValueError("temporary_test_directory_required")
    scenario = command.get("scenario", "default")
    if scenario not in SCENARIOS:
        raise ValueError("unknown_synthetic_scenario")
    # Only dedicated empty test roots can be initialized. No repair/fallback on
    # missing/corrupt storage in an already provisioned directory.
    if not root.exists():
        root.mkdir(mode=0o700)
    configuration = root / "configuration"
    initialize = not any(root.iterdir())
    host = fixture_host(configuration, initialize=initialize, scenario=scenario)
    with TestClient(create_package_app(host)) as client:
        response = client.request(
            command["method"], command["path"], json=command.get("body")
        )
        return {"status": response.status_code, "body": response.json()}


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
                    "effect": "NONE",
                },
            }
        print(json.dumps(result, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
