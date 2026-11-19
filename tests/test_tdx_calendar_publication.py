"""Exercise the real publication/read paths with an isolated SQL result fixture."""

import json
from contextlib import nullcontext
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest

from market_data_foundation.historical.tdx_p0_strategy import DailyStrategy
from market_data_foundation.storage.tdx_p0 import PgStore
from scripts import mdf_tdx_trading_calendar as command
from scripts.mdf_tdx_exchange_daily import refresh
from scripts.mdf_tdx_trading_calendar import prepare_calendar, publish_calendar
from tests.test_tdx_trading_calendar import GOV_URL, SUPPLEMENT_URL, YEAR, notice


class CalendarSQL:
    """Only emulate result rows and writes; policy stays in production PgStore."""

    def __init__(self):
        self.rows = []
        self.result = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def transaction(self):
        return nullcontext()

    def execute(self, sql, params=()):
        if sql.startswith("SELECT pg_advisory_xact_lock"):
            pass
        elif sql.startswith("SELECT holidays,available_at,evidence"):
            rows = [
                r
                for r in self.rows
                if r[0] == params[0] and (len(params) == 1 or r[2] <= params[1])
            ]
            self.result = [rows[-1][1:]] if rows else []
        elif sql.startswith("INSERT INTO mdf_tdx_daily.p0_trading_calendar"):
            year, holidays, proof = params
            at = datetime(2026, 11, 20, tzinfo=UTC) + timedelta(seconds=len(self.rows))
            self.rows.append((year, holidays, at, proof.obj))
            self.result = [(at,)]
        elif sql.startswith("SELECT DISTINCT ON (year)"):
            assert "available_at <= %s" in sql
            assert "ORDER BY year,available_at DESC" in sql
            start, end, cutoff = params
            visible = {
                r[0]: r
                for r in self.rows
                if start <= r[0] <= end
                and r[2] <= cutoff
                and r[3].get("source") != "timor_holiday_api"
            }
            self.result = [(year, row[1], row[3]) for year, row in visible.items()]
        else:
            raise AssertionError(sql)
        return self

    def fetchone(self):
        return self.result[0] if self.result else None

    def fetchall(self):
        return self.result


class CalendarStore(PgStore):
    def __init__(self):
        self.sql = CalendarSQL()
        super().__init__(lambda: self.sql)

    def _legacy_trading_calendar(self, as_of, start, end):
        return {}

    def universe(self, **_):
        return None


def candidate(tmp_path):
    def get(url):
        if url == GOV_URL:
            return notice(exchange=False)
        raise httpx.ConnectError("must not be included in observations")

    return prepare_calendar(YEAR, get=get, artifact=tmp_path, government_url=GOV_URL)


def test_complete_candidates_publish_and_drive_all_three_scheduled_consumers(
    tmp_path,
):
    store = CalendarStore()
    holidays, result = candidate(tmp_path)
    saved = publish_calendar(store, holidays, result)
    assert saved["state"] == "PUBLISHED"
    assert saved["publication"]["schedulable"]
    assert not saved["publication"]["exchange_confirmed"]
    assert saved["publication"]["source"] == "state_council_holiday_notice"

    class Jobs:
        directory = tmp_path

        def __init__(self):
            self.store = store
            self.submitted = []

        def path(self, identity):
            return self.directory / identity

        def submit(self, command, *, origin):
            self.path(command["request_id"]).touch()
            self.submitted.append(command["action"])

        def summary(self, identity):
            return {"state": "COMPLETE"}

    jobs = Jobs()
    strategy = DailyStrategy(jobs)
    strategy.set(True)
    for day in (1, 4):
        for hour, minute in ((1, 0), (7, 35)):
            strategy.tick(datetime(YEAR, 1, day, hour, minute, tzinfo=UTC))
            assert strategy.calendar_state == ("CLOSED" if day == 1 else "OPEN")
    assert jobs.submitted == ["universe", "update"]
    args = SimpleNamespace(jobs_dir=tmp_path)
    for year, month, day, expected in (
        (YEAR, 1, 1, "OUTSIDE_TRADING_DAY_WINDOW"),
        (YEAR, 1, 4, "TDX_JOBS_PENDING"),
        (YEAR + 1, 1, 4, "CALENDAR_PENDING"),
    ):
        assert (
            refresh(args, store, datetime(year, month, day, 8, 5, tzinfo=UTC))["state"]
            == expected
        )
    restart = DailyStrategy(jobs)
    restart.tick(datetime(YEAR, 1, 4, 8, tzinfo=UTC))
    assert jobs.submitted == ["universe", "update"]


@pytest.mark.parametrize("correction", [False, True])
def test_source_upgrade_is_visible_as_of_and_idempotent_without_downgrade(
    tmp_path,
    correction,
):
    store = CalendarStore()
    holidays, result = candidate(tmp_path)
    first = publish_calendar(store, holidays, result)
    same = publish_calendar(store, holidays, result)
    assert same["state"] == "ALREADY_PUBLISHED"
    assert same["available_at"] == first["available_at"]
    first_at = datetime.fromisoformat(first["available_at"])

    government = dict(source="state_council_holiday_notice", schedulable=True)
    second_at, changed = store.save_trading_calendar(YEAR, holidays, government)
    assert not changed and second_at == first_at
    assert store.save_trading_calendar(
        YEAR, holidays, dict(government, received_at="new-clock", path="new-artifact")
    ) == (second_at, False)
    market_days = holidays | ({date(YEAR, 1, 4)} if correction else set())
    exchange = dict(source="sse_annual_notice")  # historical rows have no flags
    confirmed_at, changed = store.save_trading_calendar(YEAR, market_days, exchange)
    assert changed and confirmed_at > second_at
    assert store.save_trading_calendar(YEAR, market_days, exchange) == (
        confirmed_at,
        False,
    )
    assert store.save_trading_calendar(YEAR, holidays, government) == (
        confirmed_at,
        False,
    )
    assert len(store.sql.rows) == 2
    day = date(YEAR, 1, 4)
    assert store.trading_calendar(as_of=second_at, start=day, end=day) == {day: True}
    assert store.trading_calendar(as_of=confirmed_at, start=day, end=day) == {
        day: not correction
    }
    retained = publish_calendar(store, holidays, result)
    assert retained["state"] == "RETAINED_HIGHER_AUTHORITY"
    assert retained["publication"] == dict(
        source="sse_annual_notice",
        schedulable=True,
        exchange_confirmed=True,
    )


def test_all_sources_fail_without_mutation_or_turning_unknown_days_open(tmp_path):
    store = CalendarStore()
    holidays, result = candidate(tmp_path)
    saved = publish_calendar(store, holidays, result)
    before = list(store.sql.rows)

    def unavailable(url):
        raise httpx.ConnectError("private transport detail")

    holidays, result = prepare_calendar(
        YEAR,
        get=unavailable,
        artifact=tmp_path,
        government_url=GOV_URL,
    )
    assert holidays is None
    assert result["state"] == "UNAVAILABLE" and not result["schedulable"]
    assert len(result["attempts"]) == 2
    assert all(r["classification"] == "ConnectError" for r in result["attempts"])
    assert publish_calendar(store, holidays, result) == result
    assert store.sql.rows == before
    at = datetime.fromisoformat(saved["available_at"])
    assert store.trading_calendar(
        as_of=at, start=date(YEAR, 1, 1), end=date(YEAR, 1, 1)
    ) == {date(YEAR, 1, 1): False}
    assert (
        store.trading_calendar(
            as_of=at, start=date(YEAR + 1, 1, 1), end=date(YEAR + 1, 1, 1)
        )
        == {}
    )


def test_cross_year_closure_is_visible_only_after_actual_publication():
    store = CalendarStore()
    dec = date(2026, 12, 31)
    source = dict(source="state_council_holiday_notice")
    first, _ = store.save_trading_calendar(
        2026, {date(2026, 1, 1)}, dict(source="sse_annual_notice")
    )
    later, _ = store.save_trading_calendar(2027, {dec, date(2027, 1, 1)}, source)
    assert store.trading_calendar(as_of=first, start=dec, end=dec) == {dec: True}
    assert store.trading_calendar(as_of=later, start=dec, end=dec) == {dec: False}
    assert (
        store.trading_calendar(
            as_of=first - timedelta(seconds=1),
            start=dec,
            end=dec,
        )
        == {}
    )


@pytest.mark.parametrize("available", [True, False])
def test_publish_cli_accepts_candidates_and_does_not_connect_on_source_failure(
    tmp_path,
    monkeypatch,
    capsys,
    available,
):
    store = CalendarStore()
    calls = []

    def send(request):
        if available and str(request.url) == GOV_URL:
            return httpx.Response(200, content=notice(exchange=False))
        return httpx.Response(503)

    client_type = httpx.Client
    monkeypatch.setattr(
        command.httpx,
        "Client",
        lambda **kwargs: client_type(
            **kwargs,
            transport=httpx.MockTransport(send),
        ),
    )

    def connect():
        assert available, "unavailable sources must not connect to the database"
        calls.append("connect")
        return nullcontext(store.sql)

    monkeypatch.setattr(command, "connect", connect)
    monkeypatch.setattr(command, "PgStore", lambda: store)
    monkeypatch.setattr(
        command, "migrate", lambda conn, **kwargs: calls.append("migrate")
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "calendar",
            "--dev-opt-in",
            "--publish",
            "--year",
            str(YEAR),
            "--government-url",
            GOV_URL,
            "--artifact-dir",
            str(tmp_path),
        ],
    )
    if available:
        command.main()
    else:
        with pytest.raises(SystemExit) as exit_info:
            command.main()
        assert exit_info.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == ("PUBLISHED" if available else "UNAVAILABLE")
    assert result["schedulable"] is available
    assert result["exchange_confirmed"] is False
    assert calls == (["connect", "migrate"] if available else [])
    receipt = next(tmp_path.glob("*/receipt.json"))
    assert json.loads(receipt.read_bytes()) == result


def test_provisional_year_cannot_replace_a_day_in_legacy_exchange_evidence(monkeypatch):
    store = CalendarStore()
    day = date(2026, 12, 31)
    monkeypatch.setattr(store, "_legacy_trading_calendar", lambda *_: {day: False})
    dates = {date(2026, 1, 1)}
    provisional, _ = store.save_trading_calendar(
        2026, dates, dict(source="state_council_holiday_notice")
    )
    assert store.trading_calendar(as_of=provisional, start=day, end=day) == {day: False}
    confirmed, _ = store.save_trading_calendar(
        2026, dates, dict(source="sse_annual_notice")
    )
    assert store.trading_calendar(as_of=confirmed, start=day, end=day) == {day: True}


def test_historical_third_party_publication_is_replaced_by_official(tmp_path):
    store = CalendarStore()
    day = date(YEAR, 1, 4)
    cutoff = datetime(2026, 11, 20, tzinfo=UTC)
    store.sql.rows.append(
        (YEAR, [date(YEAR, 1, 1)], cutoff, dict(source="timor_holiday_api"))
    )
    assert store.trading_calendar(as_of=cutoff, start=day, end=day) == {}
    holidays, result = candidate(tmp_path)
    saved = publish_calendar(store, holidays, result)
    assert saved["state"] == "PUBLISHED"
    at = datetime.fromisoformat(saved["available_at"])
    assert store.trading_calendar(as_of=at, start=day, end=day) == {day: True}


def test_exchange_amendment_survives_outages_and_lower_authority(tmp_path):
    store = CalendarStore()
    base, result = candidate(tmp_path)
    evidence = dict(result["evidence"], source="sse_annual_notice")
    first, _ = store.save_trading_calendar(YEAR, base, evidence)
    changed_day = date(YEAR, 5, 6)
    correction = dict(
        year=YEAR,
        state="UNAVAILABLE",
        schedulable=False,
        exchange_confirmed=False,
        evidence=dict(
            supplements=[
                dict(
                    url=SUPPLEMENT_URL,
                    published_on="2027-04-19",
                    windows=[
                        dict(names=["劳动节"], start="2027-05-01", end="2027-05-06")
                    ],
                )
            ]
        ),
    )
    saved = publish_calendar(store, None, correction)
    assert saved["state"] == "PUBLISHED"
    assert saved["exchange_confirmed"]
    assert changed_day.isoformat() in saved["holidays"]
    second = datetime.fromisoformat(saved["available_at"])
    assert second > first
    assert store.trading_calendar(as_of=first, start=changed_day, end=changed_day) == {
        changed_day: True
    }
    assert store.trading_calendar(as_of=second, start=changed_day, end=changed_day) == {
        changed_day: False
    }
    assert store.save_trading_calendar(YEAR, base, evidence) == (second, False)
    assert publish_calendar(store, base, result)["state"] == "RETAINED_HIGHER_AUTHORITY"
    assert store.save_trading_calendar(YEAR, None, correction["evidence"]) == (
        second,
        False,
    )
