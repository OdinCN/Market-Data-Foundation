"""T04 owner-local single-slot OFFLINE execution boundary.

No grant issuer, live transport, credential loader or storage adapter is exposed.
The private supervisor remains a laboratory mechanism, not a B2 hard deadline.
Owner filesystem access is the trust boundary; hashes do not authenticate an
owner, reviewer or provider. Deleting/replacing the slot is outside this API.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import re
import socket
import stat
import subprocess
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

from market_data_foundation.historical import bea_pce as pce
from market_data_foundation.historical import bea_pce_live as edge

REVISION = "t04-source-execution-boundary-r1-offline"
SLOT = "t04-source-execution-boundary-r1"
CODE_PATHS = (
    "src/market_data_foundation/historical/bea_pce.py",
    "src/market_data_foundation/historical/bea_pce_live.py",
    "src/market_data_foundation/historical/bea_pce_boundary.py",
)
FIXTURE_KEY = "offline-BEA-23456789-abcd"
PACKAGE_LIMIT = 2_800_000


class BoundaryRejected(Exception):
    """Only local constant classifications are raised; no raw errors."""


def _require(ok: bool, classification: str = "binding_rejected") -> None:
    if not ok:
        raise BoundaryRejected(classification)


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _hex(value, length=64):
    return type(value) is str and re.fullmatch(r"[0-9a-f]{%d}" % length, value)


def _head(repo: Path) -> str:
    try:
        result = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"],
                cwd=repo,
                stderr=subprocess.DEVNULL,
                timeout=1,
            )
            .decode("ascii")
            .strip()
        )
        _require(bool(_hex(result, 40)))
        return result
    except Exception:
        raise BoundaryRejected("binding_rejected") from None


def binding(repo: Path, manifest: bytes, reviewer_sha256: str) -> dict:
    """Verify current commit AND versioned code bytes, never just Git base.

    Reviewer digest is a synthetic binding assertion for offline tests, not a
    professional/security or Source verdict. Real review/grant stay absent.
    """
    _require(repo.is_absolute() and repo.resolve() == repo)
    _require(bool(_hex(reviewer_sha256)))
    document = pce.decode(manifest, max_bytes=32_768)
    _require(set(document) == {"revision", "base_git_sha", "files"})
    _require(
        document["revision"] == REVISION and document["base_git_sha"] == _head(repo)
    )
    files = document["files"]
    _require(type(files) is list and len(files) == len(CODE_PATHS))
    _require([f.get("path") for f in files] == list(CODE_PATHS))
    for entry in files:
        _require(set(entry) == {"path", "bytes", "sha256"})
        path = repo / entry["path"]
        _require(path.resolve() == path and not path.is_symlink())
        _require(type(entry["bytes"]) is int and 0 < entry["bytes"] <= 200_000)
        _require(bool(_hex(entry["sha256"])))
        content = path.read_bytes()
        _require(len(content) == entry["bytes"] and _sha(content) == entry["sha256"])
    return {
        "revision": REVISION,
        "repo_commit": document["base_git_sha"],
        "code_files": files,
        "manifest_sha256": _sha(manifest),
        "plan_sha256": edge.PLAN_SHA256,
        "offline_reviewer_binding_sha256": reviewer_sha256,
        "independent_source_review_sha256": None,
        "owner_explicit_grant_sha256": None,
        "execution_enabled": False,
        "owner_opt_in": False,
        "b2": "B2_UNPROVED/BLOCKED",
        "limits": {"request_seconds": 10, "network_seconds": 20, "attempt_seconds": 30},
        "effect_budget": {"provider_requests": 0, "pg": 0, "garage": 0},
        "synthetic_exchanges_max": 2,
        "requests": [pce.request_parameters(t) for t in pce.TABLES],
    }


def _trusted_directory(fd: int, *, private: bool, ancestor: bool = False) -> None:
    info = os.fstat(fd)
    mode = stat.S_IMODE(info.st_mode)
    _require(
        stat.S_ISDIR(info.st_mode)
        and info.st_uid in ({0, os.geteuid()} if ancestor else {os.geteuid()})
        and not mode & 0o022
        and (not private or mode == 0o700),
        "store_rejected",
    )


def _directory(
    parent: int,
    name: str,
    create: bool,
    private: bool,
    *,
    ancestor: bool = False,
    exclusive: bool = False,
) -> int:
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent)
            os.fsync(parent)
        except FileExistsError:
            # Even an empty directory may be left by failed intent creation.
            # Only the caller that created this slot may ever enter a worker.
            _require(not exclusive, "attempt_already_consumed")
    fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
    try:
        _trusted_directory(fd, private=private, ancestor=ancestor)
    except BaseException:
        os.close(fd)
        raise
    return fd


def _store(repo: Path, create: bool) -> int:
    _require(repo.is_absolute() and repo.resolve() == repo, "store_rejected")
    # Walk from / using directory descriptors. Every ancestor must be root- or
    # caller-owned and unwritable by group/other; the worktree belongs to caller.
    # No sticky-directory exception: an offline worktree under /tmp is rejected.
    root = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        _trusted_directory(root, private=False, ancestor=True)
        for index, name in enumerate(repo.parts[1:], start=1):
            child = _directory(
                root, name, False, False, ancestor=index < len(repo.parts) - 1
            )
            os.close(root)
            root = child
        artifacts = _directory(root, "artifacts", create, False)
        try:
            return _directory(artifacts, SLOT, create, True, exclusive=create)
        finally:
            os.close(artifacts)
    finally:
        os.close(root)


def _write(fd: int, name: str, content: bytes) -> None:
    """Create-only records, fsync file then directory; partial creation consumes.

    fsync/open/close and scheduling have no qualified hard upper bound. This is
    deliberately included in B2's unresolved lifecycle, not hidden as overhead.
    """
    file = os.open(
        name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=fd
    )
    try:
        view = memoryview(content)
        while view:
            written = os.write(file, view)
            _require(written > 0, "persistence_unknown")
            view = view[written:]
        os.fsync(file)
    finally:
        os.close(file)
    os.fsync(fd)


def _read(fd: int, name: str, limit: int) -> bytes:
    file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    try:
        info = os.fstat(file)
        _require(
            stat.S_ISREG(info.st_mode)
            and info.st_uid == os.geteuid()
            and stat.S_IMODE(info.st_mode) == 0o600
            and info.st_nlink == 1
            and 0 < info.st_size <= limit,
            "store_rejected",
        )
        with os.fdopen(file, "rb", closefd=False) as stream:
            value = stream.read(limit + 1)
        _require(len(value) == info.st_size, "store_rejected")
        return value
    finally:
        os.close(file)


def _no_network(*_args, **_kwargs):
    raise edge.EdgeRejected("transport_unknown")


def _fixture_loader():
    # Installed inside spawn; parent monkeypatches alone are insufficient.
    socket.socket.connect = _no_network
    socket.socket.connect_ex = _no_network
    socket.create_connection = _no_network
    socket.getaddrinfo = _no_network
    edge._load_key = _no_network
    return FIXTURE_KEY


@dataclass(frozen=True)
class OfflineFixture:
    """In-memory fabricated UTF-8 responses only; never read a credential file."""

    first: bytes
    second: bytes
    failure_table: str | None = None

    def exchange(self, table, key, deadline):
        if table == self.failure_table:
            raise edge.EdgeRejected("transport_unknown")
        return self.first if table == "T20807" else self.second


def _identity(bound: dict, attempt: str) -> edge.ExecutionIdentity:
    return edge.ExecutionIdentity(
        "offline-owner-local-boundary",
        bound["repo_commit"],
        bound["manifest_sha256"],
        attempt,
        "obs:" + attempt,
    )


def dry_run(
    repo: Path, manifest: bytes, reviewer_sha256: str, fixture: OfflineFixture
) -> dict:
    """One fixed slot, consumed BEFORE spawn/key substitute/send; never retries.

    A new attempt ID or changed manifest does not reset the slot. All incomplete,
    failed, crash, persistence-unknown and successful states consume it alike.
    """
    fd = None
    try:
        _require(type(fixture) is OfflineFixture, "offline_fixture_required")
        bound = binding(repo, manifest, reviewer_sha256)
        fd = _store(repo, True)
        # flock the directory inode: no lockfile or replay-side writes needed.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        nonce = uuid.uuid4().hex
        # Short colon-separated tokens remain unambiguous safe identifiers;
        # opaque long hex tokens can be rejected by the conservative echo scan.
        attempt = "off:" + ":".join(nonce[i : i + 4] for i in range(0, 32, 4))
        chain = []

        def append(kind, payload):
            record = pce.encode(
                {
                    "kind": kind,
                    "previous_sha256": _sha(chain[-1]) if chain else None,
                    "attempt_id": attempt,
                    "payload": payload,
                }
            )
            _write(fd, f"{len(chain):02d}-{kind}.json", record)
            chain.append(record)

        append("intent", bound)
        identity = _identity(bound, attempt)
        callback_unknown = False

        def before_send(table, requested):
            nonlocal callback_unknown
            try:
                _require(binding(repo, manifest, reviewer_sha256) == bound)
                append(
                    "request",
                    {
                        "table": table,
                        "requested_at_utc": requested,
                        "sent": "unknown",
                        "attempted": True,
                    },
                )
            except BaseException:
                callback_unknown = True
                raise

        def after_response(observation):
            nonlocal callback_unknown
            try:
                append("response", asdict(observation))
            except BaseException:
                callback_unknown = True
                raise

        result = edge._supervise(
            identity,
            fixture,
            _fixture_loader,
            before_send=before_send,
            after_response=after_response,
        )
        _require(not callback_unknown, "persistence_unknown")
        candidate_sha = None
        if result.projection is not None and result.receipt is not None:
            _require(
                len(result.observations) == 2
                and all(o.response_complete for o in result.observations)
            )
            edge.inspect_candidate(result.projection, result.receipt, expected=identity)
            _require(pce.decode(result.receipt)["execution_class"] == "synthetic")
            package = pce.encode(
                {
                    "projection": base64.b64encode(result.projection).decode("ascii"),
                    "adapter_receipt": base64.b64encode(result.receipt).decode("ascii"),
                    "producer_execution_receipt": {
                        "execution_class": "synthetic",
                        "attempt_id": attempt,
                        "binding_sha256": _sha(pce.encode(bound)),
                        "observations_sha256": _sha(b"".join(chain)),
                        "source_qualified": False,
                        "exact_original_retained": False,
                        "real_producer_trust": "UNPROVED",
                    },
                }
            )
            _require(len(package) <= PACKAGE_LIMIT)
            _write(fd, "candidate.json", package)
            candidate_sha = _sha(package)
        _require(
            result.classification in edge._FAILURES
            or result.classification == "candidate_pending_independent_readback"
        )
        append(
            "outcome",
            {
                "classification": result.classification,
                "observations": [asdict(o) for o in result.observations],
                "candidate_sha256": candidate_sha,
                "source_qualified": False,
                "b2": "B2_UNPROVED/BLOCKED",
                "automatic_retry": False,
            },
        )
        return {
            "classification": "offline_recorded",
            "attempt_id": attempt,
            "source_qualified": False,
            "candidate_retained": candidate_sha is not None,
        }
    except BoundaryRejected as exc:
        return {"classification": exc.args[0], "source_qualified": False}
    except BlockingIOError:
        return {"classification": "attempt_busy_or_consumed", "source_qualified": False}
    except Exception:
        # Includes fsync/start/cleanup errors. Never stringify, persist exception
        # args, or reinterpret a partially written intent as permission to retry.
        return {"classification": "boundary_unknown", "source_qualified": False}
    finally:
        if fd is not None:
            os.close(fd)


def replay(repo: Path, manifest: bytes, reviewer_sha256: str) -> dict:
    """Read-only local replay. No slot creation, repair, resume or acquisition."""
    bound = binding(repo, manifest, reviewer_sha256)
    fd = _store(repo, False)
    try:
        names = sorted(n for n in os.listdir(fd) if n != "candidate.json")
        _require(2 <= len(names) <= 6, "incomplete_unknown")
        chain, records = [], []
        for index, name in enumerate(names):
            _require(
                re.fullmatch(
                    r"%02d-(intent|request|response|outcome)\.json" % index, name
                )
                is not None,
                "store_rejected",
            )
            raw = _read(fd, name, 32_768)
            record = pce.decode(raw, max_bytes=32_768)
            _require(
                set(record) == {"kind", "previous_sha256", "attempt_id", "payload"}
            )
            _require(name == f"{index:02d}-{record['kind']}.json")
            _require(record["previous_sha256"] == (_sha(chain[-1]) if chain else None))
            _require(
                type(record["attempt_id"]) is str
                and re.fullmatch(
                    r"off:(?:[0-9a-f]{4}:){7}[0-9a-f]{4}", record["attempt_id"]
                )
                is not None
            )
            _require(not records or record["attempt_id"] == records[0]["attempt_id"])
            records.append(record)
            chain.append(raw)
        _require(records[0]["kind"] == "intent" and records[0]["payload"] == bound)
        _require(records[-1]["kind"] == "outcome", "incomplete_unknown")
        outcome = records[-1]["payload"]
        _require(
            set(outcome)
            == {
                "classification",
                "observations",
                "candidate_sha256",
                "source_qualified",
                "b2",
                "automatic_retry",
            }
        )
        _require(
            outcome["source_qualified"] is False and outcome["automatic_retry"] is False
        )
        _require(outcome["b2"] == "B2_UNPROVED/BLOCKED")
        _require(
            outcome["classification"] in edge._FAILURES
            or outcome["classification"] == "candidate_pending_independent_readback"
        )
        observations = outcome["observations"]
        _require(type(observations) is list and len(observations) <= 2)
        middle = records[1:-1]
        expected_kinds = []
        offset = 0
        for table, observation in zip(pce.TABLES, observations):
            _require(set(observation) == set(edge.Observation.__dataclass_fields__))
            _require(observation["table"] == table and observation["attempted"] is True)
            _require(
                observation["classification"] in edge._FAILURES
                or observation["classification"] == "response_complete"
            )
            _require(observation["sent"] in {"unknown", "acknowledged"})
            requested = observation["requested_at_utc"]
            _require(type(requested) is str and len(requested) <= 40)
            _require(
                middle[offset]["payload"]
                == {
                    "table": table,
                    "requested_at_utc": requested,
                    "sent": "unknown",
                    "attempted": True,
                }
            )
            expected_kinds.append("request")
            offset += 1
            if observation["response_complete"] is True:
                _require(observation["classification"] == "response_complete")
                _require(middle[offset]["payload"] == observation)
                expected_kinds.append("response")
                offset += 1
            else:
                _require(
                    observation["response_complete"] is False
                    and observation["obtained_at_utc"] is None
                )
        _require([r["kind"] for r in middle] == expected_kinds)
        candidate_sha = outcome["candidate_sha256"]
        if candidate_sha is None:
            _require("candidate.json" not in os.listdir(fd), "incomplete_unknown")
        else:
            _require(
                outcome["classification"] == "candidate_pending_independent_readback"
                and len(observations) == 2
                and all(o["response_complete"] for o in observations)
            )
            _require(
                [r["kind"] for r in records]
                == ["intent", "request", "response", "request", "response", "outcome"]
            )
            package_bytes = _read(fd, "candidate.json", PACKAGE_LIMIT)
            _require(_sha(package_bytes) == candidate_sha)
            package = pce.decode(package_bytes, max_bytes=PACKAGE_LIMIT)
            producer = package["producer_execution_receipt"]
            _require(
                producer
                == {
                    "execution_class": "synthetic",
                    "attempt_id": records[0]["attempt_id"],
                    "binding_sha256": _sha(pce.encode(bound)),
                    "observations_sha256": _sha(b"".join(chain[:-1])),
                    "source_qualified": False,
                    "exact_original_retained": False,
                    "real_producer_trust": "UNPROVED",
                }
            )
            edge.inspect_candidate(
                base64.b64decode(package["projection"], validate=True),
                base64.b64decode(package["adapter_receipt"], validate=True),
                expected=_identity(bound, records[0]["attempt_id"]),
            )
        return {
            "classification": "offline_replay_only",
            "outcome": outcome,
            "source_qualified": False,
            "real_producer_trust": "UNPROVED",
        }
    finally:
        os.close(fd)
