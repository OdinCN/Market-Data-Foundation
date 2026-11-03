"""Official-calendar CPI release scheduling; no import-time IO or daemon.

The host explicitly invokes checks. A state-file flock serializes scheduler
processes, and a durable attempt marker precedes provider/storage effects.
"""

import calendar
import copy
import fcntl
import hashlib
import inspect
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError, URLError
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.bls_web_transport import (
    BlsWebError,
    fetch_bls_page,
)
from market_data_foundation.historical.calendar_publications import (
    bls_calendar_publications,
)
from market_data_foundation.historical.calendar_resilience import (
    failure,
    may_fallback,
    refresh_metadata,
    retry_time,
)
from market_data_foundation.historical.cpi_revision_signals import (
    CpiSignalError,
    fetch_official_errata,
    parse_official_errata,
    validate_signal,
)
from market_data_foundation.historical.cpi_timeseries_source import validate_month

SERIES_IDS = CPI_PROFILE.series_ids
CALENDAR_URL = "https://www.bls.gov/schedule/news_release/cpi.htm"
ICS_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
_DEFAULT_FETCHER = object()
NEW_YORK = ZoneInfo("America/New_York")
MAX_CALENDAR_BYTES = 2_000_000
CALENDAR_MAX_AGE = timedelta(hours=24)
RETRY_SECONDS = (300, 900, 3600, 21600)
AUTO_HTTP_CAP = 30
AUTO_WINDOW_SECONDS = 7200
_AUTO_TERMINAL_RESPONSE_ERRORS = frozenset(
    {
        "cpi_timeseries_provider_rejected",
        "cpi_timeseries_response_invalid",
        "cpi_timeseries_series_response_invalid",
        "cpi_timeseries_index_invalid",
        "cpi_timeseries_duplicate_json_key",
        "cpi_timeseries_invalid_json",
        "cpi_timeseries_response_size_invalid",
        "cpi_timeseries_credential_echo_forbidden",
        "cpi_timeseries_point_invalid",
        "cpi_timeseries_period_invalid",
        "cpi_timeseries_duplicate_point",
        "cpi_timeseries_metadata_invalid",
        "cpi_timeseries_series_missing",
        "cpi_timeseries_content_encoding_rejected",
        "cpi_timeseries_observed_at_invalid",
        "bls_live_response_too_large",
    }
)


def release_probe_at(release_at, after=None):
    """Next official-time probe; skipped slots are never replayed in a burst."""
    release_at = _aware(release_at)
    offsets = [-30, -15, *range(0, 121, 15), *range(180, 601, 60)]
    offsets += [*range(900, 1801, 300), 3600, 5400, 7200]
    for seconds in offsets:
        candidate = release_at + timedelta(seconds=seconds)
        if after is None or candidate > after:
            return candidate
    return None


class CpiScheduleError(ValueError):
    def __init__(self, classification, *, http_status=None, retry_at=None):
        self.classification = classification
        self.http_status, self.retry_at = http_status, retry_at
        super().__init__(classification)


def now_utc():
    return datetime.now(UTC)


def _aware(value):
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CpiScheduleError("schedule_aware_clock_required")
    return value.astimezone(UTC)


def _instant(value):
    return _aware(datetime.fromisoformat(value))


class _ScheduleTable(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.table = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.table += 1
        elif tag == "tr" and self.table:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = []

    def handle_data(self, data):
        if self.cell is not None:
            self.cell.append(data)

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.cell is not None:
            self.row.append(" ".join(" ".join(self.cell).split()))
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            self.table = max(0, self.table - 1)
            self.rows.append(None)


def _month_number(name):
    normalized = name.casefold().rstrip(".")
    for index in range(1, 13):
        if normalized in {
            calendar.month_name[index].casefold(),
            calendar.month_abbr[index].casefold(),
            "sept" if index == 9 else "",
        }:
            return index
    raise CpiScheduleError("calendar_month_invalid")


def parse_official_calendar(raw):
    """Parse reference month/date/time without inferring any missing date."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CALENDAR_BYTES:
        raise CpiScheduleError("calendar_payload_invalid")
    parser = _ScheduleTable()
    try:
        parser.feed(raw.decode("utf-8"))
    except UnicodeError, ValueError:
        raise CpiScheduleError("calendar_payload_invalid") from None
    selected = False
    seen_header = False
    releases = {}
    for row in parser.rows:
        if row is None:
            selected = False
            continue
        if [cell.casefold() for cell in row[:3]] == [
            "reference month",
            "release date",
            "release time",
        ]:
            selected = seen_header = True
            continue
        if not selected or not any(row):
            continue
        if len(row) < 3:
            raise CpiScheduleError("calendar_entry_unresolved")
        reference = re.fullmatch(r"([A-Za-z.]+)\s+(\d{4})", row[0])
        date = re.fullmatch(r"([A-Za-z.]+)\s+(\d{1,2}),?\s+(\d{4})", row[1])
        time = re.fullmatch(r"(\d{1,2}):(\d{2})\s*([AP]M)", row[2], re.I)
        if not (reference and date and time):
            raise CpiScheduleError("calendar_entry_unresolved")
        try:
            ref = f"{int(reference[2]):04d}-{_month_number(reference[1]):02d}"
            hour, minute = int(time[1]), int(time[2])
            if not 1 <= hour <= 12 or not 0 <= minute < 60:
                raise ValueError
            hour = hour % 12 + (12 if time[3].upper() == "PM" else 0)
            local = datetime(
                int(date[3]),
                _month_number(date[1]),
                int(date[2]),
                hour,
                minute,
                tzinfo=NEW_YORK,
            )
            # Published CPI time is a New York wall time, never a fixed UTC offset.
            release = {
                "reference_month": ref,
                "release_at": local.astimezone(UTC).isoformat(),
                "release_at_local": local.isoformat(),
                "timezone": "America/New_York",
            }
            if ref in releases and releases[ref] != release:
                raise CpiScheduleError("calendar_duplicate_reference")
            if local.strftime("%Y-%m") <= ref:
                raise CpiScheduleError("calendar_reference_date_invalid")
            releases[ref] = release
        except ValueError, OverflowError:
            raise CpiScheduleError("calendar_entry_unresolved") from None
    if not seen_header or not releases:
        raise CpiScheduleError("calendar_table_missing")
    return sorted(releases.values(), key=lambda item: item["reference_month"])


def parse_official_history(raw, *, profile=CPI_PROFILE):
    """BLS annual/month list: explicit 'Index for Month Year' only."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CALENDAR_BYTES:
        raise CpiScheduleError("calendar_payload_invalid")
    parser = _ScheduleTable()
    try:
        parser.feed(raw.decode("utf-8"))
    except UnicodeError, ValueError:
        raise CpiScheduleError("calendar_payload_invalid") from None
    title = (
        "Employment Situation"
        if profile is EMPLOYMENT_PROFILE
        else "Job Openings and Labor Turnover Survey"
        if profile.key == "jolts"
        else "Consumer Price Index"
        if profile.key == "cpi"
        else "Producer Price Index"
    )
    rows = []
    for row in parser.rows:
        if not row or len(row) < 3:
            continue
        match = re.fullmatch(re.escape(title) + r" for ([A-Za-z]+ \d{4})", row[2])
        if match:
            rows.append(
                (match[1], re.sub(r"^[A-Za-z]+,\s+(?=[A-Za-z])", "", row[0]), row[1])
            )
    html = (
        "<table><tr><th>Reference Month</th><th>Release Date</th>"
        "<th>Release Time</th></tr>"
    )
    html += "".join(
        "<tr>" + "".join(f"<td>{cell}</td>" for cell in row) + "</tr>" for row in rows
    )
    return parse_official_calendar((html + "</table>").encode())


def _ics_text(value):
    return re.sub(r"\\([nN,;\\])", lambda m: "\n" if m[1] in "nN" else m[1], value)


def _ics_time(value, params):
    """Only the official feed's UTC and Eastern wall times are supported."""
    try:
        if params.get("VALUE", "DATE-TIME") != "DATE-TIME":
            raise ValueError
        if re.fullmatch(r"\d{8}T\d{6}Z", value) and "TZID" not in params:
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        if not re.fullmatch(r"\d{8}T\d{6}", value) or params.get("TZID") not in {
            "US-Eastern",
            "America/New_York",
        }:
            raise ValueError
        wall = datetime.strptime(value, "%Y%m%dT%H%M%S")
        first = wall.replace(tzinfo=NEW_YORK, fold=0)
        second = wall.replace(tzinfo=NEW_YORK, fold=1)
        if first.utcoffset() != second.utcoffset() or (
            first.astimezone(UTC).astimezone(NEW_YORK).replace(tzinfo=None) != wall
        ):
            raise ValueError
        return first.astimezone(UTC)
    except ValueError, OverflowError:
        raise CpiScheduleError("calendar_ics_time_unresolved") from None


def _ics_revision(event):
    return event["sequence"], event.get("revision_at") or ""


def _ics_choose(old, event):
    if old is None or _ics_revision(event) > _ics_revision(old):
        return event
    if _ics_revision(event) == _ics_revision(old) and any(
        old.get(key) != event.get(key) for key in ("release_at", "status", "summary")
    ):
        raise CpiScheduleError("calendar_ics_revision_conflict")
    return old


def parse_official_ics(raw, *, profile=CPI_PROFILE, known_uids=()):
    """Parse bounded BLS profile events, without inventing a reference month.

    US-Eastern is the feed's alias for America/New_York. Recurrences and
    ambiguous/nonexistent wall times require review rather than approximation.
    """
    profile = validate_profile(profile)
    titles = (
        {"consumer price index"}
        if profile.key == "cpi"
        else {"employment situation"}
        if profile is EMPLOYMENT_PROFILE
        else {
            "job openings and labor turnover survey",
            "job openings and labor turnover",
        }
        if profile.key == "jolts"
        else {"producer price index", "producer price indexes"}
    )
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_CALENDAR_BYTES:
        raise CpiScheduleError("calendar_payload_invalid")
    try:
        lines = re.sub(rb"\r?\n[ \t]", b"", raw).decode("utf-8-sig").splitlines()
    except UnicodeError:
        raise CpiScheduleError("calendar_payload_invalid") from None
    stack, blocks, fields = [], [], None
    method, calendars = "PUBLISH", 0
    for line in lines:
        if not line:
            continue
        match = re.fullmatch(r'((?:[^":]|"[^"]*")*):(.*)', line)
        if not match:
            raise CpiScheduleError("calendar_ics_invalid")
        parts = re.split(r";(?=(?:[^\"]*\"[^\"]*\")*[^\"]*$)", match[1])
        name, value = parts[0].upper(), match[2]
        if name == "BEGIN":
            component = value.upper()
            if component == "VCALENDAR":
                calendars += 1
                if stack or calendars != 1:
                    raise CpiScheduleError("calendar_ics_invalid")
            elif not stack:
                raise CpiScheduleError("calendar_ics_invalid")
            if component == "VEVENT":
                if stack != ["VCALENDAR"]:
                    raise CpiScheduleError("calendar_ics_invalid")
                fields = {}
            stack.append(component)
            continue
        if name == "END":
            if not stack or stack.pop() != value.upper():
                raise CpiScheduleError("calendar_ics_invalid")
            if value.upper() == "VEVENT":
                blocks.append(fields)
                fields = None
            continue
        if not stack:
            raise CpiScheduleError("calendar_ics_invalid")
        if stack == ["VCALENDAR"] and name == "METHOD":
            method = value.upper()
        if stack != ["VCALENDAR", "VEVENT"]:
            continue
        if name not in {
            "UID",
            "SUMMARY",
            "SEQUENCE",
            "DTSTART",
            "STATUS",
            "DTSTAMP",
            "LAST-MODIFIED",
            "RRULE",
            "RDATE",
            "EXDATE",
            "RECURRENCE-ID",
        }:
            continue
        if name in fields:
            raise CpiScheduleError("calendar_ics_duplicate_property")
        params = {}
        for part in parts[1:]:
            key, separator, parameter = part.partition("=")
            if not separator or key.upper() in params:
                raise CpiScheduleError("calendar_ics_invalid")
            params[key.upper()] = parameter.strip('"')
        fields[name] = (value, params)
    if stack or calendars != 1 or method not in {"PUBLISH", "CANCEL"}:
        raise CpiScheduleError("calendar_ics_invalid")
    events = {}
    for fields in blocks:
        summary = _ics_text(fields.get("SUMMARY", ("", {}))[0]).strip()
        status = fields.get("STATUS", ("CONFIRMED", {}))[0].upper()
        if method == "CANCEL":
            status = "CANCELLED"
        # A summary-less cancellation may identify an already bound profile UID.
        if summary.casefold() not in titles and not (
            not summary and status == "CANCELLED"
        ):
            continue
        uid = _ics_text(fields.get("UID", ("", {}))[0])
        if profile.key != "cpi" and not summary and uid not in known_uids:
            # An unknown summary-less cancellation cannot establish its asset.
            continue
        sequence = fields.get("SEQUENCE", ("0", {}))[0]
        if (
            not uid
            or len(uid) > 512
            or any(ord(c) < 32 for c in uid)
            or not re.fullmatch(r"\d{1,9}", sequence)
            or status not in {"CONFIRMED", "TENTATIVE", "CANCELLED"}
            or {"RRULE", "RDATE", "EXDATE", "RECURRENCE-ID"} & fields.keys()
        ):
            raise CpiScheduleError("calendar_ics_entry_unresolved")
        event = dict(uid=uid, sequence=int(sequence), summary=summary, status=status)
        revision = fields.get("LAST-MODIFIED", fields.get("DTSTAMP"))
        event["revision_at"] = _ics_time(*revision).isoformat() if revision else None
        if "DTSTART" in fields:
            release = _ics_time(*fields["DTSTART"])
            event.update(
                release_at=release.isoformat(),
                release_at_local=release.astimezone(NEW_YORK).isoformat(),
                timezone="America/New_York",
            )
        elif status != "CANCELLED":
            raise CpiScheduleError("calendar_ics_entry_unresolved")
        events[uid] = _ics_choose(events.get(uid), event)
    return sorted(events.values(), key=lambda event: event["uid"])


def _fetch_calendar(
    url, accept, *, title="CPI", allowed_path=None, **transport_options
):
    try:
        return fetch_bls_page(
            url,
            headers={
                "User-Agent": f"Market-Data-Foundation {title} calendar/1.0",
                "Accept": accept,
            },
            max_bytes=MAX_CALENDAR_BYTES,
            timeout=20,
            allowed_path=allowed_path
            or "/schedule/news_release/" + url.rsplit("/", 1)[1],
            **transport_options,
        )
    except BlsWebError as error:
        raise CpiScheduleError(
            f"calendar_{error.classification}",
            http_status=error.http_status,
            retry_at=error.retry_at,
        ) from None
    except CpiScheduleError:
        raise
    except HTTPError as error:
        raise CpiScheduleError(f"calendar_http_{error.code}") from None
    except TimeoutError:
        raise CpiScheduleError("calendar_timeout") from None
    except URLError as error:
        classification = (
            "calendar_timeout"
            if isinstance(error.reason, TimeoutError)
            else "calendar_transport_failed"
        )
        raise CpiScheduleError(classification) from None
    except Exception:
        raise CpiScheduleError("calendar_fetch_failed") from None


def fetch_official_calendar():
    """Default CLI/scheduler refresh uses the official ICS time authority."""
    return _fetch_calendar(ICS_URL, "text/calendar")


def fetch_reference_calendar():
    """HTML supplies explicit reference-month evidence, never guessed offsets."""
    return _fetch_calendar(CALENDAR_URL, "text/html")


def _write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _month_shift(month, delta):
    year, number = map(int, month.split("-"))
    year, index = divmod(year * 12 + number - 1 + delta, 12)
    return f"{year:04d}-{index + 1:02d}"


def update_window(reference_month, history_start="1999-01"):
    validate_month(reference_month)
    validate_month(history_start)
    if history_start > reference_month:
        raise CpiScheduleError("schedule_history_range_invalid")
    return history_start, reference_month, "configured_full_history"


def _stage(at, *, state="pending", attempts=0):
    return dict(state=state, attempts=attempts, next_attempt_at=at)


def _classification(error, fallback):
    value = getattr(error, "classification", None)
    return (
        value
        if isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,99}", value)
        else fallback
    )


def _keys(points):
    return {(p.get("series_id"), p.get("month")) for p in points}


def _months(start, end):
    while start <= end:
        yield start
        start = _month_shift(start, 1)


def _due(stage, at):
    next_at = stage.get("next_attempt_at")
    return (
        stage["state"] in {"pending", "awaiting_provider", "running"}
        and next_at is not None
        and _instant(next_at) <= at
    )


def _target_present(result, reference_month, series_ids=SERIES_IDS):
    if not isinstance(result, dict):
        return False
    present = {
        point.get("series_id")
        for point in result.get("points", [])
        if point.get("month") == reference_month
        and point.get("value" if "value" in point else "index_value") is not None
        and point.get("availability", "available") == "available"
    }
    return set(series_ids) <= present


def _fresh_target(result, reference_month, series_ids=SERIES_IDS):
    if not _target_present(result, reference_month, series_ids):
        return False
    if set(result.get("series_ids", [])) != set(series_ids):
        return False
    refreshed = result.get("refreshed_series_months")
    if isinstance(refreshed, list):
        return {(item.get("series_id"), item.get("month")) for item in refreshed} >= {
            (series, reference_month) for series in series_ids
        }
    if result.get("refresh_complete") is True:
        return True
    missing = result.get("source_missing")
    return (
        isinstance(missing, list)
        and bool(missing)
        and all(
            isinstance(item, dict)
            and item.get("series_id") in series_ids
            and isinstance(item.get("month"), str)
            and item["month"] != reference_month
            for item in missing
        )
    )


class CpiReleaseScheduler:
    def __init__(
        self,
        service,
        state_path,
        *,
        clock=now_utc,
        calendar_fetcher=_DEFAULT_FETCHER,
        reference_fetcher=_DEFAULT_FETCHER,
        fallback_fetcher=_DEFAULT_FETCHER,
        history_start=None,
        signal_fetcher=_DEFAULT_FETCHER,
        signal_poll_seconds=3600,
        max_attempts=5,
        auto_mode=False,
        profile=CPI_PROFILE,
    ):
        self.profile = validate_profile(profile)
        self.asset_key = profile.key
        self.series_ids = profile.series_ids
        self.calendar_title = (
            "Employment Situation"
            if profile is EMPLOYMENT_PROFILE
            else "Job Openings and Labor Turnover Survey"
            if profile.key == "jolts"
            else "Consumer Price Index"
            if profile.key == "cpi"
            else "Producer Price Index"
        )
        self.calendar_url = (
            "https://www.bls.gov/schedule/news_release/empSit.htm"
            if profile is EMPLOYMENT_PROFILE
            else f"https://www.bls.gov/schedule/news_release/{profile.key}.htm"
        )
        self.ics_url = ICS_URL
        if getattr(service, "profile", profile) is not profile:
            raise CpiScheduleError("schedule_service_profile_mismatch")
        self.service = service
        self.state_path = Path(state_path).expanduser().resolve()
        self.clock = clock
        self.calendar_fetcher = (
            (
                fetch_official_calendar
                if profile.key == "cpi"
                else lambda: _fetch_calendar(
                    self.ics_url, "text/calendar", title=profile.key.upper()
                )
            )
            if calendar_fetcher is _DEFAULT_FETCHER
            else calendar_fetcher
        )
        self.reference_fetcher = (
            (
                lambda: _fetch_calendar(
                    self.calendar_url, "text/html", title=profile.key.upper()
                )
            )
            if reference_fetcher is _DEFAULT_FETCHER
            else reference_fetcher
        )
        self.fallback_fetcher = (
            (self.reference_fetcher if calendar_fetcher is _DEFAULT_FETCHER else None)
            if fallback_fetcher is _DEFAULT_FETCHER
            else fallback_fetcher
        )
        if history_start is None:
            history_start = profile.earliest_month
            if history_start is None and profile.key == "cpi":
                history_start = "1999-01"
        if history_start is None:
            raise CpiScheduleError("schedule_history_start_required")
        validate_month(history_start)
        if profile.earliest_month and history_start < profile.earliest_month:
            raise CpiScheduleError("schedule_history_start_before_profile")
        if type(max_attempts) is not int or not 1 <= max_attempts <= 20:
            raise CpiScheduleError("schedule_attempt_budget_invalid")
        if (
            type(signal_poll_seconds) is not int
            or not 60 <= signal_poll_seconds <= 86400
        ):
            raise CpiScheduleError("schedule_signal_interval_invalid")
        self.history_start = history_start
        self.signal_fetcher = (
            fetch_official_errata
            if signal_fetcher is _DEFAULT_FETCHER and profile.key == "cpi"
            else None
            if signal_fetcher is _DEFAULT_FETCHER
            else signal_fetcher
        )
        if profile.key != "cpi" and self.signal_fetcher is not None:
            raise CpiScheduleError("schedule_signals_unsupported")
        self.signal_poll_seconds = signal_poll_seconds
        self.max_attempts = max_attempts
        if type(auto_mode) is not bool:
            raise CpiScheduleError("schedule_auto_mode_invalid")
        self.auto_mode = auto_mode

    @contextmanager
    def _locked(self):
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.state_path.with_suffix(self.state_path.suffix + ".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise CpiScheduleError("schedule_busy") from None
            yield
        finally:
            os.close(descriptor)

    def _read(self):
        if not self.state_path.exists():
            return {
                "version": 2,
                "asset": self.asset_key,
                "calendar": {},
                "targets": {},
                "signals": self._initial_signals(),
                "history_start": self.history_start,
            }
        try:
            state = json.loads(self.state_path.read_text())
            self._validate_asset(state)
            if (
                state.get("version") not in {1, 2}
                or not isinstance(state.get("calendar"), dict)
                or not isinstance(state.get("targets"), dict)
            ):
                raise ValueError
            if state.get("history_start", self.history_start) != self.history_start:
                raise CpiScheduleError("schedule_history_start_changed")
            for target in state["targets"].values():
                if "target" not in target:
                    complete = target["state"] == "complete"
                    target["legacy_state"] = target["state"]
                    target["target"] = _stage(
                        target.get("next_attempt_at") or target["release_at"],
                        state="complete" if complete else target["state"],
                        attempts=target.get("attempts", 0),
                    )
                    target["history"] = _stage(
                        None if complete else target["release_at"],
                        state="legacy_unverified" if complete else "blocked",
                    )
                    target["target_available"] = complete
                    target["legacy_completion_unverified"] = complete
            state.update(
                version=2, asset=self.asset_key, history_start=self.history_start
            )
            state.setdefault("signals", self._initial_signals())
            if self.profile.key != "cpi":
                if state["signals"].get("events") or state["signals"].get("recovery"):
                    raise CpiScheduleError("schedule_signals_unsupported")
                state["signals"] = self._initial_signals()
            return state
        except CpiScheduleError:
            raise
        except ValueError, OSError, TypeError, KeyError, AttributeError:
            raise CpiScheduleError("schedule_state_invalid") from None

    def _validate_asset(self, value, classification="schedule_asset_mismatch"):
        if not isinstance(value, dict):
            raise CpiScheduleError(classification)
        # Only historical CPI files predate the explicit asset envelope.
        asset = value.get("asset", "cpi" if self.profile.key == "cpi" else None)
        if asset != self.asset_key:
            raise CpiScheduleError(classification)

    def _initial_signals(self):
        return {
            "state": "unsupported"
            if self.profile.key != "cpi"
            else "unknown"
            if self.signal_fetcher is not None
            else "disabled",
            "events": {},
        }

    def _save(self, state):
        self._validate_asset(state)
        state["asset"] = self.asset_key
        encoded = json.dumps(state, sort_keys=True, ensure_ascii=False).encode()
        _write_atomic(self.state_path, encoded)

    def _calendar_status(self, state, at):
        calendar_state = state["calendar"]
        if not calendar_state:
            return "missing"
        acquired = calendar_state.get("acquired_at", calendar_state.get("fetched_at"))
        if acquired is not None and at < _instant(acquired):
            return "clock_reversal"
        if acquired is not None and at - _instant(acquired) >= CALENDAR_MAX_AGE:
            return "stale"
        if calendar_state.get("error"):
            return (
                "degraded_cached_calendar"
                if calendar_state.get("sha256")
                else "unavailable"
            )
        if acquired is None:
            return "unavailable"
        return "fresh"

    def _snapshot(self, state, at):
        targets = [
            {"reference_month": month, **target}
            for month, target in sorted(state["targets"].items())
        ]
        stages = [
            stage
            for target in targets
            if target["state"] != "calendar_removed"
            and not target.get("calendar_execution_blocked")
            for stage in (target.get("target", target), target.get("history", {}))
        ]
        signals = state["signals"]
        stages.extend(signals["events"].values())
        if signals.get("recovery"):
            stages.append(signals["recovery"])
        next_times = [
            stage["next_attempt_at"]
            for stage in stages
            if stage.get("state") in {"pending", "awaiting_provider", "running"}
            and stage.get("next_attempt_at")
        ]
        if self.auto_mode:
            next_times = []
            for target in targets:
                if target["state"] == "calendar_removed" or target.get(
                    "calendar_execution_blocked"
                ):
                    continue
                auto = target.get("auto", {})
                if target["target"]["state"] in {
                    "pending",
                    "awaiting_provider",
                    "running",
                } and auto.get("state") not in {"complete", "stopped"}:
                    next_at = auto.get("next_attempt_at")
                    if next_at is None and not auto:
                        release = _instant(target["release_at"])
                        next_at = (
                            release_probe_at(release)
                            if at <= release + timedelta(seconds=AUTO_WINDOW_SECONDS)
                            else at
                        ).isoformat()
                    if next_at:
                        next_times.append(next_at)
            dense_until = self._dense_until(state, at)
            deferred_until = dense_until or at
            history_deferred_until = self._history_guard_until(state, at) or at
            histories = [
                t
                for t in targets
                if t["state"] != "calendar_removed"
                and not t.get("calendar_execution_blocked")
                and t["history"]["state"] in {"pending", "awaiting_provider", "running"}
                and t["history"].get("next_attempt_at")
            ]
            if histories:
                latest = max(histories, key=lambda t: t["reference_month"])
                next_times.append(
                    max(
                        _instant(latest["history"]["next_attempt_at"]),
                        history_deferred_until,
                    ).isoformat()
                )
            for event in signals["events"].values():
                if event["state"] in {
                    "pending",
                    "awaiting_provider",
                    "running",
                } and event.get("next_attempt_at"):
                    lower = at if event.get("kind") == "withdrawal" else deferred_until
                    next_times.append(
                        max(_instant(event["next_attempt_at"]), lower).isoformat()
                    )
            recovery = signals.get("recovery")
            if (
                recovery
                and recovery["state"] in {"pending", "awaiting_provider", "running"}
                and recovery.get("next_attempt_at")
            ):
                next_times.append(
                    max(
                        _instant(recovery["next_attempt_at"]), history_deferred_until
                    ).isoformat()
                )
            if signals.get("next_refresh_at"):
                next_times.append(
                    max(
                        _instant(signals["next_refresh_at"]), deferred_until
                    ).isoformat()
                )
            next_times = [max(_instant(value), at).isoformat() for value in next_times]
        future_dates = any(
            target["state"] != "calendar_removed"
            and not target.get("calendar_execution_blocked")
            and _instant(target["release_at"]) > at
            for target in targets
        )
        return {
            "calendar_status": self._calendar_status(state, at),
            "calendar_coverage": (
                "future_dates_available" if future_dates else "no_future_dates"
            ),
            "calendar": state["calendar"],
            "targets": targets,
            "calendar_publications": bls_calendar_publications(state),
            "history_start": state["history_start"],
            "signals": signals,
            "revision_status": (
                "unsupported"
                if self.profile.key != "cpi"
                else "pending"
                if any(
                    e["state"] not in {"complete", "dismissed"}
                    for e in signals["events"].values()
                )
                else "no_pending_known_notices"
            ),
            "next_due": min(next_times) if next_times else None,
            "checked_at": at.isoformat(),
        }

    def status(self):
        return self._snapshot(self._read(), _aware(self.clock()))

    def _cache_calendar(self, raw, extension):
        sha256 = hashlib.sha256(raw).hexdigest()
        path = self.state_path.parent / (self.state_path.stem + "-calendar")
        path = path / (sha256 + extension)
        if not path.exists():
            _write_atomic(path, raw)
        return {"sha256": sha256, "raw_path": str(path)}

    def _reference_evidence(self, state):
        calendar_state = state["calendar"]
        for month, target in state["targets"].items():
            target.setdefault(
                "reference_month_evidence",
                {
                    "source": calendar_state.get("source"),
                    "sha256": target.get(
                        "calendar_sha256", calendar_state.get("sha256")
                    ),
                    "raw_path": calendar_state.get("raw_path"),
                    "reference_month": month,
                    "release_at": target["release_at"],
                },
            )

    def _accept_calendar(self, state, at, raw, **options):
        # A rejected revision must not leak partial target changes into the
        # degraded cache subsequently saved by _refresh.
        working = copy.deepcopy(state)
        if isinstance(raw, bytes) and raw.lstrip(b"\xef\xbb\xbf \r\n").startswith(
            b"BEGIN:VCALENDAR"
        ):
            result = self._accept_ics(working, at, raw, **options)
        else:
            options.pop("reference_raw", None)
            options.pop("reference_error", None)
            options.pop("fetch_reference", None)
            result = self._accept_html_calendar(working, at, raw, **options)
        state.clear()
        state.update(working)
        return result

    def _accept_ics(
        self,
        state,
        at,
        raw,
        *,
        acquired_at=None,
        imported=False,
        reference_raw=None,
        reference_error=None,
        fetch_reference=False,
    ):
        acquired_at = acquired_at or at
        incoming = parse_official_ics(
            raw, profile=self.profile, known_uids=state["calendar"].get("events", {})
        )
        reference_meta = None
        metadata = self._cache_calendar(raw, ".ics")
        self._reference_evidence(state)
        previous = state["calendar"]
        events = copy.deepcopy(previous.get("events", {}))
        latest_uids = {event["uid"] for event in incoming}
        for event in incoming:
            uid = event["uid"]
            old = events.get(uid)
            chosen = _ics_choose(old, event)
            if chosen is event:
                events[uid] = {
                    **event,
                    "reference_month": old.get("reference_month") if old else None,
                    "reference_month_evidence": old.get("reference_month_evidence")
                    if old
                    else None,
                    "calendar_sha256": metadata["sha256"],
                }
        candidates = {}
        for month, target in state["targets"].items():
            evidence = target["reference_month_evidence"]
            if (
                not target.get("ics_uid")
                and evidence.get("source") == self.calendar_url
                and evidence.get("reference_month") == month
                and evidence.get("release_at") == target["release_at"]
                and re.fullmatch(r"[0-9a-f]{64}", evidence.get("sha256") or "")
                and evidence.get("raw_path")
            ):
                candidates.setdefault(target["release_at"], {})[month] = evidence
        cached_reference = previous.get("reference_calendar") or {}
        if (
            cached_reference.get("source") == self.calendar_url
            and re.fullmatch(r"[0-9a-f]{64}", cached_reference.get("sha256") or "")
            and cached_reference.get("raw_path")
        ):
            for release in cached_reference.get("releases", []):
                candidates.setdefault(release["release_at"], {})[
                    release["reference_month"]
                ] = {
                    **{k: v for k, v in cached_reference.items() if k != "releases"},
                    **release,
                }

        def propose():
            proposals = {}
            for uid in sorted(latest_uids):
                event = events[uid]
                if event.get("reference_month"):
                    continue
                matches = candidates.get(event.get("release_at"), {})
                # A summary-less cancel can only cancel a previously bound UID.
                if len(matches) == 1 and event["summary"]:
                    month = next(iter(matches))
                    target = state["targets"].get(month, {})
                    if target.get("ics_uid") in {None, uid}:
                        proposals.setdefault(month, []).append((uid, matches[month]))
            return proposals

        proposals = propose()
        bindable = {
            matches[0][0] for matches in proposals.values() if len(matches) == 1
        }
        needs_reference = any(
            not events[uid].get("reference_month")
            and uid not in bindable
            and events[uid]["status"] != "CANCELLED"
            and events[uid].get("release_at")
            and (self.profile.key != "cpi" or _instant(events[uid]["release_at"]) > at)
            for uid in latest_uids
        )
        if reference_raw is not None or (
            fetch_reference and needs_reference and self.reference_fetcher is not None
        ):
            # ICS is authoritative for dates. HTML is fetched and parsed once,
            # only to seek missing future reference-month evidence. Historical
            # pending events must not keep the secondary channel active forever.
            try:
                if reference_raw is None:
                    reference_raw = self.reference_fetcher()
                references = parse_official_calendar(reference_raw)
                reference_meta = {
                    "source": self.calendar_url,
                    **self._cache_calendar(reference_raw, ".html"),
                    "acquired_at": acquired_at.isoformat(),
                    "releases": references,
                }
                for release in references:
                    candidates.setdefault(release["release_at"], {})[
                        release["reference_month"]
                    ] = {
                        **{k: v for k, v in reference_meta.items() if k != "releases"},
                        **release,
                    }
                proposals = propose()
            except Exception as error:
                reference_error = _classification(
                    error, "calendar_reference_fetch_failed"
                )
        for month, matches in proposals.items():
            if len(matches) == 1:
                uid, evidence = matches[0]
                events[uid].update(
                    reference_month=month, reference_month_evidence=evidence
                )
        changes = {"added": [], "retimed": [], "removed": []}
        present = set()
        for uid, event in sorted(events.items()):
            event["absent_from_latest_calendar"] = uid not in latest_uids
            if uid not in latest_uids:
                continue
            month = event.get("reference_month")
            if month is None:
                event.update(
                    mapping_status="reference_month_pending",
                    classification="calendar_reference_month_unverified",
                )
                continue
            event.update(mapping_status="verified", classification=None)
            present.add(month)
            target = state["targets"].get(month)
            if target is not None:
                target.update(ics_uid=uid, ics_sequence=event["sequence"])
            if (
                event["status"] != "CANCELLED"
                and _instant(event["release_at"]).astimezone(NEW_YORK).strftime("%Y-%m")
                <= month
            ):
                event.update(
                    mapping_status="release_time_pending",
                    classification="calendar_reference_date_invalid",
                )
                if target is not None and target["target"]["state"] not in {
                    "complete",
                    "source_unavailable",
                }:
                    target["calendar_execution_blocked"] = True
                continue
            if event["status"] == "CANCELLED":
                if target is not None:
                    target.update(
                        ics_uid=uid,
                        ics_sequence=event["sequence"],
                        cancellation_evidence={
                            "source": self.ics_url,
                            "uid": uid,
                            "sequence": event["sequence"],
                            "revision_at": event["revision_at"],
                            "sha256": event["calendar_sha256"],
                        },
                    )
                    if target["target"]["state"] not in {
                        "complete",
                        "source_unavailable",
                    }:
                        if target["state"] != "calendar_removed":
                            changes["removed"].append(month)
                        target["state"] = "calendar_removed"
                    else:
                        target["calendar_cancelled_after_completion"] = True
                continue
            if event["status"] == "TENTATIVE":
                event.update(
                    mapping_status="release_time_pending",
                    classification="calendar_release_time_unconfirmed",
                )
                if target is not None and target["target"]["state"] not in {
                    "complete",
                    "source_unavailable",
                }:
                    target["calendar_execution_blocked"] = True
                continue
            release = {
                key: event[key]
                for key in (
                    "release_at",
                    "release_at_local",
                    "timezone",
                )
            }
            if target is None:
                target = state["targets"][month] = {
                    **release,
                    "reference_month": month,
                    "state": "pending",
                    "attempts": 0,
                    "next_attempt_at": release["release_at"],
                    "target": _stage(release["release_at"]),
                    "history": _stage(None, state="blocked"),
                    "target_available": False,
                }
                changes["added"].append(month)
            else:
                changed = (
                    target.get("calendar_release_at", target["release_at"])
                    != release["release_at"]
                )
                if changed:
                    if target["target"]["state"] in {"complete", "source_unavailable"}:
                        target["calendar_changed_after_completion"] = True
                    else:
                        target["previous_release_at"] = target["release_at"]
                        target.update(release)
                        target["target"]["next_attempt_at"] = release["release_at"]
                        target["next_attempt_at"] = release["release_at"]
                    changes["retimed"].append(month)
                if target["state"] == "calendar_removed":
                    self._sync_target(target)
                target.pop("cancellation_evidence", None)
            target.update(
                ics_uid=uid,
                ics_sequence=event["sequence"],
                calendar_sha256=event["calendar_sha256"],
                reference_month_evidence=event["reference_month_evidence"],
                calendar_release_at=release["release_at"],
                calendar_execution_blocked=False,
            )
        for month, target in state["targets"].items():
            target["absent_from_latest_calendar"] = month not in present
            if month not in present and target.get("fallback_validation"):
                # A successful rolling ICS fetch cannot renew uncovered HTML dates.
                target["calendar_execution_blocked"] = True
                target["fallback_validation"] = "awaiting_primary_coverage"
        pending = [e for e in events.values() if e.get("mapping_status") != "verified"]
        dates = [
            events[uid]["release_at"]
            for uid in latest_uids
            if events[uid].get("release_at")
        ]
        state["calendar"] = {
            "source": self.ics_url,
            "format": "ics",
            **metadata,
            "fetched_at": acquired_at.isoformat(),
            "acquired_at": acquired_at.isoformat(),
            "imported_at": at.isoformat() if imported else None,
            "acquisition_method": "operator_import" if imported else "direct_https",
            "last_refresh_error": previous.get("error")
            or previous.get("last_refresh_error"),
            "error": None,
            "release_count": len(incoming),
            "missing_reference_months": [],
            "changes": changes,
            "horizon": max(dates) if dates else None,
            "events": events,
            "display_history": previous.get("display_history", []),
            "pending_events": pending,
            "reference_mapping_status": "pending" if pending else "verified",
            "reference_mapping_error": reference_error,
            "reference_calendar": reference_meta or previous.get("reference_calendar"),
        }
        self._save(state)
        return self._snapshot(state, at)

    def _accept_html_calendar(
        self, state, at, raw, *, acquired_at=None, imported=False, fallback=False
    ):
        acquired_at = acquired_at or at
        releases = parse_official_calendar(raw)
        sha256 = hashlib.sha256(raw).hexdigest()
        raw_path = (
            self.state_path.parent
            / (self.state_path.stem + "-calendar")
            / (sha256 + ".html")
        )
        if not raw_path.exists():
            _write_atomic(raw_path, raw)
        old = state["targets"]
        self._reference_evidence(state)
        present = {release["reference_month"] for release in releases}
        changes = {"added": [], "retimed": [], "removed": []}
        for release in releases:
            month = release["reference_month"]
            existing = old.get(month)
            if existing is not None and existing.get("ics_uid"):
                # A backup can corroborate a date, never roll back an ICS revision.
                if fallback:
                    event = (
                        state["calendar"].get("events", {}).get(existing["ics_uid"], {})
                    )
                    verified = (
                        event.get("status") == "CONFIRMED"
                        and event.get("release_at") == release["release_at"]
                        and not existing.get("cancellation_evidence")
                    )
                    existing["fallback_validation"] = (
                        "matched" if verified else "conflict"
                    )
                    existing["calendar_execution_blocked"] = not verified
                continue
            if existing is None:
                if fallback and _instant(release["release_at"]) <= at:
                    continue
                old[month] = {
                    **({"fallback_validation": "matched"} if fallback else {}),
                    **release,
                    "state": "pending",
                    "attempts": 0,
                    "next_attempt_at": release["release_at"],
                    "calendar_sha256": sha256,
                    "absent_from_latest_calendar": False,
                    "target": _stage(release["release_at"]),
                    "history": _stage(None, state="blocked"),
                    "target_available": False,
                    "reference_month_evidence": {
                        "source": self.calendar_url,
                        "sha256": sha256,
                        "raw_path": str(raw_path),
                        **release,
                    },
                }
                changes["added"].append(month)
                continue
            changed = existing["release_at"] != release["release_at"]
            existing["absent_from_latest_calendar"] = False
            if existing["state"] == "complete":
                if changed:
                    existing["calendar_changed_after_completion"] = True
                    changes["retimed"].append(month)
                continue
            if changed:
                existing["previous_release_at"] = existing["release_at"]
                if existing["target"]["state"] not in {
                    "complete",
                    "source_unavailable",
                }:
                    existing["next_attempt_at"] = release["release_at"]
                    existing["target"]["next_attempt_at"] = release["release_at"]
                changes["retimed"].append(month)
            existing.update(release)
            if fallback:
                existing["calendar_execution_blocked"] = False
                existing["fallback_validation"] = "matched"
            existing["calendar_sha256"] = sha256
            existing["reference_month_evidence"] = {
                "source": self.calendar_url,
                "sha256": sha256,
                "raw_path": str(raw_path),
                **release,
            }
            if existing["state"] == "calendar_removed":
                existing["state"] = "pending"
                existing["target"]["state"] = "pending"
        for month, target in old.items():
            if month in present:
                continue
            target["absent_from_latest_calendar"] = True
            if fallback:
                target["calendar_execution_blocked"] = True
                target["fallback_validation"] = "not_covered"
            # Absence from a rolling page/feed is never cancellation evidence.
        reference = min(present)
        missing_references = []
        while reference < max(present):
            if reference not in present:
                missing_references.append(reference)
            reference = _month_shift(reference, 1)
        state["calendar"] = {
            **{
                key: state["calendar"][key]
                for key in (
                    "events",
                    "display_history",
                    "pending_events",
                    "reference_mapping_status",
                    "reference_mapping_error",
                    "reference_calendar",
                )
                if key in state["calendar"]
            },
            "source": self.calendar_url,
            "format": "html",
            "fetched_at": acquired_at.isoformat(),
            "acquired_at": acquired_at.isoformat(),
            "imported_at": at.isoformat() if imported else None,
            "acquisition_method": "operator_import" if imported else "direct_https",
            "last_refresh_error": state["calendar"].get("error")
            or state["calendar"].get("last_refresh_error"),
            "sha256": sha256,
            "raw_path": str(raw_path),
            "error": None,
            "release_count": len(releases),
            "missing_reference_months": missing_references,
            "changes": changes,
            "horizon": max(release["release_at"] for release in releases),
        }
        self._save(state)
        return self._snapshot(state, at)

    def _refresh(self, state, at):
        if self._calendar_status(state, at) == "clock_reversal":
            raise CpiScheduleError("schedule_clock_reversal")
        attempts, errors = [], []
        for source, fetch, backup in (
            (self.ics_url, self.calendar_fetcher, False),
            (self.calendar_url, self.fallback_fetcher, True),
        ):
            if backup and (fetch is None or not may_fallback(errors[0])):
                break
            try:
                raw = fetch()
                options = {"fallback": True} if backup else {"fetch_reference": True}
                self._accept_calendar(state, at, raw, **options)
                attempts.append({"source": source, "outcome": "accepted"})
                refresh_metadata(
                    state["calendar"],
                    at,
                    primary=self.ics_url,
                    attempts=attempts,
                    success=True,
                )
                state["calendar"]["consecutive_refresh_failures"] = 0
                state["calendar"]["next_refresh_at"] = (
                    at + timedelta(hours=6)
                ).isoformat()
                self._save(state)
                return self._snapshot(state, at)
            except Exception as error:
                errors.append(error)
                attempts.append(
                    {
                        "source": source,
                        "outcome": "failed",
                        **failure(error, "calendar_refresh_failed"),
                    }
                )
        value = state["calendar"]
        failures = value.get("consecutive_refresh_failures", 0) + 1
        delay = RETRY_SECONDS[min(failures - 1, len(RETRY_SECONDS) - 1)]
        value.update(
            error=attempts[-1]["classification"],
            last_refresh_error=attempts[-1]["classification"],
            consecutive_refresh_failures=failures,
            next_refresh_at=retry_time(at, delay, *errors),
        )
        refresh_metadata(
            value, at, primary=self.ics_url, attempts=attempts, success=False
        )
        self._save(state)
        return self._snapshot(state, at)

    def import_display_history(self, raw, acquired_at, *, source, start, end):
        """Explicit bounded official HTML import, with no executable targets."""
        validate_month(start)
        validate_month(end)
        if start > end or _month_shift(start, 120) <= end:
            raise CpiScheduleError("calendar_history_range_invalid")
        if source != self.calendar_url and not re.fullmatch(
            r"https://www\.bls\.gov/schedule/\d{4}/(?:\d{2}_sched_list\.htm)?", source
        ):
            raise CpiScheduleError("calendar_history_source_invalid")
        at, acquired = _aware(self.clock()), _aware(acquired_at)
        if acquired > at:
            raise CpiScheduleError("calendar_acquired_at_in_future")
        if source == self.calendar_url:
            releases = parse_official_calendar(raw)
        else:
            releases = parse_official_history(raw, profile=self.profile)
        releases = [r for r in releases if start <= r["reference_month"] <= end]
        if not releases or any(_instant(r["release_at"]) > at for r in releases):
            raise CpiScheduleError("calendar_history_not_past")
        with self._locked():
            state = self._read()
            metadata = self._cache_calendar(raw, ".html")
            records = state["calendar"].setdefault("display_history", [])
            for release in releases:
                evidence = {
                    **metadata,
                    **release,
                    "source": source,
                    "acquired_at": acquired.isoformat(),
                }
                record = {**release, "reference_month_evidence": evidence}
                matches = [
                    e
                    for e in state["calendar"].get("events", {}).values()
                    if e.get("release_at") == release["release_at"]
                ]
                if len(matches) == 1:
                    record["uid"] = matches[0]["uid"]
                if record not in records:
                    records.append(record)
            self._save(state)
            return self._snapshot(state, at)

    def import_calendar(self, raw, acquired_at):
        at = _aware(self.clock())
        acquired_at = _aware(acquired_at)
        if acquired_at > at:
            raise CpiScheduleError("calendar_acquired_at_in_future")
        with self._locked():
            state = self._read()
            previous = state["calendar"].get(
                "acquired_at", state["calendar"].get("fetched_at")
            )
            if previous is not None and acquired_at < _instant(previous):
                raise CpiScheduleError("calendar_import_older_than_current")
            return self._accept_calendar(
                state, at, raw, acquired_at=acquired_at, imported=True
            )

    def refresh_calendar(self):
        with self._locked():
            return self._refresh(self._read(), _aware(self.clock()))

    def _retry(self, stage, at, classification):
        exhausted = stage["attempts"] >= self.max_attempts
        index = min(max(stage["attempts"] - 1, 0), len(RETRY_SECONDS) - 1)
        stage.update(
            state="pending_exhausted" if exhausted else "awaiting_provider",
            classification=classification,
            next_attempt_at=None
            if exhausted
            else (at + timedelta(seconds=RETRY_SECONDS[index])).isoformat(),
        )

    def _begin(self, state, stage, at):
        if stage["state"] == "running":
            self._retry(stage, at, "previous_attempt_interrupted")
            self._save(state)
            return False
        stage.update(
            state="running",
            attempts=stage["attempts"] + 1,
            last_attempt_at=at.isoformat(),
        )
        self._save(state)  # Marker is durable before the external effect.
        return True

    def _finish(self, stage, at, **extra):
        stage.update(
            state="complete",
            next_attempt_at=None,
            completed_at=at.isoformat(),
            classification=None,
            **extra,
        )

    def _sync_target(self, target):
        stage, history = target["target"], target["history"]
        target["attempts"] = stage["attempts"]
        target["target_available"] = stage["state"] == "complete" and stage.get(
            "availability_verified", True
        )
        if stage["state"] in {"complete", "source_unavailable"}:
            target["state"] = (
                "complete" if history["state"] == "complete" else "history_pending"
            )
            target["next_attempt_at"] = history.get("next_attempt_at")
        else:
            target["state"] = stage["state"]
            target["next_attempt_at"] = stage.get("next_attempt_at")
        target["classification"] = stage.get("classification") or history.get(
            "classification"
        )

    def _available_readback(self, start, end, series, persisted, *, scope=None):
        """Finish scheduled writes only after current availability is confirmed.

        The refresh result is a persisted snapshot, not a query-time upstream
        overlay. Only a fully verified scope may be explicitly confirmed now.
        Ordinary queries and unchanged updates never acquire this side effect.
        """
        explicit_scope = scope is not None
        scope = (
            {(s, m) for s in series for m in _months(start, end)}
            if scope is None
            else scope
        )

        def rows(result):
            selected = [
                p
                for field in ("points", "source_unavailable", "withdrawn")
                for p in result.get(field, [])
                if (p["series_id"], p["month"]) in scope
            ]
            indexed = {(p["series_id"], p["month"]): p for p in selected}
            if explicit_scope and len(indexed) != len(selected):
                raise CpiScheduleError("schedule_local_readback_incomplete")
            return indexed

        verified = rows(persisted)
        if not scope <= verified.keys():
            raise CpiScheduleError("schedule_local_readback_incomplete")
        if any(p.get("available_at") is None for p in verified.values()):
            if explicit_scope:
                # Never confirm local rows the current source request did not return.
                for month in sorted({m for _, m in scope}):
                    selected = tuple(s for s in series if (s, month) in scope)
                    self.service.confirm_current_availability(
                        month, month, series_ids=selected
                    )
            else:
                self.service.confirm_current_availability(start, end, series_ids=series)
        readback = self.service.query_range(
            start,
            end,
            series_ids=series,
            allow_upstream=False,
            vintage_mode="available",
        )
        actual = rows(readback)
        identity = (
            "revision",
            "observed_at",
            "value" if self.profile is EMPLOYMENT_PROFILE else "index_value",
            "availability",
            "version_status",
        )
        if not scope <= actual.keys() or any(
            actual[key].get("available_at") is None
            or any(actual[key].get(k) != point.get(k) for k in identity)
            for key, point in verified.items()
        ):
            raise CpiScheduleError("schedule_local_readback_incomplete")
        return readback

    def _history_readback(self, start, end, result, stage):
        """PPI source gaps are reconciled only against successful request batches.

        An explicit empty key list requires positive completed batch and HTTP
        counts too; an absent field never proves an empty successful response.
        """
        if self.profile.key not in {"ppi", "employment"}:
            if result.get("persisted") is not True or not (
                result.get("refresh_complete") is True
                or result.get("refresh_coverage_complete") is True
            ):
                raise CpiScheduleError("history_refresh_incomplete")
            return self._available_readback(start, end, self.series_ids, result)
        keys = result.get("source_returned_series_months")
        requests = result.get("provider_requests")
        if (
            result.get("persisted") is not True
            or not isinstance(keys, list)
            or type(requests) is not int
            or requests <= 0
            or (self._actual_http(result) or 0) <= 0
        ):
            raise CpiScheduleError("history_source_return_evidence_required")
        full = {(s, m) for s in self.series_ids for m in _months(start, end)}
        scope = set()
        for key in keys:
            if (
                not isinstance(key, dict)
                or set(key) != {"series_id", "month"}
                or not isinstance(key["series_id"], str)
                or not isinstance(key["month"], str)
            ):
                raise CpiScheduleError("history_source_return_keys_invalid")
            pair = (key["series_id"], key["month"])
            if pair not in full or pair in scope:
                raise CpiScheduleError("history_source_return_keys_invalid")
            scope.add(pair)
        readback = self._available_readback(
            start, end, self.series_ids, result, scope=scope
        )
        missing = {
            (p["series_id"], p["month"])
            for p in stage.get("source_missing", []) + result.get("source_missing", [])
        } | (full - scope)
        stage["source_missing"] = [
            {"series_id": s, "month": m} for s, m in sorted(missing)
        ]
        stage["coverage_complete"] = not missing
        return readback

    def _verify_target_availability(self, state, start, end, readback=None):
        # A later history/recovery/signal write can invalidate a previously
        # completed target's current availability. Keep its completed work, but
        # do not continue advertising an unconfirmed or withdrawn current row.
        targets = {
            month: target
            for month, target in state["targets"].items()
            if start <= month <= end and target["target"]["state"] == "complete"
        }
        if not targets:
            return
        if readback is None:
            try:
                readback = self.service.query_range(
                    min(targets),
                    max(targets),
                    series_ids=self.series_ids,
                    allow_upstream=False,
                    vintage_mode="available",
                )
            except Exception:
                readback = {}
        for month, target in targets.items():
            target["target"]["availability_verified"] = _target_present(
                readback, month, self.series_ids
            ) and all(
                p.get("available_at") is not None
                for p in readback.get("points", [])
                if p.get("month") == month
            )
            self._sync_target(target)

    def _target_run(self, state, month, target, at):
        stage = target["target"]
        if not self._begin(state, stage, at):
            self._sync_target(target)
            self._save(state)
            return "interrupted"
        try:
            result = self.service.update_range(
                month, month, series_ids=self.series_ids, include_buffer=False
            )
            target.update(
                release_material=result.get("release_material"),
                last_provider_requests=result.get("provider_requests", 0),
                last_provider_attempts=result.get("provider_attempts", 0),
                last_result_complete=result.get("complete") is True,
                last_refresh_complete=result.get("refresh_complete") is True,
                source_missing=result.get("source_missing", []),
                source_unavailable=result.get("source_unavailable", []),
                target_refreshed=_fresh_target(result, month, self.series_ids),
            )
            expected = {(s, month) for s in self.series_ids}
            unavailable = _keys(result.get("source_unavailable", []))
            covered = _keys(result.get("refreshed_series_months", [])) | unavailable
            explicit_missing = bool(expected & unavailable) and expected <= covered
            if result.get("persisted") is not True or not (
                target["target_refreshed"]
                or (
                    explicit_missing and result.get("refresh_coverage_complete") is True
                )
            ):
                raise CpiScheduleError("target_month_not_available")
            readback = self._available_readback(month, month, self.series_ids, result)
            if explicit_missing:
                if not expected <= (
                    _keys(readback.get("points", []))
                    | _keys(readback.get("source_unavailable", []))
                ):
                    raise CpiScheduleError("schedule_local_readback_incomplete")
                self._finish(stage, _aware(self.clock()))
                stage.update(
                    state="source_unavailable", classification="source_unavailable"
                )
            elif not _target_present(readback, month, self.series_ids):
                raise CpiScheduleError("schedule_local_readback_incomplete")
            else:
                stage["availability_verified"] = True
                self._finish(stage, _aware(self.clock()))
                target["verified_target_series"] = list(self.series_ids)
            target["history"] = _stage(at.isoformat())
            action = "updated" if not explicit_missing else "source_unavailable"
        except Exception as error:
            self._retry(stage, at, _classification(error, "schedule_update_failed"))
            action = "awaiting_provider"
        self._sync_target(target)
        self._save(state)
        return action

    def _history_run(self, state, stage, end, at):
        if not self._begin(state, stage, at):
            self._verify_target_availability(state, self.history_start, end)
            self._save(state)
            return "interrupted"
        readback = None
        start = self.history_start
        try:
            start, end, policy = update_window(end, self.history_start)
            stage.update(update_start=start, update_end=end, refresh_policy=policy)
            result = self.service.update_range(
                start, end, series_ids=self.series_ids, include_buffer=False
            )
            stage["release_material"] = result.get("release_material")
            stage["source_missing"] = result.get("source_missing", [])
            readback = self._history_readback(start, end, result, stage)
            self._finish(stage, _aware(self.clock()))
            if stage["source_missing"]:
                stage["classification"] = "historical_source_gaps"
            action = "history_updated"
        except Exception as error:
            self._retry(stage, at, _classification(error, "schedule_update_failed"))
            action = "history_awaiting_provider"
        self._verify_target_availability(state, start, end, readback)
        self._save(state)
        return action

    def _accept_signals(self, state, notices, at):
        events = state["signals"]["events"]
        for notice in notices:
            identity = notice["signal_id"]
            old = events.get(identity)
            if old and old["state"] == "dismissed":
                if notice.get("verified"):
                    raise CpiScheduleError("signal_dismissal_not_allowed")
                continue
            if notice.get("discovered_signal_id") and (
                old is None
                or notice["evidence_url"]
                not in {old["evidence_url"], *old.get("related_urls", [])}
            ):
                raise CpiScheduleError("signal_discovery_evidence_mismatch")
            if old and old.get("verified"):
                if not notice.get("verified"):
                    continue
                immutable = set(notice) - {"expected_points", "evidence_text"}
                previous = {
                    (p["series_id"], p["month"]): p for p in old["expected_points"]
                }
                proposed = {
                    (p["series_id"], p["month"]): p for p in notice["expected_points"]
                }
                if any(old.get(k) != notice[k] for k in immutable) or any(
                    proposed.get(k) != p for k, p in previous.items()
                ):
                    raise CpiScheduleError("signal_verified_notice_conflict")
                # Adding verified material can unblock exhausted evidence checks;
                # duplicate imports cannot reset retry budgets or completed work.
                if len(proposed) > len(previous):
                    old.update(notice, evidence_updated_at=at.isoformat())
                    if old["state"] != "complete":
                        old.update(
                            _stage(
                                max(at, _instant(notice["effective_at"])).isoformat()
                            )
                        )
                continue
            if old and not notice.get("verified"):
                continue
            events[identity] = dict(old or {}, **notice, imported_at=at.isoformat())
            if notice.get("verified"):
                events[identity].update(_stage(notice["effective_at"]))

    def import_signals(self, notices):
        if self.profile.key != "cpi":
            raise CpiScheduleError("schedule_signals_unsupported")
        if not isinstance(notices, (list, tuple)) or len(notices) > 1000:
            raise CpiScheduleError("signal_import_invalid")
        try:
            validated = [validate_signal(notice) for notice in notices]
        except CpiSignalError as error:
            raise CpiScheduleError(error.classification) from None
        with self._locked():
            state, at = self._read(), _aware(self.clock())
            self._accept_signals(state, validated, at)
            self._save(state)
            return self._snapshot(state, at)

    def import_signal(self, notice):
        return self.import_signals([notice])

    def dismiss_signal(self, signal_id, reason):
        """Persist an operator's review of an unexecuted, irrelevant notice."""
        if not isinstance(signal_id, str) or not re.fullmatch(
            r"[0-9a-f]{64}", signal_id
        ):
            raise CpiScheduleError("signal_id_invalid")
        if (
            not isinstance(reason, str)
            or not 1 <= len(reason.strip()) <= 2000
            or any(ord(c) < 32 or ord(c) == 127 for c in reason)
        ):
            raise CpiScheduleError("signal_dismiss_reason_invalid")
        reason = reason.strip()
        with self._locked():
            state, at = self._read(), _aware(self.clock())
            event = state["signals"]["events"].get(signal_id)
            if event is None:
                raise CpiScheduleError("signal_not_found")
            if event["state"] == "dismissed":
                if event["dismiss_reason"] != reason:
                    raise CpiScheduleError("signal_dismissal_conflict")
                return self._snapshot(state, at)
            if (
                event["state"] != "review_required"
                or event.get("verified")
                or event.get("attempts", 0) != 0
                or event.get("last_attempt_at")
            ):
                raise CpiScheduleError("signal_dismissal_not_allowed")
            event.update(
                state="dismissed",
                dismiss_reason=reason,
                dismissed_at=at.isoformat(),
                next_attempt_at=None,
            )
            self._save(state)
            return self._snapshot(state, at)

    def _refresh_signals(self, state, at):
        if self.signal_fetcher is None:
            state["signals"].update(
                state="unsupported" if self.profile.key != "cpi" else "disabled",
                next_refresh_at=None,
            )
            self._save(state)
            return self._snapshot(state, at)
        channel = state["signals"]
        previous = channel.get("fetched_at")
        if previous and _instant(previous) > at:
            raise CpiScheduleError("schedule_clock_reversal")
        try:
            raw = self.signal_fetcher()
            notices = parse_official_errata(raw)
            digest = hashlib.sha256(raw).hexdigest()
            raw_path = (
                self.state_path.parent
                / (self.state_path.stem + "-signals")
                / (digest + ".html")
            )
            _write_atomic(raw_path, raw)
            self._accept_signals(state, notices, at)
            if channel["state"] == "degraded":
                # New outage creates one recovery job. Repeated successful polls
                # preserve its state, including exhausted or completed markers.
                previous_recovery = channel.get("recovery")
                if not previous_recovery or previous_recovery["state"] == "complete":
                    channel["recovery"] = dict(
                        _stage(at.isoformat()), outage_since=channel.get("outage_since")
                    )
            channel.update(
                state="healthy",
                error=None,
                fetched_at=at.isoformat(),
                raw_path=str(raw_path),
                sha256=digest,
                consecutive_failures=0,
                next_refresh_at=(
                    at + timedelta(seconds=self.signal_poll_seconds)
                ).isoformat(),
            )
        except Exception as error:
            failures = channel.get("consecutive_failures", 0) + 1
            if channel["state"] != "degraded":
                channel["outage_since"] = at.isoformat()
            channel.update(
                state="degraded",
                error=_classification(error, "signal_refresh_failed"),
                consecutive_failures=failures,
                next_refresh_at=(
                    at
                    + timedelta(
                        seconds=RETRY_SECONDS[min(failures - 1, len(RETRY_SECONDS) - 1)]
                    )
                ).isoformat(),
            )
        self._save(state)
        return self._snapshot(state, at)

    def refresh_signals(self):
        with self._locked():
            return self._refresh_signals(self._read(), _aware(self.clock()))

    def _observed_readback(self, start, end, series):
        options = {
            "series_ids": series,
            "allow_upstream": False,
            "vintage_mode": "observed",
        }
        parameters = inspect.signature(self.service.query_range).parameters
        if "query_mode" in parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()
        ):
            options["query_mode"] = "history"
        return self.service.query_range(start, end, **options)

    def _event_run(self, state, event, at):
        if not self._begin(state, event, at):
            self._verify_target_availability(
                state, event["start_month"], event["end_month"]
            )
            self._save(state)
            return "interrupted"
        start, end, series = (
            event["start_month"],
            event["end_month"],
            tuple(event["series_ids"]),
        )
        try:
            if event["kind"] == "withdrawal":
                self.service.withdraw_range(
                    start, end, series_ids=series, evidence_url=event["evidence_url"]
                )
                result = self._observed_readback(start, end, series)
                expected = {(s, m) for s in series for m in _months(start, end)}
                if not expected <= _keys(result.get("withdrawn", [])):
                    raise CpiScheduleError("signal_withdrawal_unconfirmed")
                self._available_readback(start, end, series, result)
            else:
                expected = {
                    (p["series_id"], p["month"]): p for p in event["expected_points"]
                }
                scope = {(s, m) for s in series for m in _months(start, end)}
                changed = any(
                    Decimal(p["previous_value"]) != Decimal(p["index_value"])
                    for p in expected.values()
                )
                restoration = (
                    [
                        {k: p[k] for k in ("series_id", "month", "index_value")}
                        for p in expected.values()
                    ]
                    if changed and set(expected) == scope
                    else []
                )
                result = self.service.update_range(
                    start,
                    end,
                    series_ids=series,
                    include_buffer=False,
                    restoration_evidence_url=event["evidence_url"],
                    restoration_expected_points=restoration,
                )
                if expected and not changed:
                    raise CpiScheduleError("signal_revision_change_unconfirmed")
                if set(expected) != scope:
                    raise CpiScheduleError("signal_revision_evidence_incomplete")
                refreshed = _keys(result.get("refreshed_series_months", []))
                if result.get("persisted") is not True or not scope <= refreshed:
                    raise CpiScheduleError("signal_revision_unconfirmed")
                readback = self._observed_readback(start, end, series)
                actual = {
                    (p["series_id"], p["month"]): p for p in readback.get("points", [])
                }
                if any(
                    k not in actual
                    or actual[k].get("index_value") is None
                    or actual[k].get("availability", "available") != "available"
                    or Decimal(actual[k]["index_value"]) != Decimal(p["index_value"])
                    for k, p in expected.items()
                ):
                    raise CpiScheduleError("signal_revision_unconfirmed")
                self._available_readback(start, end, series, readback)
            self._finish(event, _aware(self.clock()))
            action = "signal_completed"
        except Exception as error:
            self._retry(event, at, _classification(error, "signal_execution_failed"))
            action = "signal_pending"
        self._verify_target_availability(state, start, end)
        self._save(state)
        return action

    def _recovery_end(self, state, recovery):
        if recovery.get("end_month") is not None:
            return recovery["end_month"]
        released = [
            m
            for m, t in state["targets"].items()
            if t["target"]["state"] in {"complete", "source_unavailable"}
        ]
        try:
            latest = self.service.status().get("latest_months", {})
            known = [latest.get(s) for s in self.series_ids]
            if all(isinstance(m, str) for m in known):
                for month in known:
                    validate_month(month)
                released.append(min(known))
        except Exception:
            pass
        if released:
            recovery["end_month"] = max(released)
        return recovery.get("end_month")

    @staticmethod
    def _dense_until(state, at):
        ends = [
            _instant(t["release_at"]) + timedelta(seconds=AUTO_WINDOW_SECONDS)
            for t in state["targets"].values()
            if t["state"] != "calendar_removed"
            and not t.get("calendar_execution_blocked")
            and _instant(t["release_at"]) - timedelta(seconds=30)
            <= at
            <= _instant(t["release_at"]) + timedelta(seconds=AUTO_WINDOW_SECONDS)
        ]
        return max(ends) + timedelta(seconds=5) if ends else None

    def _history_guard_until(self, state, at):
        dense = self._dense_until(state, at)
        near = [
            _instant(t["release_at"]) + timedelta(seconds=AUTO_WINDOW_SECONDS + 5)
            for t in state["targets"].values()
            if t["state"] != "calendar_removed"
            and not t.get("calendar_execution_blocked")
            and at < _instant(t["release_at"]) <= at + timedelta(minutes=2)
        ]
        return max([*near, *([dense] if dense else [])], default=None)

    def _auto_target_state(self, target, at):
        release = _instant(target["release_at"])
        auto = target.setdefault("auto", {})
        if not isinstance(auto, dict) or any(
            type(auto.get(key, 0)) is not int or auto.get(key, 0) < 0
            for key in ("actual_http", "uncertain_http")
        ):
            raise CpiScheduleError("schedule_auto_state_invalid")
        auto.setdefault("actual_http", 0)
        auto.setdefault("state", "pending")
        auto.setdefault(
            "catch_up", at > release + timedelta(seconds=AUTO_WINDOW_SECONDS)
        )
        if auto.get("release_at") != target["release_at"]:
            # A retimed official event retains its actual request count.
            auto.update(
                release_at=target["release_at"],
                preflight_checked_at=None,
                catch_up=at > release + timedelta(seconds=AUTO_WINDOW_SECONDS),
            )
            if auto["state"] not in {"complete", "stopped"}:
                auto["next_attempt_at"] = (
                    at if auto["catch_up"] else release_probe_at(release)
                ).isoformat()
        return auto

    @staticmethod
    def _actual_http(value):
        if isinstance(value, dict):
            count = value.get("source_request_count", value.get("provider_attempts"))
        else:
            count = getattr(
                value, "provider_attempts", getattr(value, "attempts", None)
            )
        return count if type(count) is int and count >= 0 else None

    def _auto_retry(self, target, at, error=None, *, missing=False):
        auto, stage = target["auto"], target["target"]
        at = max(at, _aware(self.clock()))
        classification = _classification(error, "target_month_not_available")
        status = getattr(error, "http_status", None)
        retry_at = getattr(error, "retry_at", None)
        if isinstance(retry_at, str):
            retry_at = _instant(retry_at)
        release = _instant(target["release_at"])
        auto.update(classification=classification, http_status=status)
        delay = None
        stop = auto["actual_http"] + auto.get("uncertain_http", 0) >= AUTO_HTTP_CAP
        if (
            type(status) is int and 400 <= status < 500 and status not in {408, 429}
        ) or classification in _AUTO_TERMINAL_RESPONSE_ERRORS:
            # Only a valid response missing the target month merits probing.
            # Keep exact source diagnostics and the accumulated HTTP allowance.
            stop = True
        elif stop:
            auto["classification"] = "schedule_event_http_budget_exhausted"
        elif status in {401, 403} or any(
            word in classification
            for word in ("daily_limit", "daily_quota", "daily_request_limit")
        ):
            stop = True
        elif "state_unknown" in classification and retry_at is None:
            stop = True
        elif status == 429 or "rate_limit" in classification:
            auto.setdefault("rate_limited_since", at.isoformat())
            delay = timedelta(seconds=300)
        elif auto.get("last_http_count", 0) == 0 and retry_at is not None:
            # Shared spacing, singleflight, cooldown and budget blocks consume
            # no event allowance and may carry a durable retry instant.
            delay = timedelta(seconds=1)
        elif (
            status == 408
            or status is not None
            and status >= 500
            or any(
                word in classification for word in ("timeout", "transport", "http_5")
            )
        ):
            failures = auto.get("transient_failures", 0) + 1
            auto["transient_failures"] = failures
            delay = timedelta(seconds=(30, 60, 120)[min(failures - 1, 2)])
        elif not missing and "budget" in classification:
            stop = retry_at is None
            delay = timedelta(seconds=300)
        if stop:
            auto.update(state="stopped", next_attempt_at=None)
        else:
            if delay is not None:
                next_at = at + delay
            elif auto["catch_up"]:
                failures = auto.get("slow_failures", 0) + 1
                auto["slow_failures"] = failures
                next_at = at + timedelta(seconds=RETRY_SECONDS[min(failures - 1, 3)])
            else:
                next_at = release_probe_at(release, at)
            if retry_at is not None and (next_at is None or retry_at > next_at):
                next_at = retry_at
            window_end = release + timedelta(seconds=AUTO_WINDOW_SECONDS)
            if (
                not auto["catch_up"]
                and next_at is not None
                and next_at > window_end
                and at < window_end
            ):
                # Wake only to close the window, without violating a longer
                # shared cooldown by issuing another request at this deadline.
                next_at = window_end + timedelta(seconds=5)
            if next_at is None or (not auto["catch_up"] and at >= window_end):
                auto.update(state="stopped", next_attempt_at=None)
                auto["classification"] = "schedule_release_window_expired"
            else:
                auto.update(
                    state="awaiting_provider", next_attempt_at=next_at.isoformat()
                )
        stage.update(
            state="awaiting_provider",
            classification=auto["classification"],
            next_attempt_at=auto["next_attempt_at"],
        )

    def _auto_accept_local(self, target, at, request_at):
        auto, stage = target["auto"], target["target"]
        completed_at = _aware(self.clock())
        if request_at < _instant(target["release_at"]):
            next_at = max(_instant(target["release_at"]), completed_at)
            auto.update(
                state="early_detected",
                early_detected_at=completed_at.isoformat(),
                next_attempt_at=next_at.isoformat(),
                classification="schedule_post_release_recheck_required",
            )
            stage.update(
                state="awaiting_provider",
                next_attempt_at=next_at.isoformat(),
                classification=auto["classification"],
            )
            return "early_detected"
        auto.update(
            state="complete",
            next_attempt_at=None,
            completed_at=completed_at.isoformat(),
            classification=None,
        )
        self._finish(stage, completed_at, availability_verified=True)
        target["verified_target_series"] = list(self.series_ids)
        target["collection_kind"] = (
            "catch_up" if auto["catch_up"] else "release_observation"
        )
        if target["history"]["state"] != "complete":
            target["history"].update(
                state="pending", next_attempt_at=completed_at.isoformat()
            )
        return "updated"

    def _auto_target_run(self, state, month, target, at):
        auto, stage = target["auto"], target["target"]
        if auto["state"] == "running":
            # A persisted reservation is already charged. Older interrupted
            # markers have one separate conservative, non-repeating allowance.
            if not auto.pop("reserved_http", 0):
                auto["uncertain_http"] = auto.get("uncertain_http", 0) + 1
            request_at = _instant(auto.get("last_attempt_at", at.isoformat()))
            action = "interrupted"
            try:
                readback = self.service.query_range(
                    month,
                    month,
                    series_ids=self.series_ids,
                    allow_upstream=False,
                    vintage_mode="available",
                )
                available = _target_present(readback, month, self.series_ids) and all(
                    point.get("available_at") is not None
                    for point in readback.get("points", [])
                    if point.get("month") == month
                )
            except Exception:
                available = False
            if available:
                action = self._auto_accept_local(target, at, request_at)
            else:
                auto.update(
                    state="awaiting_provider",
                    classification="previous_attempt_interrupted",
                    next_attempt_at=(at + timedelta(seconds=300)).isoformat(),
                )
                stage.update(
                    state="awaiting_provider",
                    classification=auto["classification"],
                    next_attempt_at=auto["next_attempt_at"],
                )
            self._sync_target(target)
            self._save(state)
            return action
        # A target range requires one HTTP exchange. Charge durably before the
        # effect; known zero-attempt rejection returns this reservation.
        auto.update(state="running", last_attempt_at=at.isoformat(), reserved_http=1)
        auto["actual_http"] += 1
        stage["attempts"] += 1
        self._save(state)
        counted = False
        try:
            result = self.service.update_range(
                month, month, series_ids=self.series_ids, include_buffer=False
            )
            stage["release_material"] = result.get("release_material")
            target["release_material"] = result.get("release_material")
            target["auto"]["release_material"] = result.get("release_material")
            count = self._actual_http(result)
            if count is None:
                raise CpiScheduleError("schedule_actual_http_unknown")
            auto["actual_http"] += count - auto.pop("reserved_http", 0)
            auto["last_http_count"] = count
            counted = True
            if count == 0:
                stage["attempts"] -= 1
            if count:
                auto.update(transient_failures=0)
                auto.pop("rate_limited_since", None)
            target.update(
                last_provider_attempts=count,
                target_refreshed=_fresh_target(result, month, self.series_ids),
            )
            if (
                count == 0
                or not target["target_refreshed"]
                or result.get("persisted") is not True
            ):
                self._auto_retry(target, at, missing=True)
                action = "awaiting_provider"
            else:
                readback = self._available_readback(
                    month, month, self.series_ids, result
                )
                if not _target_present(readback, month, self.series_ids):
                    raise CpiScheduleError("schedule_local_readback_incomplete")
                action = self._auto_accept_local(target, at, at)
        except Exception as error:
            if not counted:
                count = self._actual_http(error)
                if count is None:
                    auto.update(
                        state="stopped",
                        next_attempt_at=None,
                        classification="schedule_actual_http_unknown",
                    )
                    stage.update(
                        state="awaiting_provider",
                        next_attempt_at=None,
                        classification=auto["classification"],
                    )
                else:
                    auto["actual_http"] += count - auto.pop("reserved_http", 0)
                    auto["last_http_count"] = count
                    if count == 0:
                        stage["attempts"] -= 1
                    self._auto_retry(target, at, error)
            else:
                self._auto_retry(target, at, error)
            action = "awaiting_provider"
        self._sync_target(target)
        self._save(state)
        return action

    def _auto_history_run(self, state, stage, end, at):
        """One persisted, calendar-bounded history batch per low-priority tick."""
        previous_attempts = stage["attempts"]
        if not self._begin(state, stage, at):
            return "interrupted"
        start = stage.get("next_start", self.history_start)
        # BLS's registered limit is twenty inclusive calendar years.
        batch_end = min(end, f"{int(start[:4]) + 19:04d}-12")
        stage.update(update_start=start, update_end=batch_end, reconciliation_end=end)
        readback = None
        try:
            result = self.service.update_range(
                start, batch_end, series_ids=self.series_ids, include_buffer=False
            )
            stage["release_material"] = result.get("release_material")
            count = self._actual_http(result)
            if count is None:
                raise CpiScheduleError("schedule_actual_http_unknown")
            if count == 0:
                stage["attempts"] = previous_attempts
                raise CpiScheduleError("schedule_history_actual_http_required")
            readback = self._history_readback(start, batch_end, result, stage)
            stage.setdefault("completed_batches", []).append(
                {
                    "start": start,
                    "end": batch_end,
                    "completed_at": _aware(self.clock()).isoformat(),
                }
            )
            if batch_end == end:
                self._finish(stage, _aware(self.clock()))
                if self.profile.key in {"ppi", "employment"} and stage.get(
                    "source_missing"
                ):
                    stage["classification"] = "historical_source_gaps"
                action = "history_updated"
            else:
                stage.update(
                    state="pending",
                    next_start=_month_shift(batch_end, 1),
                    next_attempt_at=(
                        _aware(self.clock()) + timedelta(seconds=300)
                    ).isoformat(),
                    classification=None,
                )
                action = "history_batch_updated"
        except Exception as error:
            if self._actual_http(error) == 0:
                stage["attempts"] = previous_attempts
            self._retry(stage, at, _classification(error, "schedule_update_failed"))
            retry_at = getattr(error, "retry_at", None)
            if retry_at is not None and stage.get("next_attempt_at"):
                stage["next_attempt_at"] = max(
                    _instant(stage["next_attempt_at"]), _aware(retry_at)
                ).isoformat()
            if getattr(error, "http_status", None) in {
                401,
                403,
            } or "daily_limit" in stage.get("classification", ""):
                stage.update(state="pending_exhausted", next_attempt_at=None)
            if stage.get("classification") == "schedule_actual_http_unknown":
                stage.update(state="pending_exhausted", next_attempt_at=None)
            action = "history_awaiting_provider"
        self._verify_target_availability(state, start, batch_end, readback)
        self._save(state)
        return action

    def _auto_due_check(self):
        if self.service is None:
            raise CpiScheduleError("schedule_service_required")
        source = getattr(self.service, "source", None)
        if getattr(source, "_max_transport_attempts", 1) != 1:
            raise CpiScheduleError("schedule_single_transport_attempt_required")
        with self._locked():
            at, state = _aware(self.clock()), self._read()
            calendar_status = self._calendar_status(state, at)
            if calendar_status == "clock_reversal":
                raise CpiScheduleError("schedule_clock_reversal")
            preflight = [
                t
                for t in state["targets"].values()
                if t["state"] != "calendar_removed"
                and t["target"]["state"] not in {"complete", "source_unavailable"}
                and _instant(t["release_at"]) - timedelta(seconds=30) <= at
                and not t.get("auto", {}).get("preflight_checked_at")
            ]
            next_refresh = state["calendar"].get("next_refresh_at")
            if (
                calendar_status != "fresh" or preflight or next_refresh is not None
            ) and (next_refresh is None or at >= _instant(next_refresh)):
                self._refresh(state, at)
                if not state["calendar"].get("error"):
                    for target in preflight:
                        # _accept_calendar swaps the dictionary; resolve again.
                        for current in state["targets"].values():
                            if current["release_at"] == target["release_at"]:
                                self._auto_target_state(current, at)[
                                    "preflight_checked_at"
                                ] = at.isoformat()
            if self._calendar_status(state, at) not in {
                "fresh",
                "degraded_cached_calendar",
            }:
                return dict(action="calendar_unavailable", **self._snapshot(state, at))
            ready = []
            for month, target in state["targets"].items():
                if target["state"] == "calendar_removed" or target.get(
                    "calendar_execution_blocked"
                ):
                    continue
                if target["target"]["state"] not in {
                    "pending",
                    "awaiting_provider",
                    "running",
                }:
                    continue
                auto = self._auto_target_state(target, at)
                next_at = auto.get("next_attempt_at")
                if (
                    not auto["catch_up"]
                    and at
                    > _instant(target["release_at"])
                    + timedelta(seconds=AUTO_WINDOW_SECONDS)
                    and auto["state"] not in {"running", "complete", "stopped"}
                ):
                    auto.update(
                        state="stopped",
                        next_attempt_at=None,
                        classification="schedule_release_window_expired",
                    )
                    target["target"].update(
                        state="awaiting_provider",
                        next_attempt_at=None,
                        classification=auto["classification"],
                    )
                    self._sync_target(target)
                    continue
                if (
                    auto["state"] not in {"complete", "stopped"}
                    and next_at
                    and _instant(next_at) <= at
                ):
                    if (
                        auto["actual_http"] + auto.get("uncertain_http", 0)
                        >= AUTO_HTTP_CAP
                        and auto["state"] != "running"
                    ):
                        self._auto_retry(target, at)
                    else:
                        ready.append((month, target))
            if ready:
                month, target = max(ready)
                action = self._auto_target_run(state, month, target, at)
                return dict(
                    action=action, target_month=month, **self._snapshot(state, at)
                )
            dense_until = self._dense_until(state, at)
            channel = state["signals"]
            next_refresh = channel.get("next_refresh_at")
            if (
                self.signal_fetcher is not None
                and dense_until is None
                and (next_refresh is None or _instant(next_refresh) <= at)
            ):
                self._refresh_signals(state, at)
            events = [
                e
                for e in channel["events"].values()
                if _due(e, at)
                and (dense_until is None or e.get("kind") == "withdrawal")
            ]
            if events:
                event = min(events, key=lambda e: (e["effective_at"], e["signal_id"]))
                action = self._event_run(state, event, at)
                return dict(
                    action=action,
                    signal_id=event["signal_id"],
                    **self._snapshot(state, at),
                )
            # One history reconciliation covers all collected catch-up months.
            histories = [
                (m, t)
                for m, t in state["targets"].items()
                if t["state"] != "calendar_removed"
                and not t.get("calendar_execution_blocked")
                and t["history"]["state"] in {"pending", "awaiting_provider", "running"}
            ]
            unfinished = [
                t
                for t in state["targets"].values()
                if t["state"] != "calendar_removed"
                and not t.get("calendar_execution_blocked")
                and _instant(t["release_at"]) <= at
                and t["target"]["state"] not in {"complete", "source_unavailable"}
                and t.get("auto", {}).get("state") != "stopped"
            ]
            recovery = channel.get("recovery")
            history_guard = self._history_guard_until(state, at)
            if (
                histories
                and not unfinished
                and history_guard is None
                and _due(max(histories)[1]["history"], at)
            ):
                month, target = max(histories)
                action = self._auto_history_run(state, target["history"], month, at)
                if target["history"]["state"] == "complete":
                    for old_month, old in state["targets"].items():
                        if (
                            old_month <= month
                            and old is not target
                            and old["history"]["state"] != "complete"
                            and old["target"]["state"]
                            in {"complete", "source_unavailable"}
                        ):
                            self._finish(
                                old["history"],
                                _aware(self.clock()),
                                satisfied_by_monthly_history=month,
                            )
                            self._sync_target(old)
                if (
                    target["history"]["state"] == "complete"
                    and recovery
                    and recovery["state"] != "complete"
                    and self._recovery_end(state, recovery) is not None
                    and recovery["end_month"] <= month
                ):
                    self._finish(
                        recovery,
                        _aware(self.clock()),
                        satisfied_by_monthly_history=month,
                    )
                self._sync_target(target)
                self._save(state)
                return dict(
                    action=action, target_month=month, **self._snapshot(state, at)
                )
            if (
                recovery
                and _due(recovery, at)
                and history_guard is None
                and not unfinished
            ):
                if self._recovery_end(state, recovery) is not None:
                    action = self._auto_history_run(
                        state, recovery, recovery["end_month"], at
                    )
                    return dict(
                        action="recovery_" + action, **self._snapshot(state, at)
                    )
                recovery["attempts"] += 1
                self._retry(recovery, at, "recovery_local_range_unavailable")
                self._save(state)
                return dict(
                    action="recovery_awaiting_local_range", **self._snapshot(state, at)
                )
            self._save(state)
            snapshot = self._snapshot(state, at)
            return dict(action="not_due", **snapshot)

    def due_check(self):
        if self.auto_mode:
            return self._auto_due_check()
        if self.service is None:
            raise CpiScheduleError("schedule_service_required")
        with self._locked():
            at, state = _aware(self.clock()), self._read()
            calendar_status = self._calendar_status(state, at)
            if calendar_status == "clock_reversal":
                raise CpiScheduleError("schedule_clock_reversal")
            if calendar_status != "fresh":
                next_refresh = state["calendar"].get("next_refresh_at")
                if next_refresh is None or at >= _instant(next_refresh):
                    self._refresh(state, at)
            if self._calendar_status(state, at) not in {
                "fresh",
                "degraded_cached_calendar",
            }:
                return dict(action="calendar_unavailable", **self._snapshot(state, at))
            # Monthly targets have priority over slower history/signals work.
            ready = [
                (m, t)
                for m, t in state["targets"].items()
                if t["state"] != "calendar_removed"
                and not t.get("calendar_execution_blocked")
                and _instant(t["release_at"]) <= at
                and _due(t["target"], at)
            ]
            if ready:
                month, target = max(ready)
                action = self._target_run(state, month, target, at)
                return dict(
                    action=action, target_month=month, **self._snapshot(state, at)
                )
            channel = state["signals"]
            next_refresh = channel.get("next_refresh_at")
            if self.signal_fetcher is not None and (
                next_refresh is None or _instant(next_refresh) <= at
            ):
                self._refresh_signals(state, at)
            events = [e for e in channel["events"].values() if _due(e, at)]
            if events:
                event = min(events, key=lambda e: (e["effective_at"], e["signal_id"]))
                action = self._event_run(state, event, at)
                return dict(
                    action=action,
                    signal_id=event["signal_id"],
                    **self._snapshot(state, at),
                )
            histories = [
                (m, t)
                for m, t in state["targets"].items()
                if t["state"] != "calendar_removed" and _due(t["history"], at)
            ]
            recovery = channel.get("recovery")
            if histories:
                month, target = max(histories)
                action = self._history_run(state, target["history"], month, at)
                self._sync_target(target)
                if (
                    target["history"]["state"] == "complete"
                    and recovery
                    and recovery["state"] != "complete"
                    and self._recovery_end(state, recovery) is not None
                    and recovery["end_month"] <= month
                ):
                    self._finish(
                        recovery,
                        _aware(self.clock()),
                        satisfied_by_monthly_history=month,
                    )
                self._save(state)
                return dict(
                    action=action, target_month=month, **self._snapshot(state, at)
                )
            if recovery and _due(recovery, at):
                if self._recovery_end(state, recovery) is not None:
                    # The endpoint is saved by the attempt marker and reused.
                    action = self._history_run(
                        state, recovery, recovery["end_month"], at
                    )
                    return dict(
                        action="recovery_" + action, **self._snapshot(state, at)
                    )
                recovery["attempts"] += 1
                self._retry(recovery, at, "recovery_local_range_unavailable")
                self._save(state)
                return dict(
                    action="recovery_awaiting_local_range", **self._snapshot(state, at)
                )
            snapshot = self._snapshot(state, at)
            action = "not_due" if snapshot["next_due"] else "calendar_update_required"
            if self._calendar_status(state, at) in {"missing", "unavailable"}:
                action = "calendar_unavailable"
            return dict(action=action, **snapshot)

    def run_due(self):
        return self.due_check()
