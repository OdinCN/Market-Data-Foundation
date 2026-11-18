"""Real Go source -> periodic ingress/read -> per-batch ACK; no import effects."""

import argparse
import fcntl
import json
import math
import os
import re
import selectors
import stat
import struct
import subprocess
import time
import uuid
from collections import deque
from contextlib import ExitStack, contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from market_data_foundation.candidates.go_stock_real_observation_ingress import (
    OFFLINE_ORIGIN,
    REAL_CLOCKS,
    REAL_ORIGIN,
    Observation,
    _constant,
    _decode,
    _float,
    _object,
    _utc,
)
from market_data_foundation.candidates.stock_snapshot import (
    MEMBERS,
    SDK_RELEASE,
    Clocks,
    StockSnapshotCandidate,
    validate_members,
)
from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.qualification.contracts import Capability, Fixture

DRIVER_SDK_RELEASE = SDK_RELEASE + "+mdf-wait-preregister-r1"
V100_DRIVER_SDK_RELEASE = (
    "injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
    "+mdf-wait-preregister-r1"
)
DRIVER_SDK_RELEASES = (DRIVER_SDK_RELEASE, V100_DRIVER_SDK_RELEASE)
# Reviewed exact artifacts, not self-reported versions or adjacent build reports.
# The legacy artifact predates --version. Rebuilds need independent review/pinning.
DRIVER_BINARY_SHA256 = {
    DRIVER_SDK_RELEASE: (
        "9480165a514e97764bff2aace55d5fe56923b34724b51f947f8a17ce00ce159a"
    ),
    V100_DRIVER_SDK_RELEASE: (
        "13cfe86189ea2309bf1dc6d7c096c390021195b577f804d90f18f0c4c64a0a93"
    ),
}
# Linux UAPI (linux/fcntl.h). The qualified Python build omits these names;
# the kernel must still implement and confirm every seal, or launch is rejected.
F_ADD_SEALS = 1033
F_GET_SEALS = 1034
DRIVER_SEALS = 0x0001 | 0x0002 | 0x0004 | 0x0008  # SEAL, SHRINK, GROW, WRITE
# Includes the Go 100 ms worker join plus finite scheduler/close allowance.
TEARDOWN_TOLERANCE_NS = 250_000_000
MAX_FRAME = 131_072
EVENT_CLASSES = {
    "probe_ok",
    "probe_failed",
    "connection_failed",
    "connection_attempt",
    "quote_attempt",
    "sdk_getquote_entered",
    "protocol_write_attempt",
    "protocol_write_complete",
    "protocol_write_failed",
    "protocol_write_partial",
    "connected",
    "quote_failed",
    "ack_received",
    "complete",
    "stopped",
}
EVENT_REASONS = {
    "",
    "refused",
    "access_restricted",
    "timeout",
    "disconnected",
    "source_error",
    "failover_budget_exhausted",
    "connection_budget_exhausted",
    "ipc_error",
    "duration_exhausted",
    "cadence_missed",
    "freeze_error",
    "ack_rejected_or_timeout",
    "budget_exhausted",
    "selection_failed",
    "worker_stuck",
}


def validate_event(body: dict, config) -> None:
    require(
        body.keys() - {"requested_bytes", "written_bytes"}
        == {
            "classification",
            "reason",
            "session_id",
            "endpoint",
            "candidate_origin",
            "elapsed_ns",
            "attempt",
            "round",
            "segment",
        }
        and body["session_id"] == config.session_id
        and type(body["classification"]) is str
        and body["classification"] in EVENT_CLASSES
        and type(body["reason"]) is str
        and body["reason"] in EVENT_REASONS
        and type(body["candidate_origin"]) is str
        and body["candidate_origin"] in {"", "configured", "pinned_sdk_hosts"}
        and type(body["endpoint"]) is str
        and (
            not body["endpoint"]
            or re.fullmatch(r"[A-Za-z0-9.-]{1,253}:[0-9]{1,5}", body["endpoint"])
        )
        and all(
            type(body[k]) is int and 0 <= body[k] <= upper
            for k, upper in (
                ("elapsed_ns", 20_000_000_000),
                ("attempt", max(config.max_probes, config.max_connections)),
                ("round", config.rounds),
                ("segment", 3),
            )
        ),
        "event_identity_invalid",
    )

    require(
        body["classification"].startswith("protocol_write_")
        or not ({"requested_bytes", "written_bytes"} & body.keys()),
        "event_identity_invalid",
    )


def decode_json(wire: bytes) -> dict:
    try:
        value = json.loads(
            wire,
            object_pairs_hook=_object,
            parse_constant=_constant,
            parse_float=_float,
        )
    except ValueError, UnicodeError, RecursionError:
        raise ContractError("malformed_ipc_json") from None
    require(type(value) is dict, "ipc_object_required")
    return value


@dataclass(frozen=True)
class SessionConfig:
    securities: tuple[str, ...] = MEMBERS
    endpoints: tuple[str, ...] = ()
    sdk_candidates: bool = True
    sdk_offset: int = 0
    max_probes: int = 4
    max_connections: int = 3
    max_failovers: int = 1
    rounds: int = 3
    period_seconds: float = 12
    duration_seconds: float = 60
    dial_timeout_seconds: float = 2
    quote_timeout_seconds: float = 3
    ack_timeout_seconds: float = 3
    ttl_seconds: float = 30
    start_at: str = ""
    session_id: str = ""
    # Old receipts without this field retain their original release identity.
    driver_sdk_release: str = DRIVER_SDK_RELEASE

    def __post_init__(self):
        require(
            type(self.driver_sdk_release) is str
            and self.driver_sdk_release in DRIVER_SDK_RELEASES,
            "driver_sdk_release_invalid",
        )
        validate_members(self.securities)
        require(type(self.sdk_candidates) is bool, "sdk_pool_config_invalid")
        require(self.sdk_candidates or bool(self.endpoints), "empty_candidate_pool")
        require(
            type(self.endpoints) is tuple
            and len(self.endpoints) <= 8
            and len(set(self.endpoints)) == len(self.endpoints)
            and all(
                type(e) is str
                and re.fullmatch(r"[A-Za-z0-9.-]{1,253}:[0-9]{1,5}", e)
                and 1 <= int(e.rsplit(":", 1)[1]) <= 65535
                for e in self.endpoints
            ),
            "endpoint_config_invalid",
        )
        for name, lower, upper in (
            ("sdk_offset", 0, 255),
            ("max_probes", 1, 8),
            ("max_connections", 1, 8),
            ("max_failovers", 0, 3),
            ("rounds", 1, 120),
        ):
            value = getattr(self, name)
            require(type(value) is int and lower <= value <= upper, "budget_invalid")
        for name, lower, upper in (
            ("period_seconds", 1, 60),
            ("duration_seconds", 1, 900),
            ("dial_timeout_seconds", 0.1, 10),
            ("quote_timeout_seconds", 0.1, 10),
            ("ack_timeout_seconds", 0.1, 10),
            ("ttl_seconds", 1, 300),
        ):
            value = getattr(self, name)
            require(
                type(value) in (int, float)
                and math.isfinite(value)
                and lower <= value <= upper,
                "timing_config_invalid",
            )
        require(type(self.start_at) is str, "start_clock_invalid")
        if self.start_at:
            _utc(self.start_at)
        require(
            type(self.session_id) is str
            and (not self.session_id or re.fullmatch(r"[a-f0-9]{32}", self.session_id)),
            "session_id_invalid",
        )


class WatchlistIngress:
    """Dynamic policy around the delivered parser, Observation and state machine.

    Real mode owns its known clock. Synthetic QA has an explicit distinct origin.
    Source failures seal a segment; the next success must use a new connection.
    """

    def __init__(self, config: SessionConfig, *, offline_fixture_mode: bool = False):
        require(type(offline_fixture_mode) is bool, "fixture_mode_invalid")
        require(bool(config.session_id), "session_id_required")
        self.config = config
        self.offline = offline_fixture_mode
        self.candidate = StockSnapshotCandidate(
            members=config.securities,
            capacity=min(256, max(2, config.rounds)),
            ttl=timedelta(seconds=config.ttl_seconds),
        )
        self.history: deque[Observation] = deque(maxlen=config.rounds)
        self.connection: dict | None = None
        self.segment = 0
        self._needs_connection = True
        self._last_attempt = 0
        self._endpoints_used = set()

    def bind(self, body: dict) -> None:
        require(
            body.keys()
            == {"session_id", "sdk_release", "endpoint", "segment", "connection"}
            and body["session_id"] == self.config.session_id
            and body["sdk_release"] == self.config.driver_sdk_release
            and type(body["segment"]) is int
            and body["segment"] == self.segment
            and self._needs_connection
            and type(body["endpoint"]) is str
            and re.fullmatch(r"[A-Za-z0-9.-]{1,253}:[0-9]{1,5}", body["endpoint"]),
            "connection_identity_invalid",
        )
        conn = body["connection"]
        require(
            type(conn) is dict
            and conn.keys()
            == {"requested", "received", "elapsed_ns", "redial", "attempts", "peer"}
            and type(conn["peer"]) is str
            and 1 <= len(conn["peer"]) <= 320
            and type(conn["attempts"]) is int
            and self._last_attempt < conn["attempts"] <= self.config.max_connections
            and conn["redial"] is False
            and type(conn["elapsed_ns"]) is int
            and 0
            <= conn["elapsed_ns"]
            <= int(self.config.dial_timeout_seconds * 1e9) + TEARDOWN_TOLERANCE_NS
            and _utc(conn["requested"]) <= _utc(conn["received"])
            and self.segment <= self.config.max_failovers
            and body["endpoint"] not in self._endpoints_used,
            "connection_policy_invalid",
        )
        self._last_attempt = conn["attempts"]
        self._endpoints_used.add(body["endpoint"])
        self.connection = body
        self._needs_connection = False

    def ingest(self, wire: bytes, *, known_at: datetime | None = None) -> Observation:
        if self.offline:
            require(known_at is not None, "fixture_known_required")
        else:
            require(known_at is None, "real_known_override_forbidden")
            known_at = datetime.now(UTC)
        return self._ingest_at(wire, known_at=known_at)

    def _ingest_at(self, wire: bytes, *, known_at: datetime) -> Observation:
        """Shared reconstruction path for the bounded acceptance-receipt reader.

        This stamps no new observation and confers no acceptance authority.
        Live ingest continues to own its clock; retained callers must validate
        the receipt, exact bytes and completed Read/Go ACK barrier separately.
        """
        require(self.connection is not None, "connection_required")
        body, capture = _decode(
            wire,
            offline=self.offline,
            members=self.config.securities,
            endpoint=self.connection["endpoint"],
            maximum=self.config.rounds,
            session_id=self.config.session_id,
            connection_attempts=self.config.max_connections,
            sdk_release=self.config.driver_sdk_release,
            connection_elapsed_bound_ns=int(self.config.dial_timeout_seconds * 1e9)
            + TEARDOWN_TOLERANCE_NS,
            quote_elapsed_bound_ns=int(self.config.quote_timeout_seconds * 1e9)
            + TEARDOWN_TOLERANCE_NS,
        )
        require(not self._needs_connection, "new_connection_required")
        require(body["round"] == len(self.history) + 1, "round_not_forward")
        require(body["max_rounds"] == self.config.rounds, "session_budget_drift")
        require(
            body["segment"] == self.segment
            and body["connection"] == self.connection["connection"],
            "connection_drift",
        )
        clocks = Clocks(*capture, known_at)
        failure = body["failure"]
        if failure:
            # Close a real observation segment; source_error is retained separately.
            record = self.candidate.reconnect(
                clocks=clocks,
                failure="timeout" if failure == "timeout" else "disconnected",
                new_baseline=True,
            )
        else:
            record = self.candidate.observe(
                body["rows"], clocks=clocks, parser_revision="go-real-decoded-shape-r1"
            )
        conn = body["connection"]
        observation = Observation(
            body["batch_id"],
            record,
            body["input_origin"],
            body["clock_provenance"],
            OFFLINE_ORIGIN if self.offline else "local_ingress_wall_clock_utc",
            sha256(wire).hexdigest(),
            _utc(conn["requested"]),
            _utc(conn["received"]),
            conn["elapsed_ns"],
            body["quote_elapsed_ns"],
            failure,
            sdk_release=body["sdk_release"],
            endpoint=body["endpoint"],
        )
        self.history.append(observation)
        if failure:
            self.segment += 1
            self._needs_connection = True
        return observation

    def read(self, security: str, *, at: datetime, batch_id: str):
        observation = next((o for o in self.history if o.batch_id == batch_id), None)
        require(observation is not None, "batch_exact_not_retained")
        return self.candidate.read(
            security, at=at, acquisition=observation.record.acquisition
        )


@contextmanager
def _open_driver(path: Path):
    """Open every component without following links, including parent directories."""
    require(".." not in path.parts, "driver_path_untrusted")
    path = path.absolute()
    with ExitStack() as stack:
        directory = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        stack.callback(os.close, directory)
        for part in path.parts[1:-1]:
            directory = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=directory,
            )
            stack.callback(os.close, directory)
        fd = os.open(
            path.name,
            os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC,
            dir_fd=directory,
        )
        stack.callback(os.close, fd)
        yield fd


def _driver_file_identity(fd):
    info = os.fstat(fd)
    require(
        stat.S_ISREG(info.st_mode)
        and info.st_mode & 0o111
        and 0 < info.st_size <= 64 * 1024 * 1024,
        "driver_file_untrusted",
    )
    return (
        info.st_dev,
        info.st_ino,
        info.st_mode,
        info.st_size,
        info.st_mtime_ns,
        info.st_ctime_ns,
    )


@contextmanager
def verified_driver(path: Path, release: str):
    require(release in DRIVER_BINARY_SHA256, "driver_sdk_release_invalid")
    with verified_artifact(path, DRIVER_BINARY_SHA256[release]) as executable:
        yield executable


@contextmanager
def verified_artifact(path: Path, expected_digest: str):
    """Linux only: hash and execute the same sealed snapshot; never probe a child.

    Sidecars confer no authority. No path-based execution or unsealed fallback is
    permitted, even if the source is replaced or written after this check.
    """
    try:
        with ExitStack() as stack:
            source = stack.enter_context(_open_driver(path))
            identity = _driver_file_identity(source)
            snapshot = os.memfd_create(
                "mdf-tdx-verified", os.MFD_CLOEXEC | os.MFD_ALLOW_SEALING
            )
            stream = stack.enter_context(os.fdopen(snapshot, "w+b"))
            remaining = identity[3]
            while remaining:
                chunk = os.read(source, min(remaining, 1024 * 1024))
                require(bool(chunk), "driver_path_changed")
                stream.write(chunk)
                remaining -= len(chunk)
            require(not os.read(source, 1), "driver_path_changed")
            stream.flush()
            os.fchmod(snapshot, 0o500)
            fcntl.fcntl(snapshot, F_ADD_SEALS, DRIVER_SEALS)
            require(
                fcntl.fcntl(snapshot, F_GET_SEALS) & DRIVER_SEALS == DRIVER_SEALS,
                "driver_snapshot_unsealed",
            )
            stream.seek(0)
            digest = sha256()
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
            require(
                digest.hexdigest() == expected_digest,
                "driver_binary_release_mismatch",
            )
            require(_driver_file_identity(source) == identity, "driver_path_changed")
            # Re-walk the supplied path to detect a rename/replacement during
            # verification. Later races cannot alter the sealed executable.
            with _open_driver(path) as current:
                require(
                    _driver_file_identity(current) == identity, "driver_path_changed"
                )
                # Some filesystems have coarse timestamps. Detect same-size
                # rewrites too; this bounded read is not the execution source.
                with os.fdopen(current, "rb", closefd=False) as current_stream:
                    require(
                        sha256(current_stream.read(identity[3] + 1)).digest()
                        == digest.digest(),
                        "driver_path_changed",
                    )
            yield f"/proc/self/fd/{snapshot}", snapshot
    except ContractError:
        raise
    except OSError, ValueError, AttributeError:
        raise ContractError("driver_identity_untrusted") from None


class FramedProcess:
    """One child, bounded framing and interruptible pipe deadlines (Linux/POSIX)."""

    def __init__(self, command: list[str], config: SessionConfig):
        with ExitStack() as stack:
            inherited = ()
            if any(
                arg in {"--live", "-live"} or arg.startswith(("--live=", "-live="))
                for arg in command[1:]
            ):
                require(
                    len(command) == 2 and command[1] == "--live",
                    "driver_command_untrusted",
                )
                executable, fd = stack.enter_context(
                    verified_driver(Path(command[0]), config.driver_sdk_release)
                )
                command = [executable, "--live"]
                inherited = (fd,)
            self.process = subprocess.Popen(
                command,
                pass_fds=inherited,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        self._closed = False
        for stream in (self.process.stdin, self.process.stdout):
            os.set_blocking(stream.fileno(), False)
        try:
            # Parent-only identity policy: preserve the frozen Go K-frame schema
            # so the untouched legacy binary remains usable for rollback.
            body = asdict(config)
            body.pop("driver_sdk_release")
            self.send(b"K", body, deadline=time.monotonic() + 2)
        except BaseException:
            self.close()
            raise

    def _transfer(self, stream, *, size=0, wire=None, deadline):
        buffer = bytearray()
        sent = 0
        with selectors.DefaultSelector() as selector:
            selector.register(
                stream, selectors.EVENT_READ if wire is None else selectors.EVENT_WRITE
            )
            while len(buffer) < size if wire is None else sent < len(wire):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    raise TimeoutError("ipc_timeout")
                try:
                    if wire is None:
                        part = os.read(stream.fileno(), size - len(buffer))
                        if not part:
                            raise ContractError("driver_disconnected")
                        buffer.extend(part)
                    else:
                        sent += os.write(stream.fileno(), wire[sent:])
                except BlockingIOError:
                    continue
        return bytes(buffer)

    def receive(self, *, deadline: float) -> tuple[str, bytes]:
        header = self._transfer(self.process.stdout, size=5, deadline=deadline)
        kind, size = chr(header[0]), struct.unpack(">I", header[1:])[0]
        require(kind in {"C", "B", "E"} and 0 < size <= MAX_FRAME, "ipc_frame_invalid")
        return kind, self._transfer(self.process.stdout, size=size, deadline=deadline)

    def send(self, kind: bytes, body: dict, *, deadline: float):
        wire = json.dumps(body, allow_nan=False, separators=(",", ":")).encode()
        require(0 < len(wire) <= 16_384, "parent_frame_bound")
        self._transfer(
            self.process.stdin,
            wire=kind + struct.pack(">I", len(wire)) + wire,
            deadline=deadline,
        )

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.process.stdin.close()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=1)
        self.process.stdout.close()


class RealPeriodicWatchlistSession:
    """Serial Go cadence, Python exact-batch Read barrier; ACK never on rejection."""

    def __init__(self, config: SessionConfig, *, offline_fixture_mode=False):
        self.config = config
        self.ingress = WatchlistIngress(
            config, offline_fixture_mode=offline_fixture_mode
        )
        self.rounds: list[dict] = []
        self.events: list[dict] = []
        self.connections: list[dict] = []
        self._used = False
        self.fixture = Fixture(
            f"tdx-watchlist-{config.session_id}",
            Capability.QUOTE_SNAPSHOT,
            config.securities,
            None,
            None,
            len(config.securities),
            {"source_event_clock": "UNKNOWN", "units": "integer_li"},
        )

    def consume(self, wire: bytes, *, known_at=None, read_at=None) -> dict:
        require(self.ingress.offline or read_at is None, "real_read_override_forbidden")
        observation = self.ingress.ingest(wire, known_at=known_at)
        at = read_at or datetime.now(UTC)
        views = tuple(
            self.ingress.read(m, at=at, batch_id=observation.batch_id)
            for m in self.config.securities
        )
        expected = "unavailable" if observation.source_failure else "observed"
        require(
            observation.record.outcome in {"sample", "duplicate", "failed"}
            and (observation.source_failure or observation.record.outcome != "failed")
            and all(view.status == expected for view in views),
            "read_quality_rejected",
        )
        record = observation.record
        self.rounds.append(
            {
                "round": len(self.rounds) + 1,
                "batch_id": observation.batch_id,
                "endpoint": observation.endpoint,
                "sdk_release": observation.sdk_release,
                "quote_elapsed_ns": observation.quote_elapsed_ns,
                "connection_peer": self.ingress.connection["connection"]["peer"],
                "segment": record.segment,
                "input_origin": observation.input_origin,
                "clock_provenance": observation.clock_provenance,
                "known_provenance": observation.known_provenance,
                "sha256": observation.envelope_sha256,
                "byte_count": len(wire),
                "outcome": record.outcome,
                "ack_sent": False,
                "go_ack_received": False,
                "classification": observation.classification,
                "clocks": {
                    k: getattr(record.clocks, k).isoformat()
                    for k in ("requested", "received", "frozen", "known")
                },
                "read_at": at.isoformat(),
                "local_known_age_seconds": (at - record.clocks.known).total_seconds(),
                "reads": [
                    {
                        "security": m,
                        "status": v.status,
                        "stale": v.stale,
                        "local_received_age_seconds": None
                        if v.age is None
                        else v.age.total_seconds(),
                        "close_cny": None
                        if v.state.quote is None
                        else str(v.state.quote.close_cny),
                    }
                    for m, v in zip(self.config.securities, views, strict=True)
                ],
                "source_event_clock": "UNKNOWN",
                "source_freshness": "UNKNOWN",
            }
        )
        return {
            "session_id": self.config.session_id,
            "round": len(self.rounds),
            "segment": record.segment,
            "sha256": observation.envelope_sha256,
            "known": record.clocks.known.isoformat(),
            "reads": [at.isoformat() for _ in views],
            "accepted": True,
        }

    def run(
        self, adapter, *, archive=None, fixture_clock=None, on_read_ready=None
    ) -> dict:
        reason = "driver_error"
        candidates = set()
        probes = set()
        terminal = False
        quote_attempts = set()
        connection_attempts = set()
        pending_connection = None
        sdk_entries = set()
        write_attempts = {}
        write_results = set()
        failed_quotes = set()

        def notify(state, classification=None):
            if on_read_ready is not None:
                on_read_ready(self, state, classification)

        try:
            require(not self._used, "session_already_used")
            require(
                self.ingress.offline or fixture_clock is None,
                "real_clock_hook_forbidden",
            )
            self._used = True
            notify("starting")
            start = (
                _utc(self.config.start_at)
                if self.config.start_at
                else datetime.now(UTC)
            )
            wait = max(0, (start - datetime.now(UTC)).total_seconds())
            require(wait <= 300, "start_wait_bound")
            deadline = time.monotonic() + wait + self.config.duration_seconds + 2
            # Event budget also bounds an incorrectly behaving child.
            for _ in range(self.config.rounds * 8 + self.config.max_probes * 3 + 32):
                kind, wire = adapter.receive(deadline=deadline)
                if kind == "B" and archive:
                    archive(f"batch-{len(self.rounds) + 1}.json", wire)
                if kind == "B":
                    notify("pending_ack")
                body = decode_json(wire)
                if kind == "E":
                    validate_event(body, self.config)
                    if body["classification"] in {"probe_ok", "probe_failed"}:
                        require(
                            body["endpoint"]
                            and body["endpoint"] not in probes
                            and len(probes) < self.config.max_probes,
                            "probe_budget",
                        )
                        require(
                            (
                                body["candidate_origin"] == "configured"
                                and body["endpoint"] in self.config.endpoints
                            )
                            or (
                                body["candidate_origin"] == "pinned_sdk_hosts"
                                and self.config.sdk_candidates
                            ),
                            "candidate_origin_invalid",
                        )
                        probes.add(body["endpoint"])
                        if body["classification"] == "probe_ok":
                            candidates.add(body["endpoint"])
                    if body["classification"] == "connection_attempt":
                        require(
                            body["endpoint"] in candidates
                            and body["attempt"] == len(connection_attempts) + 1
                            and body["attempt"] <= self.config.max_connections
                            and pending_connection is None
                            and self.ingress._needs_connection
                            and body["segment"] == self.ingress.segment
                            and body["round"] == 0
                            and (not self.rounds or self.rounds[-1]["go_ack_received"]),
                            "connection_attempt_budget",
                        )
                        connection_attempts.add(body["attempt"])
                        pending_connection = body
                    if body["classification"] == "connection_failed":
                        require(
                            pending_connection is not None
                            and all(
                                body[k] == pending_connection[k]
                                for k in ("attempt", "endpoint", "segment")
                            ),
                            "connection_failure_mismatch",
                        )
                        pending_connection = None
                    if body["classification"] == "quote_attempt":
                        require(
                            body["round"] == len(quote_attempts) + 1
                            and body["round"] == len(self.rounds) + 1
                            and body["round"] <= self.config.rounds
                            and self.ingress.connection is not None
                            and not self.ingress._needs_connection
                            and body["endpoint"]
                            in {"", self.ingress.connection["endpoint"]}
                            and body["segment"] == self.ingress.segment
                            and (not self.rounds or self.rounds[-1]["go_ack_received"]),
                            "quote_attempt_budget",
                        )
                        quote_attempts.add(body["round"])
                    if body["classification"] in {
                        "sdk_getquote_entered",
                        "protocol_write_attempt",
                        "protocol_write_complete",
                        "protocol_write_partial",
                        "protocol_write_failed",
                        "quote_failed",
                    }:
                        n = body["round"]
                        require(
                            n in quote_attempts
                            and n == len(self.rounds) + 1
                            and body["segment"] == self.ingress.segment
                            and self.ingress.connection is not None
                            and not self.ingress._needs_connection
                            and body["endpoint"]
                            in {"", self.ingress.connection["endpoint"]},
                            "quote_event_mismatch",
                        )
                        cls = body["classification"]
                        if cls == "sdk_getquote_entered":
                            require(n not in sdk_entries, "duplicate_sdk_entry")
                            sdk_entries.add(n)
                        elif cls == "quote_failed":
                            require(n not in failed_quotes, "duplicate_quote_failure")
                            failed_quotes.add(n)
                        else:
                            requested = body.get("requested_bytes")
                            written = body.get("written_bytes", 0)
                            require(
                                n in sdk_entries
                                and type(requested) is int
                                and 0 < requested <= MAX_FRAME
                                and type(written) is int
                                and 0 <= written <= requested,
                                "protocol_write_shape",
                            )
                            if cls == "protocol_write_attempt":
                                require(
                                    n not in write_attempts and written == 0,
                                    "protocol_write_order",
                                )
                                write_attempts[n] = requested
                            else:
                                require(
                                    n not in write_results
                                    and write_attempts.get(n) == requested,
                                    "protocol_write_order",
                                )
                                require(
                                    (
                                        cls != "protocol_write_complete"
                                        or written == requested
                                    )
                                    and (
                                        cls != "protocol_write_partial"
                                        or 0 < written < requested
                                    )
                                    and (
                                        cls != "protocol_write_failed"
                                        or written in {0, requested}
                                    ),
                                    "protocol_write_result",
                                )
                                write_results.add(n)
                    if body["classification"] == "ack_received":
                        require(
                            self.rounds
                            and self.rounds[-1]["ack_sent"]
                            and not self.rounds[-1]["go_ack_received"]
                            and body["round"] == self.rounds[-1]["round"]
                            and body["segment"] == self.rounds[-1]["segment"],
                            "ack_receipt_mismatch",
                        )
                        self.rounds[-1]["go_ack_received"] = True
                        notify("accepted")
                    if body["classification"] in {"complete", "stopped"}:
                        require(
                            (
                                body["classification"] == "complete"
                                and body["reason"] == "budget_exhausted"
                                and len(self.rounds) == self.config.rounds
                                and body["round"] == self.config.rounds
                                and body["segment"] == self.rounds[-1]["segment"]
                                and all(r["go_ack_received"] for r in self.rounds)
                                and pending_connection is None
                            )
                            or (
                                body["classification"] == "stopped"
                                and body["reason"] not in {"", "budget_exhausted"}
                            ),
                            "terminal_state_mismatch",
                        )
                        reason = body["reason"]
                        terminal = True
                        self.events.append(body)
                        break
                    self.events.append(body)
                    if body["classification"] in {"connection_failed", "quote_failed"}:
                        notify("unavailable", body["reason"] or "source_error")
                elif kind == "C":
                    require(body.get("endpoint") in candidates, "unprobed_endpoint")
                    require(
                        pending_connection is not None
                        and body.get("endpoint") == pending_connection["endpoint"]
                        and body.get("segment") == pending_connection["segment"]
                        and type(body.get("connection")) is dict
                        and body["connection"].get("attempts")
                        == pending_connection["attempt"],
                        "connection_attempt_mismatch",
                    )
                    self.ingress.bind(body)
                    self.connections.append(body)
                    pending_connection = None
                else:
                    require(
                        kind == "B"
                        and body.get("round") in quote_attempts
                        and body.get("segment") == self.ingress.segment
                        and (not self.rounds or self.rounds[-1]["go_ack_received"]),
                        "next_batch_before_ack_receipt",
                    )
                    n = body["round"]
                    require(
                        (n in failed_quotes) == (body.get("failure") is not None),
                        "quote_failure_mismatch",
                    )
                    if body.get("failure") is None:
                        require(
                            n in sdk_entries
                            and n in write_results
                            and any(
                                e["classification"] == "protocol_write_complete"
                                and e["round"] == n
                                for e in self.events
                            ),
                            "quote_success_without_write",
                        )
                    now = fixture_clock() if fixture_clock else None
                    ack = self.consume(wire, known_at=now, read_at=now)
                    adapter.send(
                        b"A",
                        ack,
                        deadline=min(
                            deadline, time.monotonic() + self.config.ack_timeout_seconds
                        ),
                    )
                    self.rounds[-1]["ack_sent"] = True
            require(terminal, "event_budget_exhausted")
            require(
                reason != "budget_exhausted"
                or (
                    len(self.rounds) == self.config.rounds
                    and all(r["go_ack_received"] for r in self.rounds)
                ),
                "premature_completion",
            )
        except TimeoutError:
            reason = "ipc_timeout"
        except ContractError as exc:
            reason = str(exc)
        except Exception:
            reason = "runtime_error"
        finally:
            try:
                adapter.close()
            except Exception:
                reason = "runtime_error"
        child = getattr(adapter, "process", None)
        child_exit = child.returncode if child is not None else None
        if reason == "budget_exhausted" and child_exit not in (None, 0):
            reason = "child_exit_nonzero"
        notify("ended" if reason == "budget_exhausted" else "unavailable", reason)
        return {
            "child_exit": child_exit,
            "scope": "bounded_nonproduction_research",
            "provider": "tdx_go_pinned",
            "sdk_release": self.config.driver_sdk_release,
            "session_complete": reason == "budget_exhausted",
            "capability": self.fixture.capability,
            "fixture_id": self.fixture.fixture_id,
            "session_id": self.config.session_id,
            "config": asdict(self.config),
            "input_origin": OFFLINE_ORIGIN if self.ingress.offline else REAL_ORIGIN,
            "clock_provenance": OFFLINE_ORIGIN if self.ingress.offline else REAL_CLOCKS,
            "stop_reason": reason,
            "health": "readable_snapshot"
            if self.rounds and self.rounds[-1]["outcome"] in {"sample", "duplicate"}
            else "unavailable",
            "rounds": self.rounds,
            "events": self.events,
            "connections": self.connections,
            "tcp_probe_result_events": len(probes),
            "connection_attempt_events": len(connection_attempts),
            "quote_sampling_attempts": len(quote_attempts),
            "observed_sdk_getquote_entries": len(sdk_entries),
            "observed_quote_protocol_write_attempts": len(write_attempts),
            "observed_quote_protocol_write_results": {
                cls: sum(e["classification"] == cls for e in self.events)
                for cls in (
                    "protocol_write_complete",
                    "protocol_write_partial",
                    "protocol_write_failed",
                )
            },
            "go_ack_receipts": sum(r["go_ack_received"] for r in self.rounds),
            "intraday_active_refresh": "NOT_PROVEN",
            "source_event_clock": "UNKNOWN",
            "source_freshness": "UNKNOWN",
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Host opt-in; contacts TDX")
    parser.add_argument("--driver", type=Path, required=True)
    parser.add_argument("--driver-sdk-release", choices=DRIVER_SDK_RELEASES)
    parser.add_argument("--config", type=Path, help="JSON SessionConfig values")
    parser.add_argument(
        "--watchlist", help="comma-separated sh6/sz0/sz3 equity symbols"
    )
    parser.add_argument("--endpoints", help="comma-separated host:port candidates")
    parser.add_argument("--period", type=float)
    parser.add_argument("--rounds", type=int)
    parser.add_argument("--duration", type=float)
    parser.add_argument("--output", type=Path, help="new directory beneath artifacts/")
    args = parser.parse_args()
    require(args.live, "explicit_live_opt_in_required")
    values = decode_json(args.config.read_bytes()) if args.config else {}
    if args.driver_sdk_release is not None:
        values["driver_sdk_release"] = args.driver_sdk_release
    for arg, key in (("watchlist", "securities"), ("endpoints", "endpoints")):
        if getattr(args, arg) is not None:
            values[key] = tuple(getattr(args, arg).split(","))
        elif key in values:
            values[key] = tuple(values[key])
    for arg, key in (
        ("period", "period_seconds"),
        ("rounds", "rounds"),
        ("duration", "duration_seconds"),
    ):
        if getattr(args, arg) is not None:
            values[key] = getattr(args, arg)
    config = replace(SessionConfig(**values), session_id=uuid.uuid4().hex)
    if config.start_at:
        require(
            -1 <= (_utc(config.start_at) - datetime.now(UTC)).total_seconds() <= 300,
            "start_wait_bound",
        )
    root = Path.cwd() / "artifacts"
    output = args.output or root / f"tdx-mvp-{config.session_id}"
    require(
        output.resolve().is_relative_to(root.resolve()) and output != root,
        "private_artifact_path_required",
    )
    output.mkdir(parents=True, mode=0o700, exist_ok=False)

    def archive(name, data):
        with (output / name).open("xb") as stream:
            os.chmod(stream.fileno(), 0o600)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())

    archive("config.json", json.dumps(asdict(config)).encode())
    session = RealPeriodicWatchlistSession(config)
    from market_data_foundation.runtime.watchlist_receipt import AcceptanceReceiptWriter

    receipt_writer = AcceptanceReceiptWriter(output)
    adapter = FramedProcess([str(args.driver), "--live"], config)
    report = session.run(adapter, archive=archive, on_read_ready=receipt_writer)
    archive("report.json", json.dumps(report, indent=2).encode())
    print(
        json.dumps(
            {
                "stop_reason": report["stop_reason"],
                "read_batches": len(report["rounds"]),
                "health": report["health"],
                "artifacts": str(output),
            }
        )
    )
    return (
        0
        if (
            report["stop_reason"] == "budget_exhausted"
            and report["health"] == "readable_snapshot"
        )
        else 1
    )


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ContractError as exc:
        raise SystemExit(str(exc)) from None
    except Exception:
        raise SystemExit("launch_rejected") from None
