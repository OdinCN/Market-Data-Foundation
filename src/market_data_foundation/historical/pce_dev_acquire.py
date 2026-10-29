"""Host-only adapter from the sanitized live edge to the existing PCE model."""

import json
from dataclasses import asdict
from pathlib import Path

from market_data_foundation.canonical.model import require
from market_data_foundation.historical import bea_pce as pce
from market_data_foundation.historical import bea_pce_live as live


class PceAcquisitionRejected(Exception):
    def __init__(self, classification):
        self.classification = (
            classification
            if classification in live._FAILURES
            else "source_shape_rejected"
        )
        super().__init__(self.classification)


class LivePceAcquirer:
    def __init__(self, identity, artifact_root: Path, clock):
        identity.validate()
        self.identity, self.artifact_root, self.clock = identity, artifact_root, clock

    def __call__(self):
        root = self.artifact_root
        require(
            root.resolve().is_relative_to(Path("artifacts").resolve())
            and not root.is_symlink(),
            "pce_artifact_scope_required",
        )
        # Exclusive directory and exclusive files: no overwrite or source replay.
        root.mkdir(parents=True, exist_ok=False, mode=0o700)
        result = live.acquire(self.identity, enabled=True, provider_budget=2)
        observations = {
            "classification": result.classification,
            "observations": [asdict(item) for item in result.observations],
            "source_qualified": False,
        }
        with (root / "observations.json").open("x") as stream:
            json.dump(observations, stream)
        if result.projection is None or result.receipt is None:
            raise PceAcquisitionRejected(result.classification)
        try:
            checked = live.inspect_candidate(
                result.projection, result.receipt, expected=self.identity
            )
            require(checked is not None, "pce_inspection_required")
            receipt = pce.decode(result.receipt)
            require(
                receipt["execution_class"] == "owner_local_candidate",
                "pce_live_candidate_required",
            )
        except Exception:
            raise PceAcquisitionRejected("source_shape_rejected") from None
        for name, body in (
            ("sanitized-results.json", result.projection),
            ("receipt.json", result.receipt),
        ):
            with (root / name).open("xb") as stream:
                stream.write(body)
        identity = self.identity.producer_execution_identity
        evidence = pce.PceRetainedEvidence(
            result.projection,
            identity + ":acquisition",
            "mdf-artifact://" + root.as_posix() + "/receipt.json",
        )
        plan = pce.build_plan(
            evidence,
            identity=identity,
            finished_at=self.clock(),
            frozen_at=self.clock(),
        )
        return pce.PceRetainedCandidate(evidence, plan)
