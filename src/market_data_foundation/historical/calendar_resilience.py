"""Small, provider-neutral refresh diagnostics; never selects another provider."""

import re
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime


def retry_after(value):
    if not value:
        return None
    try:
        return (
            (datetime.now(UTC) + timedelta(seconds=int(value)))
            if value.strip().isdigit()
            else parsedate_to_datetime(value).astimezone(UTC)
        )
    except ValueError, TypeError, OverflowError:
        return None


def failure(error, default):
    """Only bounded classifications/statuses, never provider bodies or exceptions."""
    code = getattr(error, "classification", default)
    if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_]{1,120}", code):
        code = default
    status = getattr(error, "http_status", None)
    if status is None and (match := re.search(r"_http_(\d{3})$", code)):
        status = int(match[1])
    return {"classification": code, "http_status": status}


def may_fallback(error):
    code = failure(error, "calendar_fetch_failed")["classification"]
    return failure(error, code)["http_status"] != 429 and not any(
        token in code
        for token in (
            "request_",
            "cooldown",
            "budget",
            "quota",
            "sequence",
            "revision",
            "certificate",
            "tls_",
            "redirect",
            "url_invalid",
        )
    )


def refresh_metadata(calendar, now, *, primary, attempts, success):
    calendar.update(
        primary_source=primary,
        refresh_attempts=attempts,
        last_refresh_attempt=now.isoformat(),
        refresh_mode=("primary" if len(attempts) == 1 else "fallback")
        if success
        else "cache"
        if calendar.get("acquired_at") or calendar.get("fetched_at")
        else "unavailable",
    )
    acquired = calendar.get("acquired_at") or calendar.get("fetched_at")
    calendar["cache_valid_until"] = (
        (datetime.fromisoformat(acquired) + timedelta(hours=24)).isoformat()
        if acquired
        else None
    )
    calendar["primary_error"] = attempts[0].get("classification")


def retry_time(now, delay, *errors):
    result = now + timedelta(seconds=delay)
    for error in errors:
        value = getattr(error, "retry_at", None)
        if value:
            result = max(
                result,
                datetime.fromisoformat(value) if isinstance(value, str) else value,
            )
    return result.isoformat()


class AcquiredCalendar(bytes):
    """Shared bytes retain the real HTTP acquisition time, never cache-read time."""

    def __new__(cls, raw, obtained_at):
        value = super().__new__(cls, raw)
        value.obtained_at = obtained_at
        return value
