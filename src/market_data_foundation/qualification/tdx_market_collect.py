"""Explicit one-shot Host runner. Construction/import never starts a Provider."""

import fcntl
import os
import selectors
import signal
import subprocess
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.runtime.tdx_watchlist import (
    DRIVER_SEALS,
    F_ADD_SEALS,
    F_GET_SEALS,
    FramedProcess,
    RealPeriodicWatchlistSession,
    _open_driver,
)
from market_data_foundation.runtime.watchlist_receipt import AcceptanceReceiptWriter

from .tdx_market_evidence import (
    admit_line,
    directory_inputs,
    qualification_observation,
    quote_changes,
)
from .tdx_market_plan import (
    Plan,
    after_close_gate,
    bounded_read,
    calendar_gate,
    encoded,
    utc_now,
    verify_endpoint,
)


class ArtifactRun:
    def __init__(self, path: Path):
        root = (Path.cwd() / "artifacts").resolve()
        require(
            path.resolve().is_relative_to(root) and path.resolve() != root,
            "private_artifact_path_required",
        )
        require(not path.is_symlink(), "artifact_symlink_rejected")
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
        self.path = path

    def write(self, name: str, data: bytes):
        require(Path(name).name == name, "artifact_name_invalid")
        with (self.path / name).open("xb") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        directory = os.open(self.path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        # Material success requires exact independent readback. No overwrite/retry.
        require((self.path / name).read_bytes() == data, "artifact_readback_mismatch")
        return {
            "uri": (self.path / name).resolve().as_uri(),
            "sha256": sha256(data).hexdigest(),
            "bytes": len(data),
        }


@contextmanager
def sealed_candidate(path: Path, reviewed_sha256: str):
    """Independent research candidate identity, separate from old driver trust map."""
    with _open_driver(path) as source:
        size = os.fstat(source).st_size
        require(1 <= size <= 64 * 1024 * 1024, "candidate_size_invalid")
        fd = os.memfd_create("mdf-i2-reviewed", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING)
        with os.fdopen(fd, "w+b") as snapshot:
            remaining = size
            digest = sha256()
            while remaining:
                chunk = os.read(source, min(remaining, 1024 * 1024))
                require(bool(chunk), "candidate_changed")
                snapshot.write(chunk)
                digest.update(chunk)
                remaining -= len(chunk)
            require(
                digest.hexdigest() == reviewed_sha256, "candidate_not_host_reviewed"
            )
            snapshot.flush()
            os.fchmod(fd, 0o500)
            fcntl.fcntl(fd, F_ADD_SEALS, DRIVER_SEALS)
            require(
                fcntl.fcntl(fd, F_GET_SEALS) & DRIVER_SEALS == DRIVER_SEALS,
                "candidate_not_sealed",
            )
            yield f"/proc/self/fd/{fd}", fd


class TerminationRequested(BaseException):
    """Catchable SIGTERM so raw prefixes and UNKNOWN intent survive shutdown."""


@contextmanager
def termination_guard():
    previous = signal.getsignal(signal.SIGTERM)

    def stop(signum, frame):
        raise TerminationRequested()

    # signal.signal fails before any Provider operation outside the main thread.
    signal.signal(signal.SIGTERM, stop)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


class SourceStopped(Exception):
    def __init__(
        self, raw: bytes, classification: str, interruption=None, cleanup=None
    ):
        self.raw = raw
        self.classification = classification
        self.interruption = interruption
        self.cleanup = cleanup
        super().__init__(classification)


SOURCE_STDOUT_LIMIT = 16 * 1024 * 1024


@contextmanager
def finishing_source():
    # A second catchable stop must not interrupt drain/fsync/final receipt.
    # SIGKILL and power loss cannot be handled here.
    previous = {
        s: signal.signal(s, signal.SIG_IGN) for s in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def stop_and_drain(child, output, on_chunk):
    """Stop producer first; never wait for pipe EOF or an inherited writer."""
    cleanup = {"child_reaped": False, "drain": "UNKNOWN", "errors": []}
    try:
        if child.poll() is None:
            child.kill()
        child.wait(timeout=2)
        cleanup["child_reaped"] = True
    except (OSError, subprocess.TimeoutExpired) as exc:
        cleanup["errors"].append(type(exc).__name__)
    try:
        fd = child.stdout.fileno()
        os.set_blocking(fd, False)
        end = time.monotonic() + 0.5
        while len(output) < SOURCE_STDOUT_LIMIT:
            if time.monotonic() >= end:
                cleanup["drain"] = "TIME_LIMIT"
                break
            try:
                chunk = os.read(fd, min(65536, SOURCE_STDOUT_LIMIT - len(output)))
            except BlockingIOError:
                cleanup["drain"] = "AVAILABLE_BYTES_EXHAUSTED"
                break
            if not chunk:
                cleanup["drain"] = "EOF"
                break
            output.extend(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        else:
            cleanup["drain"] = "BYTE_LIMIT"
    except Exception as exc:
        cleanup["errors"].append(type(exc).__name__)
    return cleanup


def source_accounting(plan, raw, status):
    """Confirmed records are lower bounds until a complete successful exit.

    Missing records cannot prove that the next protocol write never happened.
    Keep per-job evidence separate from total effects, including after interruption.
    """
    jobs = [
        {
            "index": index,
            "request": asdict(request)
            if request is not None
            else {"operation": "MConnect"},
            "record_state": "UNCONFIRMED_EFFECT_UNKNOWN",
        }
        for index, request in enumerate((None, *plan.requests), -1)
    ]
    sent_bytes = response_bytes = confirmed_bytes = confirmed = 0
    all_complete = True
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n") or confirmed == len(jobs):
            break
        try:
            row, sent, response = admit_line(line, plan, confirmed - 1)
        except Exception:
            break
        complete = row["classification"] == "retained_raw"
        all_complete = all_complete and complete
        jobs[confirmed].update(
            record_state="CONFIRMED_COMPLETE" if complete else "CONFIRMED_PARTIAL",
            classification=row["classification"],
            observed_request_bytes=len(sent),
            observed_response_bytes=len(response),
        )
        confirmed += 1
        confirmed_bytes += len(line)
        sent_bytes += len(sent)
        response_bytes += len(response)
    exact = (
        status == 0
        and confirmed == len(jobs)
        and confirmed_bytes == len(raw)
        and all_complete
    )
    return {
        "actual_request_bytes": sent_bytes if exact else "UNKNOWN",
        "actual_response_bytes": response_bytes if exact else "UNKNOWN",
        "source_effect": {
            "run_id": plan.run_id,
            "plan_revision": plan.revision,
            "total_effect": "EXACT" if exact else "UNKNOWN",
            "stage": (
                "COMPLETE"
                if exact
                else "HANDSHAKE_ONLY"
                if confirmed == 1 and jobs[0]["record_state"] == "CONFIRMED_COMPLETE"
                else "PARTIAL"
                if confirmed
                else "UNCONFIRMED"
            ),
            "observed_confirmed_lower_bound": {
                "request_bytes": sent_bytes,
                "response_bytes": response_bytes,
            },
            "confirmed_records": confirmed,
            "unconfirmed_stdout_bytes": len(raw) - confirmed_bytes,
            "jobs": jobs,
        },
    }


def remaining_seconds(deadline: datetime):
    remaining = (deadline - utc_now()).total_seconds()
    require(remaining > 0, "execution_deadline_reached")
    return remaining


class DeadlineFramedProcess(FramedProcess):
    """I2-only absolute cutoff; the pinned Driver and its config stay unchanged."""

    def __init__(self, command, config, *, deadline):
        self.absolute_deadline = deadline
        self.required_seconds = config.duration_seconds + 6
        remaining_seconds(deadline)
        super().__init__(command, config)

    def send(self, kind, body, *, deadline):
        if kind == b"K":
            # Driver verification/startup may have consumed the earlier gate margin.
            require(
                remaining_seconds(self.absolute_deadline) > self.required_seconds,
                "quote_budget_crosses_deadline",
            )
        return super().send(kind, body, deadline=deadline)

    def _transfer(self, stream, *, size=0, wire=None, deadline):
        try:
            deadline = min(
                deadline, time.monotonic() + remaining_seconds(self.absolute_deadline)
            )
            return super()._transfer(stream, size=size, wire=wire, deadline=deadline)
        except BaseException:
            # Stop the child immediately, before shared session cleanup/grace periods.
            if self.process.poll() is None:
                self.process.kill()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.process.stdin.close()
        wait = min(1, max(0, (self.absolute_deadline - utc_now()).total_seconds()))
        try:
            self.process.wait(timeout=wait)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=2)
        finally:
            self.process.stdout.close()


def source_once(plan: Plan, candidate: Path, *, deadline: datetime, on_chunk=None):
    """Bound stdout and durably retain each received chunk; never auto-retry."""
    with sealed_candidate(candidate, plan.collector_sha256) as (executable, fd):
        require(
            remaining_seconds(deadline) > plan.budgets["wall_seconds"],
            "source_budget_crosses_deadline",
        )
        child = subprocess.Popen(
            [executable, "--live"],
            pass_fds=(fd,),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
    output = bytearray()
    stopped = False
    try:
        child.stdin.write(
            encoded(plan.source_config() | {"deadline": deadline.isoformat()})
        )
        child.stdin.close()
        stop_at = time.monotonic() + min(
            plan.wall_seconds + 2, remaining_seconds(deadline)
        )
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ)
            while True:
                wait = min(stop_at - time.monotonic(), remaining_seconds(deadline))
                require(wait > 0, "source_wall_budget")
                ready = selector.select(wait)
                require(bool(ready), "source_wall_budget")
                require(len(output) < SOURCE_STDOUT_LIMIT, "source_output_budget")
                chunk = os.read(
                    child.stdout.fileno(), min(65536, SOURCE_STDOUT_LIMIT - len(output))
                )
                if not chunk:
                    break
                output.extend(chunk)
                if on_chunk is not None:
                    on_chunk(chunk)
        return bytes(output), child.wait(timeout=max(0.01, stop_at - time.monotonic()))
    except BaseException as exc:
        with finishing_source():
            cleanup = stop_and_drain(child, output, on_chunk)
            stopped = True
        classification = (
            str(exc) if isinstance(exc, ContractError) else type(exc).__name__
        )
        raise SourceStopped(
            bytes(output),
            classification,
            exc if not isinstance(exc, Exception) else None,
            cleanup,
        ) from None
    finally:
        with finishing_source():
            if not stopped:
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=2)
            child.stdin.close()
            child.stdout.close()


def collect(
    plan: Plan,
    *,
    sdk: Path,
    candidate: Path,
    driver: Path,
    output: Path,
    allow_live: bool = False,
    reviewed_collector_sha256: str = "",
    calendar_raw: bytes | None = None,
    calendar_sha256: str = "",
):
    require(allow_live, "explicit_live_opt_in_required")
    require(
        not plan.source_enabled or reviewed_collector_sha256 == plan.collector_sha256,
        "independent_host_candidate_digest_required",
    )
    verify_endpoint(plan, sdk)
    # Check timer/calendar before ANY provider calls. Session mode only observes quotes.
    gate = (
        calendar_gate(calendar_raw, calendar_sha256, plan, utc_now())
        if plan.mode == "session"
        else after_close_gate(
            utc_now(), plan.observation_day, plan.budgets["wall_seconds"]
        )
    )
    deadline = datetime.fromisoformat(gate["deadline"])
    run = ArtifactRun(output)
    identities = Path.cwd() / "artifacts" / "i2-run-identities"
    identities.mkdir(mode=0o700, exist_ok=True)
    with (identities / plan.run_id).open("xb") as reservation:
        os.fchmod(reservation.fileno(), 0o600)
        reservation.write(plan.revision.encode())
        reservation.flush()
        os.fsync(reservation.fileno())
    run.write("plan.json", encoded(asdict(plan)))
    run.write(
        "start.json",
        encoded(
            {
                "plan_revision": plan.revision,
                "gate": gate,
                "started_at": utc_now().isoformat(),
                "budgets": plan.budgets,
            }
        ),
    )
    if plan.mode == "session":
        run.write("calendar-intake.json", calendar_raw)
    ledger = {
        "plan_revision": plan.revision,
        "source": "NOT_ATTEMPTED",
        "quote": "NOT_ATTEMPTED",
        "pg_writes": 0,
        "garage_writes": 0,
        "provider_retries": 0,
        "materials": [],
        "adoption": "NOT_GRANTED",
        "source_event_clock": "UNKNOWN",
        "calendar_qualification": "UNKNOWN",
    }
    try:
        with termination_guard():
            if plan.source_enabled:
                ledger["source"] = "ATTEMPT_STARTED_EFFECT_UNKNOWN_UNTIL_RECEIPT"
                ledger.update(source_accounting(plan, b"", None))
                run.write("source-intent.json", encoded(ledger))
                # Retain each received prefix before waiting for another source chunk.
                progress_path = run.path / "source-stdout-progress.jsonl"
                with progress_path.open("xb") as progress:
                    os.fchmod(progress.fileno(), 0o600)

                    def retain_chunk(chunk):
                        progress.write(chunk)
                        progress.flush()
                        os.fsync(progress.fileno())

                    try:
                        raw, status = source_once(
                            plan, candidate, deadline=deadline, on_chunk=retain_chunk
                        )
                    except SourceStopped as exc:
                        with finishing_source():
                            ledger.update(source_accounting(plan, exc.raw, None))
                            ledger["source_cleanup"] = exc.cleanup
                            ledger["materials"].append(
                                run.write("source-stdout-partial.jsonl", exc.raw)
                            )
                        if exc.interruption is not None:
                            raise exc.interruption
                        raise ContractError(exc.classification) from None
                ledger.update(source_accounting(plan, raw, status))
                ledger["source_exit"] = status
                require(
                    progress_path.read_bytes() == raw,
                    "source_progress_readback_mismatch",
                )
                ledger["materials"].append(run.write("source-stdout.jsonl", raw))
                lines = raw.splitlines()
                require(
                    1 <= len(lines) <= len(plan.requests) + 1, "source_record_count"
                )
                received_bytes = request_bytes = 0
                observations = []
                for index, line in enumerate(lines, -1):
                    row, sent, response = admit_line(line, plan, index)
                    received_bytes += len(response)
                    request_bytes += len(sent)
                    require(
                        received_bytes <= plan.max_response_bytes
                        and request_bytes <= plan.max_request_bytes,
                        "source_bytes_budget",
                    )
                    for kind, payload in (("request", sent), ("response", response)):
                        ledger["materials"].append(
                            run.write(f"source-{index + 1}-{kind}.bin", payload)
                        )
                    # Projections are derived; source-stdout retains exact Go output.
                    if index >= 0:
                        observations.append(
                            qualification_observation(
                                row, plan.requests[index], plan, index
                            )
                        )
                    run.write(f"source-{index + 1}-assessment.json", encoded(row))
                run.write("observations.json", encoded(observations))
                for market in (0, 1):
                    _, revision = directory_inputs(
                        tuple(lines), plan, market=market, known_at=utc_now()
                    )
                    run.write(f"universe-market-{market}.json", encoded(revision))
                ledger["source"] = (
                    "BOUNDED_READ_COMPLETE"
                    if ledger["source_effect"]["total_effect"] == "EXACT"
                    else "STOPPED_CLASSIFIED"
                )
            # Source failure adds no fallback or budget. Quote has a separate
            # frozen capability budget and may still be inspected at this SAME endpoint.
            if plan.mode == "session":
                gate = calendar_gate(calendar_raw, calendar_sha256, plan, utc_now())
            else:
                after_close_gate(
                    utc_now(), plan.observation_day, plan.quote_wall_seconds + 6
                )
            config = plan.quote_config()
            # Retained intake reconstructs ACKs using this canonical receipt layout.
            run.write("config.json", encoded(asdict(config)))
            ledger["quote"] = "ATTEMPT_STARTED_EFFECT_UNKNOWN_UNTIL_RECEIPT"
            session = RealPeriodicWatchlistSession(config)
            adapter = DeadlineFramedProcess(
                [str(driver), "--live"], config, deadline=deadline
            )
            batches = []

            def archive(name, data):
                ledger["materials"].append(run.write(name, data))
                batches.append(data)

            report = session.run(
                adapter,
                archive=archive,
                on_read_ready=AcceptanceReceiptWriter(run.path),
            )
            run.write("report.json", encoded(report))
            ledger["quote"] = report["stop_reason"]
            run.write(
                "quote-comparison.json", encoded(quote_changes(tuple(batches), plan))
            )
            ledger["terminal"] = (
                "COMPLETED_BOUNDED_OBSERVATION"
                if report["stop_reason"] == "budget_exhausted"
                and report["health"] == "readable_snapshot"
                and len(batches) == plan.quote_rounds
                and (
                    not plan.source_enabled
                    or ledger["source"] == "BOUNDED_READ_COMPLETE"
                )
                else "STOPPED_CLASSIFIED"
            )
            if ledger["terminal"] == "COMPLETED_BOUNDED_OBSERVATION":
                names = ["config.json", "report.json", "read-ready.json"] + [
                    f"batch-{n}.json" for n in range(1, plan.quote_rounds + 1)
                ]
                manifest = {
                    name: sha256(bounded_read(run.path / name, 1024 * 1024)).hexdigest()
                    for name in names
                }
                ledger["quote_manifest"] = run.write(
                    "quote-manifest.json", encoded(manifest)
                )
    except BaseException as exc:
        ledger["terminal"] = "STOPPED"
        # ContractError carries authored codes; discard all other error messages.
        ledger["exception_classification"] = (
            str(exc) if isinstance(exc, ContractError) else type(exc).__name__
        )
        if not isinstance(exc, Exception):
            raise
    finally:
        with finishing_source():
            ledger["finished_at"] = utc_now().isoformat()
            run.write("effect-ledger.json", encoded(ledger))
    return ledger
