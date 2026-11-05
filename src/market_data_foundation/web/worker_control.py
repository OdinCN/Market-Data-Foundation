"""Single-project DEV worker control. Client input never selects a unit or command."""

import json
import re
import subprocess
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi import Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from market_data_foundation.web.cpi_timeseries_api import admitted_job, failure
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN, admitted

ASSETS = (
    "cpi",
    "ppi",
    "pce",
    "employment",
    "retail",
    "consumption",
    "gdp",
    "claims",
    "jolts",
    "m3",
)


def admitted_worker(request):
    path = request.url.path
    return any(
        path == f"/api/r5/{asset}/timeseries/worker"
        and admitted_job(
            Request(dict(request.scope, path=f"/api/r5/{asset}/timeseries/jobs")),
            prefix=f"/api/r5/{asset}/timeseries",
        )
        for asset in ASSETS
    )


def read_window(path, *, now=None):
    try:
        value = json.loads(Path(path).read_text())
        start, end = (
            datetime.fromisoformat(value[k]) for k in ("started_at", "expires_at")
        )
        if (
            start.utcoffset() is None
            or end.utcoffset() is None
            or end - start != timedelta(days=45)
        ):
            raise ValueError
        if not start <= (now or datetime.now(UTC)) < end:
            raise WebError("dev_window_expired", 409)
        return value
    except WebError:
        raise
    except OSError, ValueError, KeyError, TypeError:
        raise WebError("dev_window_unavailable", 503) from None


def running_work(value):
    if isinstance(value, dict):
        return value.get("state") == "running" or any(
            running_work(v) for k, v in value.items() if k != "worker"
        )
    if isinstance(value, list):
        return any(running_work(v) for v in value)
    return False


class DevWorkerControl:
    def __init__(
        self, window_path, *, run=subprocess.run, clock=lambda: datetime.now(UTC)
    ):
        self.window_path = Path(window_path)
        self.run = run
        self.clock = clock
        self.locks = {asset: threading.Lock() for asset in ASSETS}

    def command(self, *args):
        try:
            result = self.run(
                ["/usr/bin/systemctl", *args],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except OSError, subprocess.TimeoutExpired:
            raise WebError("worker_service_control_unavailable", 503) from None
        if result.returncode:
            raise WebError("worker_service_action_failed", 503)
        return result.stdout

    def inspect(self, asset, host):
        if asset not in ASSETS or host is None:
            raise WebError("worker_binding_unavailable", 503)
        window = read_window(self.window_path, now=self.clock())
        unit = f"mdf-macro-dev-{asset}.service"
        output = self.command(
            "show",
            unit,
            "--property=LoadState,ActiveState,SubState,MainPID,WorkingDirectory,ActiveEnterTimestamp",
            "--no-pager",
        )
        service = dict(
            line.split("=", 1) for line in output.splitlines() if "=" in line
        )
        versions = window.get("worker_versions", {})
        if not isinstance(versions, dict):
            raise WebError("worker_version_binding_invalid", 503)
        version = versions.get(asset, {})
        if not isinstance(version, dict):
            raise WebError("worker_version_binding_invalid", 503)
        if version:
            root = Path(version.get("snapshot", ""))
            if (
                not isinstance(version.get("commit"), str)
                or not re.fullmatch(r"[0-9a-f]{40}", version["commit"])
                or not root.is_absolute()
                or ".." in root.parts
                or root.parent != Path(window["snapshot"]).parent
                or root.name != version["commit"]
            ):
                raise WebError("worker_version_binding_invalid", 503)
        expected_snapshot = version.get("snapshot", window["snapshot"])
        expected_commit = version.get(
            "commit", window.get("worker_code_commit", window.get("code_commit"))
        )
        bound = (
            service.get("LoadState") == "loaded"
            and service.get("WorkingDirectory") == expected_snapshot
        )
        worker = host.auto_strategy.get()["worker"]
        pid = int(service.get("MainPID") or 0)
        heartbeat = worker.get("last_heartbeat_at")
        fresh = False
        try:
            age = (self.clock() - datetime.fromisoformat(heartbeat)).total_seconds()
            fresh = 0 <= age <= 120
        except ValueError, TypeError:
            pass
        matches = (worker.get("process") or {}).get("pid") == pid and pid > 0
        online = (
            bound
            and service.get("ActiveState") == "active"
            and matches
            and fresh
            and worker.get("state") in {"idle", "running", "paused"}
        )
        reason = (
            "worker_online"
            if online
            else "worker_unit_not_registered"
            if not bound
            else "worker_service_not_started"
            if service.get("ActiveState") != "active"
            and not service.get("ActiveEnterTimestamp")
            else "worker_service_stopped"
            if service.get("ActiveState") != "active"
            else "worker_heartbeat_stale"
            if not fresh
            else "worker_process_not_confirmed"
            if not matches
            else "worker_loop_error"
        )
        return {
            "asset": asset,
            "registered": bound,
            "online": online,
            "reason": reason,
            "service_state": service.get("ActiveState", "unknown"),
            "process_id": pid or None,
            "worker": worker,
            "expires_at": window["expires_at"],
            "code_commit": expected_commit,
            "code_snapshot": expected_snapshot,
        }

    def act(self, asset, host, action):
        if (
            type(action) is not str
            or action not in {"start", "recover"}
            or asset not in ASSETS
        ):
            raise WebError("invalid_worker_action")
        if not self.locks[asset].acquire(blocking=False):
            raise WebError("worker_action_in_progress", 409)
        try:
            before = self.inspect(asset, host)
            if not before["registered"]:
                raise WebError("worker_unit_not_registered", 503)
            access = host.capabilities(host.source())
            if not access.get("collect"):
                raise WebError(
                    access.get("reason") or "worker_collection_not_authorized", 403
                )
            if before["online"]:
                return {**before, "operation": "already_online"}
            if any(record.get("state") == "running" for record in host.journal.list()):
                raise WebError("worker_active_collection_in_progress", 409)
            scheduler = getattr(host, "scheduler", None)
            if (
                before["service_state"] == "active"
                and scheduler is not None
                and running_work(scheduler.status())
            ):
                raise WebError("worker_active_collection_in_progress", 409)
            process = before["worker"].get("process") or {}
            if process.get("pid") and process["pid"] != before["process_id"]:
                raise WebError("worker_unmanaged_process_present", 409)
            self.command(
                "restart" if action == "recover" else "start",
                f"mdf-macro-dev-{asset}.service",
            )
            result = self.inspect(asset, host)
            for _ in range(20):
                if result["online"]:
                    break
                time.sleep(0.2)
                result = self.inspect(asset, host)
            return {
                **result,
                "operation": "confirmed_online"
                if result["online"]
                else "awaiting_heartbeat",
            }
        finally:
            self.locks[asset].release()


def install_worker_control(app, runtime, control):
    hosts = {
        asset: getattr(
            runtime,
            "timeseries"
            if asset == "cpi"
            else "m3_query_host"
            if asset == "m3"
            else asset + "_timeseries",
        )
        for asset in ASSETS
    }

    @app.get("/api/r5/{asset}/timeseries/worker")
    def status(asset: str, request: Request):
        try:
            if not admitted(request, origin=CANDIDATE_ORIGIN) or request.query_params:
                raise WebError("operator_not_authorized", 403)
            if control is None:
                raise WebError("worker_control_not_configured", 503)
            return control.inspect(asset, hosts.get(asset))
        except Exception as error:
            return failure(error)

    @app.post(
        "/api/r5/{asset}/timeseries/worker",
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "action": {
                                    "type": "string",
                                    "enum": ["start", "recover"],
                                }
                            },
                            "required": ["action"],
                        }
                    }
                },
            }
        },
    )
    async def action(asset: str, request: Request):
        try:
            if not admitted_worker(request):
                raise WebError("operator_not_authorized", 403)
            if control is None:
                raise WebError("worker_control_not_configured", 503)
            raw = await request.body()
            if len(raw) > 256:
                raise WebError("request_body_too_large", 413)
            try:
                value = json.loads(raw)
            except ValueError:
                raise WebError("invalid_worker_action") from None
            if type(value) is not dict or set(value) != {"action"}:
                raise WebError("invalid_worker_action")
            result = await run_in_threadpool(
                control.act, asset, hosts.get(asset), value["action"]
            )
            return JSONResponse(result, 200 if result["online"] else 202)
        except Exception as error:
            return failure(error)
