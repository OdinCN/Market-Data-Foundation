"""National SA JOLTS candidate contract; metadata admission remains explicit.

API observations prove returned identities and sampled coverage. They do not
prove catalog labels, the native start, publication events or historical vintages.
"""

from dataclasses import asdict, dataclass

from market_data_foundation.canonical.model import DatasetDefinition, Scope

ASSET_ID = "mdf.macro.bls.jolts"
DEFINITIONS_URL = "https://www.bls.gov/jlt/jltdef.htm"
FAQ_URL = "https://www.bls.gov/jlt/jltfaq.htm"
HANDBOOK_URL = "https://www.bls.gov/opub/hom/jlt/calculation.htm"
RELEASE_URL = "https://www.bls.gov/news.release/jolts.htm"


@dataclass(frozen=True)
class JoltsMetric:
    series_id: str
    indicator: str
    statistic: str
    value_unit: str
    reference_basis: str
    rate_denominator: str | None
    provider: str = "BLS"
    survey: str = "JOLTS"
    geography: str = "US"
    industry: str = "total_nonfarm"
    seasonal_adjustment: str = "SA"
    frequency: str = "monthly"
    start_month: str | None = None
    evidence_start_month: str = "2000-12"
    coverage_basis: str = "sampled_bls_api_not_catalog_native_start"
    native_metadata_status: str = "native_report_verified_definitions_pending"
    native_metadata_source_url: str = "https://data.bls.gov/pdq/SurveyOutputServlet"


METRICS = tuple(
    JoltsMetric(
        series_id=f"JTS000000000000000{code}{kind}",
        indicator=indicator,
        statistic="official_rate" if kind == "R" else "level",
        value_unit="percent" if kind == "R" else unit,
        reference_basis=basis,
        rate_denominator=denominator if kind == "R" else None,
    )
    for code, indicator, unit, basis, denominator in (
        (
            "JO",
            "job_openings",
            "thousand_jobs",
            "last_business_day_stock",
            "employment_plus_job_openings",
        ),
        ("HI", "hires", "thousand_persons", "during_month_flow", "employment"),
        ("QU", "quits", "thousand_persons", "during_month_flow", "employment"),
        (
            "LD",
            "layoffs_discharges",
            "thousand_persons",
            "during_month_flow",
            "employment",
        ),
    )
    for kind in ("L", "R")
)
SERIES_IDS = tuple(metric.series_id for metric in METRICS)
METADATA = tuple(
    (metric.series_id, tuple(asdict(metric).items())) for metric in METRICS
)

JOLTS_DEFINITION = DatasetDefinition(
    identity=ASSET_ID,
    revision="r1-segment1",
    domain_contract="Eight national total-nonfarm SA JOLTS series; candidate metadata",
    purpose="Job openings stock and hires/quits/layoffs flows with official rates",
    source_qualified=False,
    key_semantics="BLS series_id + reference month; level and rate independent",
    unit_semantics=(
        "Openings thousand jobs; flows thousand persons, not unique-person counts; "
        "official rates percent; rate differences percentage points; growth ratio"
    ),
    temporal_semantics=(
        "Reference, obtained, committed available, calendar and actual event separate"
    ),
    revision_semantics=(
        "Monthly preliminary/revised footnotes; annual five-year revision risk; "
        "final is not immutable; first local capture is not official first release"
    ),
    completeness_semantics=(
        "Per-series/month gaps and nulls; metadata/release proof independently pending"
    ),
    scope=Scope(),
)
