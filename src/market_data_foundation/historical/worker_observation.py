"""Local process evidence; a PID alone never proves executor identity."""

import json
import os
from pathlib import Path


def process_identity(pid):
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    if stat[0] == "Z":
        raise ProcessLookupError
    return {
        "pid": pid,
        "start_ticks": stat[19],
        "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
    }


def observe_worker(worker, strategy_path, asset):
    worker = dict(worker)
    process = worker.get("process")
    legacy = strategy_path.with_name(asset + "-worker.json")
    if legacy.exists() and process is None:
        try:
            process = json.loads(legacy.read_text())
        except OSError, ValueError:
            worker["state"] = "unknown"
            return worker
    if process is None:
        return worker
    if not isinstance(process, dict):
        return {
            **worker,
            "state": "unknown",
            "process": {"state": "unknown", "pid": None},
        }
    process = dict(process)
    try:
        pid = process.get("pid")
        if process.get("state") == "stopped":
            state = "stopped"
        elif type(pid) is not int or pid <= 0:
            state = "unknown"
        else:
            os.kill(pid, 0)
            identity = process_identity(pid)
            state = (
                "running"
                if all(process.get(k) == v for k, v in identity.items())
                else "unknown"
            )
    except ProcessLookupError, FileNotFoundError:
        state = "stopped"
    except OSError, ValueError, KeyError, IndexError:
        state = "unknown"
    process["state"] = state
    if state != "running":
        process["pid"] = None
    if state in {"stopped", "unknown"}:
        worker["state"] = state
    elif worker["state"] in {"stale", "not_started"}:
        process["state"] = worker["state"]
    worker["process"] = process
    return worker
