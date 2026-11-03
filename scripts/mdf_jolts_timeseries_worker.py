"""Explicit bounded JOLTS worker; no listener, service install or policy activation."""

import argparse
import json
import os
import signal
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Event

from market_data_foundation.historical.bls_cpi_direct_live import load_registration_key
from market_data_foundation.historical.cpi_release_schedule import (
    CpiScheduleError,
    _write_atomic,
)
from market_data_foundation.historical.cpi_timeseries_cli import emit, error_document
from market_data_foundation.web.jolts_runtime import create_jolts_app
from market_data_foundation.web.package_catalog import CredentialState, PackageCatalog
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost


def source_budget_guard(host, now):
    """Stop before due work when this bounded process has zero source budget."""
    snapshot = host.scheduler.status()
    refresh = snapshot.get("calendar", {}).get("next_refresh_at")
    due = snapshot.get("next_due")
    if (
        refresh is None
        or datetime.fromisoformat(refresh) <= now
        or (due is not None and datetime.fromisoformat(due) <= now)
    ):
        raise CpiScheduleError("jolts_worker_source_budget_exhausted")


def deny_upstream(*args, **kwargs):
    raise CpiScheduleError("jolts_worker_source_budget_exhausted")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "secret", "package-store", "state-directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--opt-in-jolts-worker", action="store_true")
    parser.add_argument("--allow-collect", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--maintenance-only", action="store_true")
    bounds = parser.add_mutually_exclusive_group()
    bounds.add_argument("--max-seconds", type=int, default=60)
    bounds.add_argument(
        "--deadline", help="Absolute aware DEV deadline, at most 4 hours"
    )
    parser.add_argument("--no-upstream", action="store_true")
    args = parser.parse_args(argv)
    if not args.opt_in_jolts_worker or not args.allow_collect:
        parser.error("explicit JOLTS live worker and collection opt-in required")
    if not 1 <= args.max_seconds <= 600:
        parser.error("max-seconds must be between 1 and 600")
    now = datetime.now(UTC)
    try:
        deadline = (
            datetime.fromisoformat(args.deadline)
            if args.deadline
            else now + timedelta(seconds=args.max_seconds)
        )
        if deadline.utcoffset() is None or not now < deadline <= now + timedelta(
            hours=4
        ):
            raise ValueError
    except ValueError, TypeError:
        parser.error("deadline must be aware, future and within 4 hours")
    max_seconds = (deadline - now).total_seconds()
    if any(
        not p.is_absolute() or ".." in p.parts
        for p in (args.runtime, args.secret, args.package_store, args.state_directory)
    ):
        parser.error("absolute paths required")
    packages = PackageHost(
        LocalPackageStore(args.package_store, PackageCatalog()),
        args.environment_id,
        credentials=lambda *_: [
            CredentialState(source_id="BLS", credential_state="configured")
        ],
        credential_loader=lambda *_: load_registration_key(args.secret),
    )
    app = create_jolts_app(
        packages=packages,
        runtime_path=args.runtime,
        secret_path=args.secret,
        state_directory=args.state_directory,
        allow_read=True,
        allow_collect=True,
    )
    host = app.state.jolts_host
    host.scheduler.maintenance_only = args.maintenance_only
    if args.no_upstream:
        # Defense against work changing between the read-only guard and tick.
        host.scheduler.calendar_fetcher = deny_upstream
        host.scheduler.reference_fetcher = deny_upstream
        host.scheduler.fallback_fetcher = deny_upstream
        host.scheduler.submit = deny_upstream
    lifecycle = dict(
        pid=os.getpid(),
        state="running",
        started_at=now.isoformat(),
        deadline=deadline.isoformat(),
        source_request_budget=0 if args.no_upstream else None,
        purpose="bounded JOLTS DEV maintenance"
        if args.maintenance_only
        else "bounded JOLTS DEV worker",
    )
    path = args.state_directory / "jolts-worker.json"
    # Stable lock prevents two workers from publishing conflicting process status.
    import fcntl

    with (args.state_directory / "jolts-worker.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            emit({"classification": "jolts_worker_already_running"})
            return 1
        _write_atomic(path, json.dumps(lifecycle).encode())
        started = time.monotonic()
        stopping = Event()

        def stop(signum, frame):
            stopping.set()

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            while not stopping.is_set() and datetime.now(UTC) < deadline:
                if args.no_upstream and host.auto_strategy.get()["enabled"]:
                    source_budget_guard(host, datetime.now(UTC))
                result = host.auto_strategy.tick()
                emit(result)
                if (result.get("execution") or {}).get("classification") == (
                    "jolts_worker_source_budget_exhausted"
                ):
                    raise CpiScheduleError("jolts_worker_source_budget_exhausted")
                if args.once:
                    break
                remaining = min(
                    max_seconds - (time.monotonic() - started),
                    (deadline - datetime.now(UTC)).total_seconds(),
                )
                if remaining > 0:
                    stopping.wait(min(5, remaining))
                else:
                    break
            return 0
        except Exception as error:
            lifecycle["classification"] = getattr(
                error, "classification", "jolts_worker_failed"
            )
            emit(error_document(error))
            return 1
        finally:
            host.auto_strategy.heartbeat("paused")
            lifecycle.update(state="stopped", stopped_at=datetime.now(UTC).isoformat())
            _write_atomic(path, json.dumps(lifecycle).encode())


if __name__ == "__main__":
    raise SystemExit(main())
