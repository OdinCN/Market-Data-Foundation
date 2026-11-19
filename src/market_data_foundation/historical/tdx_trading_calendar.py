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
HOLIDAYS = ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节")
CALENDAR_AUTHORITIES = {
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


DAY = r"(\d{1,2})日(?:[（(][^）)]{1,32}[）)])?"
YEAR = r"(?:(\d{4})年)?"
DATE = rf"{YEAR}(\d{{1,2}})月{DAY}"
WINDOW = rf"{DATE}(?:[至到]{YEAR}(?:(\d{{1,2}})月)?{DAY})?"


def _window(parts, year, *, new_year=False):
    sy, sm, sd, ey, em, ed = parts
    start_month, end_month = int(sm), int(em or sm)
    start_year = year - (new_year and start_month == 12)
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
    return start, end


def _closed_days(start, end):
    return {start + timedelta(days=i) for i in range((end - start).days + 1)}


def notice_windows(raw, year, *, exchange):
    """Parse the named holiday clauses, excluding weekend make-up work."""
    text = _text(raw)
    titles = (
        _exchange_titles(year) if exchange else {GOVERNMENT_TITLE.format(year=year)}
    )
    require(any(title in text for title in titles), "tdx_calendar_notice_title")
    closure = "休市" if exchange else "放假"
    windows = {}
    holiday = "(?:" + "|".join(HOLIDAYS) + ")"
    separator = "[、，,和及]"
    for name in HOLIDAYS:
        heading = rf"(?:{holiday}{separator})*{name}(?:{separator}{holiday})*"
        matches = re.findall(rf"{heading}[：:]{WINDOW}{closure}", text)
        # Pages may render a title/navigation more than once, but every holiday
        # must have one unambiguous date range.
        require(len(set(matches)) == 1, "tdx_calendar_holiday_missing_or_ambiguous")
        windows[name] = _window(matches[0], year, new_year=name == "元旦")
    require(
        windows["元旦"][0] <= date(year, 1, 1) <= windows["元旦"][1],
        "tdx_calendar_new_year_missing",
    )
    return windows


def holiday_dates(windows):
    return set().union(*(_closed_days(*window) for window in windows.values()))


def notice_holidays(raw, year, *, exchange):
    return holiday_dates(notice_windows(raw, year, exchange=exchange))


class _NoticeLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = {}
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
        label = re.sub(r"\s+", "", "".join(self.label))
        url = urljoin(EXCHANGE_INDEX, self.href)
        if exchange_url_valid(url):
            self.links[url] = label
        self.href = None


def exchange_url_valid(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.netloc == "www.sse.com.cn"
        and re.fullmatch(
            r"/(?:disclosure/announcement/general|star/media/news)/c/[\w-]+\.shtml",
            parsed.path,
        )
        and not parsed.query
        and not parsed.fragment
    )


def _exchange_links(raw):
    links = _NoticeLinks()
    links.feed(raw.decode("utf-8-sig"))
    return links.links


def exchange_notice_url(raw, year):
    urls = [
        url
        for url, title in _exchange_links(raw).items()
        if title in _exchange_titles(year)
    ]
    require(len(urls) == 1, "tdx_calendar_exchange_notice_unavailable")
    return urls[0]


def _supplement_title(title, year):
    return (
        title.startswith("关于")
        and f"{year}年" in title
        and "休市" in title
        and title.endswith(("公告", "通知"))
        and title not in _exchange_titles(year)
        and not re.search(r"股通|港通|B股|债券|期权|基金|回购|结算|交易测试", title)
    )


def exchange_supplement_urls(raw, year):
    return {
        url
        for url, title in _exchange_links(raw).items()
        if _supplement_title(title, year)
    }


def exchange_supplement(raw, year, url):
    """Identify replacement windows and extensions from an official SSE article."""
    require(exchange_url_valid(url), "tdx_calendar_exchange_notice_url")
    html = raw.decode("utf-8-sig")
    title = re.search(r"<title[^>]*>([^<]+)</title>", html)
    name = _text(title[1].split("|")[0].encode()) if title else ""
    require(
        _supplement_title(name, year)
        and re.search(r"/c_\d{8}_\d+\.shtml$", url) is not None,
        "tdx_calendar_supplement_identity",
    )
    # The archive filename date can precede the date seen by market users.
    timestamps = set(
        re.findall(
            r"<i(?:\s[^>]*)?>\s*(?:时间：[\s\xa0]*)?(\d{4}-\d{2}-\d{2})\s*</i>",
            unescape(html),
        )
    )
    require(len(timestamps) == 1, "tdx_calendar_supplement_publication_date")
    published_on = date.fromisoformat(timestamps.pop())
    # Keep paragraph boundaries so adjacent markup cannot concatenate
    # the preceding publication date with a leading month number.
    paragraphs = re.sub(r"</(?:p|div|h[1-6])>|<p(?:\s[^>]*)?>", "。", html)
    article = _text(paragraphs.encode())
    # End with the cash-market reopening statement, excluding the separate
    # weekend, settlement and system-testing clauses.
    resumes = rf"[，,。]{DATE}(?:起)?(?:照常|正常)开市"
    names = sorted((h for h in HOLIDAYS if h in name), key=name.index)
    changes = []
    for match in re.finditer(rf"{WINDOW}休市{resumes}", article):
        start, end = _window(match.groups()[:6], year, new_year="元旦" in names)
        if "元旦" in names:
            require(start <= date(year, 1, 1) <= end, "tdx_calendar_new_year_missing")
        changes.append((start, end, match.groups()[6:]))

    if not changes and len(names) == 1:
        holiday = re.escape(names[0])
        extended = re.findall(
            rf"延长(?:\d{{4}}年)?{holiday}休市至{DATE}{resumes}", article
        )
        if len(extended) == 1:
            ey, em, ed, *resume = extended[0]
            _, end = _window((ey, em, ed, "", "", ""), year)
            changes.append((None, end, resume))

    require(
        changes and (len(changes) == 1 or len(changes) == len(names)),
        "tdx_calendar_supplement_missing_or_ambiguous",
    )
    windows = []
    for index, (start, end, resume) in enumerate(changes):
        resumed, _ = _window((*resume, "", "", ""), year)
        require(
            end < resumed <= end + timedelta(days=3)
            and resumed.weekday() < 5
            and all(
                day.weekday() >= 5
                for day in _closed_days(
                    end + timedelta(days=1), resumed - timedelta(days=1)
                )
            ),
            "tdx_calendar_supplement_reopening",
        )
        windows.append(
            dict(
                names=names if len(changes) == 1 else [names[index]],
                start=start.isoformat() if start else None,
                end=end.isoformat(),
            )
        )
    return dict(published_on=published_on.isoformat(), windows=windows)


def calendar_with_supplements(year, base, windows, supplements):
    """Apply retained exchange windows in publication order."""
    current = {
        name: tuple(date.fromisoformat(day) for day in window)
        for name, window in windows.items()
    }
    additional = base - holiday_dates(current)
    applied, affected = [], set()
    for notice in sorted(supplements, key=lambda n: (n["published_on"], n["url"])):
        updates = [
            (window["names"], window["start"], date.fromisoformat(window["end"]))
            for window in notice["windows"]
        ]
        if any(
            start is None
            and any(
                name not in current
                or not current[name][0] <= end < current[name][0] + timedelta(days=20)
                for name in names
            )
            for names, start, end in updates
        ):
            continue
        for names, start, end in updates:
            if names and all(name in current for name in names):
                for name in names:
                    affected.update(_closed_days(*current[name]))
                    first = date.fromisoformat(start) if start else current[name][0]
                    current[name] = (first, end)
                    affected.update(_closed_days(first, end))
            elif start is not None:
                closed = _closed_days(date.fromisoformat(start), end)
                additional.update(closed)
                affected.update(closed)
        applied.append(notice["url"])
    # January 1 remains a required closure even if an amendment is malformed.
    holidays = holiday_dates(current) | additional
    require(date(year, 1, 1) in holidays, "tdx_calendar_new_year_missing")
    return holidays, applied, affected


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
