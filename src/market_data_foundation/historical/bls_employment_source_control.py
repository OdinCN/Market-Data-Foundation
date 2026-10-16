"""Historical read-only evidence inspection and explicit bounded Source entry.

Historical pins are audit inputs, not runtime grants or execution prerequisites.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from market_data_foundation.historical import bls_employment_live as live

REPOSITORY = Path(__file__).resolve().parents[3]
CONTROL = Path(
    "/srv/market-data-foundation-control/transactions/"
    "MDF-MACRO-BLS-EMPLOYMENT-VERTICAL-R1"
)
BRANCH = "mdf/t03-employment-r1-20261016"
HEAD = "b0e233305891de8d1aa9e8f20121209a8a25597d"
TREE = "ad6cb4525ac12688e427748257bdc5e6c2ff117c"
PARENT = "27437d6b34d43295a32f66a3a786e8e65faae452"
MANIFEST = "T03_POST_T04_AUTHORITY_BINDING_CORRECTION_R1_MANIFEST.json"
REVIEW = "T03_POST_T04_AUTHORITY_BINDING_CORRECTION_R1_INDEPENDENT_REVIEW.md"
PLAN = "T03_SOURCE_EFFECT_PLAN_CORRECTION_001.json"
PLAN_REVIEW = "T03_SOURCE_PLAN_REVIEW_CORRECTION_001.md"


@dataclass(frozen=True)
class _File:
    path: str
    size: int
    sha256: str
    blob: str


# Exact three historical files independently reviewed before commit. No new
# control implementation, future commit or future reviewer inherits these pins.
_PRODUCT = (
    _File(
        "docs/bls-employment-live-edge-r1.md",
        13315,
        "df8c1491a266ce79d5ffcad261a1a514fa853aa4d2c8135751f58c918b864996",
        "d0998cfa44fc7cea4e6d05b995361d7ebefc35a5",
    ),
    _File(
        "src/market_data_foundation/historical/bls_employment_live.py",
        44874,
        "ac85270a8979fbebf4ca58b709c025dbb04c80412f87ae44edee24100b05c53b",
        "b7bb762a6d9b4e220f02df26bc95e4d99047c318",
    ),
    _File(
        "tests/test_bls_employment_live.py",
        57306,
        "5c752adf90011d5d58adafed18c701376458c8ba485d1fd9f6fcebd1bc535738",
        "58bbe6b4c277f5929d1bc769407220adc75d0218",
    ),
)
_EVIDENCE = (
    (
        MANIFEST,
        16413,
        "f25a5ff663129956a71aa3248c397be4e9dda4d15ca3740ad21588c53f69b2ed",
    ),
    (REVIEW, 3550, "094176a446d3e0d1bc066f592c82a2ddab9b624493b171c966b373953b5829ed"),
    (PLAN, 15943, "81bcc3446959e9a18526e53b93d406f01982f18ee9ee3b5dc464f1ec70eefac6"),
    (
        PLAN_REVIEW,
        5547,
        "2fcfc2451e955ea4b1fbd32eb75203644de86471c3321b0a2614061313e5779c",
    ),
    (
        "T03_POST_T04_AUTHORITY_BINDING_CORRECTION_R1_HOST_FULL_EVIDENCE.json",
        1995,
        "810f4a4479ae92157dd6fd5a0d223b633d310c64a810fb60311ac55004fcf66a",
    ),
    (
        "T03_CURRENT_LIVE_EDGE_HOSTED_CI_EVIDENCE.json",
        1506,
        "9982a6c6bf5713f6c5897b86c747814aebe2fe1b128d048e1063eeecc5384da0",
    ),
    (
        "T03_CURRENT_LIVE_EDGE_GIT_CI_POSTFLIGHT.json",
        3158,
        "7aa9825060ec801b1b6d91578bce34a4c31eb7fe234d99e4241f60c0cfe86e8f",
    ),
)


class SourceAdmissionError(RuntimeError):
    """Fixed classification only; no external content or exception messages."""

    def __init__(self, classification: str):
        super().__init__(classification)
        self.classification = classification

    def safe_observation(self) -> dict:
        return {
            "classification": self.classification,
            "source_qualified": False,
            "provider_requests": 0,
            "attempt_created": False,
            "source_effects_enabled": False,
        }


@dataclass(frozen=True)
class CommittedSourceEvidence:
    """Historical identity result only. Never accepted as an execution grant."""

    head: str
    tree: str
    product_blobs: tuple[tuple[str, str], ...]

    @property
    def source_qualified(self) -> bool:
        return False

    @property
    def execution_enabled(self) -> bool:
        return False


def _git(repository: Path, *args: str) -> bytes:
    return subprocess.check_output(
        [
            "git",
            "--no-optional-locks",
            "--no-replace-objects",
            "-c",
            "core.fsmonitor=false",
            *args,
        ],
        cwd=repository,
        stderr=subprocess.DEVNULL,
        timeout=2,
    )


def _read_exact(path: Path, size: int, sha256: str) -> bytes:
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise SourceAdmissionError("evidence_or_source_drift")
    with os.fdopen(
        os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb"
    ) as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise SourceAdmissionError("evidence_or_source_drift")
        raw = stream.read(size + 1)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != sha256:
        raise SourceAdmissionError("evidence_or_source_drift")
    return raw


def _git_identity(repository: Path) -> None:
    if (
        _git(repository, "rev-parse", "HEAD").strip() != HEAD.encode()
        or _git(repository, "rev-parse", "HEAD^{tree}").strip() != TREE.encode()
        or _git(repository, "rev-parse", "HEAD^").strip() != PARENT.encode()
        or _git(repository, "branch", "--show-current").strip() != BRANCH.encode()
    ):
        raise SourceAdmissionError("commit_tree_or_branch_mismatch")
    if _git(
        repository,
        "status",
        "--porcelain=v1",
        "-z",
        "--untracked-files=all",
        "--ignore-submodules=none",
    ) or any(
        row[:1] != b"H"
        for row in _git(repository, "ls-files", "-v", "-z").split(b"\0")
        if row
    ):
        raise SourceAdmissionError("clean_complete_worktree_required")


def _inspect(repository: Path, control: Path) -> CommittedSourceEvidence:
    """Private location seam for offline fixtures; identities remain fixed."""
    try:
        _git_identity(repository)
        evidence = {
            name: _read_exact(control / name, size, sha)
            for name, size, sha in _EVIDENCE
        }
        manifest = json.loads(evidence[MANIFEST])
        expected_files = [
            {"path": row.path, "bytes": row.size, "sha256": row.sha256}
            for row in _PRODUCT
        ]
        if (
            manifest["base"] != PARENT
            or manifest["branch"] != BRANCH
            or manifest["product_revision"] != "T03_EMPLOYMENT_LIVE_EDGE_CORRECTION_002"
            or manifest["product_files"] != expected_files
        ):
            raise SourceAdmissionError("reviewed_manifest_mismatch")
        for row in _PRODUCT:
            expected = f"100644 blob {row.blob}\t{row.path}\n".encode()
            if _git(repository, "ls-tree", HEAD, "--", row.path) != expected:
                raise SourceAdmissionError("git_blob_mismatch")
            blob = _git(repository, "cat-file", "blob", row.blob)
            if len(blob) != row.size or hashlib.sha256(blob).hexdigest() != row.sha256:
                raise SourceAdmissionError("git_blob_mismatch")
            _read_exact(repository / row.path, row.size, row.sha256)
        # Detect concurrent drift during readback as well as at entry.
        _git_identity(repository)
        for name, size, sha in _EVIDENCE:
            _read_exact(control / name, size, sha)
        return CommittedSourceEvidence(
            HEAD, TREE, tuple((row.path, row.blob) for row in _PRODUCT)
        )
    except SourceAdmissionError:
        raise
    except OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError:
        raise SourceAdmissionError("commit_evidence_unavailable") from None


def inspect_committed_source() -> CommittedSourceEvidence:
    """Admit only the pinned historical commit/evidence for read-only inspection.

    These immutable historical pins do not describe current code or control
    whether the existing Source capability may execute.
    """
    return _inspect(REPOSITORY, CONTROL)


def execute_source_once(
    *, enabled: bool = False, transport: live.EmploymentTransport | None = None
) -> live.EmploymentAcquisition:
    """Use the existing capability with fixed scope, limits and explicit live opt-in.

    Historical evidence remains independently inspectable; it is not a grant
    verifier. The live runner owns one logical attempt and applies the shared
    bounded transport retry policy.
    """
    return live.execute_live_candidate(enabled=enabled, transport=transport)
