"""BEA quarterly calendar identities; planned times never prove release values."""

import hashlib
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from market_data_foundation.historical.cpi_release_schedule import CpiScheduleError
from market_data_foundation.historical.gdp_contract import quarter_start, shift_quarter
from market_data_foundation.historical.pce_release_schedule import (
    _MONTHS,
    CALENDAR_URL,
    ICS_URL,
    MAX_CALENDAR_BYTES,
    _BeaTable,
    _ics_time,
)

GdpScheduleError = CpiScheduleError
KINDS = ("advance", "second", "third", "initial", "updated")


def title_identity(title):
    """Keep exceptional Initial/Updated wording; never rename it Second/Third."""
    if not re.match(r"(?:Gross Domestic Product\s*,|GDP\s*\()", title, re.I):
        return None
    main = title.split(";")[0]
    estimates = re.findall(
        r"\((Advance|Second|Third|Initial|Updated) Estimate\)", main, re.I
    )
    periods = re.findall(
        r"\b([1-4])(?:st|nd|rd|th) Quarter(?: and Year)? (\d{4})\b", main, re.I
    )
    if len(estimates) != 1 or len(periods) != 1:
        return {"reference_quarter": None, "estimate_kind": "unknown"}
    q, year = periods[0]
    return {"reference_quarter": f"{year}-Q{q}", "estimate_kind": estimates[0].lower()}


def _event(title, at, uid, status, sequence, raw, source):
    identity = title_identity(title)
    if identity is None:
        return None
    if status not in {"CONFIRMED", "CANCELLED", "TENTATIVE"} or sequence < 0:
        raise GdpScheduleError("gdp_calendar_event_invalid")
    quarter = identity["reference_quarter"]
    if quarter and at.date() < quarter_start(shift_quarter(quarter, 1)):
        raise GdpScheduleError("gdp_calendar_release_before_quarter_end")
    date = at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    return {
        **identity,
        "event_id": f"bea:gdp:{date}:{quarter}:{identity['estimate_kind']}",
        "uid": uid,
        "summary": title,
        "release_at": at.isoformat(),
        "status": status,
        "sequence": sequence,
        "source": source,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "classification": None
        if quarter
        else "gdp_calendar_quarter_or_estimate_unverified",
    }


def _text(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CALENDAR_BYTES:
        raise GdpScheduleError("gdp_calendar_payload_invalid")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeError:
        raise GdpScheduleError("gdp_calendar_payload_invalid") from None


def parse_official_ics(raw):
    text = re.sub(r"\r?\n[ \t]", "", _text(raw))
    if not text.strip().startswith("BEGIN:VCALENDAR") or not text.strip().endswith(
        "END:VCALENDAR"
    ):
        raise GdpScheduleError("gdp_calendar_payload_invalid")
    events, active = {}, None
    try:
        for line in text.splitlines():
            if line == "BEGIN:VEVENT":
                if active is not None:
                    raise ValueError
                active = {}
            elif line == "END:VEVENT":
                if active is None:
                    raise ValueError
                title = (
                    active.get("SUMMARY", "").replace(r"\,", ",").replace(r"\;", ";")
                )
                if title_identity(title) is not None:
                    key, value = active["start"]
                    event = _event(
                        title,
                        _ics_time(key, value),
                        active["UID"],
                        active.get("STATUS", "CONFIRMED"),
                        int(active.get("SEQUENCE", "0")),
                        raw,
                        ICS_URL,
                    )
                    uid = event["uid"]
                    if not uid or (uid in events and events[uid] != event):
                        raise ValueError
                    events[uid] = event
                active = None
            elif active is not None:
                key, value = line.split(":", 1)
                name = key.split(";", 1)[0]
                if name == "DTSTART":
                    if "start" in active:
                        raise ValueError
                    active["start"] = (key, value)
                elif name in {"SUMMARY", "UID", "STATUS", "SEQUENCE"}:
                    if name in active:
                        raise ValueError
                    active[name] = value
        if active is not None or not events:
            raise ValueError
        _unique(events)
        return events
    except KeyError, ValueError, TypeError:
        raise GdpScheduleError("gdp_calendar_payload_invalid") from None


def _unique(events):
    seen = set()
    for event in events.values():
        key = (event["reference_quarter"], event["estimate_kind"])
        if key[0]:
            if key in seen:
                raise GdpScheduleError("gdp_calendar_identity_ambiguous")
            seen.add(key)


def parse_official_html(raw):
    parser = _BeaTable()
    try:
        parser.feed(_text(raw))
        if parser.tables != 1 or not parser.rows:
            raise ValueError
        year = re.fullmatch(r"Year (\d{4})", parser.rows[0][0])
        if year is None:
            raise ValueError
        events = {}
        for row in parser.rows[1:]:
            if len(row) < 3 or title_identity(row[2]) is None:
                continue
            date = re.fullmatch(
                r"([A-Za-z]+) (\d{1,2}) (\d{1,2}):(\d{2}) (AM|PM)", row[0]
            )
            if date is None or not 1 <= int(date[3]) <= 12:
                raise ValueError
            local = datetime(
                int(year[1]),
                _MONTHS[date[1].lower()],
                int(date[2]),
                int(date[3]) % 12 + (12 if date[5] == "PM" else 0),
                int(date[4]),
            )
            at = _ics_time(
                "DTSTART;TZID=America/New_York", local.strftime("%Y%m%dT%H%M%S")
            )
            uid = "bea-gdp-html:" + hashlib.sha256(row[2].encode()).hexdigest()[:24]
            if uid in events:
                raise ValueError
            events[uid] = _event(row[2], at, uid, "CONFIRMED", 0, raw, CALENDAR_URL)
        if not events:
            raise ValueError
        _unique(events)
        return events
    except ValueError, KeyError, IndexError:
        raise GdpScheduleError("gdp_calendar_html_invalid") from None


def backup_events(incoming, previous):
    """Fallback can confirm matching times; it cannot undo cancellations or moves."""
    remaining, result = dict(incoming), {}
    for uid, old in previous.items():
        matches = [
            (key, event)
            for key, event in remaining.items()
            if old["reference_quarter"] is not None
            and (event["reference_quarter"], event["estimate_kind"])
            == (old["reference_quarter"], old["estimate_kind"])
        ]
        if len(matches) == 1:
            key, event = matches[0]
            del remaining[key]
            if (
                old["status"] == "CONFIRMED"
                and old["release_at"] == event["release_at"]
            ):
                result[uid] = dict(event, uid=uid, sequence=old["sequence"])
            else:
                result[uid] = dict(old, classification="calendar_fallback_conflict")
        else:
            result[uid] = dict(old, classification="calendar_fallback_not_covered")
    result.update(remaining)
    return result
