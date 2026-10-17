"""Semantic contract for the bounded FRED/ALFRED vintage qualification."""

from dataclasses import dataclass
from decimal import Decimal

from market_data_foundation.canonical.model import DatasetDefinition, Scope

PROVIDER = "Federal Reserve Bank of St. Louis FRED/ALFRED aggregator"
SERIES = ("GDP", "PAYEMS")
OBSERVATION_START = "2025-01-01"
OBSERVATION_END = "2026-12-31"


@dataclass(frozen=True)
class SeriesMetadata:
    series_id: str
    frequency: str
    units: str
    seasonal_adjustment: str
    underlying_source: str
    provider: str = PROVIDER
    first_party_release: bool = False


METADATA = {
    "GDP": SeriesMetadata("GDP", "quarterly", "billions USD", "SAAR", "BEA via FRED"),
    "PAYEMS": SeriesMetadata(
        "PAYEMS", "monthly", "thousands persons", "SA", "BLS via FRED"
    ),
}


@dataclass(frozen=True)
class VintageObservation:
    series_id: str
    vintage: str
    realtime_end: str
    date: str
    raw_value: str
    value: Decimal | None
    unavailable: bool


@dataclass(frozen=True)
class SemanticBundle:
    metadata: tuple[SeriesMetadata, ...]
    observations: tuple[VintageObservation, ...]


VINTAGE_BUNDLE = DatasetDefinition(
    identity="mdf.macro.fred-alfred.vintage-bundle",
    revision="t06-r1",
    domain_contract="GDP and PAYEMS dual-vintage bounded FRED/ALFRED aggregator bundle",
    purpose="replayable local vintage comparison including unavailable cells",
    source_qualified=False,
    key_semantics="SERIES|VINTAGE|OBSERVATION_DATE",
    unit_semantics="GDP billions USD SAAR; PAYEMS thousands persons SA",
    temporal_semantics=(
        "vintage and observation dates are strings; acquisition, freeze, decision "
        "and usability are distinct timezone-aware instants"
    ),
    revision_semantics="two discovered vintages per series; no finality inference",
    completeness_semantics=(
        "GDP+PAYEMS each two distinct selected vintages globally represented; "
        "observation window 2025-01-01 through 2026-12-31; per-reference vintage "
        "coverage may be sparse; exact provider omission preserved by absence of "
        "cell; explicit unavailable dot cells retained separately"
    ),
    scope=Scope(),
)
