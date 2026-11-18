"""Bounded job reads use isolated journals, never sources or external stores."""

import json
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.tdx_p0_jobs import DailyJobs, strategy_slot_id
from market_data_foundation.historical.tdx_p0_source import TdxError
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.test_tdx_morning_binding import DAILY, DAY, LATEST, ROSTER, Replay
from tests.test_tdx_p0 import Store


def journal(tmp_path, count=5582):
    jobs = DailyJobs(tmp_path / "jobs", None, None)
    identity = str(uuid4())
    steps = [
        dict(market=0, instrument=f"{i:06}", state="PENDING") for i in range(count)
    ]
    folder = jobs.directory / identity
    folder.mkdir()
    (folder / "base.json").write_text(json.dumps(steps))
    header = dict(
        request_id=identity,
        state="PARTIAL",
        created_at="2026-11-17T01:00:00Z",
        command=dict(action="update", start="2026-11-02", end="2026-11-16"),
        step_refs=[f"0-{s['instrument']}" for s in steps],
        completed_step_count=0,
        provider_requests=15,
    )
    jobs.path(identity).write_text(json.dumps(header))
    return jobs, identity, steps


def test_summary_never_opens_shards_and_page_only_reads_requested_shards(
    tmp_path, monkeypatch
):
    jobs, identity, steps = journal(tmp_path)
    shard = jobs.directory / identity / "0-000024.json"
    shard.write_text(
        json.dumps(dict(steps[24], state="FAILED", classification="real_failure"))
    )
    reads = []
    original = Path.read_bytes

    def read(path):
        reads.append(path)
        return original(path)

    monkeypatch.setattr(Path, "read_bytes", read)
    summary = jobs.detail_summary(identity)
    assert reads == [jobs.path(identity)]
    assert summary["step_count"] == 5582 and summary["complete_steps"] == 0
    assert "steps" not in summary and summary["steps_paging"] is True
    page = jobs.step_page(identity, offset=24, limit=25)
    assert page["total"] == 5582 and page["next_offset"] == 49
    assert page["steps"][0]["classification"] == "real_failure"
    assert (
        len([p for p in reads if p.parent.name == identity and p.name != "base.json"])
        == 1
    )
    found = jobs.step_page(identity, search="real_failure")
    assert found["total"] == 1 and found["steps"][0]["instrument"] == "000024"
    assert jobs.step_page(identity, offset=5580)["next_offset"] is None


def test_missing_base_or_shard_is_not_a_verified_empty_result(tmp_path):
    jobs, identity, _ = journal(tmp_path)
    base = jobs.directory / identity / "base.json"
    base.unlink()
    with pytest.raises(TdxError, match="tdx_job_steps_unavailable"):
        jobs.step_page(identity)
    assert jobs.detail_summary(identity)["step_count"] == 5582
    base.write_text("[]")
    with pytest.raises(TdxError, match="tdx_job_steps_unavailable"):
        jobs.step_page(identity)


def test_absent_base_uses_actual_shards_for_full_read_page_and_search(tmp_path):
    jobs, identity, steps = journal(tmp_path, 51)
    folder = jobs.directory / identity
    (folder / "base.json").unlink()
    steps[26].update(state="FAILED", classification="retained_failure")
    for step in steps:
        (folder / f"0-{step['instrument']}.json").write_text(json.dumps(step))
    assert jobs.read(identity)["steps"] == steps
    page = jobs.step_page(identity, offset=25, limit=25)
    assert page["steps"] == steps[25:50]
    assert page["total"] == 51 and page["next_offset"] == 50
    found = jobs.step_page(identity, search="retained_failure")
    assert found["steps"] == [steps[26]] and found["total"] == 1
    (folder / "0-000026.json").unlink()
    with pytest.raises(TdxError, match="tdx_job_steps_unavailable"):
        jobs.step_page(identity, offset=25, limit=25)


def test_legacy_readers_counts_missing_and_interrupted_state_are_preserved(tmp_path):
    jobs, identity, _ = journal(tmp_path, 2)
    header = json.loads(jobs.path(identity).read_text())
    header.pop("completed_step_count")
    header["state"] = "RUNNING"
    jobs.path(identity).write_text(json.dumps(header))
    value = jobs.detail_summary(identity)
    assert value["state"] == "INTERRUPTED" and value["stored_state"] == "RUNNING"
    assert value["complete_steps"] is None
    assert len(jobs.read(identity)["steps"]) == 2
    with pytest.raises(TdxError, match="job_not_found"):
        jobs.detail_summary(str(uuid4()))


def test_actual_routes_keep_full_read_and_bound_new_read_queries(tmp_path):
    jobs, identity, _ = journal(tmp_path, 51)
    runtime = DailyRuntime(
        TdxOptions(tmp_path / "runtime", Path("/unused"), "unused", True),
        Store(),
        lambda _: pytest.fail("no source for GET"),
    )
    runtime.jobs = jobs
    app = FastAPI()
    app.include_router(tdx_router(runtime))
    app.include_router(roster_router(runtime))
    with TestClient(app) as client:
        path = f"/api/r5/tdx/daily/jobs/{identity}"
        assert len(client.get(path).json()["steps"]) == 51
        assert len(client.get(path + "?legacy_reader=retained").json()["steps"]) == 51
        assert client.get(path + "?view=summary").json()["step_count"] == 51
        page = client.get(path + "?view=steps&offset=25&limit=25").json()
        assert page["offset"] == 25 and len(page["steps"]) == 25
        assert (
            client.get(f"/api/r5/tdx/roster/jobs/{identity}?view=summary").status_code
            == 404
        )
        for query in [
            "view=bad",
            "view=steps&limit=101",
            "view=steps&offset=-1",
            "view=steps&limit=1&limit=2",
            "view=summary&offset=1",
        ]:
            assert client.get(path + "?" + query).status_code == 400
        assert (
            client.get(f"/api/r5/tdx/daily/jobs/{uuid4()}?view=summary").status_code
            == 404
        )
        runtime.options = replace(runtime.options, allow_read=False)
        for query in ["", "?view=summary", "?view=steps&limit=25"]:
            assert client.get(path + query).status_code == 403
    assert not list(tmp_path.rglob("*raw*"))


@pytest.mark.parametrize(
    "corruption", [b"{", b"null", b'"not a header"', b"{}", "bad_origin"]
)
def test_corrupt_record_is_local_to_lists_and_named_reads_then_clears(
    tmp_path, corruption
):
    replay = Replay(tmp_path)
    morning = replay.morning()
    healthy_roster = replay.manual_roster(LATEST)
    daily_id = str(uuid4())
    replay.post(DAILY + "/jobs", dict(request_id=daily_id, action="update", end=DAY))
    daily = replay.runtime.jobs.run(daily_id)
    assert daily["state"] == "COMPLETE"
    damaged = replay.runtime.jobs.path(morning["request_id"])
    original = damaged.read_bytes()
    invalid_origin = corruption == "bad_origin"
    if corruption == "bad_origin":
        header = json.loads(original)
        header["origin"] = []
        corruption = json.dumps(header).encode()
    damaged.write_bytes(corruption)
    replay.dispatch()
    before = len(replay.requests)
    with replay.client() as client:
        for prefix, ids in [
            (DAILY, {daily_id, healthy_roster["request_id"]}),
            (ROSTER, {healthy_roster["request_id"]}),
        ]:
            if prefix == DAILY and invalid_origin:
                ids = ids | {strategy_slot_id(DAY, "update")}
            response = client.get(prefix + "/jobs")
            assert response.status_code == 200, response.text
            data = response.json()
            assert data["status"] == "PARTIAL"
            assert {j["request_id"] for j in data["jobs"]} == ids
            assert len(data["read_errors"]) == 1
            error = data["read_errors"][0]
            assert error["request_id"] == morning["request_id"]
            assert error["classification"] == "tdx_job_unreadable"
            assert error["cause_classification"]
            assert set(error) == {
                "request_id",
                "classification",
                "cause_classification",
            }
            for query in ("", "?view=summary", "?view=steps"):
                unreadable = client.get(
                    prefix + "/jobs/" + morning["request_id"] + query
                )
                assert unreadable.status_code == 503
                assert unreadable.json()["classification"] == (
                    "tdx_job_steps_unavailable"
                    if corruption == b"{}" and query
                    else "tdx_job_unreadable"
                )
        for prefix, identity in [
            (DAILY, daily_id),
            (ROSTER, healthy_roster["request_id"]),
        ]:
            response = client.get(prefix + "/jobs/" + identity + "?view=summary")
            assert response.status_code == 200
            assert response.json()["request_id"] == identity
            assert response.json()["state"] == "COMPLETE"
        blocked = client.get(DAILY + "/strategy").json()["blocked"]
        if invalid_origin:
            assert blocked is None  # Readable but wrong source is a real job failure.
            failed = replay.detail(strategy_slot_id(DAY, "update"))
            assert failed["state"] == "PARTIAL"
            assert failed["classification"] == "tdx_periodic_roster_origin"
        else:
            assert blocked["request_id"] == morning["request_id"]
        assert damaged.read_bytes() == corruption and len(replay.requests) == before
        damaged.write_bytes(original)
        replay.dispatch()
        for prefix in (DAILY, ROSTER):
            data = client.get(prefix + "/jobs").json()
            assert data["status"] == "OK" and data["read_errors"] == []
            assert morning["request_id"] in {j["request_id"] for j in data["jobs"]}
            response = client.get(
                prefix + "/jobs/" + morning["request_id"] + "?view=summary"
            )
            assert (
                response.status_code == 200 and response.json()["state"] == "COMPLETE"
            )
        assert client.get(DAILY + "/strategy").json()["blocked"] is None
        if invalid_origin:
            assert (
                replay.detail(strategy_slot_id(DAY, "update"))["classification"]
                == "tdx_periodic_roster_origin"
            )
        assert len(replay.requests) == before
