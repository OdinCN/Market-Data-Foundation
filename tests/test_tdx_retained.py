"""Retained receipts keep source clocks and use normal tracked correction writes."""

import json
from datetime import UTC, datetime
from hashlib import sha256
from uuid import uuid4

import pytest

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.tdx_p0_jobs import DailyJobs
from market_data_foundation.historical.tdx_retained import RetainedDailySource
from market_data_foundation.runtime.tdx_watchlist import V100_DRIVER_SDK_RELEASE
from tests.test_tdx_p0 import Source, Store


def manifest(tmp_path):
    outcome = Source(tmp_path, length=2).request("GetKline", 2, "920002", 0, 2)
    row = outcome["row"]
    row.update(
        peer="fixture:7709",
        sdk_release=V100_DRIVER_SDK_RELEASE,
        requested_at="2020-01-01T00:00:00+00:00",
        received_at="2020-01-03T00:00:00+00:00",
    )
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(row))
    observation = tmp_path / "observation.json"
    observation.write_text(
        json.dumps(
            dict(
                success=True,
                mode="live",
                provider="tdx-v100-primary",
                capability="bars.daily",
                implementation_version=V100_DRIVER_SDK_RELEASE,
                fixture_id="p0-" + row["request"]["id"],
                endpoint=dict(host="fixture", port=7709),
            )
        )
    )
    return dict(
        endpoint="fixture:7709",
        receipts=[
            dict(
                path=str(path),
                sha256=sha256(path.read_bytes()).hexdigest(),
                observation_path=str(observation),
                observation_sha256=sha256(observation.read_bytes()).hexdigest(),
            )
        ],
    )


def test_retained_receipts_normal_job_admits_now_and_replays_identity(tmp_path):
    m = manifest(tmp_path)

    def source(_):
        return RetainedDailySource(m, endpoint="fixture:7709", dev_opt_in=True)

    store = Store()
    written = []
    original = store.write

    def write(batch):
        written.append(batch)
        return original(batch)

    store.write = write
    jobs = DailyJobs(tmp_path / "jobs", store, source, workers=1)
    identity = str(uuid4())
    command = dict(
        request_id=identity, action="update", start="2020-01-02", end="2020-01-02"
    )
    before = datetime.now(UTC)
    admission = {"manifest_sha256": "a" * 64}
    jobs.submit(command, launch=False, origin="manual", admission=admission)
    result = jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["provider_requests"] == 0
    assert result["retained_responses"] == 1
    assert len(written) == 1
    assert written[0].received_at(written[0].bars[0]) == datetime(
        2020, 1, 3, tzinfo=UTC
    )
    assert written[0].admitted_at >= before
    assert len(written[0].bars) == 1
    assert jobs.run(identity)["state"] == "COMPLETE"
    assert len(written) == 1
    replay = jobs.submit(command, launch=False, admission=admission)
    assert replay["updated_at"] == result["updated_at"]
    with pytest.raises(ContractError, match="tdx_receipt_manifest_conflict"):
        jobs.submit(command, launch=False, admission={"manifest_sha256": "b" * 64})
    assert jobs.read(identity)["admission"] == admission


@pytest.mark.parametrize(
    "mutation,classification",
    [
        ("endpoint", "tdx_retained_endpoint"),
        ("digest", "tdx_retained_digest"),
        ("observation", "tdx_retained_observation_digest"),
        ("sdk", "tdx_retained_qualification"),
    ],
)
def test_retained_receipts_reject_changed_evidence(tmp_path, mutation, classification):
    m = manifest(tmp_path)
    proof = m["receipts"][0]
    if mutation == "endpoint":
        m["endpoint"] = "other:7709"
    if mutation == "digest":
        proof["sha256"] = "0" * 64
    if mutation == "observation":
        proof["observation_sha256"] = "0" * 64
    if mutation == "sdk":
        from pathlib import Path

        path = Path(proof["path"])
        row = json.loads(path.read_bytes())
        row["sdk_release"] = "other"
        path.write_text(json.dumps(row))
        proof["sha256"] = sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ContractError, match=classification):
        RetainedDailySource(m, endpoint="fixture:7709", dev_opt_in=True)
    with pytest.raises(ContractError, match="opt_in"):
        RetainedDailySource(m, endpoint="fixture:7709")
