"""Explicit bounded JOLTS worker; no listener, service install or policy activation."""

import argparse
import json
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_foundation.historical.bls_cpi_direct_live import load_registration_key
from market_data_foundation.historical.cpi_release_schedule import _write_atomic
from market_data_foundation.historical.cpi_timeseries_cli import emit, error_document
from market_data_foundation.web.jolts_runtime import create_jolts_app
from market_data_foundation.web.package_catalog import CredentialState, PackageCatalog
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("runtime", "secret", "package-store", "state-directory"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--environment-id", required=True)
    parser.add_argument("--opt-in-jolts-worker", action="store_true")
    parser.add_argument("--allow-collect", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--maintenance-only", action="store_true")
    parser.add_argument("--max-seconds", type=int, default=60)
    args = parser.parse_args(argv)
    if not args.opt_in_jolts_worker or not args.allow_collect:
        parser.error("explicit JOLTS live worker and collection opt-in required")
    if not 1 <= args.max_seconds <= 600:
        parser.error("max-seconds must be between 1 and 600")
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
    now = datetime.now(UTC)
    lifecycle = dict(
        pid=os.getpid(),
        state="running",
        started_at=now.isoformat(),
        deadline=(now + timedelta(seconds=args.max_seconds)).isoformat(),
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
        try:
            while time.monotonic() - started < args.max_seconds:
                emit(host.auto_strategy.tick())
                if args.once:
                    break
                remaining = args.max_seconds - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(min(5, remaining))
            return 0
        except Exception as error:
            emit(error_document(error))
            return 1
        finally:
            host.auto_strategy.heartbeat("paused")
            lifecycle.update(state="stopped", stopped_at=datetime.now(UTC).isoformat())
            _write_atomic(path, json.dumps(lifecycle).encode())


if __name__ == "__main__":
    raise SystemExit(main())
