"""DOL official weekly rule, with explicit exception-table evidence and DST."""

import hashlib
from datetime import UTC, date, datetime, timedelta
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from market_data_foundation.historical.claims_contract import ClaimsError, instant

PRIMARY_URL = "https://oui.doleta.gov/unemploy/archive.asp"
BACKUP_URL = "https://oui.doleta.gov/unemploy/claims_arch.asp"
CURRENT_REPORT_URL = "https://www.dol.gov/ui/data.pdf?os=v0"
NEW_YORK = ZoneInfo("America/New_York")
RULE = (
    "Publication Schedule: The UI Weekly Claims News Release is published each week "
    "on Thursday morning at 8:30am EST. Exceptions to this schedule occur when a "
    "Thursday falls on a Federal Holiday. The following is a list of dates when "
    "the weekly news release will be published on a day other than Thursday:"
)


class _ScheduleHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.paragraphs, self.tables = [], []
        self.paragraph, self.table, self.row, self.cell = None, None, None, None

    def handle_starttag(self, tag, attrs):
        if tag == "p":
            self.paragraph = []
        elif tag == "table":
            self.table = []
        elif tag == "tr" and self.table is not None:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.paragraph is not None:
            self.paragraph.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self.paragraph is not None:
            self.paragraphs.append(" ".join(" ".join(self.paragraph).split()))
            self.paragraph = None
        elif tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.table.append(self.row)
            self.row = None
        elif tag == "table" and self.table is not None:
            self.tables.append(self.table)
            self.table = None


def parse_official_rule(raw, source_url, obtained_at):
    """Unknown paragraphs, exception rows or dates fail closed; no FRED fallback."""
    instant(obtained_at)
    try:
        if source_url not in {PRIMARY_URL, BACKUP_URL} or not 0 < len(raw) <= 2_000_000:
            raise ValueError
        parser = _ScheduleHTML()
        parser.feed(raw.decode("utf-8"))
        if parser.paragraphs.count(RULE) != 1:
            raise ValueError
        tables = [
            t for t in parser.tables if t and t[0] == ["Release Date", "Release Time"]
        ]
        if len(tables) != 1 or len(tables[0]) < 2:
            raise ValueError
        exceptions = {}
        for row in tables[0][1:]:
            if len(row) != 2 or row[1] != "8:30 AM EST":
                raise ValueError
            dt = datetime.strptime(row[0], "%A, %B %d, %Y").date()
            # Only the observed Wednesday exception layout is qualified.
            if (
                dt.weekday() != 2
                or not row[0].startswith("Wednesday, ")
                or dt in exceptions
            ):
                raise ValueError
            exceptions[dt] = row[0]
        start = obtained_at.astimezone(NEW_YORK).date()
        end = date(max(d.year for d in exceptions), 12, 31)
        if not start <= end or (end - start).days > 3 * 366:
            raise ValueError
        events = {}
        d = start + timedelta(days=(3 - start.weekday()) % 7)
        while d <= end:
            release_day = (
                d - timedelta(days=1) if d - timedelta(days=1) in exceptions else d
            )
            local = datetime.combine(
                release_day, datetime.min.time(), NEW_YORK
            ).replace(hour=8, minute=30)
            key = "dol-claims-" + release_day.isoformat()
            events[key] = dict(
                event_id=key,
                uid=key,
                source_date=release_day.isoformat(),
                release_at=local.astimezone(UTC).isoformat(),
                status="CONFIRMED",
                publication_status="scheduled",
                original_agency="DOL-ETA",
                reference_week=None,
                reference_week_basis="source_report_required",
                schedule_basis="official_exception_table"
                if release_day != d
                else "official_weekly_rule",
                source_url=source_url,
                source_sha256=hashlib.sha256(raw).hexdigest(),
                timezone="America/New_York",
                planned_only=True,
            )
            d += timedelta(weeks=1)
        return dict(
            events=events,
            rule=RULE,
            exceptions=[d.isoformat() for d in sorted(exceptions)],
            rules_valid_from=start.isoformat(),
            rules_valid_until=end.isoformat(),
            validity_basis=(
                "through_latest_explicit_exception_year; daily_cache_revalidation"
            ),
            timezone="America/New_York",
            source_timezone_label="EST",
            timezone_interpretation="Eastern_wall_time_with_DST",
        )
    except ValueError, UnicodeError, TypeError, IndexError:
        raise ClaimsError("claims_calendar_layout_unrecognized") from None
