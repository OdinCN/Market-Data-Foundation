"""Bounded, ephemeral local-first reads; no store mutation or raw retention port."""

import calendar
import time
from collections import OrderedDict
from datetime import timedelta
from decimal import Decimal
from threading import Lock
from zoneinfo import ZoneInfo

from market_data_foundation.historical.financial_daily import (
    BY_ID,
    FinancialError,
    day,
    non_observation_day,
    observation_today,
)
from market_data_foundation.historical.financial_daily_credit import (
    ARCHIVE_END,
    ARCHIVE_START,
    CREDIT,
    CreditArchiveSource,
    credit_binding,
)
from market_data_foundation.historical.financial_daily_origin import (
    OriginSource,
    binding,
)
from market_data_foundation.historical.financial_daily_runtime import (
    DEFAULT_CONTROL,
    bound_fred_credential,
)
from market_data_foundation.historical.financial_daily_service import credit_bp
from market_data_foundation.historical.financial_daily_source import DailyFredSource

CONTROL_DIRECTORY = (
    DEFAULT_CONTROL.parents[4]
    / "MDF-MACRO-RELIABLE-UPDATE-20261104-R1/runtime/financial-daily-origin-control"
)
# Confirmed bounds of these particular bindings, not FRED alias history.
STARTS = {
    "DFEDTARL": "2008-12-16",
    "DFEDTARU": "2008-12-16",
    "SOFR": "2018-04-02",
    "DGS2": "1990-01-01",
    "DGS10": "1990-01-01",
    "DFII10": "2003-01-01",
    "T10YIE": "2003-01-01",
    **dict.fromkeys(CREDIT, ARCHIVE_START),
}
GAPS = {
    "not_known_at_cutoff",
    "not_collected",
    "request_failed",
    "request_outcome_unknown",
    "derived_inputs_unavailable",
    "not_yet_available_from_provider",
    "not_returned_calendar_unconfirmed",
}


def live_query_source(kind):
    """Same approved transports/controls as jobs, deliberately no raw_sink/store."""
    if kind == "origin":
        return OriginSource.live(control_directory=CONTROL_DIRECTORY, live_opt_in=True)
    if kind == "archive":
        # Share the existing job/archive admission file with manual acquisition.
        return CreditArchiveSource.live(
            control_path=CONTROL_DIRECTORY.parent
            / "financial-daily-jobs/archive-control.json",
            live_opt_in=True,
        )
    if kind != "fred":
        raise FinancialError("financial_query_source_binding_invalid")
    return DailyFredSource.live(
        series=CREDIT,
        credential_loader=bound_fred_credential,
        request_control_path=DEFAULT_CONTROL,
        live_opt_in=True,
    )


def availability(sid, at, now):
    if at < STARTS.get(sid, "0001-01-01"):
        return "outside_provider_range"
    if non_observation_day(BY_ID[sid], at):
        return "non_observation_day"
    local = now.astimezone(ZoneInfo("America/New_York"))
    latest = observation_today(now)
    if sid in {"EFFR", "SOFR"}:
        # Observation date is published the next working day; do not fetch it early.
        if local.weekday() >= 5 or local.hour < (9 if sid == "EFFR" else 8):
            latest -= timedelta(days=1)
            while latest.weekday() >= 5:
                latest -= timedelta(days=1)
        latest -= timedelta(days=1)
        while latest.weekday() >= 5:
            latest -= timedelta(days=1)
    elif not sid.startswith("DFED") and (local.hour, local.minute) < (15, 30):
        latest -= timedelta(days=1)
    if day(at) > latest:
        return "not_yet_available_from_provider"
    return None


def window(sid, at, now):
    if sid in CREDIT and at <= ARCHIVE_END:
        return "archive", sid, ARCHIVE_START, ARCHIVE_END
    a = at[:7] + "-01"
    b = at[:7] + f"-{calendar.monthrange(day(at).year, day(at).month)[1]:02}"
    a = max(a, STARTS.get(sid, a))
    b = min(b, observation_today(now).isoformat())
    if sid in CREDIT:
        a = max(a, (day(ARCHIVE_END) + timedelta(days=1)).isoformat())
    return "fred" if sid in CREDIT else "origin", sid, a, b


class FinancialQuery:
    """Process-local bounded LRU, five-minute success/negative cache, serial admission.

    Cache source windows, not a whole multi-decade query or a mutable query session.
    Each page re-reads the store at its cutoff and local values always win. Pagination
    and refresh reuse source windows; expiry/process restart never implies persistence.
    """

    def __init__(self, source_factory=live_query_source, *, timer=time.monotonic):
        self.source_factory, self.timer = source_factory, timer
        self.cache, self.lock = OrderedDict(), Lock()

    def supplement(self, service, rows, cutoff):
        with self.lock:
            return self._supplement(service, rows, cutoff)

    def _supplement(self, service, rows, cutoff):
        sources, issues = {}, []
        counts = dict(provider_requests=0, cache_hits=0)
        local = {(r["series_id"], r["observation_date"]): r for r in rows}
        fetched = {}

        def fetch(sid, at):
            key = window(sid, at, cutoff)
            if key in fetched:
                return fetched[key]
            cached = self.cache.get(key)
            if cached and cached[0] > self.timer():
                self.cache.move_to_end(key)
                counts["cache_hits"] += 1
                fetched[key] = cached[1:]
                return fetched[key]
            kind, _, a, b = key
            source, before, batch, classification = None, 0, None, None
            retry_at = None
            try:
                if counts["provider_requests"] >= 20:
                    raise FinancialError("financial_query_request_budget_exceeded")
                if kind not in sources:
                    sources[kind] = self.source_factory(kind)
                source = sources[kind]
                before = source.request_count
                source.max_requests = before + (20 - counts["provider_requests"])
                batch = source.fetch(sid, a, b)
            except Exception as error:
                classification = getattr(
                    error, "classification", "financial_query_source_unavailable"
                )
                retry_at = getattr(error, "retry_at", None)
            finally:
                if source:
                    counts["provider_requests"] += source.request_count - before
            # No store.save/record_attempt/commit, no sink, no journal dispatch.
            ttl = 300 if kind != "archive" or classification else 24 * 3600
            if retry_at:
                ttl = max(ttl, (retry_at - cutoff).total_seconds())
            result = (batch, classification)
            self.cache[key] = (self.timer() + ttl, *result)
            self.cache.move_to_end(key)
            while len(self.cache) > 64:
                self.cache.popitem(last=False)
            fetched[key] = result
            return result

        def missing(row):
            return row["value"] is None and row["state"] in GAPS

        def enrich(row):
            if not missing(row):
                return row
            sid, at = row["series_id"], row["observation_date"]
            state = availability(sid, at, cutoff)
            if state:
                return dict(row, state=state)
            if sid == "T10YIE":
                return derive_row(row)
            batch, error = fetch(sid, at)
            if error:
                issues.append(
                    dict(series_id=sid, start=at, end=at, classification=error)
                )
                return dict(row, query_classification=error)
            point = next((p for p in batch.points if p.observation_date == at), None)
            if not point:
                state = (
                    "outside_provider_range"
                    if batch.metadata.get("observation_start", at) > at
                    else "not_yet_available_from_provider"
                    if batch.metadata.get("observation_end", at) < at
                    else "provider_field_absent"
                    if at in batch.metadata.get("native_field_absent_dates", [])
                    else "not_returned_calendar_unconfirmed"
                )
                return dict(
                    row,
                    state=state,
                    query_classification="financial_query_no_observation",
                )
            spec = (
                credit_binding(sid, origin=batch.origin)
                if sid in CREDIT
                else binding(sid)
            )
            return dict(
                row,
                value=str(point.value) if point.value is not None else None,
                state="observed" if point.value is not None else "provider_missing",
                basis_points=credit_bp(point.value)
                if sid in CREDIT and point.value is not None
                else None,
                origin="upstream",
                persisted=False,
                identity=spec["identity"],
                original_agency=spec["original_agency"],
                distributor=spec["distributor"],
                source_url=spec["source_url"],
                attribution=spec["attribution"],
                source_binding=spec,
                source_request=batch.request,
                is_copyrighted=batch.metadata.get("is_copyrighted"),
                provider_extra=dict(point.provider_extra, source_origin=batch.origin),
                policy_effective_date=point.provider_extra.get("policy_effective_date"),
                revision=None,
                batch_id=None,
                available_at=None,
                observed_at=None,
                raw_locator=None,
                metadata_locator=None,
                coverage_evidence=None,
            )

        def leg(sid, at):
            key = sid, at
            if key not in local:
                local[key] = service.query(at, at, selected=(sid,), as_of=cutoff)[
                    "rows"
                ][0]
            local[key] = enrich(local[key])
            return local[key]

        def derive_row(row):
            at = row["observation_date"]
            legs = [leg(sid, at) for sid in ("DGS10", "DFII10")]
            if not all(
                r["state"] == "observed"
                and r["value"] is not None
                and (
                    r["origin"] == "us_treasury"
                    or (
                        r["origin"] == "upstream"
                        and r["source_binding"].get("origin") == "us_treasury"
                    )
                )
                for r in legs
            ):
                issues.append(
                    dict(
                        series_id="T10YIE",
                        start=at,
                        end=at,
                        classification="financial_derived_inputs_unavailable",
                    )
                )
                return dict(
                    row,
                    state="derived_inputs_unavailable",
                    query_classification="financial_derived_inputs_unavailable",
                )
            spec = binding("T10YIE")
            return dict(
                row,
                value=str(Decimal(legs[0]["value"]) - Decimal(legs[1]["value"])),
                state="observed",
                origin="upstream",
                persisted=False,
                source_binding=spec,
                identity=spec["identity"],
                provider_extra=dict(
                    formula="DGS10 - DFII10",
                    inputs=legs,
                    cutoff=cutoff.isoformat(),
                    source_origin="mdf_derived",
                ),
                revision=None,
                batch_id=None,
                available_at=None,
                observed_at=None,
                raw_locator=None,
                metadata_locator=None,
                coverage_evidence=None,
            )

        result = []
        for row in rows:
            key = row["series_id"], row["observation_date"]
            local[key] = enrich(local[key])
            result.append(local[key])
        return result, dict(**counts, issues=issues)
