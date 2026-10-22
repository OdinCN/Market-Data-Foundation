"""Caller-supplied venue calendar evidence; no weekday or cross-market inference."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .model import Operation, Page, require


@dataclass(frozen=True)
class SessionWindow:
    venue: str
    calendar_revision: str
    calendar_material_sha256: str
    market: int
    code: str
    trading_day: date
    timezone: str
    opens_at: datetime
    closes_at: datetime
    # Separate explicit UTC endpoints disambiguate DST folds and midnight sessions.
    opens_utc: datetime
    closes_utc: datetime
    category: int | None = None

    def __post_init__(self) -> None:
        import re

        require(
            type(self.venue) is str
            and type(self.calendar_revision) is str
            and bool(self.venue and self.calendar_revision),
            "calendar_identity_required",
        )
        require(
            bool(re.fullmatch(r"[a-f0-9]{64}", self.calendar_material_sha256)),
            "calendar_digest_required",
        )
        require(type(self.trading_day) is date, "trading_day_required")
        require(
            type(self.market) is int
            and 0 <= self.market <= 255
            and type(self.code) is str
            and bool(self.code),
            "session_instrument_required",
        )
        require(
            self.category is None
            or (type(self.category) is int and 0 <= self.category <= 255),
            "session_category_required",
        )
        try:
            zone = ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError:
            from .model import DecodeError

            raise DecodeError("unknown_venue_timezone") from None
        for wall, instant in (
            (self.opens_at, self.opens_utc),
            (self.closes_at, self.closes_utc),
        ):
            require(
                type(wall) is datetime and wall.utcoffset() is not None,
                "aware_session_wall_required",
            )
            require(
                type(instant) is datetime and instant.utcoffset() is not None,
                "aware_session_instant_required",
            )
            require(
                instant.utcoffset().total_seconds() == 0,
                "explicit_utc_endpoint_required",
            )
            converted = instant.astimezone(zone)
            # UTC round-trip rejects nonexistent local wall times, and
            # checks fold/offset.
            require(
                converted.replace(tzinfo=None) == wall.replace(tzinfo=None)
                and converted.utcoffset() == wall.utcoffset()
                and wall.astimezone(UTC) == instant.astimezone(UTC),
                "session_dst_or_zone_mismatch",
            )
        require(
            self.opens_utc.astimezone(UTC) < self.closes_utc.astimezone(UTC),
            "session_order",
        )

    def contains(self, value: datetime) -> bool:
        require(
            type(value) is datetime and value.utcoffset() is not None,
            "aware_session_check_required",
        )
        return self.opens_utc <= value.astimezone(UTC) < self.closes_utc


def adjudicate_refresh(pages: tuple[Page, ...], window: SessionWindow | None) -> dict:
    """Content change and source freshness are separate claims. No implicit clock."""
    result = {
        "active_refresh": "NOT_PROVEN",
        "source_freshness": "UNKNOWN",
        "source_event_clock": "UNKNOWN",
        "calendar": "UNKNOWN",
        "rounds": len(pages),
        "changed_transitions": 0,
    }
    if window is None:
        return result
    result["calendar"] = "CALLER_SUPPLIED_EXACT_VENUE_WINDOW"
    if len(pages) < 3:
        return result
    first = pages[0].material
    req = first.request
    # Historical API returning today's records still does not establish a live API.
    if req.operation in (
        Operation.HISTORY_MINUTE,
        Operation.HISTORY_TRADE,
        Operation.EX_HISTORY_MINUTE,
        Operation.EX_HISTORY_TRADE,
    ):
        return result
    if (req.market, req.category, req.code) != (
        window.market,
        window.category,
        window.code,
    ):
        return result
    last_received = None
    for page in pages:
        material = page.material
        if (
            material.mode != "retained"
            or not material.producer_sha256
            or not page.rows
            or material.request != req
            or (
                material.source,
                material.binding,
                material.producer_sha256,
                material.directory_identity,
            )
            != (
                first.source,
                first.binding,
                first.producer_sha256,
                first.directory_identity,
            )
        ):
            return result
        for instant in (material.obtained_at, material.received_at):
            if instant is None or not window.contains(instant):
                return result
        if (
            last_received is not None
            and material.obtained_at.astimezone(UTC) <= last_received
        ):
            return result
        last_received = material.received_at.astimezone(UTC)
    changes = sum(
        _refresh_content_identity(left) != _refresh_content_identity(right)
        for left, right in zip(pages, pages[1:])
    )
    result["changed_transitions"] = changes
    if changes >= 2:
        result["active_refresh"] = "OBSERVED_CONTENT_CHANGE_IN_EXACT_WINDOW"
    # No operation in this slice has an independently authenticated full event clock.
    return result


def _refresh_content_identity(page: Page) -> str:
    """Compare named live fields, excluding opaque bytes and derived row labels."""
    from .model import canonical, sha

    operation = page.material.request.operation
    if operation == Operation.TRADE:
        names = ("Time", "Price", "Volume", "Number", "Status")
    elif operation == Operation.BARS:
        names = ("Time", "Open", "High", "Low", "Close", "Volume", "Amount")
    elif operation == Operation.EX_QUOTE:
        names = ("price", "zongLiang", "xianLiang", "bid", "bidVol", "ask", "askVol")
    elif operation == Operation.EX_BARS:
        names = ("datetime", "open", "high", "low", "close", "trade", "position")
    elif operation == Operation.EX_MINUTE:
        names = ("hour", "minute", "price", "volume")
    else:
        names = ("hour", "minute", "price", "volume", "nature", "zengCang")
    return sha(
        canonical([{name: row.native.get(name) for name in names} for row in page.rows])
    )
