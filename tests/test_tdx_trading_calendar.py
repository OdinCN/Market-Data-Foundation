import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.tdx_p0_jobs import strategy_slot_id
from market_data_foundation.historical.tdx_p0_strategy import DailyStrategy
from market_data_foundation.historical.tdx_trading_calendar import (
    EXCHANGE_FULL_YEAR_TITLE,
    EXCHANGE_INDEX,
    EXCHANGE_TITLE,
    GOVERNMENT_TITLE,
    HOLIDAYS,
    TIMOR_URL,
    exchange_notice_url,
    government_notice_url,
    government_search_url,
    notice_holidays,
    timor_holidays,
    year_sessions,
)
from scripts.mdf_tdx_exchange_daily import refresh
from scripts.mdf_tdx_trading_calendar import prepare_calendar, scheduled_year

SSE_URL = "https://www.sse.com.cn/disclosure/announcement/general/c/annual.shtml"
GOV_URL = "https://www.gov.cn/zhengce/content/annual.htm"
YEAR = 2027
NOTICE = """
元旦：1月1日（周五）至3日（周日）{closure}。
春节：2月5日（农历除夕、周五）至2月13日（周六）{closure}。2月14日（周日）上班。
清明节：4月3日至5日{closure}。
劳动节：5月1日至5月5日{closure}。
端午节：6月9日（周三）{closure}。
中秋节：9月15日（周三）{closure}。
国庆节：10月1日至7日{closure}。
"""


def notice(*, exchange):
    title = EXCHANGE_TITLE if exchange else GOVERNMENT_TITLE
    return (
        f"<h1>{title.format(year=YEAR)}</h1>"
        + NOTICE.format(closure="休市" if exchange else "放假")
    ).encode()


def index():
    title = EXCHANGE_TITLE.format(year=YEAR)
    return f'<a href="{SSE_URL}"><strong>{title}</strong></a>'.encode()


def timor():
    days = {
        "01-01": "元旦",
        "02-05": "春节",
        "04-05": "清明节",
        "05-05": "劳动节",
        "06-09": "端午节",
        "09-15": "中秋节",
        "10-01": "国庆节",
    }
    rows = {k: dict(date=f"{YEAR}-{k}", name=v, holiday=True) for k, v in days.items()}
    rows["02-14"] = dict(date=f"{YEAR}-02-14", name="春节调休", holiday=False)
    return json.dumps(dict(code=0, holiday=rows)).encode()


def test_annual_holidays_support_single_dates_and_weekend_workdays():
    for exchange in (True, False):
        holidays = notice_holidays(notice(exchange=exchange), YEAR, exchange=exchange)
        calendar = year_sessions(YEAR, holidays)
        assert len(calendar) == 365 and sum(calendar.values()) == 243
        assert not calendar[date(YEAR, 1, 1)]
        assert calendar[date(YEAR, 1, 4)]
        assert not calendar[date(YEAR, 2, 14)]
        assert not calendar[date(YEAR, 6, 9)]
        assert not calendar[date(YEAR, 9, 15)]


def test_annual_input_rejects_partial_notice_and_unsafe_link():
    with pytest.raises(ContractError, match="holiday_missing_or_ambiguous"):
        notice_holidays(
            notice(exchange=True).replace("中秋节".encode(), b""), YEAR, exchange=True
        )
    with pytest.raises(ContractError, match="exchange_notice_unavailable"):
        exchange_notice_url(index().replace(b"www.sse.com.cn", b"example.com"), YEAR)


@pytest.mark.parametrize(
    ("missing", "expected", "attempted"),
    [
        (
            set(),
            "sse_annual_notice",
            [GOV_URL, EXCHANGE_INDEX, SSE_URL],
        ),
        (
            {EXCHANGE_INDEX},
            "state_council_holiday_notice",
            [GOV_URL, EXCHANGE_INDEX],
        ),
        (
            {EXCHANGE_INDEX, GOV_URL},
            "timor_holiday_api",
            [GOV_URL, TIMOR_URL.format(year=YEAR), EXCHANGE_INDEX],
        ),
        (
            {GOV_URL, TIMOR_URL.format(year=YEAR)},
            "sse_annual_notice",
            [GOV_URL, TIMOR_URL.format(year=YEAR), EXCHANGE_INDEX, SSE_URL],
        ),
    ],
)
def test_source_fallback_is_explicit_and_candidates_are_not_confirmed(
    tmp_path, missing, expected, attempted
):
    responses = {
        EXCHANGE_INDEX: index(),
        SSE_URL: notice(exchange=True),
        GOV_URL: notice(exchange=False),
        TIMOR_URL.format(year=YEAR): timor(),
    }
    calls = []

    def get(url):
        calls.append(url)
        if url in missing:
            raise httpx.ConnectError("upstream unavailable")
        return responses[url]

    holidays, result = prepare_calendar(
        YEAR, get=get, artifact=tmp_path, government_url=GOV_URL
    )
    assert calls == attempted
    assert date(YEAR, 1, 1) in holidays
    assert result["source"] == expected
    assert result["state"] == (
        "CONFIRMED" if expected == "sse_annual_notice" else "AWAITING_EXCHANGE"
    )
    assert len(result["attempts"]) == len(missing)
    assert result["schedulable"]
    assert result["exchange_confirmed"] == (expected == "sse_annual_notice")
    assert (
        result["evidence"]["url"]
        == {
            "sse_annual_notice": SSE_URL,
            "state_council_holiday_notice": GOV_URL,
            "timor_holiday_api": TIMOR_URL.format(year=YEAR),
        }[expected]
    )
    if expected == "sse_annual_notice" and GOV_URL not in missing:
        assert (
            result["evidence"]["candidate"]["source"] == "state_council_holiday_notice"
        )
        assert result["evidence"]["reconciliation"]["matches"]


def test_both_scheduled_consumers_read_one_calendar_across_years(tmp_path):
    class Store:
        def trading_calendar(self, *, as_of, start, end):
            return {
                day: opened
                for day, opened in (
                    (date(2026, 12, 31), True),
                    (date(2027, 1, 1), False),
                    (date(2027, 1, 4), True),
                )
                if start <= day <= end
            }

    class Jobs:
        directory = tmp_path
        store = Store()

        def path(self, identity):
            return tmp_path / identity

        def submit(self, command, *, origin):
            self.path(command["request_id"]).touch()
            submitted.append((command["end"], command["action"], origin))

        def summary(self, identity):
            return {"state": "COMPLETE"}

    submitted = []
    strategy = DailyStrategy(Jobs())
    strategy.set(True)
    for year, month, day, expected in (
        (2026, 12, 31, "OPEN"),
        (2027, 1, 1, "CLOSED"),
        (2027, 1, 4, "OPEN"),
    ):
        for hour, minute in ((1, 0), (7, 35)):
            strategy.tick(datetime(year, month, day, hour, minute, tzinfo=UTC))
            assert strategy.calendar_state == expected
    assert submitted == [
        ("2026-12-31", "universe", "periodic"),
        ("2026-12-31", "update", "periodic"),
        ("2027-01-04", "universe", "periodic"),
        ("2027-01-04", "update", "periodic"),
    ]
    restart = DailyStrategy(Jobs())
    restart.tick(datetime(2027, 1, 4, 8, tzinfo=UTC))
    assert len(submitted) == 4
    assert Jobs().path(strategy_slot_id("2027-01-04", "update")).exists()
    assert (
        refresh(None, Store(), datetime(2027, 1, 1, 8, 5, tzinfo=UTC))["state"]
        == "OUTSIDE_TRADING_DAY_WINDOW"
    )
    assert (
        refresh(None, Store(), datetime(2027, 1, 5, 8, 5, tzinfo=UTC))["state"]
        == "CALENDAR_PENDING"
    )


def test_delayed_yearly_timer_uses_shanghai_rollover():
    assert scheduled_year(datetime(2026, 10, 31, 15, 59, tzinfo=UTC)) == 2026
    assert scheduled_year(datetime(2026, 10, 31, 16, tzinfo=UTC)) == 2027
    assert scheduled_year(datetime(2026, 11, 30, 16, tzinfo=UTC)) == 2027
    assert scheduled_year(datetime(2026, 12, 31, 16, tzinfo=UTC)) == 2027
    assert scheduled_year(datetime(2027, 1, 4, 1, tzinfo=UTC)) == 2027


def test_exchange_new_year_holiday_can_start_in_previous_december():
    raw = (
        notice(exchange=True)
        .replace(
            "1月1日（周五）至3日（周日）".encode(),
            "2026年12月31日（周四）至2027年1月3日（周日）".encode(),
        )
        .replace(
            EXCHANGE_TITLE.format(year=YEAR).encode(),
            EXCHANGE_FULL_YEAR_TITLE.format(year=YEAR).encode(),
        )
    )
    holidays = notice_holidays(raw, YEAR, exchange=True)
    assert date(YEAR - 1, 12, 31) in holidays
    assert date(YEAR, 1, 3) in holidays


def test_two_holidays_can_share_one_official_clause():
    raw = (
        notice(exchange=True)
        .replace("中秋节：9月15日（周三）休市。".encode(), b"")
        .replace("国庆节：".encode(), "国庆节、中秋节：".encode())
    )
    holidays = notice_holidays(raw, YEAR, exchange=True)
    assert date(YEAR, 10, 1) in holidays
    assert date(YEAR, 10, 7) in holidays
    assert date(YEAR, 9, 15) not in holidays


def test_official_government_discovery_requires_unique_exact_title(tmp_path):
    index_url = government_search_url(YEAR)
    row = {
        "title": GOVERNMENT_TITLE.format(year=f"<em>{YEAR}</em>"),
        "puborg": "国务院办公厅",
        "url": GOV_URL,
    }

    def policy_results(*rows):
        return json.dumps(dict(code=200, searchVO={"listVO": list(rows)})).encode()

    raw = policy_results(row)
    assert government_notice_url(raw, YEAR) == GOV_URL
    responses = {index_url: raw, GOV_URL: notice(exchange=False), EXCHANGE_INDEX: b""}
    holidays, result = prepare_calendar(
        YEAR, get=responses.__getitem__, artifact=tmp_path
    )
    assert holidays and result["schedulable"] and not result["exchange_confirmed"]
    assert result["evidence"]["index"]["url"] == index_url
    for invalid in (
        policy_results(dict(row, url="https://example.org/zhengce/annual.htm")),
        policy_results(dict(row, title=GOVERNMENT_TITLE.format(year=2026))),
        policy_results(dict(row, puborg="其他机构")),
        policy_results(row, dict(row, url=GOV_URL.replace("annual", "second"))),
    ):
        with pytest.raises(ContractError, match="government_notice_unavailable"):
            government_notice_url(invalid, YEAR)


def test_exchange_disagreement_retains_both_originals_and_market_correction(tmp_path):
    responses = {
        GOV_URL: notice(exchange=False),
        EXCHANGE_INDEX: index(),
        SSE_URL: notice(exchange=True).replace(
            "端午节：6月9日（周三）休市".encode(), "端午节：6月9日至11日休市".encode()
        ),
    }
    holidays, result = prepare_calendar(
        YEAR,
        get=responses.__getitem__,
        artifact=tmp_path,
        government_url=GOV_URL,
    )
    assert date(YEAR, 6, 10) in holidays
    assert result["exchange_confirmed"]
    assert result["evidence"]["reconciliation"] == dict(
        matches=False,
        exchange_only=["2027-06-10", "2027-06-11"],
        candidate_only=[],
    )
    assert (tmp_path / "government-notice.html").read_bytes() == responses[GOV_URL]
    assert (tmp_path / "sse-notice.html").read_bytes() == responses[SSE_URL]


def historical_timor(year):
    # Compact semantic fixtures for the retained originals, without copying raw
    # provider responses into Git. 2025's Mid-Autumn day is inside National Day.
    spans = {
        2013: [
            ("01-01", "01-03", "元旦"),
            ("02-09", "02-15", "春节"),
            ("04-04", "04-06", "清明节"),
            ("04-29", "05-01", "劳动节"),
            ("06-10", "06-12", "端午节"),
            ("09-19", "09-21", "中秋节"),
            ("10-01", "10-07", "国庆节"),
        ],
        2025: [
            ("01-01", "01-01", "元旦"),
            ("01-28", "02-04", "春节"),
            ("04-04", "04-06", "清明节"),
            ("05-01", "05-05", "劳动节"),
            ("05-31", "06-02", "端午节"),
            ("10-01", "10-08", "国庆节"),
            ("10-06", "10-06", "中秋节"),
        ],
        2027: [
            ("01-01", "01-03", "元旦"),
            ("02-05", "02-13", "春节"),
            ("04-03", "04-05", "清明节"),
            ("05-01", "05-05", "劳动节"),
            ("06-09", "06-09", "端午节"),
            ("09-15", "09-15", "中秋节"),
            ("10-01", "10-07", "国庆节"),
        ],
    }[year]
    rows = {}
    spring_names = (
        "除夕",
        "初一",
        "初二",
        "初三",
        "初四",
        "初五",
        "初六",
        "初七",
        "初八",
    )
    for start, end, name in spans:
        start, end = (date.fromisoformat(f"{year}-{d}") for d in (start, end))
        for offset in range((end - start).days + 1):
            day = start + timedelta(days=offset)
            rows[day.strftime("%m-%d")] = dict(
                date=day.isoformat(),
                holiday=True,
                name=spring_names[offset] if name == "春节" else name,
            )
    return dict(code=0, holiday=rows)


@pytest.mark.parametrize("year", [2013, 2025, 2027])
def test_timor_historical_shapes_remain_complete_without_spring_festival_label(year):
    payload = historical_timor(year)
    holidays = timor_holidays(json.dumps(payload).encode(), year)
    assert len(holidays) == {2013: 29, 2025: 28, 2027: 29}[year]
    if year == 2025:
        assert date(year, 10, 6) in holidays and date(year, 6, 2) in holidays
    # A make-up-work label must never satisfy the missing holiday category.
    rows = {k: r for k, r in payload["holiday"].items() if r["name"] != "中秋节"}
    rows["09-22"] = dict(date=f"{year}-09-22", name="中秋节后补班", holiday=False)
    with pytest.raises(ContractError, match="timor_incomplete"):
        timor_holidays(json.dumps(dict(code=0, holiday=rows)).encode(), year)


def test_timor_new_year_uses_row_year_and_validates_month_day_key():
    payload = historical_timor(2027)
    payload["holiday"]["12-31"] = dict(date="2026-12-31", name="元旦", holiday=True)
    assert date(2026, 12, 31) in timor_holidays(json.dumps(payload).encode(), 2027)
    payload["holiday"]["12-31"]["date"] = "2025-12-31"
    with pytest.raises(ContractError, match="timor_row"):
        timor_holidays(json.dumps(payload).encode(), 2027)


def test_government_lookup_without_results_uses_timor(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        if url == TIMOR_URL.format(year=YEAR):
            return timor()
        if url == government_search_url(YEAR):
            return b'{"code":1001,"searchVO":null}'
        return b""

    holidays, result = prepare_calendar(YEAR, get=get, artifact=tmp_path)
    assert holidays and result["schedulable"]
    assert calls == [
        government_search_url(YEAR),
        TIMOR_URL.format(year=YEAR),
        EXCHANGE_INDEX,
    ]
    assert result["attempts"][0] == dict(
        source="state_council_holiday_notice",
        classification="tdx_calendar_government_notice_unavailable",
    )


@pytest.mark.parametrize("missing", HOLIDAYS)
def test_timor_still_rejects_every_missing_holiday_category(missing):
    payload = json.loads(timor())
    payload["holiday"] = {
        k: row for k, row in payload["holiday"].items() if missing not in row["name"]
    }
    with pytest.raises(ContractError, match="timor_incomplete"):
        timor_holidays(json.dumps(payload).encode(), YEAR)
