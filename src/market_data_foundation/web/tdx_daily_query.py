"""Bounded, ephemeral Type9 reads. No ingestion, journal, raw sink or write port.

Local publication wins except for the current unfinished session. Historical
as_of is strictly local. Cross-section cursors count published-roster candidates,
not invented bars; unavailable candidates and unfinished values stay explicit.
"""

import re
import time
from datetime import UTC, date, datetime, timedelta
from datetime import time as wall_time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.tdx_p0_contract import BINDING, SOURCE
from market_data_foundation.historical.tdx_p0_roster import PATTERNS
from market_data_foundation.historical.tdx_p0_source import TdxError, classify
from market_data_foundation.historical.tdx_roster import facts
from market_data_foundation.integration.tdx_daily_ingest import native_bar
from market_data_foundation.qualification.tdx_market_evidence import inspect_daily

ZONE = ZoneInfo("Asia/Shanghai")
FIELDS = ("open", "high", "low", "close", "volume", "amount")
MAX_REQUESTS = 100
CROSS_PAGE = 100
SOURCE_PAGE = 800


def instant(value):
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def day_key(row):
    value = row["trading_day"]
    return value if isinstance(value, date) else date.fromisoformat(value)


def key(row):
    return day_key(row), row["market"], row["instrument"]


def session(at):
    clock = at.astimezone(ZONE).time().replace(tzinfo=None)
    if clock < wall_time(9, 25):
        return "pre_open"
    if clock < wall_time(9, 30):
        return "opening_auction"
    if clock < wall_time(11, 30):
        return "morning"
    if clock < wall_time(13):
        return "lunch_break"
    if clock < wall_time(15):
        return "afternoon"
    # Some A shares still trade at the closing price until 15:30 Beijing.
    if clock < wall_time(15, 30):
        return "closing_price_session"
    return "after_close"


def validate(start, end, market, instrument, offset, limit, order, as_of, snapshot_id):
    require(
        type(start) is date
        and type(end) is date
        and date(1990, 1, 1) <= start <= end <= date(2100, 12, 31),
        "tdx_query_range",
    )
    require(
        market is None or type(market) is int and market in (0, 1, 2),
        "tdx_query_market",
    )
    require(
        instrument is None
        or market is not None
        and type(instrument) is str
        and re.fullmatch(r"[0-9]{6}", instrument) is not None,
        "tdx_query_instrument",
    )
    require(
        type(offset) is int
        and offset >= 0
        and type(limit) is int
        and 1 <= limit <= 10000,
        "tdx_query_page",
    )
    require(order in {"asc", "desc"}, "tdx_query_order")
    require(as_of is None or as_of.utcoffset() is not None, "tdx_query_cutoff")
    require(
        snapshot_id is None
        or isinstance(snapshot_id, str)
        and 1 <= len(snapshot_id) <= 256,
        "tdx_query_snapshot",
    )


class DailyQuery:
    def __init__(
        self,
        store,
        source_factory,
        *,
        allow_source=False,
        clock=lambda: datetime.now(UTC),
        timer=time.monotonic,
        max_requests=MAX_REQUESTS,
        max_seconds=20,
    ):
        self.store, self.source_factory = store, source_factory
        self.allow_source, self.clock, self.timer = allow_source, clock, timer
        self.max_requests, self.max_seconds = max_requests, max_seconds

    def query(
        self,
        *,
        start,
        end,
        market=None,
        instrument=None,
        as_of=None,
        offset=0,
        limit=100,
        order="asc",
        local_only=False,
        snapshot_id=None,
    ):
        validate(
            start, end, market, instrument, offset, limit, order, as_of, snapshot_id
        )
        at = self.clock()
        cutoff = as_of or at
        pinned_snapshot = None
        if snapshot_id is not None:
            pinned_snapshot = self.store.universe(
                snapshot_id=snapshot_id,
                as_of=cutoff,
                day=start if start == end else None,
            )
            require(pinned_snapshot is not None, "tdx_query_snapshot_unavailable")
        local_policy = local_only or as_of is not None or not self.allow_source
        options = dict(
            start=start,
            end=end,
            market=market,
            instrument=instrument,
            as_of=cutoff,
            order=order,
        )
        if snapshot_id is not None:
            options["snapshot_id"] = snapshot_id
        if local_policy or start != end and instrument is None:
            value = self.store.query(**options, offset=offset, limit=limit)
            value["rows"] = [self.present(r, cutoff) for r in value["rows"]]
            reason = (
                "tdx_source_query_not_enabled"
                if not self.allow_source and not local_only and as_of is None
                else "tdx_query_history_instrument_required"
                if not local_policy
                else None
            )
            value.update(
                query_mode="history"
                if as_of
                else "local"
                if local_only
                else "local_first",
                local_only=True,
                queried_at=self.clock(),
                pagination_basis="rows",
                upstream=dict(
                    provider_requests=0,
                    attempted_operations=0,
                    issues=[dict(classification=reason)] if reason else [],
                ),
            )
            return value

        # Only one stock's history or one day, never a whole-market history scan.
        first = self.store.query(**options, offset=0, limit=10000)
        local = list(first["rows"])
        cursor = len(local)
        while first["total"] > cursor:
            chunk = self.store.query(**options, offset=cursor, limit=10000)["rows"]
            if not chunk:
                raise TdxError("tdx_query_local_page_no_progress")
            local.extend(chunk)
            cursor += len(chunk)
        snapshot = pinned_snapshot or self.store.universe(
            as_of=cutoff,
            day=start if start == end else None,
            **({"snapshot_id": snapshot_id} if snapshot_id else {}),
        )
        if snapshot_id is not None:
            require(snapshot is not None, "tdx_query_snapshot_unavailable")
        members = {
            (m["market"], m["instrument"]): m
            for m in (snapshot or {}).get("members", [])
        }
        calendar = {}
        for c in (snapshot or {}).get("details", {}).get("calendar", []):
            if instant(c["available_at"]) <= cutoff:
                calendar[(c["market"], date.fromisoformat(c["day"]))] = c["is_open"]
        unified_calendar = (
            self.store.trading_calendar(as_of=cutoff, start=start, end=end)
            if hasattr(self.store, "trading_calendar")
            else {}
        )

        def calendar_open(market, day):
            return unified_calendar.get(day, calendar.get((market, day)))

        today = at.astimezone(ZONE).date()
        phase = session(at)
        reported_phase = (
            "historical" if end < today else "future_day" if start > today else phase
        )
        scope_markets = (market,) if market is not None else (0, 1, 2)
        if start == end and all(
            calendar_open(m, start) is False
            or calendar_open(m, start) is None
            and start.weekday() >= 5
            for m in scope_markets
        ):
            reported_phase = "market_closed"
        merged = {key(r): self.present(r, at) for r in local}
        issues, missing = [], []
        counts = dict(provider_requests=0, attempted_operations=0)
        deadline = self.timer() + self.max_seconds
        source = None
        blocked = False

        def issue(classification, **scope):
            if len(issues) < 100:
                issues.append(dict(classification=classification, **scope))

        def eligibility(m, code, day):
            member = members.get((m, code))
            evidence = (member or {}).get("evidence", {})
            listed, delisted = evidence.get("listing_date"), evidence.get("delist_date")
            if (listed and day < date.fromisoformat(listed)) or (
                delisted and day >= date.fromisoformat(delisted)
            ):
                return "outside_listing_interval"
            opened = calendar_open(m, day)
            if opened is False or opened is None and day.weekday() >= 5:
                return (
                    "market_closed"
                    if opened is False
                    else "weekend_calendar_unconfirmed"
                )
            if day > today:
                return "future_day"
            if day == today and phase == "pre_open":
                return "pre_open"
            for s in (snapshot or {}).get("details", {}).get("suspensions", []):
                if (
                    (s["market"], s["instrument"]) == (m, code)
                    and s["start"] <= day.isoformat() <= s["end"]
                    and instant(s["available_at"]) <= cutoff
                ):
                    return "confirmed_suspended"
            return None

        def needs(m, code, day):
            current = merged.get((day, m, code))
            return current is None or not current["is_complete"]

        def unfilled(k):
            row = merged.get(k)
            if row is None:
                return "source_not_returned"
            if row["origin"] == "local" and not row["is_complete"]:
                row["current_observation"] = "unverified"
                issue(
                    "tdx_query_current_observation_unavailable",
                    market=k[1],
                    instrument=k[2],
                    day=k[0].isoformat(),
                )
                return "tdx_query_current_observation_unavailable"
            return None

        def budget():
            return (
                counts["attempted_operations"] < self.max_requests
                and self.timer() < deadline
            )

        def fetch(m, code, earliest, latest):
            nonlocal source, blocked
            page_offset, previous_oldest = 0, None
            while budget() and not blocked:
                before = source.requests if source else 0
                try:
                    if source is None:
                        source = self.source_factory()
                        before = source.requests
                    count = 1 if earliest == latest == today else SOURCE_PAGE
                    counts["attempted_operations"] += 1
                    response = source.request(
                        "GetKline", m, code, start=page_offset, count=count
                    )
                    native = response["row"]
                    projection = native["source_projection"]
                    require(
                        not inspect_daily(projection, SimpleNamespace(count=count))[
                            "issues"
                        ],
                        "tdx_page_quality",
                    )
                    rows = projection["List"] or []
                    if not rows:
                        return
                    observed = instant(native["received_at"])
                    require(
                        observed.utcoffset() is not None, "tdx_query_observation_clock"
                    )
                    bars = [native_bar(m, code, r) for r in rows]
                    oldest = min(b.trading_day for b in bars)
                    if previous_oldest is not None and oldest >= previous_oldest:
                        raise TdxError("tdx_query_page_no_progress")
                    for b in bars:
                        if not earliest <= b.trading_day <= latest or eligibility(
                            m, code, b.trading_day
                        ):
                            continue
                        k = (b.trading_day, m, code)
                        if k in merged and merged[k]["is_complete"]:
                            continue
                        r = dict(zip(FIELDS, b.values))
                        r.update(
                            market=m,
                            instrument=code,
                            trading_day=b.trading_day,
                            source=SOURCE.identity,
                            source_binding=f"{BINDING.identity}@{BINDING.revision}",
                            batch_id=None,
                            available_at=None,
                            received_at=observed,
                            observed_at=observed,
                            upstream_request_id=native["request"]["id"],
                            source_event_time=None,
                            origin="upstream",
                            persisted=False,
                            price_basis="native_projection",
                            return_qualification="NOT_QUALIFIED",
                        )
                        merged[k] = self.present(r, at, temporary=True)
                    if oldest <= earliest or len(rows) < count:
                        return
                    previous_oldest = oldest
                    page_offset += max(1, len(rows) - 1)
                    require(page_offset <= 65535, "tdx_query_page_offset_exhausted")
                except Exception as error:
                    issue(classify(error), market=m, instrument=code)
                    blocked = True
                    return
                finally:
                    if source:
                        counts["provider_requests"] += source.requests - before
            issue("tdx_query_request_budget_exceeded", market=m, instrument=code)

        try:
            if instrument is not None:
                require(
                    re.fullmatch(PATTERNS[market], instrument) is not None,
                    "tdx_query_stock_scope",
                )
                gaps = []
                held_days = [day_key(row) for row in local]
                first_held, last_held = (
                    min(held_days, default=end),
                    max(held_days, default=start),
                )
                d = start
                while d <= min(end, today):
                    # Weekday holidays in years without an exchange calendar
                    # are not evidence of a hole inside the published range.
                    known_gap_or_edge = (
                        not first_held < d < last_held
                        or calendar.get((market, d)) is True
                    )
                    if (
                        known_gap_or_edge
                        and needs(market, instrument, d)
                        and not eligibility(market, instrument, d)
                    ):
                        gaps.append(d)
                    d += timedelta(days=1)
                if gaps:
                    fetch(market, instrument, min(gaps), min(end, today))
                missing = []
                for d in gaps:
                    classification = unfilled((d, market, instrument))
                    if classification:
                        missing.append(
                            dict(
                                market=market,
                                instrument=instrument,
                                day=d.isoformat(),
                                classification=classification,
                            )
                        )
                all_rows = sorted(merged.values(), key=key, reverse=order == "desc")
                selected = all_rows[offset : offset + limit]
                total, end_offset = len(all_rows), offset + len(selected)
                pagination = "rows"
                page_candidates = len(selected)
            else:
                # The published roster is the bounded denominator, never an on-GET
                # directory refresh. Preserve local keys even for an old/missing roster.
                candidates = {k[1:] for k in merged}
                candidates.update(
                    (m, code)
                    for m, code in members
                    if (market is None or m == market)
                    and not eligibility(m, code, start)
                )
                ordered = sorted(candidates)
                total, consumed = len(ordered), 0
                selected_keys = []
                for m, code in ordered[offset : offset + min(limit, CROSS_PAGE)]:
                    if not eligibility(m, code, start) and needs(m, code, start):
                        if blocked or not budget():
                            issue(
                                "tdx_query_source_blocked"
                                if blocked
                                else "tdx_query_request_budget_exceeded"
                            )
                            break
                        fetch(m, code, start, end)
                    consumed += 1
                    selected_keys.append((start, m, code))
                selected = [merged[k] for k in selected_keys if k in merged]
                missing = []
                for d, m, code in selected_keys:
                    classification = unfilled((d, m, code))
                    if classification:
                        missing.append(
                            dict(
                                market=m,
                                instrument=code,
                                day=d.isoformat(),
                                classification=eligibility(m, code, d)
                                or classification,
                            )
                        )
                # A failing first candidate must not become an endless same-offset loop.
                end_offset = offset + consumed
                pagination, page_candidates = "roster_candidates", consumed
                if snapshot is None:
                    issue("tdx_query_roster_required")
            for row in selected:
                day, m, code = key(row)
                if (
                    row["persisted"]
                    and day == today
                    and phase not in {"pre_open", "after_close"}
                    and not eligibility(m, code, day)
                ):
                    issue(
                        "tdx_query_current_observation_unavailable",
                        market=m,
                        instrument=code,
                    )
                    missing.append(
                        dict(
                            market=m,
                            instrument=code,
                            day=day.isoformat(),
                            classification="current_observation_unavailable",
                        )
                    )
            first.update(
                rows=selected,
                total=total,
                offset=offset,
                limit=min(limit, CROSS_PAGE)
                if pagination == "roster_candidates"
                else limit,
                next_offset=end_offset
                if end_offset < total and end_offset > offset
                else None,
                page_end_offset=end_offset,
                pagination_basis=pagination,
                provider_requests=counts["provider_requests"],
                local_only=False,
                query_mode="local_first",
                queried_at=self.clock(),
                upstream=dict(**counts, issues=issues),
                query_coverage=dict(
                    scope="requested_page"
                    if instrument is None
                    else "requested_history",
                    candidate_total=total if instrument is None else None,
                    page_candidates=page_candidates,
                    returned_rows=len(selected),
                    local_rows=sum(r["persisted"] for r in selected),
                    temporary_rows=sum(not r["persisted"] for r in selected),
                    unfinished_rows=sum(not r["is_complete"] for r in selected),
                    missing_count=len(missing),
                    missing=missing[:100],
                    page_has_all_values=not missing,
                    roster=facts(snapshot, end),
                    session=reported_phase,
                    local_coverage_unchanged=True,
                ),
            )
            return first
        finally:
            if source:
                try:
                    source.close()
                except Exception:
                    issue("tdx_query_source_close_failed")

    @staticmethod
    def present(row, at, temporary=False):
        value = dict(row)
        unfinished = (
            day_key(row) == at.astimezone(ZONE).date() and session(at) != "after_close"
        )
        observed = instant(row.get("observed_at", row.get("received_at")))
        if observed is not None and observed.utcoffset() is not None:
            observed_local = observed.astimezone(ZONE)
            unfinished = unfinished or (
                observed_local.date() == day_key(row) == at.astimezone(ZONE).date()
                and session(observed) != "after_close"
            )
        value.update(
            origin="upstream" if temporary else "local",
            persisted=not temporary,
            observed_at=row.get("observed_at", row.get("received_at")),
            is_complete=not unfinished,
            is_final=False,
            bar_state="intraday_incomplete"
            if unfinished
            else "after_close_unpersisted"
            if temporary
            else "local_published",
        )
        return value
