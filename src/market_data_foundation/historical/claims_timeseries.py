"""Pure weekly projection and read-only, local-first queries at one cutoff.

Persistence/DDL and event-material ingestion are separate later capabilities.
All comparison operands are selected before arithmetic at the same cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from market_data_foundation.historical.claims_contract import (
    BY_ID,
    ORIGINAL_AGENCY,
    PROVIDER,
    SERIES_IDS,
    ClaimsError,
    buffer_start,
    day,
    instant,
    validate_series,
    weeks,
)


@dataclass(frozen=True)
class ClaimsObservation:
    series_id: str
    week_ending: str
    value: Decimal | None
    observed_at: datetime
    available_at: datetime | None
    revision: int | None
    provider_extra: dict = field(default_factory=dict)
    origin: str = "local"
    batch_id: str | None = None

    def __post_init__(self):
        validate_series((self.series_id,))
        if day(self.week_ending).weekday() != 5:
            raise ClaimsError("claims_week_ending_invalid")
        instant(self.observed_at)
        if self.origin not in {"local", "upstream"}:
            raise ClaimsError("claims_origin_invalid")
        if self.origin == "local":
            instant(self.available_at)
            if self.available_at < self.observed_at:
                raise ClaimsError("claims_availability_inverted")
            if type(self.revision) is not int or self.revision < 1:
                raise ClaimsError("claims_revision_invalid")
        elif self.available_at is not None or self.revision is not None:
            raise ClaimsError("claims_upstream_not_persisted")
        if self.value is not None and (
            type(self.value) is not Decimal
            or not self.value.is_finite()
            or self.value < 0
        ):
            raise ClaimsError("claims_value_invalid")


def _cutoff(value):
    try:
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return instant(value) if value is not None else None
    except ValueError, TypeError:
        raise ClaimsError("claims_cutoff_invalid") from None


def _select(observations, series_ids, start, end, cutoff, basis):
    indexed = {}
    for row in observations:
        if not isinstance(row, ClaimsObservation):
            raise ClaimsError("claims_observation_invalid")
        if row.series_id not in series_ids or not start <= row.week_ending <= end:
            continue
        at = row.available_at if basis == "available" else row.observed_at
        if cutoff is not None and (at is None or at > cutoff):
            continue
        if cutoff is not None and row.origin == "upstream":
            continue
        key = row.series_id, row.week_ending
        previous = indexed.get(key)
        if previous is None:
            indexed[key] = row
            continue
        # Economic revision order, with deterministic replay conflict detection.
        if previous.revision == row.revision:
            if previous != row:
                raise ClaimsError("claims_revision_conflict")
        elif (row.revision or 0) > (previous.revision or 0):
            indexed[key] = row
    return indexed


def _ref(row):
    return {
        "series_id": row.series_id,
        "week_ending": row.week_ending,
        "revision": row.revision,
        "batch_id": row.batch_id,
        "origin": row.origin,
        "value": str(row.value) if row.value is not None else None,
        "observed_at": row.observed_at.isoformat(),
        "available_at": row.available_at.isoformat() if row.available_at else None,
    }


def _missing(row):
    if row is None:
        return "local_not_provided"
    return "official_null" if row.value is None else None


def _derive(inputs, operation):
    missing = next((_missing(row) for row in inputs if _missing(row)), None)
    refs = [_ref(row) for row in inputs if row is not None]
    if missing:
        return {"value": None, "missing_reason": missing, "inputs": refs}
    with localcontext() as context:
        context.prec = 28
        context.rounding = ROUND_HALF_EVEN
        values = [row.value for row in inputs]
        if operation == "difference":
            value = values[0] - values[1]
        elif operation == "ratio":
            if values[1] == 0:
                return {
                    "value": None,
                    "missing_reason": "zero_denominator",
                    "inputs": refs,
                }
            value = values[0] / values[1] - 1
        else:
            value = sum(values, Decimal(0)) / 4
    return {"value": str(value), "missing_reason": None, "inputs": refs}


def project_range(
    observations,
    start,
    end,
    *,
    series_ids=SERIES_IDS,
    as_of=None,
    as_of_basis="available",
):
    """MDF observed/available history, never inferred official vintage."""
    validate_series(series_ids)
    targets = weeks(start, end)
    cutoff = _cutoff(as_of)
    if as_of_basis not in {"available", "observed"}:
        raise ClaimsError("claims_cutoff_basis_unsupported")
    observations = tuple(observations)
    indexed = _select(
        observations, series_ids, buffer_start(start), end, cutoff, as_of_basis
    )
    first_captures = {}
    for row in observations:
        at = row.available_at if as_of_basis == "available" else row.observed_at
        if row.origin != "local" or (cutoff is not None and at > cutoff):
            continue
        key = row.series_id, row.week_ending
        first = first_captures.get(key)
        if first is None or (row.observed_at, row.revision) < (
            first.observed_at,
            first.revision,
        ):
            first_captures[key] = row
    result = []
    for sid in series_ids:
        metric = BY_ID[sid]
        for week in reversed(targets):
            row = indexed.get((sid, week))
            previous = (day(week) - timedelta(weeks=1)).isoformat()
            pair = [row, indexed.get((sid, previous))]
            change = _derive(pair, "difference")
            change.update(unit=metric.unit, method="value(t)-value(t-7d)")
            ratio = _derive(pair, "ratio")
            ratio.update(unit="ratio", method="value(t)/value(t-7d)-1")
            first = first_captures.get((sid, week))
            item = {
                "series_id": sid,
                "indicator": metric.indicator,
                "week_ending": week,
                "frequency": metric.frequency,
                "geography": metric.geography,
                "program": metric.program,
                "seasonal_adjustment": metric.seasonal_adjustment,
                "unit": metric.unit,
                "statistic": metric.statistic,
                "value_origin": "official_series_via_fred",
                "value": str(row.value) if row and row.value is not None else None,
                "missing_reason": _missing(row),
                "original_agency": ORIGINAL_AGENCY,
                "provider": PROVIDER,
                "provenance": _ref(row) if row else None,
                "provider_extra": dict(row.provider_extra) if row else {},
                "first_captured_at": first.observed_at.isoformat() if first else None,
                "first_captured_value": (
                    str(first.value) if first and first.value is not None else None
                ),
                "first_capture_provenance": _ref(first) if first else None,
                "official_first_release": None,
                "event_binding_status": "unconfirmed",
                "consensus": None,
                "surprise": None,
                "surprise_missing_reason": "event_and_named_consensus_unbound",
                "weekly_change": change,
                "weekly_change_ratio": ratio,
            }
            if metric.statistic == "weekly_level":
                window = [
                    indexed.get((sid, (day(week) - timedelta(weeks=i)).isoformat()))
                    for i in range(4)
                ]
                average = _derive(window, "average")
                average.update(
                    unit=metric.unit,
                    origin="local_derived",
                    method="mean(value(t),value(t-7d),value(t-14d),value(t-21d))",
                )
                item["local_four_week_average"] = average
            result.append(item)
    available = sum(row["value"] is not None for row in result)
    expected = len(series_ids) * len(targets)
    return {
        "asset_id": "mdf.macro.dol.claims",
        "start_date": start,
        "end_date": end,
        "buffer_start_date": buffer_start(start),
        "as_of": cutoff.isoformat() if cutoff else None,
        "as_of_basis": as_of_basis,
        "status": "complete"
        if available == expected
        else ("partial" if available else "unavailable"),
        "coverage": {
            "expected": expected,
            "available": available,
            "missing_rows": sum(
                r["missing_reason"] == "local_not_provided" for r in result
            ),
            "official_nulls": sum(
                r["missing_reason"] == "official_null" for r in result
            ),
        },
        "decimal_context": "precision_28_half_even_no_display_rounding",
        "rows": result,
    }


class ClaimsTimeSeriesService:
    """Injected read store; query performs no writes, migration or event lookup.

    Store.read_range returns ClaimsObservation revisions in a short read snapshot.
    Segment 2 supplies durable storage. Upstream supplementation is current-only,
    response-only, and can fill absent keys; local nulls/values remain authoritative.
    """

    def __init__(self, store, source=None, *, clock=lambda: datetime.now(UTC)):
        self.store, self.source, self.clock = store, source, clock

    def query_range(
        self,
        start,
        end,
        *,
        series_ids=SERIES_IDS,
        as_of=None,
        as_of_basis="available",
        allow_upstream=False,
    ):
        validate_series(series_ids)
        targets = weeks(buffer_start(start), end)
        cutoff = _cutoff(as_of)
        if type(allow_upstream) is not bool:
            raise ClaimsError("claims_upstream_flag_invalid")
        if as_of_basis not in {"available", "observed"}:
            raise ClaimsError("claims_cutoff_basis_unsupported")
        with self.store.read_snapshot():
            local = tuple(
                self.store.read_range(
                    series_ids, buffer_start(start), end, cutoff, as_of_basis
                )
            )
        observations = list(local)
        indexed = _select(
            local, series_ids, buffer_start(start), end, cutoff, as_of_basis
        )
        requests = attempts = 0
        failure = None
        if allow_upstream and cutoff is None:
            # An unfinished current week or a future week never triggers fetching.
            today = instant(self.clock()).date().isoformat()
            completed_target = any(week < today for week in weeks(start, end))
            missing = (
                {
                    (sid, week)
                    for sid in series_ids
                    for week in targets
                    if week < today and (sid, week) not in indexed
                }
                if completed_target
                else set()
            )
            if missing and self.source is None:
                failure = "claims_fred_capability_unbound"
            elif missing:
                selected = tuple(
                    s for s in series_ids if any(k[0] == s for k in missing)
                )
                before_requests = self.source.request_count
                before_attempts = self.source.attempts
                try:
                    batches = self.source.fetch(
                        selected, min(w for _, w in missing), max(w for _, w in missing)
                    )
                    supplements = []
                    for batch in batches:
                        for point in batch.points:
                            if (point.series_id, point.week_ending) in missing:
                                supplements.append(
                                    ClaimsObservation(
                                        point.series_id,
                                        point.week_ending,
                                        point.value,
                                        batch.observed_at,
                                        None,
                                        None,
                                        point.provider_extra,
                                        "upstream",
                                    )
                                )
                    observations.extend(supplements)
                except ClaimsError as error:
                    failure = error.classification
                requests = self.source.request_count - before_requests
                attempts = self.source.attempts - before_attempts
        result = project_range(
            observations,
            start,
            end,
            series_ids=series_ids,
            as_of=cutoff,
            as_of_basis=as_of_basis,
        )
        result.update(
            query_mode="history" if cutoff else "current",
            local_only=not (allow_upstream and cutoff is None),
            provider_requests=requests,
            provider_attempts=attempts,
            source_failure=failure,
        )
        if failure:
            result["status"] = (
                "partial" if result["coverage"]["available"] else "unavailable"
            )
        return result
