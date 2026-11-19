import json
from datetime import UTC, date, datetime

import httpx
import pytest

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.tdx_p0_jobs import strategy_slot_id
from market_data_foundation.historical.tdx_p0_strategy import DailyStrategy
from market_data_foundation.historical.tdx_trading_calendar import (
    EXCHANGE_INDEX,
    EXCHANGE_TITLE,
    GOVERNMENT_TITLE,
    TIMOR_URL,
    exchange_notice_url,
    notice_holidays,
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
            [EXCHANGE_INDEX, SSE_URL],
        ),
        (
            {EXCHANGE_INDEX},
            "state_council_holiday_notice",
            [EXCHANGE_INDEX, GOV_URL],
        ),
        (
            {EXCHANGE_INDEX, GOV_URL},
            "timor_holiday_api",
            [EXCHANGE_INDEX, GOV_URL, TIMOR_URL.format(year=YEAR)],
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
    assert result["evidence"]["url"] == calls[-1]


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
    assert scheduled_year(datetime(2026, 11, 30, 16, tzinfo=UTC)) == 2027
    assert scheduled_year(datetime(2026, 12, 31, 16, tzinfo=UTC)) == 2027


def test_exchange_new_year_holiday_can_start_in_previous_december():
    raw = notice(exchange=True).replace(
        "1月1日（周五）至3日（周日）".encode(),
        "12月31日（周四）至1月3日（周日）".encode(),
    )
    holidays = notice_holidays(raw, YEAR, exchange=True)
    assert date(YEAR - 1, 12, 31) in holidays
    assert date(YEAR, 1, 3) in holidays
