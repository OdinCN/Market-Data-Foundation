"""Real TDX router, ephemeral Type9 reads, closed write ports and simulated clocks."""

from copy import deepcopy
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.tdx_p0_contract import ORIGIN, PREFIX
from market_data_foundation.historical.tdx_p0_source import TdxError
from market_data_foundation.web.tdx_daily_api import (
    DailyRuntime,
    TdxOptions,
    tdx_router,
)
from market_data_foundation.web.tdx_daily_query import DailyQuery

DAY = date(2026, 11, 16)
CODES = [(0, "000001"), (1, "600519"), (2, "920002")]


def at(clock="10:00", day=DAY):
    return datetime.fromisoformat(f"{day}T{clock}:00+08:00")


def native(day=DAY, close=1050):
    return dict(
        Time=f"{day}T15:00:00+08:00",
        Open=1000,
        High=max(1100, close),
        Low=900,
        Close=close,
        Volume=10,
        Amount=10000,
        Last=0,
        Order=0,
        UpCount=0,
        DownCount=0,
    )


def local(day=DAY, market=1, code="600519", close=1050):
    return dict(
        market=market,
        instrument=code,
        trading_day=day,
        open=1000,
        high=max(1100, close),
        low=900,
        close=close,
        volume=10,
        amount=10000,
        source="held-native",
        batch_id="held",
        received_at=at("15:36", day),
        available_at=at("15:37", day),
    )


class ReadStore:
    def __init__(self, rows=(), codes=CODES, closed=()):
        self.rows = list(rows)
        self.reads = []
        self.snapshot = dict(
            snapshot_id="published-roster",
            observed_at=at("16:00", date(2026, 11, 13)),
            available_at=at("16:00", date(2026, 11, 13)),
            members=[
                dict(
                    market=m,
                    instrument=c,
                    name="fixture",
                    evidence=dict(listing_date="2000-01-01"),
                )
                for m, c in codes
            ],
            details=dict(
                valid_through="2026-11-16",
                markets=[dict(market=m, state="COMPLETE") for m in (0, 1, 2)],
                calendar=[
                    dict(
                        market=m,
                        day=str(DAY + timedelta(days=i)),
                        is_open=(DAY + timedelta(days=i)).weekday() < 5
                        and (DAY + timedelta(days=i)) not in closed,
                        available_at="2026-11-01T00:00:00+00:00",
                    )
                    for m in (0, 1, 2)
                    for i in range(-10, 4)
                ],
            ),
        )

    def query(
        self,
        *,
        start,
        end,
        market=None,
        instrument=None,
        as_of=None,
        offset=0,
        limit=100,
        order="asc",
        snapshot_id=None,
    ):
        self.reads.append(
            dict(start=start, end=end, as_of=as_of, snapshot_id=snapshot_id)
        )
        rows = [
            r
            for r in self.rows
            if start <= r["trading_day"] <= end
            and (market is None or r["market"] == market)
            and (instrument is None or r["instrument"] == instrument)
            and (as_of is None or r["available_at"] <= as_of)
        ]
        rows = sorted(
            rows,
            key=lambda r: (r["trading_day"], r["market"], r["instrument"]),
            reverse=order == "desc",
        )
        return dict(
            rows=deepcopy(rows[offset : offset + limit]),
            total=len(rows),
            offset=offset,
            limit=limit,
            next_offset=offset + limit if offset + limit < len(rows) else None,
            as_of=as_of,
            provider_requests=0,
            snapshot_id=(self.snapshot or {}).get("snapshot_id"),
            price_basis="native_projection",
            return_qualification="NOT_QUALIFIED",
        )

    def universe(self, snapshot_id=None, **_):
        if snapshot_id and (self.snapshot or {}).get("snapshot_id") != snapshot_id:
            return None
        return deepcopy(self.snapshot)

    def forbidden(self, *_, **__):
        pytest.fail("GET attempted store/journal/collection write")

    write = save_universe = close_writer = record_attempt = forbidden


class BoundSource:
    def __init__(self, rig):
        self.rig, self.requests, self.closed = rig, 0, False

    def request(self, operation, market, code, start=0, count=0):
        assert operation == "GetKline"
        self.requests += 1
        self.rig.calls.append((market, code, start, count))
        if self.rig.failure:
            raise TdxError(self.rig.failure)
        rows = sorted(self.rig.data.get((market, code), []), key=lambda r: r["Time"])
        begin = max(0, len(rows) - start - count)
        end = max(0, len(rows) - start)
        result = rows[begin:end]
        return dict(
            row=dict(
                request=dict(id=str(uuid4())),
                received_at=self.rig.now.isoformat(),
                source_projection=dict(Count=len(result), List=deepcopy(result)),
            )
        )

    def close(self):
        self.closed = True


class Rig:
    def __init__(self, store=None, *, now=None, allowed=True):
        self.store, self.now = store or ReadStore(), now or at()
        self.data = {k: [native()] for k in CODES}
        self.calls, self.sources, self.failure = [], [], None
        self.allowed = allowed

    def factory(self):
        result = BoundSource(self)
        self.sources.append(result)
        return result

    def reader(self, **kwargs):
        return DailyQuery(
            self.store,
            self.factory,
            allow_source=self.allowed,
            clock=lambda: self.now,
            **kwargs,
        )

    def client(self, tmp_path):
        runtime = DailyRuntime(
            TdxOptions(
                tmp_path,
                Path("/unused"),
                "bound.invalid:1",
                allow_collect=False,
                allow_source_query=self.allowed,
            ),
            self.store,
            query_source_factory=self.factory,
            query_clock=lambda: self.now,
        )
        runtime.jobs.submit = runtime.jobs.resume = self.store.forbidden
        self.runtime = runtime
        app = FastAPI()
        app.include_router(tdx_router(runtime))
        return TestClient(app, base_url=ORIGIN, client=("192.168.10.25", 1234))


def get(client, tail="", mode="history"):
    query = (
        "market=1&instrument=600519&start=2026-11-13&end=2026-11-16"
        if mode == "history"
        else "day=2026-11-16"
    )
    response = client.get(PREFIX + "/query?" + query + tail)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("mode", ["history", "day"])
def test_local_first_supplements_and_refreshes_today_without_any_write(tmp_path, mode):
    held = local(date(2026, 11, 13))
    rig = Rig(ReadStore([held]))
    rig.data[(1, "600519")] = [native(date(2026, 11, 13), 2000), native(close=1100)]
    before = deepcopy(rig.store.rows)
    with rig.client(tmp_path) as client:
        first = get(client, mode=mode)
        today = next(
            r
            for r in first["rows"]
            if r["instrument"] == "600519" and r["trading_day"] == str(DAY)
        )
        assert today["origin"] == "upstream" and today["persisted"] is False
        assert (
            today["is_complete"] is False
            and today["bar_state"] == "intraday_incomplete"
        )
        assert (
            today["available_at"] is None
            and today["observed_at"] == rig.now.isoformat()
        )
        assert today["source_binding"].startswith(
            "mdf.binding.tdx-v100.stock.daily.native@"
        )
        assert first["provider_requests"] == (1 if mode == "history" else 3)
        rig.data[(1, "600519")][-1] = native(close=1200)
        rig.now += timedelta(seconds=1)
        second = get(client, mode=mode)
        changed = next(
            r
            for r in second["rows"]
            if r["instrument"] == "600519" and r["trading_day"] == str(DAY)
        )
        assert (
            changed["close"] == 1200 and changed["observed_at"] != today["observed_at"]
        )
        if mode == "history":
            old = next(r for r in first["rows"] if r["trading_day"] == "2026-11-13")
            assert old["close"] == held["close"] and old["origin"] == "local"
    assert rig.store.rows == before and all(s.closed for s in rig.sources)
    assert not list(tmp_path.rglob("*.json"))


@pytest.mark.parametrize(
    "clock,expected,unfinished",
    [
        ("08:00", 0, None),
        ("09:24", 0, None),
        ("09:25", 1, True),
        ("10:00", 1, True),
        ("11:30", 1, True),
        ("12:30", 1, True),
        ("13:00", 1, True),
        ("14:59", 1, True),
        ("15:00", 1, True),
        ("16:30", 1, False),
    ],
)
def test_sessions_today_no_synthetic_previous_bar(
    tmp_path, clock, expected, unfinished
):
    rig = Rig(now=at(clock))
    with rig.client(tmp_path) as client:
        response = client.get(
            PREFIX + "/query?day=2026-11-16&market=1&instrument=600519"
        )
        assert response.status_code == 200
        result = response.json()
    assert len(rig.calls) == expected
    assert len(result["rows"]) == expected
    if expected:
        assert result["rows"][0]["is_complete"] is (not unfinished)
        assert result["rows"][0]["persisted"] is False


@pytest.mark.parametrize("mode", ["history", "day"])
def test_persisted_after_close_is_zero_source_requests(tmp_path, mode):
    rows = [local(DAY, m, c) for m, c in CODES]
    rows += [local(date(2026, 11, 13))]
    rig = Rig(ReadStore(rows), now=at("16:00"))
    with rig.client(tmp_path) as client:
        result = get(client, mode=mode)
    assert result["provider_requests"] == 0 and not rig.sources
    assert all(r["persisted"] for r in result["rows"])


@pytest.mark.parametrize(
    "tail", ["&local_only=true", "&as_of=2026-11-16T02:00:00.123456Z"]
)
def test_explicit_local_and_historical_cutoff_never_create_source(tmp_path, tail):
    rig = Rig()
    with rig.client(tmp_path) as client:
        result = get(client, tail)
    assert result["provider_requests"] == 0 and not rig.sources
    assert not result["rows"]
    if "as_of" in tail:
        assert result["as_of"].endswith("123456Z") or "123456+00:00" in result["as_of"]


def test_read_source_permission_independent_of_collection_permission(tmp_path):
    rig = Rig(allowed=False)
    with rig.client(tmp_path) as client:
        result = get(client)
    assert (
        result["upstream"]["issues"][0]["classification"]
        == "tdx_source_query_not_enabled"
    )
    assert not rig.sources
    rig.allowed = True  # collection remains disabled in this real router fixture
    with rig.client(tmp_path) as client:
        assert get(client)["provider_requests"] == 1


@pytest.mark.parametrize("day", [date(2026, 11, 14), DAY])
def test_closed_calendar_has_no_source(tmp_path, day):
    rig = Rig(ReadStore(closed=(DAY,)))
    with rig.client(tmp_path) as client:
        response = client.get(PREFIX + f"/query?day={day}")
    assert response.status_code == 200
    assert response.json()["rows"] == [] and not rig.calls


@pytest.mark.parametrize("mode", ["history", "day"])
def test_empty_or_failed_upstream_is_partial_not_yesterday_or_zero_bar(tmp_path, mode):
    rig = Rig()
    rig.data = {k: [native(date(2026, 11, 13))] for k in CODES}
    with rig.client(tmp_path) as client:
        empty = get(client, mode=mode)
        assert not [r for r in empty["rows"] if r["trading_day"] == str(DAY)]
        assert empty["query_coverage"]["missing_count"] > 0
        rig.failure = "timeout"
        failed = get(client, mode=mode)
        assert failed["upstream"]["issues"][0]["classification"] == "timeout"
        assert failed["provider_requests"] == 1
    assert not list(tmp_path.rglob("*.json"))


def test_cross_candidate_pagination_does_not_skip_empty_or_budget_limited_targets():
    rig = Rig()
    rig.data[(0, "000001")] = []
    reader = rig.reader(max_requests=1)
    first = reader.query(start=DAY, end=DAY, limit=100)
    assert first["rows"] == [] and first["total"] == 3
    assert first["next_offset"] == 1 and first["page_end_offset"] == 1
    second = reader.query(start=DAY, end=DAY, offset=first["next_offset"], limit=100)
    assert [(r["market"], r["instrument"]) for r in second["rows"]] == [(1, "600519")]
    assert second["next_offset"] == 2
    third = reader.query(start=DAY, end=DAY, offset=2)
    assert third["next_offset"] is None and third["rows"][0]["market"] == 2
    assert all(c[2:] == (0, 1) for c in rig.calls)


def test_middle_gap_is_filled_local_wins_merge_dedup_sort_and_page():
    d = date(2026, 11, 10)
    rig = Rig(ReadStore([local(d), local(d + timedelta(days=2))]), now=at("16:00"))
    rig.data[(1, "600519")] = [native(d + timedelta(days=i), 1500) for i in range(3)]
    result = rig.reader().query(
        start=d,
        end=d + timedelta(days=2),
        market=1,
        instrument="600519",
        order="desc",
        limit=2,
    )
    assert [r["trading_day"] for r in result["rows"]] == [
        d + timedelta(days=2),
        d + timedelta(days=1),
    ]
    assert result["rows"][0]["close"] == 1050
    assert result["rows"][1]["close"] == 1500
    assert result["total"] == 3 and result["next_offset"] == 2
    assert result["provider_requests"] == 1


def test_no_roster_no_directory_refresh_or_fictitious_denominator():
    rig = Rig()
    rig.store.snapshot = None
    result = rig.reader().query(start=DAY, end=DAY)
    assert result["total"] == 0 and result["rows"] == [] and not rig.calls
    assert (
        result["upstream"]["issues"][0]["classification"] == "tdx_query_roster_required"
    )


@pytest.mark.parametrize(
    "query",
    [
        "day=2026-11-16&local_only=x",
        "day=2026-11-16&limit=-1",
        "day=2026-11-16&market=3",
        "day=2026-11-16&instrument=600519",
        "day=2026-11-16&as_of=2026-11-16T00:00:00",
        "day=2026-11-16&order=bad",
    ],
)
def test_invalid_query_never_constructs_source(tmp_path, query):
    rig = Rig()
    with rig.client(tmp_path) as client:
        assert client.get(PREFIX + "/query?" + query).status_code == 400
    assert not rig.calls


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://public.invalid"},
        {"Host": "192.168.10.20:18767"},
        {"X-Forwarded-Host": "192.168.10.20:18766"},
    ],
)
def test_untrusted_origin_or_forwarding_never_reaches_source(tmp_path, headers):
    rig = Rig()
    with rig.client(tmp_path) as client:
        response = client.get(PREFIX + "/query?day=2026-11-16", headers=headers)
        assert response.status_code == 403
    assert not rig.calls


def test_failed_source_keeps_existing_local_value():
    held = local(date(2026, 11, 13))
    rig = Rig(ReadStore([held]))
    rig.failure = "timeout"
    result = rig.reader().query(
        start=date(2026, 11, 13), end=DAY, market=1, instrument="600519"
    )
    assert len(result["rows"]) == 1 and result["rows"][0]["close"] == held["close"]
    assert result["rows"][0]["persisted"] is True
    assert result["provider_requests"] == 1
    assert result["query_coverage"]["missing_count"] == 1


def test_history_native_pages_overlap_merge_without_duplicate():
    days = []
    d = date(2020, 1, 1)
    while len(days) < 1001:
        if d.weekday() < 5:
            days.append(d)
        d += timedelta(days=1)
    rig = Rig()
    rig.data[(1, "600519")] = [native(d) for d in days]
    result = rig.reader().query(
        start=days[0], end=days[-1], market=1, instrument="600519", limit=10000
    )
    assert (
        len(result["rows"]) == len({r["trading_day"] for r in result["rows"]}) == 1001
    )
    assert rig.calls == [(1, "600519", 0, 800), (1, "600519", 799, 800)]
    assert result["provider_requests"] == 2


def test_snapshot_cursor_does_not_silently_select_new_roster(tmp_path):
    rig = Rig()
    with rig.client(tmp_path) as client:
        first = get(client, "&limit=1&snapshot_id=published-roster", mode="day")
        assert first["next_offset"] == 1
        rig.store.snapshot["snapshot_id"] = "replacement"
        count = len(rig.calls)
        response = client.get(
            PREFIX + "/query?day=2026-11-16&offset=1&snapshot_id=published-roster"
        )
        assert response.status_code == 400
        assert response.json()["classification"] == "tdx_query_snapshot_unavailable"
        assert len(rig.calls) == count


def test_future_day_never_calls_source(tmp_path):
    rig = Rig()
    with rig.client(tmp_path) as client:
        value = client.get(PREFIX + "/query?day=2026-11-17").json()
    assert value["query_coverage"]["session"] == "future_day"
    assert value["rows"] == [] and not rig.calls


@pytest.mark.parametrize("direction", ["history", "day"])
def test_intraday_cached_value_is_identified_when_live_observation_is_missing(
    tmp_path, direction
):
    held = local(DAY)
    held.update(received_at=at("09:45"), available_at=at("09:46"))
    rig = Rig(ReadStore([held]))
    rig.data[(1, "600519")] = [native(date(2026, 11, 13))]
    with rig.client(tmp_path) as client:
        result = get(client, mode=direction)
    today = next(
        row
        for row in result["rows"]
        if row["instrument"] == "600519" and row["trading_day"] == DAY.isoformat()
    )
    assert today["origin"] == "local"
    assert result["query_coverage"]["page_has_all_values"] is False
    assert any(
        i["classification"] == "tdx_query_current_observation_unavailable"
        for i in result["upstream"]["issues"]
    )


def test_real_go_adapter_ephemeral_mode_keeps_qualification_without_retaining_files(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical.tdx_p0_source import GoDailySource
    from market_data_foundation.runtime.tdx_watchlist import V100_DRIVER_SDK_RELEASE
    from tests.test_tdx_p0 import Source

    # Prepared, deterministic wire frame; the adapter still verifies request,
    # response identity, native units and the qualification observation.
    prepared = Source(tmp_path, length=1).request("GetKline", 1, "600519", count=1)
    row = prepared["row"]
    Path(prepared["path"]).unlink()
    sent = {}
    source = GoDailySource(
        "unused", "bound.invalid:1", None, live_opt_in=True, retain_raw=False
    )
    monkeypatch.setattr(source, "_connect", lambda: None)
    monkeypatch.setattr(source, "_send", lambda _kind, request: sent.update(request))

    def receive():
        value = deepcopy(row)
        value.update(
            request=dict(sent),
            sdk_release=V100_DRIVER_SDK_RELEASE,
            requested_at=row["received_at"],
        )
        return value

    monkeypatch.setattr(source, "_receive", receive)
    result = source.request("GetKline", 1, "600519", count=1)
    assert source.requests == 1 and result["path"] is None
    assert result["observation"]["success"] is True
    assert result["row"]["source_projection"]["Count"] == 1
    assert list(tmp_path.iterdir()) == []


def test_held_intraday_version_is_not_mistaken_for_after_close_completion():
    partial = local(DAY, close=1000)
    partial.update(received_at=at("10:00"), available_at=at("10:01"))
    rig = Rig(ReadStore([partial]), now=at("16:00"))
    rig.data[(1, "600519")] = [native(close=1300)]
    historical = rig.reader().query(
        start=DAY, end=DAY, market=1, instrument="600519", as_of=at("10:30")
    )
    assert historical["rows"][0]["is_complete"] is False
    assert historical["provider_requests"] == 0 and not rig.calls
    current = rig.reader().query(start=DAY, end=DAY, market=1, instrument="600519")
    assert current["rows"][0]["close"] == 1300
    assert current["rows"][0]["is_complete"] is True
    assert current["rows"][0]["persisted"] is False
    assert current["provider_requests"] == 1
    assert rig.store.rows == [partial]
