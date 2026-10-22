"""Explicit local offline binary checks; never build or contact a provider in CI."""

import json
import os
import subprocess
from hashlib import sha256
from pathlib import Path

import pytest

from market_data_foundation.candidates.tdx_intraday_v1.host import I4_RELEASE

ROOT = Path(__file__).resolve().parents[1]
PROFILES = {
    "default-v100": (
        "default",
        [],
        "injoyai/tdx v1.0.0@2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
        "+mdf-wait-preregister-r1",
    ),
    "rollback-v0087": (
        "default",
        [],
        "injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e"
        "+mdf-wait-preregister-r1",
    ),
    "i4-candidate": ("i4-candidate", ["mdf_i4"], I4_RELEASE),
}


@pytest.fixture(params=PROFILES)
def offline_binary(request):
    directory = os.environ.get("MDF_I4_OFFLINE_BUILD_DIR")
    if not directory:
        pytest.skip("Requires explicit local offline build receipts")
    binary = Path(directory).resolve() / request.param
    report = json.loads(binary.with_suffix(".build.json").read_text())
    profile, tags, release = PROFILES[request.param]
    assert report["state"] == "built" and report["tests"] == "passed"
    assert report["build_profile"] == profile
    assert report["go_tags"] == tags
    assert report["sdk_release"] == release
    assert report["runtime_admission"] == "not_reviewed"
    assert report["race_detector"] is True
    assert report["binary_path"] == str(binary)
    assert report["binary_sha256"] == sha256(binary.read_bytes()).hexdigest()
    assert report["binary_size_bytes"] == binary.stat().st_size
    assert report["driver_inputs"] == {
        file.name: sha256(file.read_bytes()).hexdigest()
        for file in sorted((ROOT / "drivers/tdx").glob("*.go"))
    }
    return binary, release


def test_offline_profile_version(offline_binary):
    binary, release = offline_binary
    result = subprocess.run(
        [str(binary), "--version"], capture_output=True, timeout=3, check=True
    )
    assert result.stdout == (release + "\n").encode()
    assert result.stderr == b""


@pytest.mark.parametrize("opt_in", [None, "", "0", "true", "01", "1 "])
def test_probe_without_exact_host_opt_in_exits_before_stdin(offline_binary, opt_in):
    binary, release = offline_binary
    env = dict(os.environ)
    env.pop("MDF_I4_ALLOW_LIVE", None)
    if opt_in is not None:
        env["MDF_I4_ALLOW_LIVE"] = opt_in
    # Keep stdin open with no bytes: the tagged opt-in guard must exit before
    # attempting even a plan read. No executable invocation enables live access.
    with subprocess.Popen(
        [str(binary), "--i4-probe"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    ) as child:
        try:
            code = child.wait(timeout=3)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
        assert code == (2 if release == I4_RELEASE else 1)
        assert child.stdout.read() == b""
        assert child.stderr.read() == b""


@pytest.mark.parametrize("args", [[], ["--i4-probe", "extra"], ["--unknown"]])
def test_existing_invalid_invocation_semantics(offline_binary, args):
    binary, _ = offline_binary
    env = dict(os.environ, MDF_I4_ALLOW_LIVE="0")
    result = subprocess.run(
        [str(binary), *args],
        input=b"",
        capture_output=True,
        timeout=3,
        check=False,
        env=env,
    )
    assert result.returncode == 1
    assert result.stdout == result.stderr == b""
