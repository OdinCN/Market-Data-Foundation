"""Pure payload checks used by the opt-in TDX lab and deterministic tests."""

import math
from datetime import date


def bar_date(row: dict) -> date:
    for key in ("time", "datetime", "date"):
        if key in row:
            return date.fromisoformat(str(row[key])[:10])
    return date(int(row["year"]), int(row["month"]), int(row["day"]))


def daily_window(rows: list[dict], start: date, end: date) -> list[dict]:
    """Validate dates/OHLC before selecting an inclusive fixture window.

    Do not deduplicate malformed pages or fill missing trading sessions.
    A successful window does not establish exhaustive provider history.
    """
    seen = set()
    selected = []
    for row in rows:
        day = bar_date(row)
        if day in seen:
            raise ValueError("overlapping daily pages")
        seen.add(day)
        values = [row[key] for key in ("open", "high", "low", "close")]
        if any(not math.isfinite(value) or value <= 0 for value in values):
            raise ValueError("invalid daily OHLC")
        opening, high, low, close = values
        if high < max(opening, close) or low > min(opening, close) or high < low:
            raise ValueError("inconsistent daily OHLC")
        if start <= day <= end:
            selected.append(row)
    return sorted(selected, key=bar_date)


def concept_memberships(groups: list[dict]) -> set[tuple[str, str]]:
    """Validate grouped TDX concept identities and bare constituent codes."""
    memberships = set()
    names = set()
    for group in groups:
        name = group["name"]
        if not name or name in names:
            raise ValueError("concept identity is missing or duplicated")
        names.add(name)
        category = group.get("category", group.get("type"))
        if category != 2:
            raise ValueError("not a TDX concept taxonomy")
        codes = group["codes"]
        if "count" in group and group["count"] != len(codes):
            raise ValueError("membership count mismatch")
        for code in codes:
            pair = (name, code)
            if len(code) != 6 or not code.isascii() or not code.isdigit():
                raise ValueError("invalid constituent code")
            if pair in memberships:
                raise ValueError("duplicate membership")
            memberships.add(pair)
    return memberships


def select_representatives(universes: dict[str, list[dict]]) -> list[dict]:
    """Select observed positive-price samples; this does not establish active status."""
    selections = (
        ("SH", "SH-main", ("600",)),
        ("SH", "STAR", ("688",)),
        ("SZ", "SZ-main", ("000",)),
        ("SZ", "ChiNext", ("300", "301")),
        ("BJ", "BJ", ("920", "8", "4")),
        ("SH", "ETF", ("510", "512", "513", "515", "588")),
        ("SH", "index", ("000001",)),
    )
    result = []
    for market, label, prefixes in selections:
        matches = sorted(
            (
                row
                for row in universes[market]
                if row["code"].startswith(prefixes) and row["last_price"] > 0
            ),
            key=lambda row: row["code"],
        )
        if not matches:
            raise ValueError("no observed representative for " + label)
        result.append(
            {
                **matches[0],
                "market": market,
                "label": label,
                "prefixes": list(prefixes),
                "selection_rule": "first observed positive-price code in lexical order",
            }
        )
    return result
