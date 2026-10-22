"""Offline build guards; fake inputs must fail before Go can be launched."""

import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path

import pytest

from scripts import build_tdx_driver as builder
from scripts.build_tdx_driver import REVISIONS, validate_checkout


@pytest.mark.parametrize("version", REVISIONS)
@pytest.mark.parametrize("bad_ref", ["HEAD", "tag", "dirty"])
def test_sdk_pin_rejects_wrong_head_tag_and_tracked_edits(
    monkeypatch, version, bad_ref
):
    def git(args, **kwargs):
        if "diff" in args:
            return b"tracked edits" if bad_ref == "dirty" else b""
        ref = "HEAD" if args[-1] == "HEAD" else "tag"
        return "0" * 40 if ref == bad_ref else REVISIONS[version]

    monkeypatch.setattr(subprocess, "check_output", git)
    with pytest.raises(ValueError, match="SDK revision/tag|Tracked SDK"):
        validate_checkout(Path("/unused-sdk"), version)


@pytest.mark.parametrize("existing", ["binary", "report", "symlink"])
@pytest.mark.parametrize("profile", [[], ["--i4-candidate"]])
def test_builder_refuses_to_clobber_rollback_material(tmp_path, existing, profile):
    output = tmp_path / "tdx-driver"
    report = output.with_suffix(".build.json")
    original = b"immutable rollback bytes"
    protected = report if existing == "report" else output
    if existing == "symlink":
        protected.symlink_to(tmp_path / "missing-target")
    else:
        protected.write_bytes(original)
    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_tdx_driver.py",
            "--sdk-checkout",
            str(tmp_path / "missing-sdk"),
            "--go",
            str(tmp_path / "must-not-run-go"),
            "--module-cache",
            str(tmp_path),
            "--output",
            str(output),
            *profile,
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode != 0
    if existing == "symlink":
        assert protected.is_symlink() and not protected.exists()
    else:
        assert protected.read_bytes() == original
    assert not (output if existing == "report" else report).exists()


@pytest.mark.parametrize(
    "extra",
    [
        ["--i4-candidate", "--sdk-version", "v0.0.87"],
        ["--i4-candidate", "--sdk-version", "v9.9.9"],
        ["--i4-candidate=unknown"],
        ["--profile", "i4-candidate"],
    ],
)
def test_invalid_profile_rejected_before_sdk_go_or_output(tmp_path, extra):
    output = tmp_path / "must-not-create" / "driver"
    result = subprocess.run(
        [
            sys.executable,
            "scripts/build_tdx_driver.py",
            "--sdk-checkout",
            str(tmp_path / "missing-sdk"),
            "--go",
            str(tmp_path / "must-not-run-go"),
            "--module-cache",
            str(tmp_path),
            "--output",
            str(output),
            *extra,
        ],
        capture_output=True,
        check=False,
    )
    assert result.returncode == 2
    assert not output.parent.exists()
    assert b"Traceback" not in result.stderr


@pytest.mark.parametrize(
    ("version", "i4", "expected_release"),
    [
        (
            "v0.0.87",
            False,
            "injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e"
            "+mdf-wait-preregister-r1",
        ),
        (
            "v1.0.0",
            False,
            "injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
            "+mdf-wait-preregister-r1",
        ),
        (
            "v1.0.0",
            True,
            "injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
            "+mdf-wait-preregister-r1+i4-raw-r1",
        ),
    ],
)
def test_builder_commands_and_receipt_match_selected_profile(
    tmp_path, monkeypatch, version, i4, expected_release
):
    sdk = tmp_path / "sdk"
    sdk.mkdir()
    for name, content in {
        "client.go": "synthetic input",
        "go.mod": "module github.com/injoyai/tdx\n\ngo 1.25.0\n",
        "go.sum": "",
    }.items():
        (sdk / name).write_text(content)
    monkeypatch.setattr(
        builder, "CLIENT_SHA256", sha256(b"synthetic input").hexdigest()
    )
    monkeypatch.setattr(
        builder, "DERIVED_CLIENT_SHA256", sha256(b"synthetic derived").hexdigest()
    )
    output = tmp_path / "candidate"
    go = str(tmp_path / "synthetic-go")
    argv = [
        "build_tdx_driver.py",
        "--sdk-checkout",
        str(sdk),
        "--sdk-version",
        version,
        "--go",
        go,
        "--module-cache",
        str(tmp_path / "modules"),
        "--output",
        str(output),
        "--race",
    ]
    if i4:
        argv.append("--i4-candidate")
    monkeypatch.setattr(sys, "argv", argv)
    commands = []

    def check_output(command, **kwargs):
        if command[:2] == [go, "version"]:
            return "go version go1.25.3 linux/amd64\n"
        if command[-1] == "--version":
            return expected_release + "\n"
        if command[0] == "git":
            if "rev-parse" in command:
                return REVISIONS[version]
            if "diff" in command:
                return b""
            if "ls-files" in command:
                return b"client.go\0go.mod\0go.sum\0"
        pytest.fail(f"Unexpected check_output: {command}")

    def run(command, *, cwd, **kwargs):
        if command[0] == "git":
            if "--check" not in command:
                (cwd / "client.go").write_text("synthetic derived")
            return
        assert command[0] == go
        env = kwargs["env"]
        assert {key: env[key] for key in ("GOPROXY", "GOSUMDB", "GOTOOLCHAIN")} == {
            "GOPROXY": "off",
            "GOSUMDB": "off",
            "GOTOOLCHAIN": "local",
        }
        assert env["GOFLAGS"] == "" and env["GOWORK"] == "off"
        assert env["MDF_TDX_TEST_SDK_VERSION"] == version
        commands.append(command)
        if command[1] == "build":
            Path(command[command.index("-o") + 1]).write_bytes(b"synthetic binary")

    monkeypatch.setattr(subprocess, "check_output", check_output)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("GOFLAGS", "-tags=mdf_i4")
    builder.main()
    assert [command[1] for command in commands] == ["test", "test", "build"]
    assert [command[-1] for command in commands] == ["./protocol", "./...", "."]
    for command in commands:
        assert [flag for flag in command if flag.startswith("-tags")] == (
            ["-tags=mdf_i4"] if i4 else []
        )
        assert "-mod=readonly" in command
        assert command[command.index("-ldflags") + 1] == (
            f"-X 'main.release={expected_release}'"
        )
        if command[1] == "test":
            assert "-race" in command
        else:
            assert "-trimpath" in command and "-buildvcs=false" in command
    report = json.loads(output.with_suffix(".build.json").read_text())
    assert report["state"] == "built" and report["tests"] == "passed"
    assert report["build_profile"] == ("i4-candidate" if i4 else "default")
    assert report["go_tags"] == (["mdf_i4"] if i4 else [])
    assert report["sdk_release"] == expected_release
    assert report["binary_path"] == str(output)
    assert report["binary_size_bytes"] == output.stat().st_size
    assert report["binary_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert report["runtime_admission"] == "not_reviewed"
