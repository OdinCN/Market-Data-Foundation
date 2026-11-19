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
    exchange_notice_url,
    exchange_supplement,
    government_notice_url,
    government_search_url,
    notice_holidays,
    year_sessions,
)
from scripts.mdf_tdx_exchange_daily import refresh
from scripts.mdf_tdx_trading_calendar import (
    prepare_calendar,
    scheduled_year,
    scheduled_years,
    verify_history,
)

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
            {GOV_URL},
            "sse_annual_notice",
            [GOV_URL, EXCHANGE_INDEX, SSE_URL],
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


def test_government_lookup_without_results_cannot_synthesize_calendar(tmp_path):
    calls = []

    def get(url):
        calls.append(url)
        if url == government_search_url(YEAR):
            return json.dumps(dict(code=200, searchVO=dict(listVO=[]))).encode()
        return b"<html></html>"

    holidays, result = prepare_calendar(YEAR, get=get, artifact=tmp_path)
    assert holidays is None
    assert calls == [government_search_url(YEAR), EXCHANGE_INDEX]
    assert result["state"] == "UNAVAILABLE"
    assert not result["schedulable"]


SUPPLEMENT_URL = (
    "https://www.sse.com.cn/disclosure/announcement/general/c/c_20270419_1234.shtml"
)


def correction(text, title=f"关于调整{YEAR}年劳动节休市安排的公告"):
    return (
        f"<title>{title} | 上海证券交易所</title>"
        f"<i>时间：&nbsp;2027-04-21</i><p>{text}</p>"
    ).encode()


def test_market_supplement_overrides_provisional_window(tmp_path):
    title = f"关于调整{YEAR}年劳动节休市安排的公告"
    data = {
        GOV_URL: notice(exchange=False),
        EXCHANGE_INDEX: f'<a href="{SUPPLEMENT_URL}">{title}</a>'.encode(),
        SUPPLEMENT_URL: correction("5月3日至5日休市，5月6日起照常开市。", title),
    }
    # Prior closure begins on a Friday so the shorter window reopens a weekday.
    data[GOV_URL] = data[GOV_URL].replace(
        "5月1日至5月5日".encode(), "4月30日至5月5日".encode()
    )
    holidays, result = prepare_calendar(
        YEAR,
        get=data.__getitem__,
        artifact=tmp_path,
        government_url=GOV_URL,
    )
    assert date(YEAR, 4, 30) not in holidays
    assert date(YEAR, 5, 3) in holidays
    assert not result["exchange_confirmed"]
    assert result["evidence"]["applied_supplements"] == [SUPPLEMENT_URL]


def test_official_extension_and_unrelated_market_scope():
    extension = correction(
        "一、延长2027年春节休市至2月14日（星期日），2月15日（星期一）正常开市。",
        "关于调整2027年春节休市相关安排的公告",
    )
    proof = exchange_supplement(extension, YEAR, SUPPLEMENT_URL)
    assert proof["published_on"] == "2027-04-21"
    assert proof["windows"] == [dict(names=["春节"], start=None, end="2027-02-14")]
    unrelated = correction(
        "5月3日至5日休市，5月6日起照常开市。", "关于2027年沪股通休市安排的公告"
    )
    with pytest.raises(ContractError, match="supplement_identity"):
        exchange_supplement(unrelated, YEAR, SUPPLEMENT_URL)


def test_scheduled_refresh_always_scans_current_year():
    assert scheduled_years(datetime(2026, 11, 19, 17, tzinfo=UTC)) == [2026, 2027]
    assert scheduled_years(datetime(2026, 12, 31, 16, tzinfo=UTC)) == [2027]
    assert scheduled_years(datetime(2027, 7, 4, tzinfo=UTC)) == [2027]


def test_historical_reconciliation_uses_market_trade_evidence():
    start = date(2026, 11, 16)
    open_day, closed_day, unknown_day, missing_day = (
        start + timedelta(days=i) for i in range(4)
    )
    as_of = datetime(2026, 11, 20, tzinfo=UTC)

    class Store:
        def trading_calendar(self, **params):
            assert params == dict(start=start, end=missing_day, as_of=as_of)
            return {open_day: True, closed_day: False, missing_day: True}

        def market_trade_counts(self, **params):
            assert params == dict(start=start, end=missing_day, as_of=as_of)
            return {
                open_day: {"sh": 1200, "sz": 1500, "bj": 100},
                closed_day: {"sz": 1500},
                unknown_day: {"sh": 1200},
            }

    receipt = verify_history(Store(), start=start, end=missing_day, as_of=as_of)
    assert [row["status"] for row in receipt["days"]] == [
        "CONFIRMED_OPEN",
        "PLANNED_CLOSED_WITH_TRADES",
        "OBSERVED_OPEN_NO_PLAN",
        "UNVERIFIED",
    ]
    assert receipt["days"][0]["markets_with_positive_volume"]["bj"] == 100
    assert receipt["days"][-1]["markets_with_positive_volume"] == {}
