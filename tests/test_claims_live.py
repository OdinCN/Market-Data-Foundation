"""Explicit, separately marked FRED source fixture; never deterministic CI IO."""

import json
import os
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest

from market_data_foundation.historical.claims_contract import (
    SERIES_IDS,
    buffer_start,
    twenty_year_range,
)
from market_data_foundation.historical.claims_source import ClaimsFredSource

pytestmark = [
    pytest.mark.live_provider,
    pytest.mark.skipif(
        os.environ.get("MDF_CLAIMS_LIVE_OPT_IN") != "1",
        reason="explicit MDF_CLAIMS_LIVE_OPT_IN=1 required",
    ),
]


def test_six_fred_series_bounded_real_source_fixture():
    root = Path(__file__).resolve().parents[1] / "artifacts/claims/qualification"
    root = root / uuid4().hex
    root.mkdir(parents=True)
    start, end = twenty_year_range(datetime.now(UTC).date().isoformat())
    evidence = {
        "capability": "macro.claims.fred.continuous.weekly",
        "fixture": {
            "id": "claims-six-twenty-calendar-years-r1",
            "series_ids": list(SERIES_IDS),
            "start": start,
            "end": end,
            "buffer_weeks": 4,
            "max_logical_requests": 12,
        },
        "observations": [],
    }

    def save():
        (root / "observation.json").write_text(json.dumps(evidence, indent=2))

    def retain(request, response, observed_at, attempts):
        name = f"{len(evidence['observations']) + 1}-{request.operation}.json"
        (root / name).write_bytes(response.body)
        evidence["observations"].append(
            {
                "url": request.url,
                "parameters": request.parameters,
                "status": response.status,
                "attempts": attempts,
                "observed_at": observed_at.isoformat(),
                "raw_path": name,
                "raw_sha256": sha256(response.body).hexdigest(),
            }
        )
        save()

    source = None
    try:
        source = ClaimsFredSource(
            live_opt_in=True,
            max_requests=12,
            raw_sink=retain,
            request_control_path=root.parent / "fred-request-control.json",
        )
        for sid in SERIES_IDS:
            source.metadata(sid)
        batches = source.fetch(SERIES_IDS, buffer_start(start), end)
        assert len(batches) == 6
        assert all(batch.points for batch in batches)
    except Exception as error:
        evidence["failure_classification"] = getattr(
            error, "classification", "unexpected_exception"
        )
        raise
    finally:
        evidence.update(
            logical_requests=source.request_count if source else 0,
            transport_attempts=source.attempts if source else 0,
        )
        save()
