"""Real isolated R5 routes/strategy/executor, distinguishable roster members; no LAN."""

import json
import os
import shutil
from copy import deepcopy
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.tdx_p0_jobs import strategy_slot_id
from market_data_foundation.historical.tdx_p0_source import TdxError, encoded
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_roster_api import roster_router
from tests.test_tdx_p0 import Source, Store

DAY = "2026-11-16"
DAILY = "/api/r5/tdx/daily"
ROSTER = "/api/r5/tdx/roster"
ORIGIN = "http://192.168.10.20:18766"
MORNING = ["000001", "600000", "920002"]
MIDDAY = ["000002", "600001", "920003"]
LATEST = ["000003", "600002", "920004"]


class Publications(Store):
    def __init__(self):
        super().__init__()
        self.lookups = []
        self.bars = {}

    def universe(self, snapshot_id=None, **_):
        self.lookups.append(snapshot_id)
        return deepcopy(
            next(
                (
                    s
                    for s in reversed(self.snapshots)
                    if snapshot_id is None or s["snapshot_id"] == snapshot_id
                ),
                None,
            )
        )

    def strategy_calendar(self, **_):
        return [
            dict(
                market=m,
                day=DAY,
                is_open=True,
                available_at="2026-11-01T00:00:00+00:00",
            )
            for m in range(3)
        ]

    def write(self, batch):
        result = super().write(batch)
        for b in batch.bars:
            self.bars[b.key] = dict(
                market=b.market,
                instrument=b.instrument,
                trading_day=b.trading_day,
                open=b.values[0],
                high=b.values[1],
                low=b.values[2],
                close=b.values[3],
                volume=b.values[4],
                amount=b.values[5],
                received_at=batch.observed_at,
                available_at=batch.admitted_at,
            )
        return result

    def query(self, *, start, end, market=None, instrument=None, **_):
        rows = [
            r
            for r in self.bars.values()
            if start <= r["trading_day"] <= end
            and (market is None or r["market"] == market)
            and (instrument is None or r["instrument"] == instrument)
        ]
        return dict(
            rows=rows,
            total=len(rows),
            as_of=datetime.now(UTC),
            next_offset=None,
            provider_requests=0,
        )


class Replay:
    def __init__(self, tmp_path):
        self.root = tmp_path
        self.store = Publications()
        self.members = MORNING
        self.requests = []
        self.fail = set()
        self.launched = []
        self.at = None
        self.runtime = self.rebuild()

    def source(self, _):
        owner = self

        class DistinctSource(Source):
            def request(self, operation, market, code="", start=0, count=0):
                owner.requests.append((operation, market, code))
                if operation == "GetKline" and code in owner.fail:
                    self.requests += 1
                    raise TdxError("fixture_daily_timeout")
                result = super().request(operation, market, code, start, count)
                row = result["row"]
                if owner.at is not None:
                    row["received_at"] = owner.at.isoformat()
                if operation in {"GetCode", "GetBJDirectory"}:
                    row["source_projection"]["List"][0]["Code"] = owner.members[market]
                elif operation == "GetKline":
                    row["source_projection"]["List"][0]["Time"] = (
                        DAY + "T15:00:00+08:00"
                    )
                raw = encoded(row)
                Path(result["path"]).write_bytes(raw)
                return dict(
                    row=row, path=result["path"], sha256=sha256(raw).hexdigest()
                )

        return DistinctSource(self.root, length=1)

    def rebuild(self):
        runtime = DailyRuntime(
            TdxOptions(
                self.root / "state",
                Path("unused"),
                "fixture.invalid:1",
                allow_collect=True,
                workers=1,
            ),
            self.store,
            self.source,
        )
        runtime.jobs.launch = self.launched.append
        self.runtime = runtime
        return runtime

    def client(self):
        app = FastAPI()
        app.include_router(tdx_router(self.runtime))
        app.include_router(roster_router(self.runtime))
        return TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234))

    def post(self, path, body):
        with self.client() as client:
            response = client.post(path, json=body, headers={"origin": ORIGIN})
        assert response.status_code == 202, response.text
        return response.json()

    def morning(self):
        with self.client() as client:
            response = client.post(
                DAILY + "/strategy", json={"enabled": True}, headers={"origin": ORIGIN}
            )
        assert response.status_code == 200
        self.runtime.strategy.tick(datetime(2026, 11, 16, 1, tzinfo=UTC))
        self.roster_id = strategy_slot_id(DAY, "universe")
        record = self.runtime.jobs.run(self.roster_id)
        assert record["state"] == "COMPLETE"
        self.morning_snapshot = record["snapshot_id"]
        return record

    def manual_roster(self, members):
        self.members = members
        identity = str(uuid4())
        self.post(ROSTER + "/jobs", dict(request_id=identity))
        record = self.runtime.jobs.run(identity)
        assert record["state"] == "COMPLETE"
        return record

    def dispatch(self):
        self.runtime.strategy.tick(datetime(2026, 11, 16, 7, 35, tzinfo=UTC))
        return strategy_slot_id(DAY, "update")

    def detail(self, identity, view="summary"):
        with self.client() as client:
            response = client.get(DAILY + "/jobs/" + identity + "?view=" + view)
        assert response.status_code == 200, response.text
        return response.json()

    def rows(self):
        with self.client() as client:
            response = client.get(DAILY + "/query?day=" + DAY + "&local_only=true")
        assert response.status_code == 200, response.text
        return response.json()["rows"]


def emit(name, value):
    if directory := os.environ.get("MDF_BINDING_EVIDENCE"):
        Path(directory, name + ".json").write_text(json.dumps(value, indent=2))


def test_afternoon_dispatch_uses_morning_members_after_two_manual_refreshes(
    tmp_path, monkeypatch
):
    replay = Replay(tmp_path)
    replay.at = datetime(2026, 11, 16, 1, tzinfo=UTC)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return replay.at.astimezone(tz) if tz else replay.at.replace(tzinfo=None)

    monkeypatch.setattr("market_data_foundation.historical.tdx_p0_jobs.datetime", Clock)
    morning = replay.morning()
    replay.at = datetime(2026, 11, 16, 3, 38, tzinfo=UTC)
    midday = replay.manual_roster(MIDDAY)
    replay.at = datetime(2026, 11, 16, 4, 30, tzinfo=UTC)
    latest = replay.manual_roster(LATEST)
    replay.at = datetime(2026, 11, 16, 7, 35, tzinfo=UTC)
    identity = replay.dispatch()
    accepted = replay.detail(identity)
    replay.rebuild()
    result = replay.runtime.jobs.run(identity)
    rows = replay.rows()
    ui_fixture = None
    if directory := os.environ.get("MDF_BINDING_EVIDENCE"):
        ui_fixture = Path(directory, "ui-fixtures", uuid4().hex)
        shutil.copytree(replay.runtime.jobs.directory, ui_fixture / "journal")
        (ui_fixture / "fixture-only.json").write_text('{"scenario":"morning_binding"}')
    emit(
        "dispatch",
        dict(
            simulated_clock=True,
            ui_fixture=str(ui_fixture) if ui_fixture else None,
            morning_job=morning["request_id"],
            morning_snapshot=morning["snapshot_id"],
            manual_snapshots=[midday["snapshot_id"], latest["snapshot_id"]],
            daily_job=identity,
            accepted=accepted,
            result=replay.detail(identity),
            rows=rows,
        ),
    )
    assert result["state"] == "COMPLETE", result
    assert result["provider_requests"] == 3
    assert sorted(r["instrument"] for r in rows) == sorted(MORNING)
    assert result["snapshot_id"] == replay.morning_snapshot
    assert accepted["roster_binding"]["request_id"] == replay.roster_id
    assert accepted["roster_binding"]["snapshot_id"] == replay.morning_snapshot
    assert result["roster_binding"] == accepted["roster_binding"]
    before = list(replay.launched)
    replay.runtime.strategy.tick(datetime(2026, 11, 16, 8, tzinfo=UTC))
    assert replay.launched == before
    duplicate = replay.runtime.jobs.submit(
        result["command"], origin="periodic", launch=False
    )
    assert duplicate["roster_binding"] == accepted["roster_binding"]


@pytest.mark.parametrize("interruption", ["before_first_shard", "failed_shard"])
def test_same_id_resume_and_rebuild_keep_morning_binding(
    tmp_path, monkeypatch, interruption
):
    replay = Replay(tmp_path)
    replay.morning()
    replay.manual_roster(MIDDAY)
    replay.manual_roster(LATEST)
    identity = replay.dispatch()
    accepted = replay.detail(identity)
    if interruption == "before_first_shard":

        def interrupted(*_):
            raise SystemExit("isolated interruption before first shard")

        monkeypatch.setattr(replay.runtime.jobs, "_daily", interrupted)
        with pytest.raises(SystemExit):
            replay.runtime.jobs.run(identity)
        assert replay.runtime.jobs.read(identity)["steps"] == []
        assert replay.runtime.jobs.read(identity)["state"] == "INTERRUPTED"
    else:
        replay.fail = {MORNING[2]}
        record = replay.runtime.jobs.run(identity)
        assert record["state"] == "PARTIAL"
        assert [s["instrument"] for s in record["steps"] if s["state"] == "FAILED"] == [
            MORNING[2]
        ]
    # Another publication appears after admission / interruption.
    replay.manual_roster(["000004", "600003", "920005"])
    replay.rebuild()
    replay.fail.clear()
    before = len(replay.requests)
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE", result
    assert result["request_id"] == identity
    assert result["provider_requests"] == (
        3 if interruption == "before_first_shard" else 4
    )
    assert result["roster_binding"] == accepted["roster_binding"]
    assert result["snapshot_id"] == replay.morning_snapshot
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MORNING)
    reads = [code for op, _, code in replay.requests[before:] if op == "GetKline"]
    assert sorted(reads) == sorted(
        MORNING if interruption == "before_first_shard" else [MORNING[2]]
    )
    emit(
        interruption,
        dict(
            binding=result["roster_binding"],
            daily_job=identity,
            resume_count=result["resume_count"],
            provider_requests=result["provider_requests"],
            resumed_instruments=reads,
            rows=replay.rows(),
        ),
    )


@pytest.mark.parametrize(
    "fault,classification",
    [
        ("missing_job", "tdx_periodic_roster_job_missing"),
        ("unreadable_job", "tdx_periodic_roster_job_unreadable"),
        ("scalar_job", "tdx_periodic_roster_job_unreadable"),
        ("origin", "tdx_periodic_roster_origin"),
        ("unknown_origin", "tdx_periodic_roster_origin"),
        ("job_identity", "tdx_periodic_roster_origin"),
        ("day", "tdx_periodic_roster_scope"),
        ("action", "tdx_periodic_roster_scope"),
        ("markets", "tdx_periodic_roster_scope"),
        ("state", "tdx_periodic_roster_incomplete"),
        ("incomplete_steps", "tdx_periodic_roster_incomplete"),
        ("missing_output", "tdx_periodic_roster_snapshot_missing"),
        ("changed_output", "tdx_periodic_roster_snapshot_changed"),
        ("missing_snapshot", "tdx_bound_roster_missing"),
        ("wrong_snapshot", "tdx_bound_roster_snapshot_mismatch"),
        ("unreadable_snapshot", "tdx_bound_roster_unreadable"),
        ("producer_source_binding", "tdx_bound_roster_producer_mismatch"),
        ("producer_request_id", "tdx_bound_roster_producer_mismatch"),
        ("producer_day", "tdx_bound_roster_producer_mismatch"),
        ("producer_origin", "tdx_bound_roster_producer_mismatch"),
        ("incomplete_snapshot", "tdx_roster_incomplete"),
        ("stale_snapshot", "tdx_roster_stale"),
        ("publication_before_job", "tdx_bound_roster_publication_mismatch"),
        ("missing_publication_clock", "tdx_bound_roster_publication_mismatch"),
        ("missing_job_clock", "tdx_bound_roster_publication_mismatch"),
    ],
)
def test_bad_bound_evidence_blocks_before_source_without_latest_fallback(
    tmp_path, fault, classification
):
    replay = Replay(tmp_path)
    replay.morning()
    replay.manual_roster(LATEST)
    identity = replay.dispatch()
    binding = replay.detail(identity)["roster_binding"]
    path = replay.runtime.jobs.path(replay.roster_id)
    header = json.loads(path.read_bytes())
    snapshot = replay.store.snapshots[0]
    if fault == "missing_job":
        path.unlink()
    elif fault == "unreadable_job":
        path.write_text("broken isolated header")
    elif fault == "scalar_job":
        path.write_text('"not a job header"')
    elif fault == "missing_snapshot":
        replay.store.snapshots.pop(0)
    elif fault == "wrong_snapshot":
        replay.store.universe = lambda **_: deepcopy(replay.store.snapshots[-1])
    elif fault == "unreadable_snapshot":

        def unreadable(**_):
            raise OSError("synthetic private detail must not escape")

        replay.store.universe = unreadable
    elif fault.startswith("producer_"):
        snapshot["details"]["producer"][fault.removeprefix("producer_")] = "wrong"
    elif fault == "incomplete_snapshot":
        snapshot["details"]["markets"][0]["state"] = "FAILED"
    elif fault == "stale_snapshot":
        snapshot["details"]["valid_through"] = "2026-11-15"
    elif fault == "publication_before_job":
        snapshot["details"].pop("producer")
        snapshot["observed_at"] = snapshot["available_at"] = "2020-01-01T00:00:00Z"
    elif fault == "missing_publication_clock":
        snapshot.pop("observed_at")
    else:
        if fault == "origin":
            header["origin"] = "manual"
        if fault == "unknown_origin":
            header["origin"] = None
        if fault == "job_identity":
            header["request_id"] = str(uuid4())
        if fault == "day":
            header["command"]["end"] = "2026-11-13"
        if fault == "action":
            header["command"]["action"] = "update"
        if fault == "markets":
            header["command"]["markets"] = [0, 1]
        if fault == "state":
            header["state"] = "PARTIAL"
        if fault == "incomplete_steps":
            header["steps"][0]["state"] = "FAILED"
        if fault == "missing_output":
            header.pop("snapshot_id")
        if fault == "changed_output":
            header["snapshot_id"] = replay.store.snapshots[-1]["snapshot_id"]
        if fault == "missing_job_clock":
            header.pop("started_at")
        path.write_bytes(encoded(header))
    before = len(replay.requests)
    replay.rebuild()
    # The bad data remains in the isolated store and journal after reconstruction.
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "PARTIAL"
    assert result["classification"] == classification
    assert result["roster_binding"] == binding
    assert result["steps"] == [] and result["provider_requests"] == 0
    assert len(replay.requests) == before and replay.store.bars == {}
    assert replay.detail(identity)["classification"] == classification


def test_unresolved_admission_can_only_resolve_original_morning_job(tmp_path):
    replay = Replay(tmp_path)
    replay.manual_roster(MIDDAY)
    identity = strategy_slot_id(DAY, "update")
    command = dict(request_id=identity, action="update", end=DAY)
    record = replay.runtime.jobs.submit(command, origin="periodic", launch=False)
    assert record["state"] == "PARTIAL"
    assert record["classification"] == "tdx_periodic_roster_job_missing"
    assert record["roster_binding"]["snapshot_id"] is None
    assert record["roster_binding"]["request_id"] == strategy_slot_id(DAY, "universe")
    replay.manual_roster(LATEST)
    before = len(replay.requests)
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    failed = replay.runtime.jobs.run(identity)
    assert failed["classification"] == "tdx_periodic_roster_job_missing"
    assert len(replay.requests) == before
    replay.members = MORNING
    replay.morning()
    replay.rebuild()
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["roster_binding"]["snapshot_id"] == replay.morning_snapshot
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MORNING)


def test_missed_morning_slot_catches_up_without_backdating_or_extra_daily_job(tmp_path):
    replay = Replay(tmp_path)
    replay.runtime.strategy.set(True)
    identity = replay.dispatch()
    assert not replay.runtime.jobs.path(identity).exists()
    roster_id = strategy_slot_id(DAY, "universe")
    roster = replay.runtime.jobs.run(roster_id)
    assert roster["state"] == "COMPLETE"
    replay.manual_roster(LATEST)
    replay.dispatch()
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["roster_binding"]["request_id"] == roster_id
    assert result["snapshot_id"] == roster["snapshot_id"]
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MORNING)
    # Actual receipt stays actual (the replay target is in the past).
    assert replay.store.snapshots[0]["observed_at"] > DAY


@pytest.mark.parametrize("origin", ["periodic", None, "manual", "absent"])
@pytest.mark.parametrize("automatic_id", [True, False])
def test_old_stored_snapshot_is_readable_and_not_relabelled_as_morning(
    tmp_path, origin, automatic_id
):
    replay = Replay(tmp_path)
    replay.morning()
    old = replay.manual_roster(MIDDAY)
    replay.manual_roster(LATEST)
    identity = strategy_slot_id(DAY, "update") if automatic_id else str(uuid4())
    record = replay.runtime.jobs.submit(
        dict(request_id=identity, action="update", end=DAY), launch=False
    )
    record.update(origin=origin, snapshot_id=old["snapshot_id"])
    if origin == "absent":
        record.pop("origin")
    replay.runtime.jobs.save(record)
    replay.rebuild()
    assert "roster_binding" not in replay.detail(identity)
    assert replay.detail(identity)["snapshot_id"] == old["snapshot_id"]
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["snapshot_id"] == old["snapshot_id"]
    assert "roster_binding" not in result
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MIDDAY)


def test_manual_collection_still_uses_normal_selected_scope_without_periodic_binding(
    tmp_path,
):
    replay = Replay(tmp_path)
    replay.morning()
    replay.manual_roster(LATEST)
    identity = str(uuid4())
    replay.post(
        DAILY + "/jobs",
        dict(
            request_id=identity,
            action="update",
            end=DAY,
            markets=[2],
            instruments=["2:" + LATEST[2]],
        ),
    )
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert "roster_binding" not in result
    assert [r["instrument"] for r in replay.rows()] == [LATEST[2]]


def test_old_snapshot_without_producer_retains_explicit_job_to_snapshot_evidence(
    tmp_path,
):
    replay = Replay(tmp_path)
    replay.morning()
    replay.store.snapshots[0]["details"].pop("producer")
    replay.manual_roster(LATEST)
    identity = replay.dispatch()
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["snapshot_id"] == replay.morning_snapshot
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MORNING)


@pytest.mark.parametrize("origin", ["periodic", None, "absent"])
@pytest.mark.parametrize("slot", ["update", "review"])
def test_old_automatic_slot_without_stored_snapshot_cannot_guess_latest_on_resume(
    tmp_path, origin, slot
):
    replay = Replay(tmp_path)
    replay.morning()
    replay.manual_roster(LATEST)
    identity = strategy_slot_id(DAY, slot)
    record = replay.runtime.jobs.submit(
        dict(request_id=identity, action="update", end=DAY), launch=False
    )
    record["origin"] = origin
    if origin == "absent":
        record.pop("origin")
    replay.runtime.jobs.save(record)
    before = len(replay.requests)
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "PARTIAL"
    assert result["classification"] == "tdx_job_roster_binding_missing"
    assert "roster_binding" not in result and result.get("snapshot_id") is None
    assert len(replay.requests) == before


@pytest.mark.parametrize("fault", ["origin", "state", "snapshot"])
def test_invalid_morning_admission_is_persisted_without_launching(tmp_path, fault):
    replay = Replay(tmp_path)
    replay.morning()
    replay.manual_roster(LATEST)
    path = replay.runtime.jobs.path(replay.roster_id)
    header = json.loads(path.read_bytes())
    if fault == "origin":
        header["origin"] = "manual"
    if fault == "state":
        header["state"] = "PARTIAL"
    if fault == "snapshot":
        header.pop("snapshot_id")
    path.write_bytes(encoded(header))
    before = list(replay.launched)
    identity = strategy_slot_id(DAY, "update")
    record = replay.runtime.jobs.submit(
        dict(request_id=identity, action="update", end=DAY), origin="periodic"
    )
    assert record["state"] == "PARTIAL"
    assert (
        record["classification"]
        == {
            "origin": "tdx_periodic_roster_origin",
            "state": "tdx_periodic_roster_incomplete",
            "snapshot": "tdx_periodic_roster_snapshot_missing",
        }[fault]
    )
    assert record["roster_binding"]["snapshot_id"] is None
    assert replay.launched == before
    assert replay.detail(identity)["classification"] == record["classification"]


def test_bad_morning_evidence_on_resume_preserves_completed_daily_receipts(tmp_path):
    replay = Replay(tmp_path)
    replay.morning()
    identity = replay.dispatch()
    replay.fail = {MORNING[2]}
    first = replay.runtime.jobs.run(identity)
    assert first["state"] == "PARTIAL"
    assert sum(s["state"] == "COMPLETE" for s in first["steps"]) == 2
    path = replay.runtime.jobs.path(replay.roster_id)
    header = json.loads(path.read_bytes())
    header["origin"] = "manual"
    path.write_bytes(encoded(header))
    replay.rebuild()
    before = len(replay.requests)
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["classification"] == "tdx_periodic_roster_origin"
    assert result["steps"] == first["steps"]
    assert result["roster_binding"] == first["roster_binding"]
    assert len(replay.requests) == before
    assert replay.detail(identity)["complete_steps"] == 2


def test_manual_selected_roster_survives_interruption_before_first_shard(
    tmp_path, monkeypatch
):
    replay = Replay(tmp_path)
    replay.morning()
    selected = replay.manual_roster(MIDDAY)
    identity = str(uuid4())
    replay.post(DAILY + "/jobs", dict(request_id=identity, action="update", end=DAY))

    def interrupted(*_):
        raise SystemExit("isolated interruption after usable roster selection")

    monkeypatch.setattr(replay.runtime.jobs, "_daily", interrupted)
    with pytest.raises(SystemExit):
        replay.runtime.jobs.run(identity)
    first = replay.detail(identity)
    assert first["snapshot_id"] == selected["snapshot_id"]
    assert first["roster_selected"] is True
    assert first["step_count"] == 0 and first["provider_requests"] == 0
    replay.manual_roster(LATEST)
    replay.rebuild()
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["snapshot_id"] == selected["snapshot_id"]
    assert "roster_binding" not in result
    assert result["provider_requests"] == 3
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MIDDAY)
    emit(
        "manual_selected",
        dict(first=first, result=replay.detail(identity), rows=replay.rows()),
    )


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("fault", ["stale", "missing", "incomplete"])
def test_manual_rejected_first_selection_can_resume_with_new_usable_roster(
    tmp_path, legacy, fault
):
    replay = Replay(tmp_path)
    replay.morning()
    snapshot = replay.store.snapshots[0]
    if fault == "stale":
        snapshot["details"]["valid_through"] = "2026-11-13"
    elif fault == "incomplete":
        snapshot["details"]["markets"][0]["state"] = "FAILED"
    else:
        replay.store.snapshots.clear()
    identity = str(uuid4())
    replay.post(DAILY + "/jobs", dict(request_id=identity, action="update", end=DAY))
    before = len(replay.requests)
    rejected = replay.runtime.jobs.run(identity)
    classification = {
        "stale": "tdx_roster_stale",
        "missing": "tdx_universe_required",
        "incomplete": "tdx_roster_incomplete",
    }[fault]
    assert rejected["state"] == "PARTIAL"
    assert rejected["classification"] == classification
    assert not rejected["steps"] and len(replay.requests) == before
    assert rejected.get("roster_selected") is not True
    if legacy:
        # Earlier writers saved the rejected candidate ID before require_usable.
        rejected["snapshot_id"] = rejected["roster"]["snapshot_id"]
        replay.runtime.jobs.save(rejected)
    fresh = replay.manual_roster(LATEST)
    replay.rebuild()
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE", result
    assert result["snapshot_id"] == fresh["snapshot_id"]
    assert "roster_binding" not in result
    assert result["provider_requests"] == 3
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(LATEST)


@pytest.mark.parametrize("legacy", [False, True])
def test_manual_accepted_scope_does_not_become_a_rejected_first_selection(
    tmp_path, monkeypatch, legacy
):
    replay = Replay(tmp_path)
    replay.morning()
    identity = str(uuid4())
    replay.post(DAILY + "/jobs", dict(request_id=identity, action="update", end=DAY))

    def interrupted(*_):
        raise SystemExit("after usable selection")

    monkeypatch.setattr(replay.runtime.jobs, "_daily", interrupted)
    with pytest.raises(SystemExit):
        replay.runtime.jobs.run(identity)
    assert replay.detail(identity)["roster_selected"] is True
    if legacy:
        old = replay.runtime.jobs.read(identity)
        old.pop("roster_selected")
        replay.runtime.jobs.save(old)
    snapshot = replay.store.snapshots[0]
    snapshot["details"]["markets"][0]["state"] = "FAILED"
    replay.manual_roster(LATEST)
    before = len(replay.requests)
    replay.rebuild()
    for _ in range(2):
        replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
        rejected = replay.runtime.jobs.run(identity)
        assert rejected["state"] == "PARTIAL"
        assert rejected["classification"] == "tdx_roster_incomplete"
        assert rejected["snapshot_id"] == replay.morning_snapshot
        assert len(replay.requests) == before
    snapshot["details"]["markets"][0]["state"] = "COMPLETE"
    replay.post(DAILY + "/jobs/resume", dict(request_id=identity))
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["snapshot_id"] == replay.morning_snapshot
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MORNING)


@pytest.mark.parametrize("bad_header", [b"{", b"null", b"{}", b'"not a job header"'])
def test_unreadable_morning_is_visible_on_strategy_get_and_recovers_original_slot(
    tmp_path, bad_header
):
    replay = Replay(tmp_path)
    morning = replay.morning()
    replay.manual_roster(MIDDAY)
    replay.manual_roster(LATEST)
    path = replay.runtime.jobs.path(replay.roster_id)
    original = path.read_bytes()
    path.write_bytes(bad_header)
    before, launches = len(replay.requests), list(replay.launched)
    identity = replay.dispatch()
    with replay.client() as client:
        response = client.get(DAILY + "/strategy")
    assert response.status_code == 200
    blocked = response.json()["blocked"]
    assert blocked["classification"] == "tdx_periodic_roster_job_unreadable"
    assert blocked["request_id"] == replay.roster_id and blocked["day"] == DAY
    assert len(replay.requests) == before and replay.launched == launches
    assert path.read_bytes() == bad_header
    replay.rebuild()
    assert replay.runtime.strategy.read()["blocked"] == blocked
    replay.dispatch()
    assert replay.launched == launches
    path.write_bytes(original)
    replay.dispatch()
    result = replay.runtime.jobs.run(identity)
    assert result["state"] == "COMPLETE"
    assert result["roster_binding"]["request_id"] == morning["request_id"]
    assert result["snapshot_id"] == morning["snapshot_id"]
    assert sorted(r["instrument"] for r in replay.rows()) == sorted(MORNING)
    assert replay.runtime.strategy.read()["blocked"] is None
    replay.dispatch()
    assert replay.launched.count(identity) == 1
    emit(
        "corrupt_recovery",
        dict(blocked=blocked, result=replay.detail(identity), rows=replay.rows()),
    )


def test_unreadable_old_morning_does_not_starve_next_evidenced_day(tmp_path):
    replay = Replay(tmp_path)
    replay.morning()
    broken = replay.runtime.jobs.path(replay.roster_id)
    broken.write_bytes(b"{")
    replay.dispatch()
    replay.rebuild()
    calendar = replay.store.strategy_calendar()
    next_day = "2026-11-17"
    replay.store.strategy_calendar = lambda **_: (
        calendar + [dict(c, day=next_day) for c in calendar]
    )
    next_roster = strategy_slot_id(next_day, "universe")
    # Independent completed roster for the next actual calendar day.
    replay.runtime.jobs.submit(
        dict(request_id=next_roster, action="universe", end=next_day),
        origin="periodic",
        launch=False,
    )
    record = replay.runtime.jobs.run(next_roster)
    assert record["state"] == "COMPLETE"
    at = datetime(2026, 11, 17, 7, 35, tzinfo=UTC)
    for _ in range(4):
        replay.runtime.strategy.tick(at)
    identity = strategy_slot_id(next_day, "update")
    result = replay.detail(identity)
    assert result["snapshot_id"] == record["snapshot_id"]
    assert result["roster_binding"]["request_id"] == next_roster
    assert replay.launched.count(identity) == 1
    assert result["state"] == "INTERRUPTED"  # launch stub is intentionally idle.
    assert broken.read_bytes() == b"{"
    assert replay.runtime.strategy.read()["blocked"]["request_id"] == replay.roster_id
