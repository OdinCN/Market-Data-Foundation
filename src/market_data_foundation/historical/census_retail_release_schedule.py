"""Offline Advance MARTS schedule parsing; planned times only, no worker or IO."""

import calendar
import hashlib
import re
from datetime import UTC, datetime
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailSourceError,
    _range,
    _require,
)

CALENDAR_URL = "https://www.census.gov/retail/release_schedule.html"
TITLE = "Advance Monthly Retail Trade Report"
MAX_CALENDAR_BYTES = 2_000_000
_MONTHS = {name.casefold(): n for n, name in enumerate(calendar.month_name) if name}


class _AdvanceTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = None
        self.title = None
        self.depth = 0
        self.active = False
        self.tables = []
        self.rows = None
        self.row = None
        self.cell = None

    def handle_starttag(self, tag, attrs):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self.heading = []
            self.title = None
        elif tag == "table":
            _require(not (self.active and self.depth), "calendar_structure_invalid")
            if self.depth == 0:
                self.active = self.title == TITLE
                self.title = None
                if self.active:
                    self.rows = []
            self.depth += 1
        elif self.active and tag == "tr":
            self.row = []
        elif self.active and tag in {"td", "th"}:
            self.cell = []

    def handle_data(self, data):
        if self.heading is not None:
            self.heading.append(data)
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"} and self.heading is not None:
            self.title = " ".join(" ".join(self.heading).split())
            self.heading = None
        elif self.active and tag in {"td", "th"} and self.cell is not None:
            _require(self.row is not None, "calendar_structure_invalid")
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif self.active and tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.depth -= 1
            if self.depth == 0 and self.active:
                self.tables.append(self.rows)
                self.active = False


def parse_official_calendar(raw):
    """Return shared calendar-shaped records, including unresolved TBA months.

    release_at is aware UTC; it is a scheduled time, never API availability.
    Only the table immediately following the exact Advance report heading is used.
    """
    _require(
        type(raw) is bytes and 0 < len(raw) <= MAX_CALENDAR_BYTES,
        "calendar_payload_invalid",
    )
    try:
        parser = _AdvanceTable()
        parser.feed(raw.decode("utf-8-sig"))
        parser.close()
        _require(len(parser.tables) == 1, "calendar_table_missing_or_ambiguous")
        rows = parser.tables[0]
        _require(
            len(rows) > 1
            and len(rows[0]) == 2
            and rows[0][0].casefold() == "data month"
            and re.fullmatch(r"release date at 8:30\s*am", rows[0][1], re.I),
            "calendar_header_invalid",
        )
        events, digest = {}, hashlib.sha256(raw).hexdigest()
        for row in rows[1:]:
            _require(len(row) == 2, "calendar_entry_invalid")
            reference = re.fullmatch(r"([A-Za-z]+)\s+(\d{4})", row[0])
            _require(reference is not None, "calendar_reference_invalid")
            month = f"{reference[2]}-{_MONTHS[reference[1].casefold()]:02d}"
            _range(month, month)
            tba = row[1].casefold() in {
                "tba",
                "to be announced",
                "to be announced at a later date",
            }
            local = None
            if not tba:
                date = re.fullmatch(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", row[1])
                _require(date is not None, "calendar_date_invalid")
                local = datetime(
                    int(date[3]),
                    _MONTHS[date[1].casefold()],
                    int(date[2]),
                    8,
                    30,
                    tzinfo=ZoneInfo("America/New_York"),
                )
                _require(local.strftime("%Y-%m") > month, "calendar_date_invalid")
            event = {
                "reference_month": month,
                "release_at": local.astimezone(UTC) if local else None,
                "release_at_local": local if local else None,
                "timezone": "America/New_York",
                "status": "TBA" if tba else "scheduled",
                "actual_release_at": None,
                "source": CALENDAR_URL,
                "sha256": digest,
            }
            _require(
                month not in events or events[month] == event,
                "calendar_duplicate_conflict",
            )
            events[month] = event
        return tuple(events[month] for month in sorted(events))
    except RetailSourceError:
        raise
    except Exception:
        raise RetailSourceError("census_retail_calendar_entry_invalid") from None
