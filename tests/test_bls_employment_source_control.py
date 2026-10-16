"""Offline boundary fixtures. Synthetic evidence cannot authorize effects."""

import hashlib
import json
import os
import socket
import subprocess
from dataclasses import replace

import psycopg
import pytest

from market_data_foundation.historical import bls_employment_live as live
from market_data_foundation.historical import bls_employment_source_control as control


@pytest.fixture(autouse=True)
def no_effects(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Source control crossed zero-effect boundary")

    for name in ("getaddrinfo",):
        monkeypatch.setattr(socket, name, forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    for name in (
        "_load_key",
        "_supervise",
        "_write_pending",
        "_write_attempt_record",
        "_write_artifacts",
        "uuid4",
        "EmploymentHttpsTransport",
    ):
        monkeypatch.setattr(live, name, forbidden)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    """Test-only replacement pins; public API has no pin or verifier parameter.

    File reads are real. Git protocol output is simulated here; the owner-local
    EO readback additionally exercises the untouched real historical Git objects.
    """
    repo = tmp_path / "repo"
    evidence = tmp_path / "evidence"
    repo.mkdir()
    evidence.mkdir()
    files = []
    commands = {
        ("rev-parse", "HEAD"): (control.HEAD + "\n").encode(),
        ("rev-parse", "HEAD^{tree}"): (control.TREE + "\n").encode(),
        ("rev-parse", "HEAD^"): (control.PARENT + "\n").encode(),
        ("branch", "--show-current"): (control.BRANCH + "\n").encode(),
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ): b"",
        ("ls-files", "-v", "-z"): b"H tracked-file\0",
    }
    for index, original in enumerate(control._PRODUCT):
        raw = f"offline Employment fixture {index}\n".encode()
        row = replace(
            original,
            size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
            blob=hashlib.sha1(
                b"blob " + str(len(raw)).encode() + b"\0" + raw
            ).hexdigest(),
        )
        path = repo / row.path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
        files.append(row)
        commands[("ls-tree", control.HEAD, "--", row.path)] = (
            f"100644 blob {row.blob}\t{row.path}\n".encode()
        )
        commands[("cat-file", "blob", row.blob)] = raw
    manifest = {
        "base": control.PARENT,
        "branch": control.BRANCH,
        "product_revision": "T03_EMPLOYMENT_LIVE_EDGE_CORRECTION_002",
        "product_files": [
            {"path": row.path, "bytes": row.size, "sha256": row.sha256} for row in files
        ],
    }
    anchors = []
    for name, _, _ in control._EVIDENCE:
        raw = (
            json.dumps(manifest).encode()
            if name == control.MANIFEST
            else b"OFFLINE FIXTURE ONLY: no verdict or grant\n"
        )
        (evidence / name).write_bytes(raw)
        anchors.append((name, len(raw), hashlib.sha256(raw).hexdigest()))

    def git(repository, *args):
        assert repository == repo
        return commands[args]

    monkeypatch.setattr(control, "_PRODUCT", tuple(files))
    monkeypatch.setattr(control, "_EVIDENCE", tuple(anchors))
    monkeypatch.setattr(control, "_git", git)
    monkeypatch.setattr(control, "REPOSITORY", repo)
    monkeypatch.setattr(control, "CONTROL", evidence)
    return repo, evidence, commands


def denied(expected, operation=control.inspect_committed_source):
    with pytest.raises(control.SourceAdmissionError) as failure:
        operation()
    assert failure.value.classification == expected
    assert failure.value.safe_observation() == {
        "classification": expected,
        "source_qualified": False,
        "provider_requests": 0,
        "attempt_created": False,
        "retry_allowed": False,
    }


@pytest.mark.parametrize("optional_locks", [None, "1"])
@pytest.mark.parametrize(
    "args",
    [
        ("rev-parse", "HEAD"),
        ("rev-parse", "HEAD^{tree}"),
        ("rev-parse", "HEAD^"),
        ("branch", "--show-current"),
        (
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--ignore-submodules=none",
        ),
        ("ls-files", "-v", "-z"),
        ("ls-tree", control.HEAD, "--", control._PRODUCT[0].path),
        ("cat-file", "blob", control._PRODUCT[0].blob),
    ],
)
def test_git_introspection_disables_optional_locks_without_launcher(
    tmp_path, monkeypatch, args, optional_locks
):
    if optional_locks is None:
        monkeypatch.delenv("GIT_OPTIONAL_LOCKS", raising=False)
    else:
        monkeypatch.setenv("GIT_OPTIONAL_LOCKS", optional_locks)
    calls = []

    def capture(argv, **kwargs):
        calls.append((argv, kwargs, os.environ.get("GIT_OPTIONAL_LOCKS")))
        return b"exact git output\0"

    monkeypatch.setattr(subprocess, "check_output", capture)
    assert control._git(tmp_path, *args) == b"exact git output\0"
    assert calls == [
        (
            [
                "git",
                "--no-optional-locks",
                "--no-replace-objects",
                "-c",
                "core.fsmonitor=false",
                *args,
            ],
            {"cwd": tmp_path, "stderr": subprocess.DEVNULL, "timeout": 2},
            optional_locks,
        )
    ]


def test_committed_clean_evidence_positive_never_qualifies_or_grants(rig):
    result = control.inspect_committed_source()
    assert result.head == control.HEAD
    assert result.tree == control.TREE
    assert result.product_blobs == tuple((r.path, r.blob) for r in control._PRODUCT)
    assert result.source_qualified is False
    assert result.execution_enabled is False
    denied("named_source_only_owner_opt_in_required", control.execute_source_once)


@pytest.mark.parametrize(
    "command,value",
    [
        (("rev-parse", "HEAD"), control.PARENT.encode()),
        (("rev-parse", "HEAD"), b"0" * 40),
        (("rev-parse", "HEAD^{tree}"), b"1" * 40),
        (("rev-parse", "HEAD^"), b"2" * 40),
        (("branch", "--show-current"), b"main\n"),
        (("branch", "--show-current"), b""),
    ],
)
def test_stale_commit_wrong_sha_tree_parent_branch(rig, command, value):
    rig[2][command] = value
    denied("commit_tree_or_branch_mismatch")


@pytest.mark.parametrize("dirty", [b" M src/extra.py\0", b"?? extra\0", b"A  staged\0"])
def test_full_dirty_set_rejected_before_any_effect(rig, dirty):
    key = next(k for k in rig[2] if k[0] == "status")
    rig[2][key] = dirty
    denied("clean_complete_worktree_required")


@pytest.mark.parametrize("flag", [b"S", b"h", b"s"])
def test_hidden_index_changes_not_a_clean_admission(rig, flag):
    rig[2][("ls-files", "-v", "-z")] = flag + b" tracked-file\0"
    denied("clean_complete_worktree_required")


@pytest.mark.parametrize("index", range(7))
def test_wrong_historical_reviewer_plan_manifest_or_postflight_bytes(rig, index):
    name, _, _ = control._EVIDENCE[index]
    (rig[1] / name).write_bytes(b"forged or stale external evidence")
    denied("evidence_or_source_drift")


@pytest.mark.parametrize("index", range(3))
def test_each_working_source_blob_must_match_reviewed_bytes(rig, index):
    (rig[0] / control._PRODUCT[index].path).write_bytes(b"drift")
    denied("evidence_or_source_drift")


def test_git_blob_sha_and_mode_must_match(rig):
    row = control._PRODUCT[0]
    rig[2][("ls-tree", control.HEAD, "--", row.path)] = (
        f"100755 blob {'0' * 40}\t{row.path}\n".encode()
    )
    denied("git_blob_mismatch")


def test_git_object_contents_must_match_external_review(rig):
    row = control._PRODUCT[1]
    rig[2][("cat-file", "blob", row.blob)] = b"wrong object"
    denied("git_blob_mismatch")


@pytest.mark.parametrize("target", ["review", "source", "fifo"])
def test_no_symlinks_or_special_evidence_files(rig, tmp_path, target):
    path = rig[1] / control.REVIEW
    if target == "source":
        path = rig[0] / control._PRODUCT[0].path
    raw = path.read_bytes()
    path.unlink()
    if target == "fifo":
        os.mkfifo(path)
    else:
        other = tmp_path / "same-bytes"
        other.write_bytes(raw)
        path.symlink_to(other)
    denied("evidence_or_source_drift")


def test_readback_race_rejected(rig, monkeypatch):
    original = control._git
    counts = 0

    def concurrent(repository, *args):
        nonlocal counts
        if args == ("rev-parse", "HEAD"):
            counts += 1
            if counts == 2:
                return b"0" * 40
        return original(repository, *args)

    monkeypatch.setattr(control, "_git", concurrent)
    denied("commit_tree_or_branch_mismatch")


def test_git_failure_hides_raw_exception(rig, monkeypatch):
    def broken(*args):
        raise subprocess.TimeoutExpired("untrusted exception text", 2)

    monkeypatch.setattr(control, "_git", broken)
    denied("commit_evidence_unavailable")


@pytest.mark.parametrize(
    "grant",
    [
        False,
        True,
        1,
        "named Owner approves one POST",
        {"owner": "Odin", "provider_requests": 1, "head": control.HEAD},
        {"review_receipt": "a" * 64, "signature": "forged"},
        {"state": "unknown", "retry": True},
        {"state": "success", "attempts": 0},
    ],
)
def test_forged_grant_unknown_and_repeat_cannot_create_attempt(rig, grant):
    for _ in range(2):
        denied(
            "trusted_external_grant_verifier_unavailable",
            lambda: control.execute_source_once(owner_opt_in=grant),
        )
    assert sorted(rig[0].rglob("*"))  # source fixture exists; no runtime tree created
    assert not (rig[0] / "artifacts").exists()


@pytest.mark.parametrize(
    "key", ["head", "tree", "review_receipt", "enabled", "verifier"]
)
def test_public_entry_has_no_caller_self_authorization_inputs(key):
    with pytest.raises(TypeError):
        control.execute_source_once(**{key: "caller assertion"})


def test_even_forged_positive_evidence_is_never_an_effect_grant(rig, monkeypatch):
    result = control.inspect_committed_source()
    monkeypatch.setattr(control, "inspect_committed_source", lambda: result)
    denied(
        "trusted_external_grant_verifier_unavailable",
        lambda: control.execute_source_once(owner_opt_in=result),
    )
