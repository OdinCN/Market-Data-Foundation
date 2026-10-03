"""Linux x86_64 lab process guard, including Go-native connect syscalls.

No host firewall changes. Kill before an out-of-scope or over-budget syscall.
The trace counts connect entries once, not Python/async wrapper events twice.
"""

import ctypes
import hashlib
import ipaddress
import json
import os
import signal
import socket
import struct
import subprocess
import time
from pathlib import Path

LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ptrace.argtypes = [
    ctypes.c_ulong,
    ctypes.c_ulong,
    ctypes.c_void_p,
    ctypes.c_void_p,
]
LIBC.ptrace.restype = ctypes.c_long
OPTIONS = 1 | 2 | 4 | 8 | 16 | 0x100000  # syscall, children, exec, exit-kill


def ptrace(request, pid, address=0, data=0):
    ctypes.set_errno(0)
    result = LIBC.ptrace(request, pid, address, data)
    if result == -1 and ctypes.get_errno():
        raise OSError(ctypes.get_errno(), "qualification ptrace failed")
    return result


def memory(pid, address, length):
    words = []
    for offset in range(0, length, 8):
        value = ptrace(2, pid, address + offset)
        words.append((value & ((1 << 64) - 1)).to_bytes(8, "little"))
    return b"".join(words)[:length]


def target(address):
    family = int.from_bytes(address[:2], "little")
    if family == socket.AF_INET:
        return socket.inet_ntop(family, address[4:8]), int.from_bytes(
            address[2:4], "big"
        )
    if family == socket.AF_INET6:
        return socket.inet_ntop(family, address[8:24]), int.from_bytes(
            address[2:4], "big"
        )
    if family == socket.AF_UNIX:
        return None
    raise ValueError("unrecognized network address family")


def allowed(endpoint, hosts):
    if endpoint is None:
        return True
    host, port = endpoint
    return port in (7709, 7727) and (
        host in hosts if hosts else ipaddress.ip_address(host).is_global
    )


def peer_snapshot(pid):
    """Only sockets owned by this traced process; established state is witnessed."""
    try:
        inodes = {
            link[8:-1]
            for fd in Path(f"/proc/{pid}/fd").iterdir()
            if (link := os.readlink(fd)).startswith("socket:[")
        }
        peers = []
        for line in Path(f"/proc/{pid}/net/tcp").read_text().splitlines()[1:]:
            fields = line.split()
            if fields[3] == "01" and fields[9] in inodes:
                host, port = fields[2].split(":")
                peers.append(
                    [socket.inet_ntoa(bytes.fromhex(host)[::-1]), int(port, 16)]
                )
        return peers
    except OSError, IndexError:
        return []


def run_guarded(
    command,
    output,
    *,
    hosts,
    max_attempts=20,
    phase_attempts=4,
    wall_seconds=25,
    phase_seconds=8,
):
    if os.uname().machine != "x86_64":
        raise RuntimeError("qualification syscall guard requires Linux x86_64")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    source_root = Path(__file__).resolve().parents[1]
    source_paths = [
        source_root / "provider_lab" / p
        for p in ("tdx_bounds.py", "tdx_r1.py", "tdx_r1_worker.py")
    ]
    source_paths += list(
        (source_root / "src/market_data_foundation/qualification").glob("*.py")
    )
    sources = {
        str(p.relative_to(source_root)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in source_paths
    }
    snapshots = output / "execution-source"
    for p in source_paths:
        dest = snapshots / p.relative_to(source_root)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(p.read_bytes())
    (output / "revision.json").write_text(
        json.dumps(
            {
                "source_sha256": sources,
                "capture": "before child spawn",
                "baseline": "5a52dc44b597553dbb551aa424c1450dfcb9fcac",
            },
            indent=2,
        )
        + "\n"
    )
    result = {
        "command": command,
        "hosts": sorted(hosts),
        "limits": {
            "connect_attempts": max_attempts,
            "connect_attempts_per_phase": phase_attempts,
            "wall_seconds": wall_seconds,
            "phase_seconds": phase_seconds,
        },
        "events": [],
        "established_peers": [],
        "stop": None,
    }
    counts = {}
    started = time.monotonic()
    active = set()
    pending = {}
    next_peer_poll = 0.0
    child = None

    def phase():
        path = output / "phase.json"
        try:
            value = json.loads(path.read_text())
            return value["label"], value["started"]
        except OSError, ValueError:
            return "import", started

    def trace_me():
        ptrace(0, 0)

    try:
        with (output / "process.log").open("w") as log:
            child = subprocess.Popen(
                command,
                stdout=log,
                stderr=log,
                start_new_session=True,
                preexec_fn=trace_me,
            )
            active.add(child.pid)
            pid, status = os.waitpid(child.pid, 0)
            if not os.WIFSTOPPED(status):
                raise RuntimeError("child did not enter syscall supervision")
            ptrace(0x4200, pid, 0, OPTIONS)
            ptrace(24, pid)
            while active:
                label, phase_started = phase()
                if time.monotonic() - started > wall_seconds:
                    result["stop"] = "wall_limit"
                    break
                if time.monotonic() - phase_started > phase_seconds:
                    result["stop"] = "phase_wall_limit"
                    break
                try:
                    pid, status = os.waitpid(-1, os.WNOHANG | 0x40000000)
                except ChildProcessError:
                    break
                if pid == 0:
                    time.sleep(0.00005)
                    continue
                active.add(pid)
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    active.discard(pid)
                    if pid == child.pid:
                        result["process_exit"] = os.waitstatus_to_exitcode(status)
                        child.returncode = result["process_exit"]
                    continue
                sig = os.WSTOPSIG(status)
                event = status >> 16
                if event in (1, 2, 3):
                    new_pid = ctypes.c_ulong()
                    ptrace(0x4201, pid, 0, ctypes.addressof(new_pid))
                    active.add(new_pid.value)
                if sig == (signal.SIGTRAP | 0x80):
                    buf = ctypes.create_string_buffer(128)
                    ptrace(0x420E, pid, 128, ctypes.addressof(buf))
                    op = buf.raw[0]
                    if op == 1:
                        number, *args = struct.unpack_from("7Q", buf.raw, 24)
                        endpoint = None
                        network = number == 42 or (number == 44 and args[4])
                        if network:
                            pointer, length = (
                                (args[1], args[2])
                                if number == 42
                                else (args[4], args[5])
                            )
                            endpoint = target(memory(pid, pointer, min(length, 28)))
                            if endpoint is not None:
                                entry = {
                                    "phase": label,
                                    "tid": pid,
                                    "syscall": "connect" if number == 42 else "sendto",
                                    "target": list(endpoint),
                                    "elapsed": time.monotonic() - started,
                                }
                                result["events"].append(entry)
                                if not allowed(endpoint, hosts):
                                    entry["sent"] = False
                                    result["stop"] = "endpoint_boundary"
                                    break
                                if number == 42:
                                    counts[label] = counts.get(label, 0) + 1
                                    if (
                                        sum(counts.values()) > max_attempts
                                        or counts[label] > phase_attempts
                                    ):
                                        entry["sent"] = False
                                        result["stop"] = "attempt_limit"
                                        break
                                entry["sent"] = True
                                pending[pid] = entry
                    elif op == 2 and pid in pending:
                        pending.pop(pid)["syscall_return"] = struct.unpack_from(
                            "q", buf.raw, 24
                        )[0]
                    if time.monotonic() >= next_peer_poll:
                        next_peer_poll = time.monotonic() + 0.05
                        for peer in peer_snapshot(child.pid):
                            item = {"phase": label, "peer": peer}
                            if item not in result["established_peers"]:
                                result["established_peers"].append(item)
                forward = (
                    0
                    if sig in (signal.SIGSTOP, signal.SIGTRAP, signal.SIGTRAP | 0x80)
                    else sig
                )
                try:
                    ptrace(24, pid, 0, forward)
                except ProcessLookupError:
                    active.discard(pid)
    finally:
        if child is not None and active:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            for pid in list(active):
                try:
                    ptrace(7, pid, 0, signal.SIGKILL)
                except OSError:
                    pass
            while active:
                try:
                    pid, status = os.waitpid(-1, 0x40000000)
                except ChildProcessError:
                    break
                if os.WIFEXITED(status) or os.WIFSIGNALED(status):
                    active.discard(pid)
                    if pid == child.pid:
                        result["process_exit"] = os.waitstatus_to_exitcode(status)
                        child.returncode = result["process_exit"]
                else:
                    try:
                        ptrace(7, pid, 0, signal.SIGKILL)
                    except OSError:
                        pass
        result["elapsed_seconds"] = time.monotonic() - started
        result["connect_attempts_sent"] = sum(
            e.get("sent", False) and e["syscall"] == "connect" for e in result["events"]
        )
        result["phase_counts"] = dict(collections_count(result["events"]))
        (output / "boundary.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def collections_count(events):
    counts = {}
    for e in events:
        if e.get("sent") and e["syscall"] == "connect":
            counts[e["phase"]] = counts.get(e["phase"], 0) + 1
    return counts.items()
