"""Local-first continuous CPI ranges with explicit initialization and updates."""

from __future__ import annotations

import re
import tomllib
from dataclasses import replace
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    GarageCredentials,
)
from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE,
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    MAX_RANGE_REQUESTS,
    BlsRangeSource,
    CpiSourceError,
    year_windows,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    SERIES_IDS as SERIES_IDS,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    GarageCpiRawStore,
    PostgresTimeSeriesStore,
    _point_document,
    _restoration_expectations,
    _validate_batches,
    validate_evidence_url,
)
from market_data_foundation.storage.market_dataset_postgres import connect_dev
from market_data_foundation.storage.postgres_runtime_config import DEFAULT_RUNTIME_PATH

DEFAULT_SECRET_PATH = Path("/srv/market-data-foundation/config/local/secret.toml")
DEFAULT_REQUEST_CONTROL_PATH = Path(
    "/srv/market-data-foundation/artifacts/cpi-timeseries/bls-request-control.json"
)
CALCULATION_METHOD = (
    "change_km_ratio=index[t]/index[t-k]-1 for k=1,3,6,12; cumulative, "
    "not annualized; Decimal ROUND_HALF_UP to 4 ratio places. "
    "Same BLS series index ratio: mom_pct=(index[t]/index[t-1]-1)*100; "
    "yoy_pct=(index[t]/index[t-12]-1)*100; Decimal ROUND_HALF_UP to 2 places. "
    "Provider calculations are retained separately and may use different rounding."
)
PCE_CALCULATION_METHOD = (
    "change_km_ratio=index[t]/index[t-k]-1 for k=1,3,6,12; "
    "same-cutoff BEA T20804 monthly SA index endpoints; cumulative, "
    "not annualized; Decimal ROUND_HALF_UP to 4 ratio places. "
    "0.0312 means 3.12%; mom_pct/yoy_pct are derived percent changes. "
    "annualized_3m_ratio=(index[t]/index[t-3])^4-1; "
    "annualized_6m_ratio=(index[t]/index[t-6])^2-1; same-cutoff unrounded indexes, "
    "one final ROUND_HALF_UP to 4 ratio places; distinct from cumulative fields."
)
SERIES_METADATA = CPI_PROFILE.series_metadata
_DEFAULT_SERIES = object()


class CpiTimeSeriesError(RuntimeError):
    def __init__(
        self,
        classification,
        provider_attempts=0,
        *,
        http_status=None,
        retry_at=None,
        provider_requests=0,
    ):
        self.classification = classification
        self.provider_attempts = provider_attempts
        self.attempts = provider_attempts
        self.http_status, self.retry_at = http_status, retry_at
        self.provider_requests = provider_requests
        super().__init__(classification)


def month_number(value):
    if (
        not isinstance(value, str)
        or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) is None
    ):
        raise CpiTimeSeriesError("cpi_timeseries_month_invalid")
    year, month = map(int, value.split("-"))
    if year < 1900 or year > 9998:
        raise CpiTimeSeriesError("cpi_timeseries_month_invalid")
    return year * 12 + month - 1


def month_at(number):
    year, month = divmod(number, 12)
    return f"{year:04d}-{month + 1:02d}"


def month_range(start, end):
    return tuple(month_at(n) for n in range(month_number(start), month_number(end) + 1))


def _parameters(start, end, series_ids, as_of, *, profile=CPI_PROFILE):
    profile = validate_profile(profile)
    if series_ids is _DEFAULT_SERIES:
        series_ids = profile.series_ids
    begin, finish = month_number(start), month_number(end)
    if begin > finish or finish - begin >= 2400 or begin < 1901 * 12:
        raise CpiTimeSeriesError("cpi_timeseries_range_invalid")
    if (
        not isinstance(series_ids, (tuple, list))
        or not series_ids
        or any(
            not isinstance(s, str) or s not in profile.series_ids for s in series_ids
        )
        or len(set(series_ids)) != len(series_ids)
    ):
        raise CpiTimeSeriesError("cpi_timeseries_series_invalid")
    if as_of is not None:
        try:
            if isinstance(as_of, str):
                as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if not isinstance(as_of, datetime) or as_of.utcoffset() is None:
                raise ValueError
            as_of = as_of.astimezone(UTC)
        except TypeError, ValueError, OverflowError:
            raise CpiTimeSeriesError("cpi_timeseries_as_of_invalid") from None
    return tuple(series_ids), month_at(begin - 12), as_of


def _rate(current, previous):
    numerator, denominator = Decimal(current), Decimal(previous)
    with localcontext() as context:
        context.prec = max(
            50,
            len(numerator.as_tuple().digits)
            + len(denominator.as_tuple().digits)
            + abs(numerator.adjusted() - denominator.adjusted())
            + 20,
        )
        return str(
            ((numerator / denominator - 1) * 100).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        )


def _ratio(current, previous):
    numerator, denominator = Decimal(current), Decimal(previous)
    # CPI values are finite positive decimals; retain enough working precision
    # even for unusually long source values before the single final rounding.
    with localcontext() as context:
        context.prec = max(
            50,
            len(numerator.as_tuple().digits)
            + len(denominator.as_tuple().digits)
            + abs(numerator.adjusted() - denominator.adjusted())
            + 20,
        )
        return str(
            (numerator / denominator - 1).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )


def _annualized(current, previous, exponent):
    numerator, denominator = Decimal(current), Decimal(previous)
    with localcontext() as context:
        context.prec = max(
            50,
            len(numerator.as_tuple().digits)
            + len(denominator.as_tuple().digits)
            + abs(numerator.adjusted() - denominator.adjusted()) * exponent
            + 30,
        )
        return str(
            ((numerator / denominator) ** exponent - 1).quantize(
                Decimal("0.0001"), rounding=ROUND_HALF_UP
            )
        )


def _provider_status(footnotes):
    # Only provider claims establish status; absence never means final.
    claims = [dict(note) for note in footnotes if note.get("code") or note.get("text")]
    codes = {str(note.get("code", "")).upper() for note in claims}
    return {
        "preliminary": True if "P" in codes else None,
        "revised": True if "R" in codes else None,
        "footnotes": claims,
        "basis": "provider_footnotes_only",
    }


def _metrics(profile, series_id):
    if profile is CONSUMPTION_PROFILE and series_id == "A072RC":
        return (
            (1, "change_1m_percentage_points"),
            (12, "change_12m_percentage_points"),
        )
    if profile is EMPLOYMENT_PROFILE:
        if profile.series_metadata[series_id]["value_unit"] == "thousand_jobs":
            return ((1, "net_change_1m_thousand_jobs"),)
        if series_id in {"LNS14000000", "LNS11300000", "LNS12300000"}:
            return ((1, "change_1m_percentage_points"),)
        if series_id == "CES0500000002":
            return ((1, "change_1m_hours"),)
        return (
            (1, "mom_pct"),
            (12, "yoy_pct"),
            (1, "change_1m_ratio"),
            (12, "change_12m_ratio"),
        )
    return (
        (1, "mom_pct"),
        (12, "yoy_pct"),
        (1, "change_1m_ratio"),
        (3, "change_3m_ratio"),
        (6, "change_6m_ratio"),
        (12, "change_12m_ratio"),
        *(
            ((3, "annualized_3m_ratio"), (6, "annualized_6m_ratio"))
            if profile is PCE_PROFILE
            else ()
        ),
    )


def _difference(current, previous):
    numerator, denominator = Decimal(current), Decimal(previous)
    with localcontext() as context:
        context.prec = max(
            50,
            len(numerator.as_tuple().digits)
            + len(denominator.as_tuple().digits)
            + abs(numerator.adjusted() - denominator.adjusted())
            + 20,
        )
        return str(numerator - denominator)


def _gap_requests(series, fetch_keys):
    """Coalesce only consecutive years containing gaps, never full local years."""
    years = sorted({int(month[:4]) for _, month in fetch_keys})
    groups = []
    for year in years:
        if groups and year == groups[-1][-1] + 1:
            groups[-1].append(year)
        else:
            groups.append([year])
    result = []
    for group in groups:
        keys = {(s, m) for s, m in fetch_keys if int(m[:4]) in group}
        selected = tuple(s for s in series if any(k[0] == s for k in keys))
        result.append((selected, min(m for _, m in keys), max(m for _, m in keys)))
    return result


class CpiTimeSeriesService:
    def __init__(self, store, source, *, profile=CPI_PROFILE):
        self.profile = validate_profile(profile)
        for component in (store, source):
            if getattr(component, "profile", profile) is not profile:
                raise CpiTimeSeriesError("bls_timeseries_profile_mismatch")
        self.store, self.source = store, source

    def query_range(
        self,
        start_month,
        end_month,
        *,
        series_ids=_DEFAULT_SERIES,
        allow_upstream=True,
        as_of=None,
        vintage_mode=None,
        query_mode=None,
        as_of_basis=None,
    ):
        """Merge individual missing series/months without persisting responses."""
        if type(allow_upstream) is not bool:
            raise CpiTimeSeriesError("cpi_timeseries_upstream_flag_invalid")
        for value in (vintage_mode, as_of_basis):
            if value == "official":
                raise CpiTimeSeriesError("cpi_timeseries_official_vintage_unsupported")
            if value is not None and (
                not isinstance(value, str) or value not in {"observed", "available"}
            ):
                raise CpiTimeSeriesError("cpi_timeseries_vintage_mode_invalid")
        if query_mode is not None and (
            not isinstance(query_mode, str) or query_mode not in {"current", "history"}
        ):
            raise CpiTimeSeriesError("cpi_timeseries_query_mode_invalid")
        if (
            vintage_mode is not None
            and as_of_basis is not None
            and vintage_mode != as_of_basis
        ):
            raise CpiTimeSeriesError("cpi_timeseries_conflicting_as_of_basis")
        mode = query_mode or (
            "history"
            if as_of is not None
            or vintage_mode == "available"
            or as_of_basis == "available"
            else "current"
        )
        if mode not in {"current", "history"}:
            raise CpiTimeSeriesError("cpi_timeseries_query_mode_invalid")
        basis = (
            as_of_basis
            or vintage_mode
            or ("available" if mode == "history" or as_of is not None else "observed")
        )
        current = mode == "current" and as_of is None
        if current:
            basis = "observed"
        result = self._run(
            start_month,
            end_month,
            series_ids,
            allow_upstream and mode == "current",
            as_of,
            persist=False,
            refresh=False,
            vintage_mode=basis,
            confirmed_current=current,
        )
        result.update(query_mode=mode, as_of_basis=basis)
        return result

    def init_range(self, start_month, end_month, *, series_ids=_DEFAULT_SERIES):
        """Persist only missing indices; repeated complete initialization is inert."""
        return self._run(
            start_month,
            end_month,
            series_ids,
            True,
            None,
            persist=True,
            refresh=False,
        )

    def update_range(
        self,
        start_month,
        end_month,
        *,
        series_ids=_DEFAULT_SERIES,
        include_buffer=True,
        restoration_evidence_url=None,
        restoration_expected_points=None,
    ):
        """Refresh source indices, including the shared 12-month calculation buffer."""
        return self._run(
            start_month,
            end_month,
            series_ids,
            True,
            None,
            persist=True,
            refresh=True,
            include_buffer=include_buffer,
            restoration_evidence_url=restoration_evidence_url,
            restoration_expected_points=restoration_expected_points,
        )

    def confirm_current_availability(
        self, start_month, end_month, *, series_ids=_DEFAULT_SERIES
    ):
        """Confirm currently committed latest data now, never its old commit time."""
        series, _, _ = _parameters(
            start_month, end_month, series_ids, None, profile=self.profile
        )
        try:
            return self.store.confirm_current_availability(
                series, start_month, end_month
            )
        except CpiStorageError as error:
            raise CpiTimeSeriesError(error.classification) from None
        except Exception:
            raise CpiTimeSeriesError(
                "cpi_timeseries_confirmation_outcome_unknown"
            ) from None

    def withdraw_range(
        self, start_month, end_month, *, series_ids=_DEFAULT_SERIES, evidence_url
    ):
        series, _, _ = _parameters(
            start_month, end_month, series_ids, None, profile=self.profile
        )
        try:
            validate_evidence_url(evidence_url, profile=self.profile)
            with self.store.operation():
                return self.store.withdraw_range(
                    series, start_month, end_month, evidence_url=evidence_url
                )
        except CpiStorageError as error:
            raise CpiTimeSeriesError(error.classification) from None
        except Exception:
            raise CpiTimeSeriesError("cpi_timeseries_storage_write_failed") from None

    def _run(
        self,
        start,
        end,
        series_ids,
        allow_upstream,
        as_of,
        *,
        persist,
        refresh,
        vintage_mode="observed",
        confirmed_current=False,
        include_buffer=True,
        restoration_evidence_url=None,
        restoration_expected_points=None,
    ):
        series, buffer_start, as_of = _parameters(
            start, end, series_ids, as_of, profile=self.profile
        )
        if vintage_mode == "official":
            raise CpiTimeSeriesError("cpi_timeseries_official_vintage_unsupported")
        if vintage_mode not in ("observed", "available"):
            raise CpiTimeSeriesError("cpi_timeseries_vintage_mode_invalid")
        if type(include_buffer) is not bool:
            raise CpiTimeSeriesError("cpi_timeseries_buffer_flag_invalid")
        if not include_buffer:
            buffer_start = start
        if type(allow_upstream) is not bool:
            raise CpiTimeSeriesError("cpi_timeseries_upstream_flag_invalid")
        if as_of is not None or vintage_mode == "available":
            allow_upstream = False
        attempts = 0
        fetched = []
        try:
            if restoration_evidence_url is not None:
                validate_evidence_url(restoration_evidence_url, profile=self.profile)
            expected = _restoration_expectations(
                restoration_expected_points,
                restoration_evidence_url,
                profile=self.profile,
            )
            if expected is not None and any(
                s not in series or not start <= month <= end for s, month in expected
            ):
                raise CpiTimeSeriesError(
                    "cpi_timeseries_restoration_expected_points_invalid"
                )
            with self.store.operation():
                read_options = {"as_of": as_of}
                if vintage_mode != "observed":
                    read_options["vintage_mode"] = vintage_mode
                local = self.store.read_range(series, buffer_start, end, **read_options)
                indexed = {
                    (p["series_id"], p["month"]): dict(p, origin="local") for p in local
                }
                if confirmed_current:
                    confirmed = self.store.read_range(
                        series, buffer_start, end, vintage_mode="available"
                    )
                    indexed.update(
                        {
                            (p["series_id"], p["month"]): dict(p, origin="local")
                            for p in confirmed
                            if indexed.get((p["series_id"], p["month"]), {}).get(
                                "availability"
                            )
                            != "withdrawn"
                        }
                    )
                    for point in indexed.values():
                        if (
                            point.get("available_at") is None
                            and point.get("availability") != "withdrawn"
                        ):
                            point["availability"] = "unknown"
                needed = {
                    (s, month)
                    for s in series
                    for month in month_range(buffer_start, end)
                }
                missing = needed - indexed.keys()
                fetch_keys = needed if refresh else missing
                if self.profile is CONSUMPTION_PROFILE and not persist:
                    # Skip current/future reference months; this does not assert
                    # that every prior month has been published. Keep local rows.
                    now = self.store._clock().astimezone(UTC)
                    last_complete = month_at(now.year * 12 + now.month - 2)
                    fetch_keys = (
                        {(s, m) for s, m in fetch_keys if m <= last_complete}
                        if start <= last_complete
                        else set()
                    )
                batches = ()
                attempted_keys = set()
                source_failure = None
                failed_requests = failed_attempts = 0
                if allow_upstream and fetch_keys:
                    requests = _gap_requests(series, fetch_keys)
                    planner = getattr(self.source, "plan_windows", None)
                    count = sum(
                        len(
                            planner(first, last)
                            if planner is not None
                            else year_windows(
                                first,
                                last,
                                registered=getattr(self.source, "registered", True),
                            )
                        )
                        for _, first, last in requests
                    ) * (2 if self.profile.provider == "BEA" else 1)
                    if count > getattr(
                        self.source, "max_range_requests", MAX_RANGE_REQUESTS
                    ):
                        raise CpiTimeSeriesError(
                            "cpi_timeseries_request_budget_exceeded"
                        )
                    if self.profile is CONSUMPTION_PROFILE and planner is not None:
                        requests = [
                            (
                                selected,
                                max(first, f"{a:04d}-01"),
                                min(last, f"{b:04d}-12"),
                            )
                            for selected, first, last in requests
                            for a, b in planner(first, last)
                        ]
                    fetched = []
                    for selected, first, last in requests:
                        try:
                            acquired = self.source.fetch(selected, first, last)
                        except CpiSourceError as error:
                            trusted_local = any(
                                start <= m <= end
                                and p.get("index_value") is not None
                                and p.get("availability")
                                not in {"withdrawn", "unknown"}
                                for (_, m), p in indexed.items()
                            )
                            if not (
                                self.profile is CONSUMPTION_PROFILE
                                and not persist
                                and trusted_local
                                and error.transient
                                and error.attempts > 0
                            ):
                                raise
                            source_failure = {
                                "classification": error.classification,
                                "http_status": error.http_status,
                                "retry_at": error.retry_at,
                            }
                            failed_attempts = error.attempts
                            failed_requests = error.request_count
                            break
                        attempts += sum(batch.attempts for batch in acquired)
                        _validate_batches(acquired, profile=self.profile)
                        if any(
                            p.series_id not in selected
                            for batch in acquired
                            for p in batch.points
                        ):
                            raise CpiTimeSeriesError("cpi_timeseries_series_invalid")
                        if acquired:
                            attempted_keys.update(
                                (s, m)
                                for s in selected
                                for m in month_range(first, last)
                                if (s, m) in fetch_keys
                            )
                        fetched.extend(acquired)
                    batches = tuple(
                        replace(
                            batch,
                            points=tuple(
                                point
                                for point in batch.points
                                if (point.series_id, point.month) in fetch_keys
                            ),
                        )
                        for batch in fetched
                    )
                fetched_keys = {
                    (p.series_id, p.month) for b in batches for p in b.points
                }
                saved = {"saved_points": 0, "saved_revisions": 0}
                if persist and batches:
                    save_options = {}
                    if restoration_evidence_url is not None:
                        save_options["restoration_evidence_url"] = (
                            restoration_evidence_url
                        )
                    if restoration_expected_points is not None:
                        save_options["restoration_expected_points"] = (
                            restoration_expected_points
                        )
                    saved = self.store.save_batches(batches, **save_options)
                    local = self.store.read_range(series, buffer_start, end)
                    indexed = {
                        (p["series_id"], p["month"]): dict(
                            p,
                            origin=(
                                "upstream"
                                if (p["series_id"], p["month"]) in fetched_keys
                                and p.get("availability") != "withdrawn"
                                else "local"
                            ),
                        )
                        for p in local
                    }
                elif batches:
                    for batch in batches:
                        for point in batch.points:
                            document = _point_document(
                                point, batch, None, profile=self.profile
                            )
                            # An unpersisted query must not claim a durable locator.
                            document.update(
                                raw_locator=None,
                                origin="upstream",
                                availability="unpersisted"
                                if point.index_value is not None
                                else "source_unavailable",
                                available_at=None,
                            )
                            indexed[(point.series_id, point.month)] = document
                result = self._result(
                    start,
                    end,
                    series,
                    indexed,
                    as_of,
                    persist,
                    batches,
                    saved,
                    vintage_mode,
                )
                if refresh or attempted_keys:
                    valid_fetched_keys = {
                        (p.series_id, p.month)
                        for b in batches
                        for p in b.points
                        if p.index_value is not None
                        and indexed.get((p.series_id, p.month), {}).get("availability")
                        != "withdrawn"
                    }
                    source_missing = sorted(attempted_keys - valid_fetched_keys)
                    result["refresh_complete"] = not source_missing
                    result["refresh_coverage_complete"] = attempted_keys <= fetched_keys
                    result["source_missing"] = [
                        {"series_id": s, "month": m} for s, m in source_missing
                    ]
                    result["refreshed_series_months"] = [
                        {"series_id": s, "month": m}
                        for s, m in sorted(valid_fetched_keys)
                    ]
                result["source_attempted_series_months"] = [
                    {"series_id": s, "month": m} for s, m in sorted(attempted_keys)
                ]
                result["source_unprovided"] = [
                    {"series_id": s, "month": m}
                    for s, m in sorted(attempted_keys - fetched_keys)
                ]
                if self.profile is CONSUMPTION_PROFILE:
                    result.update(
                        status="PARTIAL" if source_failure else "OK",
                        source_failure=source_failure,
                    )
                    result["provider_requests"] += failed_requests
                    result["provider_attempts"] += failed_attempts
                    result["source_request_count"] += failed_attempts
                return result
        except CpiTimeSeriesError:
            raise
        except CpiSourceError as error:
            raise CpiTimeSeriesError(
                error.classification,
                attempts + error.attempts,
                http_status=getattr(error, "http_status", None),
                retry_at=getattr(error, "retry_at", None),
                provider_requests=sum(
                    b.request.get("request_count", 1) for b in fetched
                )
                + error.request_count,
            ) from None
        except CpiStorageError as error:
            raise CpiTimeSeriesError(error.classification, attempts) from None
        except Exception:
            raise CpiTimeSeriesError(
                "cpi_timeseries_operation_failed", attempts
            ) from None

    def _result(
        self,
        start,
        end,
        series,
        indexed,
        as_of,
        persist,
        batches,
        saved,
        vintage_mode="observed",
    ):
        points, missing, calculation_missing, unavailable, withdrawn = (
            [],
            [],
            [],
            [],
            [],
        )
        for s in series:
            for month in month_range(start, end):
                point = indexed.get((s, month))
                if point is None:
                    gap = {"series_id": s, "month": month}
                    if vintage_mode == "available":
                        gap["reason"] = "availability_evidence_insufficient"
                    missing.append(gap)
                    continue
                if point.get("availability") == "withdrawn":
                    missing.append(
                        {"series_id": s, "month": month, "reason": "withdrawn"}
                    )
                    withdrawn.append(dict(point))
                    continue
                if point.get("availability") == "unknown":
                    missing.append(
                        {
                            "series_id": s,
                            "month": month,
                            "reason": "availability_evidence_insufficient",
                        }
                    )
                    unavailable.append(dict(point))
                    continue
                if point["index_value"] is None:
                    missing.append(
                        {"series_id": s, "month": month, "reason": "source_unavailable"}
                    )
                    unavailable.append(dict(point))
                    continue
                point = dict(point)
                point["calculation_inputs"] = {}
                for lag, metric in _metrics(self.profile, s):
                    previous_month = month_at(month_number(month) - lag)
                    previous = indexed.get((s, previous_month))
                    point[metric] = None
                    if (
                        previous is not None
                        and previous["index_value"] is not None
                        and previous.get("availability") not in {"withdrawn", "unknown"}
                    ):
                        calculate = (
                            _difference
                            if metric
                            in {
                                "net_change_1m_thousand_jobs",
                                "change_1m_percentage_points",
                                "change_12m_percentage_points",
                                "change_1m_hours",
                            }
                            else _ratio
                            if metric.endswith("_ratio")
                            else _rate
                        )
                        point[metric] = (
                            _annualized(
                                point["index_value"],
                                previous["index_value"],
                                4 if lag == 3 else 2,
                            )
                            if metric.startswith("annualized_")
                            else calculate(
                                point["index_value"], previous["index_value"]
                            )
                        )
                        point["calculation_inputs"][metric] = {
                            "numerator_month": month,
                            "numerator_origin": point["origin"],
                            "denominator_month": previous_month,
                            "denominator_origin": previous["origin"],
                            "denominator_index_value": previous["index_value"],
                            "denominator_observed_at": previous["observed_at"],
                            "denominator_revision": previous["revision"],
                            "denominator_available_at": previous.get("available_at"),
                            "derived_origin": (
                                point["origin"]
                                if point["origin"] == previous["origin"]
                                else "mixed"
                            ),
                        }
                    else:
                        calculation_missing.append(
                            {
                                "series_id": s,
                                "month": month,
                                "metric": metric,
                                "required_month": previous_month,
                                "reason": (
                                    previous.get("availability", "source_unavailable")
                                    if previous is not None
                                    else (
                                        "availability_evidence_insufficient"
                                        if vintage_mode == "available"
                                        else "not_observed"
                                    )
                                ),
                            }
                        )
                if self.profile is PPI_PROFILE:
                    point["provider_status"] = _provider_status(
                        point.get("footnotes", [])
                    )
                point.update(self.profile.series_metadata[s])
                points.append(point)
        result = {
            "start_month": start,
            "end_month": end,
            "series_ids": list(series),
            "points": points,
            "missing": missing,
            "complete": not missing,
            "coverage_complete": all(
                (s, month) in indexed
                for s in series
                for month in month_range(start, end)
            ),
            "source_unavailable": unavailable,
            "withdrawn": withdrawn,
            "calculation_missing": calculation_missing,
            "persisted": persist,
            "provider_requests": sum(
                b.request.get("request_count", 1) for b in batches
            ),
            "provider_attempts": sum(b.attempts for b in batches),
            "source_request_count": sum(b.attempts for b in batches),
            "source_returned_series_months": [
                {"series_id": s, "month": month}
                for s, month in sorted(
                    {(p.series_id, p.month) for b in batches for p in b.points}
                )
            ],
            "as_of": None if as_of is None else as_of.isoformat(),
            "as_of_semantics": (
                "mdf_post_commit_confirmation_available_at"
                if vintage_mode == "available"
                else "source_response_observed_at_not_mdf_commit_or_historical_vintage"
            ),
            "vintage_mode": vintage_mode,
            "units": {
                **{f"change_{k}m_ratio": "ratio" for k in (1, 3, 6, 12)},
                "mom_pct": "percent",
                "yoy_pct": "percent",
                **{f"bls_pct_{k}m": "percent" for k in (1, 3, 6, 12)},
            },
            "calculation_method": CALCULATION_METHOD,
        }
        if self.profile is PCE_PROFILE:
            result.update(
                provider="BEA",
                calculation_method=PCE_CALCULATION_METHOD,
                retention_class="credential_free_bea_results_projection",
                exact_original_retained=False,
            )
            result["units"] = {
                k: v for k, v in result["units"].items() if not k.startswith("bls_")
            }
            result["units"]["bea_pct_1m"] = "percent"
            result["units"].update(
                annualized_3m_ratio="ratio", annualized_6m_ratio="ratio"
            )
            result["official_calculation_method"] = (
                "bea_pct_1m is the BEA T20807 published monthly percent change; "
                "0.3 means 0.3%; nullable when BEA did not supply a value or the "
                "historical official value has not yet been collected. NULL never "
                "means 0. coverage_complete describes index coverage only and "
                "does not assert complete official monthly percent coverage. "
                "mom_pct/yoy_pct and change_km_ratio remain derived from T20804."
            )
        if self.profile in (EMPLOYMENT_PROFILE, CONSUMPTION_PROFILE):
            for point in points + unavailable + withdrawn:
                point["value"] = point.pop("index_value")
                point.update(self.profile.series_metadata[point["series_id"]])
                for inputs in point.get("calculation_inputs", {}).values():
                    inputs["comparison_value"] = inputs.pop("denominator_index_value")
            result["units"] = {
                "value": "per_series_value_unit",
                "net_change_1m_thousand_jobs": "thousand_jobs",
                "change_1m_percentage_points": "percentage_points",
                "change_1m_hours": "hours",
                "mom_pct": "percent",
                "yoy_pct": "percent",
                "change_1m_ratio": "ratio",
                "change_12m_ratio": "ratio",
            }
            result["calculation_method"] = (
                "CES employee levels: value[t]-value[t-1], thousand jobs, not people. "
                "CPS unemployment, participation and employment/population rates: "
                "value[t]-value[t-1], percentage points. "
                "CES average weekly paid hours: value[t]-value[t-1], hours. "
                "CES AHE: USD/hour; mom_pct/yoy_pct and 1m/12m ratios derived "
                "from same-cutoff endpoints, not annualized. Official net_changes "
                "and pct_changes remain separate in provider_calculations. "
                "Revision 1 is first local observation, not official first release."
            )
        if self.profile is CONSUMPTION_PROFILE:
            result.update(
                provider="BEA",
                retention_class="credential_free_bea_results_projection",
                exact_original_retained=False,
                calculation_method=(
                    "Native money values in millions; SAAR levels are not "
                    "annualized growth. Same-cutoff unrounded endpoints; "
                    "cumulative ratios. Saving rate percent: differences in "
                    "percentage points only. Chained 2017-dollar components "
                    "are not additive."
                ),
            )
            result["units"] = {
                "value": "per_series_value_unit",
                "mom_pct": "percent",
                "yoy_pct": "percent",
                **{f"change_{k}m_ratio": "ratio" for k in (1, 3, 6, 12)},
                **{
                    f"change_{k}m_percentage_points": "percentage_points"
                    for k in (1, 12)
                },
            }
        if persist:
            result.update(saved)
        return result

    def status(self):
        try:
            with self.store.operation():
                return self.store.status()
        except CpiStorageError as error:
            raise CpiTimeSeriesError(error.classification) from None
        except Exception:
            raise CpiTimeSeriesError("cpi_timeseries_storage_read_failed") from None

    def close(self):
        self.store.close()


def build_source(
    *,
    credential_loader,
    request_control=None,
    request_control_path=None,
    secret_path=DEFAULT_SECRET_PATH,
    registered=True,
    max_transport_attempts=1,
    profile=CPI_PROFILE,
):
    profile = validate_profile(profile)
    if profile is CONSUMPTION_PROFILE:
        from market_data_foundation.historical.consumption_source import (
            ConsumptionRangeSource,
        )

        return ConsumptionRangeSource(
            credential_loader=credential_loader,
            request_control=request_control,
            request_control_path=request_control_path,
            max_transport_attempts=max_transport_attempts,
        )
    if profile is PCE_PROFILE:
        from market_data_foundation.historical.pce_timeseries_source import (
            BeaRangeSource,
        )

        return BeaRangeSource(
            credential_loader=credential_loader,
            request_control=request_control,
            request_control_path=request_control_path,
            max_transport_attempts=max_transport_attempts,
            profile=profile,
        )
    if request_control_path is not None:
        raise CpiTimeSeriesError("cpi_timeseries_request_control_binding_invalid")
    if credential_loader is None:
        raise CpiTimeSeriesError("cpi_package_credential_binding_required")
    if request_control is None:
        from market_data_foundation.historical.cpi_request_control import (
            CpiRequestControl,
        )

        request_control = CpiRequestControl(DEFAULT_REQUEST_CONTROL_PATH)
    return BlsRangeSource(
        secret_path=secret_path,
        registered=registered,
        credential_loader=credential_loader,
        request_control=request_control,
        max_transport_attempts=max_transport_attempts,
        profile=profile,
    )


def build_service(
    *,
    runtime_path=DEFAULT_RUNTIME_PATH,
    secret_path=DEFAULT_SECRET_PATH,
    registered=True,
    credential_loader=None,
    request_control=None,
    request_control_path=None,
    max_transport_attempts=1,
    profile=CPI_PROFILE,
):
    """Use existing MDF DEV configuration; does not create schemas or run requests."""
    profile = validate_profile(profile)
    connection = None
    try:
        source = build_source(
            credential_loader=credential_loader,
            request_control=request_control,
            request_control_path=request_control_path,
            secret_path=secret_path,
            registered=registered,
            max_transport_attempts=max_transport_attempts,
            profile=profile,
        )
        runtime_path, secret_path = Path(runtime_path), Path(secret_path)
        runtime = tomllib.loads(runtime_path.read_text())
        s3 = runtime["s3"]
        if not (
            runtime["storage"]["backend"] == "s3"
            and s3["endpoint_url"] == GARAGE_ENDPOINT
            and s3["bucket"] == BUCKET
            and s3["region"] == "garage"
            and s3["key_prefix"] == ""
            and s3["addressing_style"] == "path"
        ):
            raise CpiTimeSeriesError("cpi_timeseries_storage_binding_invalid")
        secret = tomllib.loads(secret_path.read_text())
        password = secret["postgres"]["password"]
        if type(password) is not str or not password:
            raise CpiTimeSeriesError("cpi_timeseries_credentials_invalid")
        credentials = GarageCredentials(
            secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
        )
        connection = connect_dev(password, runtime_path=runtime_path)
        return CpiTimeSeriesService(
            PostgresTimeSeriesStore(
                connection,
                GarageCpiRawStore(credentials, profile=profile),
                profile=profile,
            ),
            source,
            profile=profile,
        )
    except CpiTimeSeriesError:
        if connection is not None:
            connection.close()
        raise
    except Exception:
        if connection is not None:
            connection.close()
        raise CpiTimeSeriesError("cpi_timeseries_configuration_failed") from None
