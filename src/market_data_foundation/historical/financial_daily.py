"""Period-native financial conditions definitions; no source or storage effects."""

import calendar
import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from market_data_foundation.canonical.model import (
    DatasetDefinition,
    EvidenceRequirement,
    Grade,
    OutputMapping,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
)
from market_data_foundation.historical.cpi_timeseries_source import CpiSourceError

FinancialError = CpiSourceError
DATASET_ID = "mdf.macro.financial-conditions.daily"
DEFINITION_REVISION = "r1"
TERMS_URL = "https://fred.stlouisfed.org/legal/"
SOURCE_REVIEW = "2026-11-05-fred-api-storage-permission-unconfirmed"
DEFINITION = DatasetDefinition(
    DATASET_ID,
    DEFINITION_REVISION,
    "Ten daily metric aliases, explicit sources and observation dates",
    "政策目标、隔夜利率、国债收益率、通胀补偿与信用利差",
    False,
    "series_id + observation_date; never a monthly key",
    "native percent; credit basis points = percent * 100",
    "source observation date != release instant != MDF observed/available",
    "changed value/status revisions; no inferred historical first release",
    "provider dots, non-observation days, future, failed and unknown stay distinct",
    Scope(frozenset({"project:market-data-foundation-dev"})),
)


@dataclass(frozen=True)
class Metric:
    series_id: str
    original_agency: str
    meaning: str
    calendar: str = "business_day_calendar_unconfirmed"
    frequency: str = "Daily"
    credit_bp: bool = False
    policy_effective: bool = False
    unit: str = "Percent"
    seasonal_adjustment: str = "NSA"
    distributor: str = "FRED"

    @property
    def source_url(self):
        return "https://fred.stlouisfed.org/series/" + self.series_id


METRICS = (
    Metric(
        "DFEDTARL",
        "Federal Reserve Board",
        "policy_target_lower",
        "seven_day",
        "Daily, 7-Day",
        policy_effective=True,
    ),
    Metric(
        "DFEDTARU",
        "Federal Reserve Board",
        "policy_target_upper",
        "seven_day",
        "Daily, 7-Day",
        policy_effective=True,
    ),
    Metric(
        "EFFR", "Federal Reserve Bank of New York", "unsecured_overnight_effective_rate"
    ),
    Metric(
        "SOFR", "Federal Reserve Bank of New York", "secured_overnight_financing_rate"
    ),
    Metric("DGS2", "Federal Reserve Board", "nominal_constant_maturity_2y"),
    Metric("DGS10", "Federal Reserve Board", "nominal_constant_maturity_10y"),
    Metric("DFII10", "Federal Reserve Board", "real_constant_maturity_10y"),
    Metric(
        "T10YIE",
        "Federal Reserve Bank of St. Louis",
        "treasury_breakeven_not_pure_inflation_expectations",
    ),
    Metric(
        "BAMLC0A0CM",
        "ICE Data Indices, LLC",
        "investment_grade_option_adjusted_spread",
        "business_day_or_month_end",
        "Daily, Close",
        credit_bp=True,
    ),
    Metric(
        "BAMLH0A0HYM2",
        "ICE Data Indices, LLC",
        "high_yield_option_adjusted_spread",
        "business_day_or_month_end",
        "Daily, Close",
        credit_bp=True,
    ),
)
SERIES_IDS = tuple(m.series_id for m in METRICS)
BY_ID = {m.series_id: m for m in METRICS}
SOURCE = SourceOperationRef(
    "mdf.source.fred.financial-conditions.daily",
    "r1",
    "market-data-foundation",
    "FRED",
    "fred/series/observations:" + ",".join(SERIES_IDS),
    "financial-daily-native-r1",
    "ten-explicit-daily-series",
    None,
    None,
    None,
    "read",
    "explicit_daily_capture",
    DEFINITION.scope,
    "observation dates; no inferred publication or historical availability",
    "per-series complete native response; gaps never forward filled",
    "offset zero, count equals rows, no hidden pagination",
    "MDF capture history only; source-use permission currently unconfirmed",
    frozenset({Grade.ORIGINAL}),
)
BINDING = SourceBindingRevision(
    "mdf.binding.fred.financial-conditions.daily",
    "r1",
    SOURCE.key,
    (
        OutputMapping(
            "fred-financial-daily",
            DEFINITION.key,
            "r1",
            "daily-observed-available-r1",
            DEFINITION.scope,
            EvidenceRequirement(frozenset({Grade.ORIGINAL}), True, True),
        ),
    ),
    "exact series, Percent, Daily/7-Day/Close, NSA, no frequency conversion",
    (
        "retention_permission_unconfirmed",
        "no_inferred_official_vintage",
        "no_silent_provider_fallback",
    ),
    silent_fallback=False,
)


def day(value):
    try:
        if type(value) is not str or len(value) != 10:
            raise ValueError
        parsed = date.fromisoformat(value)
        if parsed.isoformat() != value:
            raise ValueError
        return parsed
    except ValueError, TypeError:
        raise FinancialError("financial_date_invalid") from None


def instant(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise FinancialError("financial_instant_invalid")
    return value.astimezone(UTC)


def dates(start, end):
    a, b = day(start), day(end)
    if a > b or (b - a).days > 366 * 100:
        raise FinancialError("financial_range_invalid")
    return tuple((a + timedelta(days=n)).isoformat() for n in range((b - a).days + 1))


def series_ids(values):
    if (
        type(values) is not tuple
        or not values
        or len(set(values)) != len(values)
        or any(v not in BY_ID for v in values)
    ):
        raise FinancialError("financial_series_invalid")
    return values


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


def source_policy(sid):
    m = BY_ID[sid]
    return dict(
        series_id=sid,
        status="owner_authorized_dev" if m.credit_bp else "permission_required",
        classification="owner_credit_dev_scope"
        if m.credit_bp
        else "fred_retention_permission_unconfirmed",
        review=SOURCE_REVIEW,
        terms_url=TERMS_URL,
        storage_permitted=m.credit_bp,
        authorized_scope="personal noncommercial MDF DEV research only"
        if m.credit_bp
        else None,
        third_party_permission_required=m.credit_bp,
        range_limit="FRED three-year limit since April 2026; API range unmeasured"
        if m.credit_bp
        else None,
        actual_api_start=None,
        actual_api_end=None,
    )


def require_source_permission(sid):
    series_ids((sid,))
    # Registered source-use review, not a client checkbox or an API key.
    if not source_policy(sid)["storage_permitted"]:
        raise FinancialError("fred_retention_permission_unconfirmed")


def permitted_series(selected):
    return tuple(
        sid for sid in series_ids(selected) if source_policy(sid)["storage_permitted"]
    )


def registry():
    from market_data_foundation.historical.financial_daily_origin import binding

    return [
        dict(
            asdict(m),
            source_url=m.source_url,
            source_use=source_policy(m.series_id),
            active_binding=binding(m.series_id),
        )
        for m in METRICS
    ]


def non_observation_day(metric, value):
    d = day(value)
    if metric.calendar == "seven_day" or d.weekday() < 5:
        return False
    return not (
        metric.calendar == "business_day_or_month_end"
        and d.day == calendar.monthrange(d.year, d.month)[1]
    )


@dataclass(frozen=True)
class DailyPoint:
    series_id: str
    observation_date: str
    value: Decimal | None
    provider_extra: dict = field(default_factory=dict)


@dataclass(frozen=True)
class DailyBatch:
    series_id: str
    start: str
    end: str
    points: tuple[DailyPoint, ...]
    raw: bytes = field(repr=False)
    metadata_raw: bytes = field(repr=False)
    metadata: dict
    metadata_observed_at: datetime
    observed_at: datetime
    request: dict
    origin: str
    attempts: int = 1

    @property
    def batch_id(self):
        return digest(
            canonical(
                [
                    DATASET_ID,
                    DEFINITION_REVISION,
                    self.origin,
                    self.request,
                    digest(self.raw),
                    digest(self.metadata_raw),
                ]
            ).encode()
        )
