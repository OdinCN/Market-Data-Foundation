"""Historical offline boundary experiment, never a runtime admission gate."""

import hashlib
import json
import multiprocessing
import multiprocessing.connection
import os
import signal
import stat
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_bea_pce_live import (
    DUMMY,
    IDENTITY,
    SyntheticTransport,
    dummy_loader,
    fabricated_response,
    forbid_external,
    no_external,  # noqa: F401 -- installs parent IO prohibition for every test
    serialize,
)

from market_data_foundation.historical import bea_pce as pce
from market_data_foundation.historical import bea_pce_boundary as boundary
from market_data_foundation.historical import bea_pce_live as edge

REVIEWER = "e" * 64  # Synthetic reviewer binding, not independent acceptance.


@pytest.fixture
def workspace(monkeypatch):
    actual = Path(__file__).resolve().parents[1]
    # Admission rejects writable ancestors, including shared /tmp. Keep these
    # disposable filesystem fixtures beneath the repository's ignored artifacts.
    artifacts = actual / "artifacts"
    artifacts.mkdir(mode=0o700, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix="t04-boundary-test-", dir=artifacts
    ) as temp:
        repo = Path(temp) / "repo"
        repo.mkdir()
        for name in boundary.CODE_PATHS:
            target = repo / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((actual / name).read_bytes())
        monkeypatch.setattr(boundary, "_head", lambda _repo: "a" * 40)
        manifest = pce.encode(
            {
                "revision": boundary.REVISION,
                "base_git_sha": "a" * 40,
                "files": [
                    {
                        "path": name,
                        "bytes": (repo / name).stat().st_size,
                        "sha256": hashlib.sha256(
                            (repo / name).read_bytes()
                        ).hexdigest(),
                    }
                    for name in boundary.CODE_PATHS
                ],
            }
        )
        yield repo, manifest


def fixture(failure=None):
    return boundary.OfflineFixture(
        serialize(fabricated_response("T20807")),
        serialize(fabricated_response("T20804")),
        failure,
    )


def slot(repo):
    return repo / "artifacts" / boundary.SLOT


def run(workspace, failure=None):
    repo, manifest = workspace
    return boundary.dry_run(repo, manifest, REVIEWER, fixture(failure))


def snapshot(path):
    return {
        p.name: (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_mode)
        for p in path.iterdir()
    }


def test_durable_intent_precedes_spawn_and_every_send(workspace, monkeypatch):
    repo, manifest = workspace
    context = multiprocessing.get_context("spawn")
    original = context.Process.start

    def start(process):
        intent = json.loads((slot(repo) / "00-intent.json").read_bytes())
        payload = intent["payload"]
        assert payload["execution_enabled"] is False
        assert payload["owner_opt_in"] is False
        assert payload["repo_commit"] == "a" * 40
        assert payload["manifest_sha256"] == hashlib.sha256(manifest).hexdigest()
        assert payload["plan_sha256"] == edge.PLAN_SHA256
        assert payload["offline_reviewer_binding_sha256"] == REVIEWER
        assert payload["effect_budget"] == {
            "provider_requests": 0,
            "pg": 0,
            "garage": 0,
        }
        assert [r["TableName"] for r in payload["requests"]] == ["T20807", "T20804"]
        assert len(intent["attempt_id"]) == 43
        original(process)

    monkeypatch.setattr(context.Process, "start", start)
    original_send = multiprocessing.connection.Connection.send_bytes
    grants = []

    def send(pipe, message, *args, **kwargs):
        if b'"deadline"' in message:
            index = len(grants) * 2 + 1
            record = json.loads((slot(repo) / f"{index:02d}-request.json").read_bytes())
            assert record["payload"]["sent"] == "unknown"
            assert record["payload"]["attempted"] is True
            grants.append(record)
        return original_send(pipe, message, *args, **kwargs)

    monkeypatch.setattr(multiprocessing.connection.Connection, "send_bytes", send)
    result = run(workspace)
    assert result["classification"] == "offline_recorded"
    assert result["candidate_retained"]
    assert len(grants) == 2
    records = sorted(slot(repo).glob("[0-9]*.json"))
    assert len(records) == 6
    for path in slot(repo).iterdir():
        assert path.stat().st_mode & 0o777 == 0o600
        assert DUMMY.encode() not in path.read_bytes()
        assert b"UserID" not in path.read_bytes()
        assert b"RequestParam" not in path.read_bytes()
    assert slot(repo).stat().st_mode & 0o777 == 0o700


def test_success_replay_is_read_only_never_source_pass(workspace, monkeypatch):
    repo, manifest = workspace
    assert run(workspace)["candidate_retained"]
    before = snapshot(slot(repo))
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    monkeypatch.setattr(boundary, "_write", forbid_external)
    result = boundary.replay(repo, manifest, REVIEWER)
    assert result["classification"] == "offline_replay_only"
    assert result["source_qualified"] is False
    assert result["real_producer_trust"] == "UNPROVED"
    assert result["outcome"]["b2"] == "B2_UNPROVED/BLOCKED"
    assert before == snapshot(slot(repo))


@pytest.mark.parametrize("failure", [None, "T20807", "T20804"])
def test_success_failure_partial_unknown_and_restart_all_consume_slot(
    workspace, monkeypatch, failure
):
    repo, manifest = workspace
    first = run(workspace, failure)
    before = snapshot(slot(repo))
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    again = boundary.dry_run(repo, manifest, REVIEWER, fixture())
    assert again["classification"] == "attempt_already_consumed"
    assert snapshot(slot(repo)) == before
    replay = boundary.replay(repo, manifest, REVIEWER)
    assert not replay["source_qualified"]
    if failure:
        count = 1 if failure == "T20807" else 2
        observations = replay["outcome"]["observations"]
        assert len(observations) == count
        assert observations[-1]["sent"] == "unknown"
        assert not observations[-1]["response_complete"]
        assert not (slot(repo) / "candidate.json").exists()
    else:
        assert len(first["attempt_id"]) == 43


def test_crash_after_durable_intent_does_not_reset_on_restart(workspace, monkeypatch):
    class OfflineCrash(BaseException):
        pass

    repo, manifest = workspace

    def crash(*args, **kwargs):
        raise OfflineCrash()

    monkeypatch.setattr(edge, "_supervise", crash)
    with pytest.raises(OfflineCrash):
        run(workspace)
    assert [p.name for p in slot(repo).iterdir()] == ["00-intent.json"]
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    assert run(workspace)["classification"] == "attempt_already_consumed"
    with pytest.raises(boundary.BoundaryRejected, match="incomplete_unknown"):
        boundary.replay(repo, manifest, REVIEWER)


@pytest.mark.parametrize("stage", ["intent_fsync", "request", "candidate", "outcome"])
def test_persistence_failure_is_consumed_without_raw_error_or_retry(
    workspace, monkeypatch, stage
):
    repo, manifest = workspace
    original = boundary._write

    def fail(fd, name, content):
        if stage == "intent_fsync" and name == "00-intent.json":
            original(fd, name, content)
            raise OSError("auth-url-" + DUMMY)
        if (
            stage == "request"
            and "request" in name
            or stage == "candidate"
            and name == "candidate.json"
            or stage == "outcome"
            and "outcome" in name
        ):
            raise OSError("auth-url-" + DUMMY)
        return original(fd, name, content)

    monkeypatch.setattr(boundary, "_write", fail)
    result = run(workspace)
    assert not result["source_qualified"]
    assert DUMMY not in str(result)
    before = snapshot(slot(repo))
    assert run(workspace)["classification"] == "attempt_already_consumed"
    assert before == snapshot(slot(repo))
    assert all(DUMMY.encode() not in v[0] for v in before.values())
    with pytest.raises((boundary.BoundaryRejected, IndexError, KeyError)):
        boundary.replay(repo, manifest, REVIEWER)


def test_append_only_exclusive_create_and_concurrent_admission(workspace):
    repo, manifest = workspace
    fd = boundary._store(repo, True)
    try:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert run(workspace)["classification"] == "attempt_already_consumed"
        boundary._write(fd, "00-intent.json", b"incomplete")
        with pytest.raises(FileExistsError):
            boundary._write(fd, "00-intent.json", b"replacement")
        assert boundary._read(fd, "00-intent.json", 100) == b"incomplete"
    finally:
        os.close(fd)
    assert run(workspace)["classification"] == "attempt_already_consumed"


@pytest.mark.parametrize("drift", ["commit", "code", "manifest", "reviewer"])
def test_binding_drift_before_admission_or_replay_stops(workspace, monkeypatch, drift):
    repo, manifest = workspace
    assert run(workspace)["candidate_retained"]
    reviewer = REVIEWER
    if drift == "commit":
        monkeypatch.setattr(boundary, "_head", lambda _: "b" * 40)
    elif drift == "code":
        (repo / boundary.CODE_PATHS[0]).write_bytes(b"changed")
    elif drift == "manifest":
        manifest += b" "
    else:
        reviewer = "d" * 64
    with pytest.raises(boundary.BoundaryRejected):
        boundary.replay(repo, manifest, reviewer)


def test_code_drift_before_second_send_never_allows_second_request(
    workspace, monkeypatch
):
    repo, manifest = workspace
    original = boundary._write

    def drift(fd, name, content):
        original(fd, name, content)
        if name == "02-response.json":
            (repo / boundary.CODE_PATHS[0]).write_bytes(b"changed-after-response")

    monkeypatch.setattr(boundary, "_write", drift)
    result = run(workspace)
    assert not result["source_qualified"]
    assert not (slot(repo) / "03-request.json").exists()
    assert not (slot(repo) / "candidate.json").exists()


@pytest.mark.parametrize(
    "attack",
    [
        "directory_symlink",
        "world_readable",
        "candidate_symlink",
        "candidate_hardlink",
        "modified_receipt",
    ],
)
def test_artifact_io_and_replay_tamper_rejection(workspace, attack, tmp_path):
    repo, manifest = workspace
    if attack == "directory_symlink":
        (repo / "artifacts").mkdir()
        slot(repo).symlink_to(tmp_path, target_is_directory=True)
        assert run(workspace)["classification"] == "attempt_already_consumed"
        with pytest.raises(OSError):
            boundary.replay(repo, manifest, REVIEWER)
        return
    assert run(workspace)["candidate_retained"]
    candidate = slot(repo) / "candidate.json"
    if attack == "world_readable":
        slot(repo).chmod(0o755)
    elif attack == "candidate_symlink":
        candidate.unlink()
        candidate.symlink_to(tmp_path / "missing")
    elif attack == "candidate_hardlink":
        os.link(candidate, repo.parent / "second-link")
    else:
        candidate.write_bytes(b'{"untrusted":"auth-url"}')
    with pytest.raises((boundary.BoundaryRejected, OSError)):
        boundary.replay(repo, manifest, REVIEWER)


@pytest.mark.parametrize("location", ["ancestor", "repo", "artifacts"])
@pytest.mark.parametrize("mode", [0o775, 0o777])
def test_writable_slot_ancestors_cannot_admit_or_move_consumed_slot(
    workspace, monkeypatch, location, mode
):
    repo, manifest = workspace
    assert run(workspace)["candidate_retained"]
    before = snapshot(slot(repo))
    parent = {"ancestor": repo.parent, "repo": repo, "artifacts": repo / "artifacts"}[
        location
    ]
    original_mode = stat.S_IMODE(parent.stat().st_mode)
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    try:
        parent.chmod(mode)
        assert run(workspace)["classification"] == "store_rejected"
        with pytest.raises(boundary.BoundaryRejected, match="store_rejected"):
            boundary.replay(repo, manifest, REVIEWER)
        # Simulate an unsafe parent allowing a third party to move the slot.
        moved = slot(repo).with_name("moved-slot")
        slot(repo).rename(moved)
        assert run(workspace)["classification"] == "store_rejected"
        assert not slot(repo).exists()
        moved.rename(slot(repo))
    finally:
        parent.chmod(original_mode)
    assert before == snapshot(slot(repo))
    assert run(workspace)["classification"] == "attempt_already_consumed"


@pytest.mark.parametrize("mode", [0o755, 0o700])
def test_trusted_ancestor_modes_allow_one_offline_attempt(workspace, mode):
    repo, manifest = workspace
    (repo / "artifacts").mkdir()
    for parent in (repo.parent, repo, repo / "artifacts"):
        parent.chmod(mode)
    assert run(workspace)["candidate_retained"]
    assert boundary.replay(repo, manifest, REVIEWER)["source_qualified"] is False
    assert run(workspace)["classification"] == "attempt_already_consumed"


@pytest.mark.parametrize("ancestor", [False, True])
def test_directory_owner_is_verified(monkeypatch, ancestor):
    monkeypatch.setattr(os, "geteuid", lambda: 1234)
    for uid in (1234, 0, 5678):
        monkeypatch.setattr(
            os,
            "fstat",
            lambda fd, uid=uid: SimpleNamespace(
                st_uid=uid, st_mode=stat.S_IFDIR | 0o755
            ),
        )
        if uid == 1234 or ancestor and uid == 0:
            boundary._trusted_directory(123, private=False, ancestor=ancestor)
        else:
            with pytest.raises(boundary.BoundaryRejected, match="store_rejected"):
                boundary._trusted_directory(123, private=False, ancestor=ancestor)


@pytest.mark.parametrize("location", ["artifacts", "ancestor"])
def test_symlink_ancestors_never_admit(workspace, monkeypatch, location):
    repo, manifest = workspace
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    if location == "artifacts":
        (repo / "artifacts").symlink_to(repo.parent, target_is_directory=True)
        assert run(workspace)["classification"] == "boundary_unknown"
    else:
        alias = repo.parent / "alias"
        alias.symlink_to(repo, target_is_directory=True)
        with pytest.raises(boundary.BoundaryRejected, match="store_rejected"):
            boundary._store(alias, True)
    assert not slot(repo).exists()


@pytest.mark.parametrize("failure", ["intent_open", "slot_fsync"])
def test_virtual_fs_empty_slot_after_failure_never_readmits(
    workspace, monkeypatch, failure
):
    """Entire store is in memory; restore IO after the first uncertain failure."""
    repo, manifest = workspace
    directories = {p: 0o755 for p in (repo, *repo.parents)}
    descriptors, attempts = {}, []
    injected = False

    def mkdir(name, mode, *, dir_fd):
        path = descriptors[dir_fd] / name
        if path in directories:
            raise FileExistsError()
        directories[path] = mode

    def open_file(name, flags, mode=0o777, *, dir_fd=None):
        nonlocal injected
        path = Path(name) if dir_fd is None else descriptors[dir_fd] / name
        if flags & os.O_CREAT:
            assert path == slot(repo) / "00-intent.json"
            attempts.append("intent_open")
            if failure == "intent_open" and not injected:
                injected = True
                raise OSError("synthetic_fs_unavailable")
            pytest.fail("a recovered empty slot must never create another intent")
        assert path in directories
        fd = len(descriptors) + 10
        descriptors[fd] = path
        return fd

    def fsync(fd):
        nonlocal injected
        if failure == "slot_fsync" and slot(repo) in directories and not injected:
            injected = True
            raise OSError("synthetic_fs_unavailable")

    def supervise(*args, **kwargs):
        attempts.append("supervisor")
        pytest.fail("unknown attempt must never start a worker")

    with monkeypatch.context() as patch:
        patch.setattr(boundary, "binding", lambda *args: {})
        patch.setattr(os, "mkdir", mkdir)
        patch.setattr(os, "open", open_file)
        patch.setattr(os, "fsync", fsync)
        patch.setattr(os, "close", lambda fd: None)
        patch.setattr(
            os,
            "fstat",
            lambda fd: SimpleNamespace(
                st_uid=os.geteuid(), st_mode=stat.S_IFDIR | directories[descriptors[fd]]
            ),
        )
        patch.setattr(boundary.fcntl, "flock", lambda *args: None)
        patch.setattr(edge, "_supervise", supervise)
        assert run(workspace)["classification"] == "boundary_unknown"
        assert injected and slot(repo) in directories
        for _ in range(2):
            assert run(workspace)["classification"] == "attempt_already_consumed"
    assert attempts == (["intent_open"] if failure == "intent_open" else [])
    assert not slot(repo).exists()  # No actual store filesystem writes.


def test_preexisting_empty_slot_is_unknown_consumed(workspace, monkeypatch):
    repo, manifest = workspace
    slot(repo).mkdir(mode=0o700, parents=True)
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    for _ in range(2):
        assert run(workspace)["classification"] == "attempt_already_consumed"
    with pytest.raises(boundary.BoundaryRejected, match="incomplete_unknown"):
        boundary.replay(repo, manifest, REVIEWER)
    assert not list(slot(repo).iterdir())


def test_first_intent_exclusive_open_failure_consumes_actual_empty_slot(
    workspace, monkeypatch
):
    repo, manifest = workspace
    original = os.open
    opens = []

    def fail(name, flags, *args, **kwargs):
        if name == "00-intent.json":
            opens.append(flags)
            assert flags & os.O_EXCL and flags & os.O_CREAT
            raise OSError("synthetic_fs_unavailable")
        return original(name, flags, *args, **kwargs)

    monkeypatch.setattr(edge, "_supervise", forbid_external)
    with monkeypatch.context() as patch:
        patch.setattr(os, "open", fail)
        assert run(workspace)["classification"] == "boundary_unknown"
    assert len(opens) == 1 and not list(slot(repo).iterdir())
    for _ in range(2):
        assert run(workspace)["classification"] == "attempt_already_consumed"
    with pytest.raises(boundary.BoundaryRejected, match="incomplete_unknown"):
        boundary.replay(repo, manifest, REVIEWER)


def test_replay_missing_slot_never_creates_it(workspace):
    repo, manifest = workspace
    with pytest.raises(FileNotFoundError):
        boundary.replay(repo, manifest, REVIEWER)
    assert not (repo / "artifacts").exists()


def test_non_fixture_cannot_inject_live_loader_or_transport(workspace, monkeypatch):
    repo, manifest = workspace
    monkeypatch.setattr(edge, "_load_key", forbid_external)
    monkeypatch.setattr(edge, "_supervise", forbid_external)
    for transport in (edge._HttpsTransport(), SyntheticTransport(), None):
        assert (
            boundary.dry_run(repo, manifest, REVIEWER, transport)["classification"]
            == "offline_fixture_required"
        )
    assert not (repo / "artifacts").exists()


def test_current_git_commit_is_read_without_external_effects():
    actual = Path(__file__).resolve().parents[1]
    assert len(boundary._head(actual)) == 40


@pytest.mark.parametrize("operation", ["start", "cleanup_join", "pipe_send", "clock"])
def test_b2_unbounded_lifecycle_operations_exceed_laboratory_deadline(
    monkeypatch, operation
):
    """Counterexample at reduced scale, not a fabricated full 30-second PASS.

    A finite slow host operation makes the private timer expire yet prevents
    timely return. No real HTTP, indefinite stuck process or orphan is needed.
    """
    context = multiprocessing.get_context("spawn")
    delay, threshold = 0.18, 0.03
    if operation in {"start", "cleanup_join"}:
        name = "start" if operation == "start" else "join"
        original = getattr(context.Process, name)

        def delayed(process, *args, **kwargs):
            time.sleep(delay)
            return original(process, *args, **kwargs)

        monkeypatch.setattr(context.Process, name, delayed)
    elif operation == "pipe_send":
        original = multiprocessing.connection.Connection.send_bytes

        def delayed(pipe, *args, **kwargs):
            time.sleep(delay)
            return original(pipe, *args, **kwargs)

        monkeypatch.setattr(
            multiprocessing.connection.Connection, "send_bytes", delayed
        )
        threshold = 0.6  # Allow spawn before the deliberately stalled pipe.
        delay = 0.7
    else:
        original = edge.time.monotonic

        def delayed():
            time.sleep(delay)
            return original()

        monkeypatch.setattr(edge.time, "monotonic", delayed)
    began = time.perf_counter()
    result = edge._supervise(
        IDENTITY,
        SyntheticTransport(),
        dummy_loader,
        request_seconds=0.02,
        network_seconds=0.04,
        attempt_seconds=threshold,
    )
    elapsed = time.perf_counter() - began
    assert elapsed > threshold
    assert result.projection is result.receipt is None
    assert not multiprocessing.active_children()


def ignore_term_loader():
    dummy_loader()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.01)


def test_b2_blocking_child_requires_kill_and_reap_not_idle_timeout():
    result = edge._supervise(
        IDENTITY, SyntheticTransport(), ignore_term_loader, attempt_seconds=0.4
    )
    assert result.classification == "deadline_unknown"
    assert result.projection is result.receipt is None
    assert not multiprocessing.active_children()


def test_b2_persistent_observation_latency_is_inside_unproved_lifecycle(
    workspace, monkeypatch
):
    original = boundary._write

    def slow(fd, name, content):
        time.sleep(0.05)
        return original(fd, name, content)

    monkeypatch.setattr(boundary, "_write", slow)
    start = time.perf_counter()
    assert run(workspace)["candidate_retained"]
    assert time.perf_counter() - start >= 7 * 0.05
    repo, manifest = workspace
    result = boundary.replay(repo, manifest, REVIEWER)
    assert result["outcome"]["b2"] == "B2_UNPROVED/BLOCKED"
