"""Offline build guards; fake inputs must fail before Go can be launched."""

import subprocess
import sys
from pathlib import Path

import pytest

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
def test_builder_refuses_to_clobber_rollback_material(tmp_path, existing):
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
