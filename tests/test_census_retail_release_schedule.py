"""Synthetic Advance report tables, DST and unresolved schedules."""

from datetime import UTC, datetime

import pytest

from market_data_foundation.historical.census_retail_release_schedule import (
    CALENDAR_URL,
    parse_official_calendar,
)
from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailSourceError,
)


def table(
    rows, title="Advance Monthly Retail Trade Report", header="Release Date at 8:30 am"
):
    body = "".join(f"<tr><td>{month}</td><td>{date}</td></tr>" for month, date in rows)
    return (
        f"<h2><strong>{title}</strong></h2><table><tr><th>Data Month</th>"
        f"<th>{header}</th></tr>{body}</table>"
    )


def test_exact_advance_table_only_dst_cross_year_and_tba():
    raw = table(
        [
            ("September 2026", "October 15, 2026"),
            ("October 2026", "November 17, 2026"),
            ("December 2026", "January 15, 2027"),
            ("February 2027", "March 16, 2027"),
            ("December 2027", "To be announced at a later date"),
        ]
    )
    raw += table(
        [("September 2026", "November 17, 2026")], title="Monthly Retail Trade Report"
    )
    events = {e["reference_month"]: e for e in parse_official_calendar(raw.encode())}
    assert events["2026-09"]["release_at"] == datetime(2026, 10, 15, 12, 30, tzinfo=UTC)
    assert events["2026-10"]["release_at"] == datetime(2026, 11, 17, 13, 30, tzinfo=UTC)
    assert events["2026-12"]["release_at"] == datetime(2027, 1, 15, 13, 30, tzinfo=UTC)
    assert events["2027-02"]["release_at"].hour == 12
    assert (
        events["2027-12"]["release_at"] is None and events["2027-12"]["status"] == "TBA"
    )
    assert all(
        e["actual_release_at"] is None and e["source"] == CALENDAR_URL
        for e in events.values()
    )


def test_identical_duplicates_coalesce_but_conflicts_rejected():
    rows = [("September 2026", "October 15, 2026")] * 2
    assert len(parse_official_calendar(table(rows).encode())) == 1
    rows[1] = ("September 2026", "October 16, 2026")
    with pytest.raises(RetailSourceError, match="duplicate_conflict"):
        parse_official_calendar(table(rows).encode())


@pytest.mark.parametrize(
    "raw",
    [
        table(
            [("September 2026", "October 15, 2026")],
            title="Monthly Retail Trade Report",
        ),
        table([("September 2026", "October 15, 2026")], header="Release Date"),
        table([("September 2026", "invalid")]),
        table([("September 2026", "September 15, 2026")]),
        table([("September 2026", "October 15, 2026")]) * 2,
        "<h2>Advance Monthly Retail Trade Report</h2><h2>Other</h2>"
        + table([], title="Other"),
    ],
)
def test_missing_ambiguous_or_unresolved_table_rejected(raw):
    with pytest.raises(RetailSourceError):
        parse_official_calendar(raw.encode())
