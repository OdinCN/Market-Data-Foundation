"""Deterministic pages, retained recovery, local API and coverage failures."""

import base64
import struct
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.tdx_p0_contract import ORIGIN, PREFIX
from market_data_foundation.historical.tdx_p0_jobs import (
    DailyJobs,
    batch_from_pending,
    coverage,
    latest_day,
    validate,
)
from market_data_foundation.historical.tdx_p0_source import (
    GoDailySource,
    TdxError,
    encoded,
    verify_wire,
)
from market_data_foundation.qualification.tdx_market_evidence import request_wire
from market_data_foundation.storage.tdx_daily_pg import OfflineCurrent
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)


def test_daily_driver_forged_sidecar_cannot_authorize_unknown_bytes(
    tmp_path, monkeypatch
):
    binary = tmp_path / "unknown-driver"
    binary.write_bytes(b"#!/bin/sh\nexit 0\n")
    binary.chmod(0o700)
    binary.with_suffix(".build.json").write_bytes(
        encoded(
            dict(state="built", binary_sha256=sha256(binary.read_bytes()).hexdigest())
        )
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("untrusted_child_must_not_start")

    monkeypatch.setattr(
        "market_data_foundation.historical.tdx_p0_source.subprocess.Popen", forbidden
    )
    source = GoDailySource(binary, "unused:1", tmp_path / "raw", live_opt_in=True)
    with pytest.raises(ContractError, match="driver_binary_release_mismatch"):
        source._connect()


class Store:
    def __init__(self):
        self.oracle = OfflineCurrent()
        self.fail_after_commit = False
        self.snapshots = []

    def universe(self, **_):
        return (
            self.snapshots[-1]
            if self.snapshots
            else dict(
                snapshot_id="roster",
                members=[dict(market=2, instrument="920002", name="fixture")],
                details=dict(markets=[dict(market=2, state="COMPLETE")]),
            )
        )

    def save_universe(self, snapshot):
        self.snapshots.append(snapshot)

    def write(self, batch):
        result = self.oracle.apply(batch)
        if self.fail_after_commit:
            self.fail_after_commit = False
            raise TdxError("commit_outcome_unknown")
        return result

    def query(self, **_):
        return dict(rows=[], total=0, as_of=datetime.now(UTC), provider_requests=0)

    def summary(self):
        return dict(markets=[], provider_requests=0)


class Source:
    def __init__(self, directory, *, repeated=False, fail=False, length=1000):
        self.directory = directory
        self.requests = 0
        self.closed = False
        self.repeated = repeated
        self.fail = fail
        self.offsets = []
        self.length = length

    def request(self, operation, market, code="", start=0, count=0):
        self.requests += 1
        self.offsets.append(start)
        if self.fail:
            raise TdxError("timeout")
        request = dict(
            id=uuid4().hex,
            operation=operation,
            market=market,
            code=code,
            start=start,
            count=count,
        )
        if operation == "GetKline":
            all_days = [
                date(2020, 1, 1) + timedelta(days=i) for i in range(self.length)
            ]
            offset = 0 if self.repeated else start
            selected = all_days[
                max(0, len(all_days) - offset - count) : len(all_days) - offset
            ]
            rows = [
                dict(
                    Time=d.isoformat() + "T15:00:00+08:00",
                    Open=1000,
                    High=1100,
                    Low=900,
                    Close=1050,
                    Volume=0,
                    Amount=0,
                    Last=999,
                    Order=0,
                    UpCount=0,
                    DownCount=0,
                )
                for d in selected
            ]
            projection = dict(Count=len(rows), List=rows)
            payload = struct.pack("<H", len(rows))
        else:
            projection = dict(
                Count=1,
                List=[
                    dict(
                        Code="920002"
                        if market == 2
                        else "600000"
                        if market == 1
                        else "000001",
                        Name="fixture",
                    )
                ],
            )
            payload = b"\x01\x00"
        sent = (
            request_wire(SimpleNamespace(**request), 0)
            if operation != "GetBJDirectory"
            else request_wire(None, 0)
        )
        header = bytearray(16)
        header[:4] = bytes.fromhex("b1cb7400")
        struct.pack_into("<I", header, 5, 2)
        header[10:12] = sent[10:12]
        struct.pack_into("<HH", header, 12, len(payload), len(payload))
        row = dict(
            request=request,
            received_at=datetime.now(UTC).isoformat(),
            request_wire=base64.b64encode(sent).decode(),
            response_wire=base64.b64encode(header + payload).decode(),
            source_projection=projection,
            classification="retained_raw",
        )
        raw = encoded(row)
        path = self.directory / (request["id"] + ".json")
        path.write_bytes(raw)
        return dict(row=row, path=str(path), sha256=sha256(raw).hexdigest())

    def close(self):
        self.closed = True


def jobs(tmp_path, store=None, **source_options):
    store = store or Store()
    sources = []

    def factory(_):
        source = Source(tmp_path, **source_options)
        sources.append(source)
        return source

    return DailyJobs(tmp_path / "jobs", store, factory, workers=1), store, sources


def test_bj_long_history_overlap_and_fresh_rerun_only_confirms(tmp_path):
    host, store, sources = jobs(tmp_path)
    command = dict(
        request_id=str(uuid4()), action="init", start="1990-01-01", end="2025-01-01"
    )
    host.submit(command, launch=False)
    first = host.run(command["request_id"])
    assert first["state"] == "COMPLETE"
    assert sources[0].offsets == [0, 799]
    assert len(store.oracle.current) == 1000
    assert first["steps"][0]["boundary"] == "SOURCE_EXHAUSTED_LISTING_UNKNOWN"
    assert first["provider_requests"] == 2
    second = dict(command, request_id=str(uuid4()))
    host.submit(second, launch=False)
    repeated = host.run(second["request_id"])
    assert repeated["state"] == "COMPLETE"
    assert repeated["steps"][0]["counts"]["new"] == 0
    assert repeated["steps"][0]["counts"]["unchanged"] == 1001
    assert store.oracle.corrections == []


def test_commit_unknown_cold_resume_uses_original_page_without_refetch(tmp_path):
    host, store, sources = jobs(tmp_path)
    store.fail_after_commit = True
    command = dict(
        request_id=str(uuid4()), action="init", start="1990-01-01", end="2025-01-01"
    )
    host.submit(command, launch=False)
    failed = host.run(command["request_id"])
    pending = failed["steps"][0]["pending"]
    assert failed["state"] == "PARTIAL" and pending
    assert len(store.oracle.current) == 800
    rebuilt = DailyJobs(host.directory, store, host.source_factory, workers=1)
    queued = rebuilt.resume(command["request_id"], launch=False)
    assert queued["finished_at"] is None
    assert queued["last_attempt_finished_at"] == failed["finished_at"]
    assert queued["resumed_at"] >= failed["finished_at"]
    completed = rebuilt.run(command["request_id"])
    assert completed["state"] == "COMPLETE"
    assert sources[1].offsets == [799]
    assert completed["steps"][0]["counts"]["replays"] == 1
    assert len(store.oracle.current) == 1000
    assert completed["steps"][0]["pending"] is None


def test_terminal_single_overlap_is_source_exhaustion_not_no_progress(tmp_path):
    host, store, sources = jobs(tmp_path, length=1599)
    command = dict(
        request_id=str(uuid4()), action="init", start="1990-01-01", end="2025-01-01"
    )
    host.submit(command, launch=False)
    result = host.run(command["request_id"])
    assert result["state"] == "COMPLETE"
    assert sources[0].offsets == [0, 799, 1598]
    assert len(store.oracle.current) == 1599


def test_resume_old_increment_excludes_proven_retired_member_without_refetch(tmp_path):
    host, store, sources = jobs(tmp_path, fail=True)
    command = dict(
        request_id=str(uuid4()), action="update", start="2024-12-01", end="2025-01-01"
    )
    host.submit(command, launch=False)
    failed = host.run(command["request_id"])
    assert failed["state"] == "PARTIAL"
    assert failed["steps"][0]["pending"] is None
    snapshot = store.universe()
    snapshot["members"][0]["evidence"] = dict(
        listing_date="2020-01-01", delist_date="2024-11-30", valid_through="2025-01-01"
    )
    store.snapshots.append(snapshot)
    host.resume(command["request_id"], launch=False)
    completed = host.run(command["request_id"])
    assert completed["state"] == "COMPLETE"
    assert completed["steps"][0]["boundary"] == "EXCLUDED_BY_EXCHANGE_LIFECYCLE"
    assert sources[1].offsets == []


def test_targeted_large_roster_uses_declared_body_budget(tmp_path):
    runtime = DailyRuntime(
        TdxOptions(tmp_path, Path("/unused"), "unused", True),
        Store(),
        lambda _: Source(tmp_path),
    )
    runtime.jobs.launch = lambda _: None
    app = FastAPI()
    app.include_router(tdx_router(runtime))
    command = dict(
        request_id=str(uuid4()),
        action="init",
        end="2025-01-01",
        instruments=[f"0:{i:06}" for i in range(339)],
    )
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        response = client.post(
            PREFIX + "/jobs", json=command, headers={"Origin": ORIGIN}
        )
    assert response.status_code == 202
    assert len(response.json()["command"]["instruments"]) == 339


def test_duplicate_page_stops_and_preserves_durable_progress(tmp_path):
    host, store, _ = jobs(tmp_path, repeated=True)
    command = dict(
        request_id=str(uuid4()), action="init", start="1990-01-01", end="2025-01-01"
    )
    host.submit(command, launch=False)
    result = host.run(command["request_id"])
    assert result["state"] == "PARTIAL"
    assert result["steps"][0]["pending"] is not None
    assert len(store.oracle.current) == 800


def test_retained_tamper_rejected_before_write(tmp_path):
    source = Source(tmp_path)
    outcome = source.request("GetKline", 2, "920002", 0, 32)
    pending = dict(
        path=outcome["path"],
        sha256=outcome["sha256"],
        admitted_at=datetime.now(UTC).isoformat(),
        batch_id="test",
        start="1990-01-01",
        end="2025-01-01",
    )
    assert len(batch_from_pending(pending).bars) == 32
    Path(outcome["path"]).write_bytes(b"{}")
    with pytest.raises(ContractError, match="tdx_retained_digest"):
        batch_from_pending(pending)
    row = outcome["row"]
    wire = bytearray(base64.b64decode(row["response_wire"]))
    wire[5] ^= 1
    row["response_wire"] = base64.b64encode(wire).decode()
    with pytest.raises(ContractError):
        verify_wire(row)


def test_native_bj_zero_volume_does_not_prove_suspension_or_historical_membership():
    result = coverage(Store(), date(2025, 1, 1))
    assert result["complete"] is False
    assert result["markets"][2]["roster_count"] == 1
    assert result["markets"][2]["unknown"] == 1
    assert result["markets"][2]["confirmed_suspended"] == 0
    assert result["markets"][0]["roster_count"] is None
    assert result["markets"][2]["expected_trading_count"] is None


def test_intraday_day_is_never_admitted_as_after_close():
    assert latest_day(datetime(2026, 11, 12, 7, 4, tzinfo=UTC)) == date(2026, 11, 11)
    assert latest_day(datetime(2026, 11, 12, 7, 5, tzinfo=UTC)) == date(2026, 11, 12)
    with pytest.raises(ContractError):
        validate(dict(request_id=str(uuid4()), action="init", end="2099-01-01"))
    with pytest.raises(ContractError):
        validate(dict(request_id=str(uuid4()), action="init", markets=[True]))
    with pytest.raises(ContractError):
        GoDailySource("unused", "unused", Path("/unused"))


def test_local_query_and_help_no_source_effects_and_strict_parameters(tmp_path):
    store = Store()

    def forbidden(_):
        raise AssertionError("local query created a source")

    runtime = DailyRuntime(
        TdxOptions(tmp_path, Path("/unused"), "unused"), store, forbidden
    )
    app = FastAPI()
    app.include_router(tdx_router(runtime))
    with TestClient(app) as client:
        assert (
            client.get(PREFIX + "/query?day=2025-01-01").json()["provider_requests"]
            == 0
        )
        assert (
            client.get(PREFIX + "/query?day=2025-01-01&local_only=false").status_code
            == 400
        )
        assert (
            client.get(PREFIX + "/query?day=2025-01-01&day=2025-01-02").status_code
            == 400
        )
        assert client.get(PREFIX + "/query?day=2025-01-01&basis=qfq").status_code == 400
        help = client.get(PREFIX + "/help").json()
        assert help["markets"]["2"] == "BJ"
        assert client.get(help["examples"]["cross_section"]).status_code == 200
        assert (
            client.post(
                PREFIX + "/jobs", json=dict(request_id=str(uuid4()), action="init")
            ).status_code
            == 403
        )


def test_strategy_actual_executor_dispatches_slot_and_pause_stops_new_jobs(tmp_path):
    runtime = DailyRuntime(
        TdxOptions(tmp_path, Path("/unused"), "unused", True),
        Store(),
        lambda _: Source(tmp_path),
    )
    launched = []
    runtime.jobs.launch = lambda identity: launched.append(identity)
    at = datetime(2026, 1, 5, 8, tzinfo=UTC)
    runtime.strategy.set(True)
    runtime.strategy.tick(at)
    assert not launched
    assert runtime.strategy.read()["calendar_state"] == "UNKNOWN"
    snapshot = runtime.store.universe()
    snapshot["details"]["calendar"] = [
        dict(
            market=m,
            day="2026-01-05",
            is_open=True,
            available_at="2026-01-01T00:00:00+00:00",
        )
        for m in range(3)
    ]
    runtime.store.snapshots.append(snapshot)
    runtime.strategy.tick(at)
    assert len(launched) == 1
    snapshot["details"]["calendar"][0]["is_open"] = False
    runtime.strategy.tick(at)
    assert runtime.strategy.read()["calendar_state"] == "CLOSED"
    runtime.strategy.tick(at)
    assert len(launched) == 1
    runtime.strategy.set(False)
    runtime.strategy.tick(at + timedelta(days=1))
    assert len(launched) == 1


def test_dev_hosts_share_jobs_but_have_independent_strategy_switches(tmp_path):
    first = DailyRuntime(
        TdxOptions(tmp_path, Path("/unused"), "unused", True),
        Store(),
        lambda _: Source(tmp_path),
    )
    second = DailyRuntime(
        TdxOptions(
            tmp_path,
            Path("/unused"),
            "unused",
            True,
            origin="http://192.168.10.20:18767",
        ),
        Store(),
        lambda _: Source(tmp_path),
    )
    first.strategy.set(True)
    second.strategy.set(False)
    assert first.strategy.read()["enabled"] is True
    assert second.strategy.read()["enabled"] is False
    assert first.jobs.directory == second.jobs.directory


def test_write_origin_and_job_id_replay_conflict(tmp_path):
    runtime = DailyRuntime(
        TdxOptions(tmp_path, Path("/unused"), "unused", True),
        Store(),
        lambda _: Source(tmp_path),
    )
    runtime.jobs.launch = lambda _: None
    app = FastAPI()
    app.include_router(tdx_router(runtime))
    command = dict(request_id=str(uuid4()), action="init", end="2025-01-01")
    with TestClient(app, base_url=ORIGIN, client=("127.0.0.1", 5000)) as client:
        assert client.post(PREFIX + "/jobs", json=command).status_code == 403
        first = client.post(PREFIX + "/jobs", json=command, headers={"Origin": ORIGIN})
        assert first.status_code == 202
        assert (
            client.post(
                PREFIX + "/jobs", json=command, headers={"Origin": ORIGIN}
            ).json()["request_id"]
            == command["request_id"]
        )
        assert (
            client.post(
                PREFIX + "/jobs",
                json=dict(command, end="2024-01-01"),
                headers={"Origin": ORIGIN},
            ).status_code
            == 400
        )
