"""Annual calendar sources for mainland A-share cash equities."""

import json
import re
from datetime import date, datetime, timedelta
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlencode, urljoin, urlsplit

from market_data_foundation.canonical.model import require

EXCHANGE_INDEX = "https://www.sse.com.cn/disclosure/dealinstruc/closed/list/"
GOVERNMENT_SEARCH = "https://sousuo.www.gov.cn/search-gov/data"
GOVERNMENT_TITLE = "国务院办公厅关于{year}年部分节假日安排的通知"
EXCHANGE_TITLE = "关于上海证券交易所{year}年部分节假日休市安排的通知"
EXCHANGE_FULL_YEAR_TITLE = "关于上海证券交易所{year}年全年休市安排的通知"
TIMOR_URL = "https://timor.tech/api/holiday/year/{year}/"
HOLIDAYS = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
CALENDAR_AUTHORITIES = {
    "timor_holiday_api": 1,
    "state_council_holiday_notice": 2,
    "sse_annual_notice": 3,
}


def government_url_valid(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "www.gov.cn"
        and parsed.path.startswith("/zhengce/")
        and not parsed.query
        and not parsed.fragment
    )


def year_days(year):
    day = date(year, 1, 1)
    while day.year == year:
        yield day
        day += timedelta(days=1)


def year_sessions(year, holidays):
    return {day: day.weekday() < 5 and day not in holidays for day in year_days(year)}


def _text(raw):
    return re.sub(r"\s+", "", unescape(re.sub(r"<[^>]+>", "", raw.decode("utf-8-sig"))))


def _exchange_titles(year):
    return {
        title.format(year=year) for title in (EXCHANGE_TITLE, EXCHANGE_FULL_YEAR_TITLE)
    }


def notice_holidays(raw, year, *, exchange):
    """Parse the named holiday clauses, excluding weekend make-up work."""
    text = _text(raw)
    titles = (
        _exchange_titles(year) if exchange else {GOVERNMENT_TITLE.format(year=year)}
    )
    require(any(title in text for title in titles), "tdx_calendar_notice_title")
    day_expr = r"(\d{1,2})日(?:[（(][^）)]{1,32}[）)])?"
    year_expr = r"(?:(\d{4})年)?"
    dates = (
        rf"{year_expr}(\d{{1,2}})月{day_expr}"
        rf"(?:[至到]{year_expr}(?:(\d{{1,2}})月)?{day_expr})?"
    )
    closure = "休市" if exchange else "放假"
    closed = set()
    holiday = "(?:" + "|".join(HOLIDAYS) + ")"
    separator = "[、，,和及]"
    for name in HOLIDAYS:
        heading = rf"(?:{holiday}{separator})*{name}(?:{separator}{holiday})*"
        matches = re.findall(rf"{heading}[：:]{dates}{closure}", text)
        # Pages may render a title/navigation more than once, but every holiday
        # must have one unambiguous date range.
        require(len(set(matches)) == 1, "tdx_calendar_holiday_missing_or_ambiguous")
        sy, sm, sd, ey, em, ed = matches[0]
        start_month, end_month = int(sm), int(em or sm)
        # A New Year's notice can include the previous December.
        start_year = year - (name == "元旦" and start_month == 12)
        end_year = start_year + (end_month < start_month)
        start = date(start_year, start_month, int(sd))
        end = date(end_year, end_month, int(ed or sd))
        require(
            end.year == year
            and (not sy or int(sy) == start_year)
            and (not ey or int(ey) == end_year)
            and 0 <= (end - start).days < 20,
            "tdx_calendar_holiday_range",
        )
        closed.update(start + timedelta(days=i) for i in range((end - start).days + 1))
    require(date(year, 1, 1) in closed, "tdx_calendar_new_year_missing")
    return closed


class _NoticeLinks(HTMLParser):
    def __init__(self, titles):
        super().__init__()
        self.titles = titles
        self.urls = set()
        self.href = None
        self.label = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.label = []

    def handle_data(self, data):
        if self.href:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag != "a" or not self.href:
            return
        if re.sub(r"\s+", "", "".join(self.label)) in self.titles:
            url = urljoin(EXCHANGE_INDEX, self.href)
            parsed = urlsplit(url)
            if (
                parsed.scheme == "https"
                and parsed.netloc == "www.sse.com.cn"
                and parsed.path.startswith("/disclosure/announcement/general/")
                and not parsed.query
                and not parsed.fragment
            ):
                self.urls.add(url)
        self.href = None


def exchange_notice_url(raw, year):
    links = _NoticeLinks(_exchange_titles(year))
    links.feed(raw.decode("utf-8-sig"))
    require(len(links.urls) == 1, "tdx_calendar_exchange_notice_unavailable")
    return next(iter(links.urls))


def government_search_url(year):
    query = urlencode(
        {
            "t": "zhengcelibrary_gw",
            "q": f"{year}年部分节假日安排",
            "searchfield": "title",
            "p": 1,
            "n": 5,
        }
    )
    return f"{GOVERNMENT_SEARCH}?{query}"


def government_notice_url(raw, year):
    """Select the original notice from China's official policy-library results."""
    result = json.loads(raw)
    code = "tdx_calendar_government_notice_unavailable"
    require(isinstance(result, dict) and result.get("code") == 200, code)
    search = result.get("searchVO")
    require(isinstance(search, dict) and isinstance(search.get("listVO"), list), code)
    title = GOVERNMENT_TITLE.format(year=year)
    urls = {
        row["url"]
        for row in search["listVO"]
        if isinstance(row, dict)
        and row.get("puborg") == "国务院办公厅"
        and isinstance(row.get("title"), str)
        and _text(row["title"].encode()) == title
        and isinstance(row.get("url"), str)
        and government_url_valid(row["url"])
    }
    require(len(urls) == 1, code)
    return next(iter(urls))


def timor_holidays(raw, year):
    """Validate the fallback projection. It is never exchange confirmation."""
    result = json.loads(raw)
    require(isinstance(result, dict), "tdx_calendar_timor_payload")
    rows = result.get("holiday")
    require(
        type(result.get("code")) is int
        and result["code"] == 0
        and isinstance(rows, dict)
        and rows,
        "tdx_calendar_timor_payload",
    )
    names, closed = set(), set()
    for key, row in rows.items():
        require(isinstance(row, dict), "tdx_calendar_timor_row")
        day = date.fromisoformat(row.get("date") or "")
        require(
            (day.year == year or (day.year == year - 1 and day.month == 12))
            and day.strftime("%m-%d") == key
            and type(row.get("holiday")) is bool
            and isinstance(row.get("name"), str)
            and (day.year == year or "元旦" in row["name"]),
            "tdx_calendar_timor_row",
        )
        if row["holiday"]:
            closed.add(day)
            names.update(name for name in HOLIDAYS if name in row["name"])
            if row["name"] in {
                "除夕",
                "初一",
                "初二",
                "初三",
                "初四",
                "初五",
                "初六",
                "初七",
                "初八",
            }:
                names.add("春节")
    require(
        names == set(HOLIDAYS) and date(year, 1, 1) in closed,
        "tdx_calendar_timor_incomplete",
    )
    return closed


def unanimous_legacy_days(rows, *, as_of):
    """Read historical snapshot calendars during the transition."""
    markets = {}
    for row in rows:
        if datetime.fromisoformat(row["available_at"]) <= as_of:
            markets.setdefault(date.fromisoformat(row["day"]), {})[row["market"]] = row[
                "is_open"
            ]
    return {
        day: all(states.values())
        for day, states in markets.items()
        if set(states) == {0, 1, 2}
    }


def scheduled_days(store, *, as_of, start, end):
    if hasattr(store, "trading_calendar"):
        return store.trading_calendar(as_of=as_of, start=start, end=end)
    if hasattr(store, "strategy_calendar"):
        rows = store.strategy_calendar(as_of=as_of)
    else:
        rows = (
            (store.universe(as_of=as_of) or {}).get("details", {}).get("calendar", [])
        )
    return {
        day: opened
        for day, opened in unanimous_legacy_days(rows, as_of=as_of).items()
        if start <= day <= end
    }
