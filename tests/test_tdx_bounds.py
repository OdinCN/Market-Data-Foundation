"""Counterexamples for the OS guard; intercepted targets send no network traffic."""

import importlib.util
import socket
import struct
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "tdx_bounds", ROOT / "provider_lab/tdx_bounds.py"
)
BOUNDS = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BOUNDS)


def test_address_decode_and_private_network_boundary():
    address = (
        struct.pack("H", socket.AF_INET)
        + struct.pack("!H", 7709)
        + socket.inet_aton("192.168.1.1")
        + bytes(8)
    )
    assert BOUNDS.target(address) == ("192.168.1.1", 7709)
    assert not BOUNDS.allowed(BOUNDS.target(address), set())
    assert not BOUNDS.allowed(("8.8.8.8", 53), set())


@pytest.mark.parametrize("kind", ["boundary", "attempt", "wall"])
def test_child_cannot_execute_denied_connection_or_overrun(tmp_path, kind):
    if sys.platform != "linux" or BOUNDS.os.uname().machine != "x86_64":
        pytest.skip("Linux x86_64 process supervision")
    code = "import socket; socket.socket().connect(('127.0.0.1',7709))"
    hosts, attempts, wall = set(), 1, 5
    if kind == "attempt":
        hosts, attempts = {"127.0.0.1"}, 0
    elif kind == "wall":
        code, wall = "import time; time.sleep(10)", 0.1
    result = BOUNDS.run_guarded(
        [sys.executable, "-B", "-c", code],
        tmp_path,
        hosts=hosts,
        max_attempts=attempts,
        wall_seconds=wall,
        phase_seconds=5,
    )
    assert (
        result["stop"]
        == {
            "boundary": "endpoint_boundary",
            "attempt": "attempt_limit",
            "wall": "wall_limit",
        }[kind]
    )
    assert result["process_exit"] == -9
    assert result["connect_attempts_sent"] == 0


def test_trace_follows_threads_without_provider_access(tmp_path):
    if sys.platform != "linux" or BOUNDS.os.uname().machine != "x86_64":
        pytest.skip("Linux x86_64 process supervision")
    code = (
        "import threading; t=threading.Thread(target=lambda:None); t.start(); t.join()"
    )
    result = BOUNDS.run_guarded(
        [sys.executable, "-B", "-c", code],
        tmp_path,
        hosts=set(),
        wall_seconds=5,
        phase_seconds=5,
    )
    assert result["process_exit"] == 0 and result["stop"] is None
    assert result["connect_attempts_sent"] == 0
