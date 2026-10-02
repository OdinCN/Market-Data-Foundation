"""Forced staging cannot bypass the local runtime/artifact boundary."""

import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "path",
    ["config/local/runtime.toml", "tls/client.crt", "artifacts/observation.json"],
)
def test_hygiene_rejects_forced_local_material(tmp_path, path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / ".gitignore").write_text((ROOT / ".gitignore").read_text())
    material = tmp_path / path
    material.parent.mkdir(parents=True)
    material.write_text("content-must-not-be-disclosed")
    subprocess.run(
        ["git", "add", "--force", ".gitignore", path], cwd=tmp_path, check=True
    )
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_repository_hygiene.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert path in result.stderr
    assert "content-must-not-be-disclosed" not in result.stderr + result.stdout


def test_hygiene_accepts_source(tmp_path) -> None:
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "source.py").write_text('"""Synthetic source."""\n')
    subprocess.run(["git", "add", "source.py"], cwd=tmp_path, check=True)
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_repository_hygiene.py")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
