"""Explicit opt-in BLS development qualification; no canonical/storage effects."""

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_foundation.historical import cpi_history
from market_data_foundation.historical.bls_cpi_direct_live import (
    ExactOneHttpsTransport,
    LiveExecutionError,
)


@pytest.mark.live_provider
@pytest.mark.parametrize("reference", ["2024-12", "2025-01"])
def test_live_bls_history_indices(reference):
    if os.environ.get("MDF_R5_CPI_HISTORY_LIVE") != "1":
        pytest.skip("explicit history live opt-in required")
    # Conservative unregistered tier: no credential discovery or fallback.
    qualification = cpi_history.HistoryQualification("unregistered", 10, 6)
    identity = "live-" + reference + "-" + datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
    root = Path("artifacts/r5-i5-bls-live-qualification")
    root.mkdir(parents=True, exist_ok=True)
    failure = None
    try:
        candidate = cpi_history.acquire(
            reference=reference,
            qualification=qualification,
            secret_path=Path("unused-unregistered"),
            transport=ExactOneHttpsTransport(),
            clock=lambda: datetime.now(UTC),
            artifact_root=root,
            job_id=identity,
        )
    except Exception as error:
        failure = (
            error.classification
            if type(error) is LiveExecutionError
            else "live_qualification_failed"
        )
    if failure is not None:
        # The provider exception may include private wire details; never emit it.
        (root / (identity + "-failure.json")).write_text(
            json.dumps(
                {
                    "capability": "bls.cpi.history.indices",
                    "fixture": reference,
                    "request": cpi_history.request(reference),
                    "classification": failure,
                    "live": True,
                }
            )
            + "\n"
        )
        pytest.fail(failure, pytrace=False)
    assert candidate.evidence.reference == reference
    assert json.loads(candidate.plan.content)["provider_vintage"] is None
