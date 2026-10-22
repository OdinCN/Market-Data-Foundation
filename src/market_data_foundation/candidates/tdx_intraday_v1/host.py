"""Explicit single-run Host launcher; requires a centrally reviewed binary pin."""

import argparse
import json
import signal
import subprocess
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from market_data_foundation.qualification.harness import classify_exception
from market_data_foundation.runtime.tdx_watchlist import (
    DRIVER_BINARY_SHA256,
    verified_driver,
)

from .model import SDK_COMMIT, canonical, require, sha
from .plan import validate_plan
from .replay import normalized_plan, read_bounded, replay, strict_json
from .session import SessionWindow

# Intentionally absent from the existing production allowlist. Only central
# review of the exact new binary may add this release and SHA. No CLI override.
I4_RELEASE = f"injoyai/tdx v1.0.0@{SDK_COMMIT}+mdf-wait-preregister-r1+i4-raw-r1"


class HostSignal(BaseException):
    """A catchable cancellation; no signal value enters an observation."""


def _cleanup_child(child, report):
    """Attempt every cleanup step; only a successful wait confirms reap.

    Two wait attempts of at most two seconds each, with an independent kill
    before the retry. Persistent OS failures leave UNKNOWN, never a reap claim.
    """
    report.update(
        reaped="UNKNOWN", kill="NOT_REQUIRED", status="UNKNOWN", errors=[], pipes={}
    )
    first_error = None

    def attempt(operation, action):
        nonlocal first_error
        try:
            return True, action()
        except Exception as error:
            if first_error is None:
                first_error = error
            report["errors"].append(
                {"operation": operation, "exception": classify_exception(error)}
            )
            return False, None

    polled, exit_code = attempt("poll", child.poll)
    if not polled or exit_code is None:
        killed, _ = attempt("kill", child.kill)
        report["kill"] = "REQUESTED" if killed else "UNKNOWN"
    for index in range(2):
        if index:
            killed, _ = attempt("kill_retry", child.kill)
            report["kill"] = "REQUESTED" if killed else "UNKNOWN"
        waited, _ = attempt(
            "wait" if index == 0 else "wait_retry", lambda: child.wait(timeout=2)
        )
        if waited:
            report["reaped"] = "CONFIRMED"
            break
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(child, name)
        if stream is None:
            report["pipes"][name] = "NOT_OWNED"
        else:
            closed, _ = attempt(f"close_{name}", stream.close)
            report["pipes"][name] = "CLOSED" if closed else "UNKNOWN"
    if not report["errors"]:
        report["status"] = "REAPED_PIPES_CLOSED"
    return first_error


@contextmanager
def managed_child(*args, on_started=lambda: None, cleanup=None, **kwargs):
    """Own the child from Popen through reap, including persistence failures.

    Signal handlers defer interruption during Popen's return/assignment window.
    Cleanup attempts to ignore repeated catchable termination signals, kills
    immediately, then makes at most two two-second wait attempts. Every handler
    restoration is attempted independently. Signal faults leave UNKNOWN evidence;
    the first cleanup fault propagates only without an active exception. SIGKILL
    cannot be handled by any launcher.
    """
    child = None
    armed = False
    pending = False
    previous = {}
    primary_failed = False

    def interrupted(signum, frame):
        nonlocal pending
        pending = True
        if armed:
            raise HostSignal()

    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP, signal.SIGQUIT):
            previous[sig] = signal.signal(sig, interrupted)
        child = subprocess.Popen(*args, **kwargs)
        on_started()
        armed = True
        if pending:
            raise HostSignal()
        yield child
    except BaseException:
        primary_failed = True
        raise
    finally:
        cleanup_error = None
        armed = False
        report = cleanup if cleanup is not None else {}
        report.update(
            reaped="NOT_STARTED",
            kill="NOT_REQUIRED",
            status="UNKNOWN",
            errors=[],
            pipes={},
        )
        signal_errors = []
        signal_states = {sig.name: {} for sig in previous}

        def set_handler(sig, handler, phase):
            nonlocal cleanup_error
            try:
                signal.signal(sig, handler)
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
                signal_states[sig.name][phase] = "UNKNOWN"
                signal_errors.append(
                    {
                        "operation": f"signal_{phase}",
                        "signal": sig.name,
                        "exception": classify_exception(error),
                    }
                )
            else:
                signal_states[sig.name][phase] = "CONFIRMED"

        try:
            for sig in previous:
                set_handler(sig, signal.SIG_IGN, "ignore")
            if child is not None:
                child_error = _cleanup_child(child, report)
                if cleanup_error is None:
                    cleanup_error = child_error
        finally:
            for sig, handler in previous.items():
                set_handler(sig, handler, "restore")
            report["signals"] = signal_states
            report["errors"].extend(signal_errors)
            if report["errors"]:
                report["status"] = "UNKNOWN"
        if not primary_failed and cleanup_error is not None:
            raise cleanup_error


def check_session_guard(plan: dict, guard_path: Path, now: datetime) -> dict:
    """Reread calendar bytes and exact UTC windows immediately before launch."""
    raw = read_bounded(guard_path, 65536)
    guard = strict_json(raw)
    require(
        type(guard) is dict
        and set(guard) == {"calendar_material", "calendar_sha256", "windows"},
        "guard_shape",
    )
    evidence_path = Path(guard["calendar_material"])
    require(evidence_path.is_absolute(), "absolute_calendar_material_path_required")
    evidence = read_bounded(evidence_path, 4 * 1024 * 1024)
    require(
        sha(evidence) == guard["calendar_sha256"], "calendar_material_digest_mismatch"
    )
    require(
        type(guard["windows"]) is list and 1 <= len(guard["windows"]) <= 24,
        "guard_window_bound",
    )
    windows = []
    for raw_window in guard["windows"]:
        values = dict(raw_window)
        values["trading_day"] = date.fromisoformat(values["trading_day"])
        for field in ("opens_at", "closes_at", "opens_utc", "closes_utc"):
            values[field] = datetime.fromisoformat(values[field])
        window = SessionWindow(**values)
        require(
            window.calendar_material_sha256 == guard["calendar_sha256"],
            "window_calendar_digest_mismatch",
        )
        windows.append(window)
    for request in plan["requests"]:
        # Historical responses and directory intake are never a live window probe.
        require(
            request["operation"]
            in ("GetTrade", "GetKline", "ExQuote", "ExBars", "ExMinute", "ExTrade"),
            "session_requires_live_api_request",
        )
        matching = [
            w
            for w in windows
            if (w.market, w.category, w.code)
            == (request["market"], request["category"], request["code"])
            and w.contains(now)
        ]
        require(len(matching) == 1, "exact_open_session_not_proven")
        window = matching[0]
        require(
            (window.closes_utc - now.astimezone(UTC)).total_seconds()
            >= plan["wall_seconds"] + 2,
            "run_would_cross_session_close",
        )
        if request["operation"] == "GetTrade":
            require(
                request["date"] == window.trading_day.strftime("%Y%m%d"),
                "trade_request_calendar_day_mismatch",
            )
    return {
        "guard_sha256": sha(raw),
        "calendar_sha256": sha(evidence),
        "checked_at": now.astimezone(UTC).isoformat(),
        "status": "EXACT_CALLER_SUPPLIED_WINDOWS_RECHECKED",
    }


def run_host(
    plan_path: Path,
    driver: Path,
    output: Path,
    *,
    allow_live: bool = False,
    stage: str,
    calendar_guard: Path | None = None,
    directory_receipts: Path | None = None,
) -> dict:
    require(allow_live is True, "explicit_live_opt_in_required")
    require(stage in ("afterclose", "session"), "explicit_run_stage_required")
    plan = strict_json(read_bounded(plan_path, 65536))
    preflight = validate_plan(plan)
    root = (Path.cwd() / "artifacts").absolute()
    require(".." not in output.parts, "artifact_parent_traversal_rejected")
    destination = output.absolute()
    require(
        destination.is_relative_to(root) and destination != root,
        "ignored_artifacts_output_required",
    )
    for parent in (destination, *destination.parents):
        require(not parent.is_symlink(), "artifact_symlink_rejected")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    frozen = canonical(normalized_plan(plan)) + b"\n"
    frozen_path = destination / "plan.json"
    frozen_path.write_bytes(frozen)
    ledger = {
        "schema": "mdf.tdx.i4.host-effect.v1",
        "stage": stage,
        "run_id": plan["run_id"],
        "plan_sha256": preflight["plan_sha256"],
        "driver_release": I4_RELEASE,
        "subprocess_started": False,
        "provider_effect": "ZERO_NOT_LAUNCHED",
        "pg_writes": 0,
        "garage_writes": 0,
        "timer_installed": False,
        "source_freshness": "UNKNOWN",
        "active_refresh": "NOT_PROVEN",
        "status": "INTENT",
    }
    ledger_path = destination / "host-ledger.json"

    def save():
        ledger_path.write_bytes(canonical(ledger) + b"\n")

    save()
    try:
        # A missing candidate pin is a real gate, not a request to trust sidecars.
        if I4_RELEASE not in DRIVER_BINARY_SHA256:
            ledger["gate"] = "CANDIDATE_RELEASE_HAS_NO_REVIEWED_BINARY_PIN"
        require(I4_RELEASE in DRIVER_BINARY_SHA256, "candidate_binary_pin_required")
        from market_data_foundation.candidates.tdx_exhq_v1.retained import (
            directory_associations,
        )

        directory_document = None
        if directory_receipts is not None:
            directory_document = strict_json(
                read_bounded(directory_receipts, 4 * 1024 * 1024)
            )
        directory_associations(
            plan,
            directory_document,
            mode="retained",
            producer_sha256=DRIVER_BINARY_SHA256[I4_RELEASE],
            require_all=True,
        )
        directory_path = None
        if directory_document is not None:
            directory_path = destination / "directory-receipts.json"
            directory_path.write_bytes(canonical(directory_document) + b"\n")
            ledger["directory_receipts_sha256"] = sha(canonical(directory_document))
        with verified_driver(driver, I4_RELEASE) as (sealed, fd):
            ledger["producer_sha256"] = DRIVER_BINARY_SHA256[I4_RELEASE]
            if stage == "session":
                require(calendar_guard is not None, "session_calendar_guard_required")
                ledger["session_recheck"] = check_session_guard(
                    plan, calendar_guard, datetime.now(UTC)
                )
            # A crash or persistent disk failure after this save cannot leave a
            # durable claim of zero effects for an attempted launch.
            ledger.update(
                status="LAUNCH_INTENT",
                subprocess_started=None,
                provider_effect="UNKNOWN_UNTIL_EXACT_RECEIPTS",
            )
            save()
            with (
                frozen_path.open("rb") as input_stream,
                (destination / "records.jsonl").open("xb") as output_stream,
            ):
                cleanup = {}
                with managed_child(
                    [sealed, "--i4-probe"],
                    stdin=input_stream,
                    stdout=output_stream,
                    stderr=subprocess.DEVNULL,
                    pass_fds=(fd,),
                    close_fds=True,
                    on_started=lambda: ledger.update(
                        subprocess_started=True, status="RUNNING", child_cleanup=cleanup
                    ),
                    cleanup=cleanup,
                    env={
                        "PATH": "/usr/bin:/bin",
                        "TZ": "UTC",
                        "MDF_I4_ALLOW_LIVE": "1",
                    },
                ) as child:
                    save()
                    try:
                        ledger["child_exit"] = child.wait(
                            timeout=plan["wall_seconds"] + 2
                        )
                    except subprocess.TimeoutExpired:
                        ledger["status"] = "HOST_WALL_STOP"
                ledger["child_exit"] = child.returncode
            raw = read_bounded(destination / "records.jsonl", 6 * 1024 * 1024)
            records = tuple(strict_json(line) for line in raw.splitlines() if line)
            summary = replay(
                plan,
                records,
                destination / "readback",
                producer_sha256=ledger["producer_sha256"],
                evidence_mode="retained",
                directory_receipts=directory_path,
            )
            ledger["readback_terminal"] = summary["terminal"]
            ledger["readback_failures"] = len(summary["failures"])
            ledger["all_requested_pages_received"] = summary[
                "all_requested_pages_received"
            ]
            if ledger["status"] != "HOST_WALL_STOP":
                complete = (
                    ledger["child_exit"] == 0
                    and not summary["failures"]
                    and summary["all_requested_pages_received"]
                    and summary["terminal"]
                    == "TRANSPORT_BUDGET_COMPLETE_NOT_QUALIFICATION"
                )
                ledger["status"] = (
                    "RETURNED_FOR_INDEPENDENT_REVIEW"
                    if complete
                    else "STOP_TRANSPORT_OR_INTAKE"
                )
    except BaseException as error:
        ledger["status"] = "STOP"
        ledger["exception"] = classify_exception(error)
        if not isinstance(error, Exception):
            raise
    finally:
        try:
            save()
        except Exception as error:
            ledger["status"] = "STOP"
            ledger["ledger_persistence"] = "FAILED"
            ledger["exception"] = classify_exception(error)
    return ledger


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--stage", choices=("afterclose", "session"), required=True)
    parser.add_argument("--calendar-guard", type=Path)
    parser.add_argument("--directory-receipts", type=Path)
    args = parser.parse_args()
    try:
        result = run_host(
            args.plan,
            args.driver,
            args.output,
            allow_live=args.live,
            stage=args.stage,
            calendar_guard=args.calendar_guard,
            directory_receipts=args.directory_receipts,
        )
        print(
            json.dumps(
                {
                    "status": result["status"],
                    "subprocess_started": result["subprocess_started"],
                }
            )
        )
        return 0 if result["status"] == "RETURNED_FOR_INDEPENDENT_REVIEW" else 1
    except Exception as error:
        print(json.dumps({"exception": classify_exception(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
