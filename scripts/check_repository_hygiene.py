"""Reject tracked credentials, private keys and local runtime material offline."""

import re
import subprocess
from fnmatch import fnmatch
from pathlib import Path, PurePosixPath

FORBIDDEN_DIRECTORIES = {
    ".aws",
    ".credentials",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".ssh",
    ".tox",
    ".venv",
    "__pycache__",
    "artifacts",
    "build",
    "credentials",
    "data",
    "dist",
    "logs",
    "notes",
    "provider-lab",
    "secrets",
    "tmp",
    "venv",
}
FORBIDDEN_NAMES = (
    ".env*",
    "*.env",
    "*secret*",
    "*credential*",
    "*.pem",
    "*.key",
    "*.p12",
    "*.pfx",
    "id_rsa*",
    "id_ed25519*",
    "*_deploy",
    "*_deploy.pub",
    "*.py[cod]",
    "*.log",
    "*.sqlite*",
    "*.db",
    "*.csv",
    "*.parquet",
    "*.arrow",
    "*.feather",
    "*.pkl",
    "*.pickle",
    ".coverage*",
)
SENSITIVE_CONTENT = re.compile(
    rb"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----"
    rb"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
    rb"|\bgithub_pat_[A-Za-z0-9_]{20,}\b"
    rb"|\bAKIA[A-Z0-9]{16}\b"
    rb"|\bssh-ed25519 AAAA[A-Za-z0-9+/=]+"
)


def main() -> None:
    root = Path(
        subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"], text=True
        ).strip()
    )
    paths = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).split(b"\0")
    failures = []
    for raw_path in filter(None, paths):
        path = PurePosixPath(raw_path.decode())
        file = root / path
        if (
            FORBIDDEN_DIRECTORIES.intersection(path.parts)
            or any(
                fnmatch(part.lower(), pattern)
                for part in path.parts
                for pattern in FORBIDDEN_NAMES
            )
            or path.as_posix() == "README.txt"
            or file.is_symlink()
            or SENSITIVE_CONTENT.search(file.read_bytes())
        ):
            failures.append(str(path))
    if failures:
        raise SystemExit(
            "Repository hygiene failed (paths only):\n" + "\n".join(failures)
        )
    print(f"Repository hygiene passed: {len(list(filter(None, paths)))} tracked files.")


if __name__ == "__main__":
    main()
