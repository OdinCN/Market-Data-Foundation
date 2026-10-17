"""T08 exact-two offline contract; these constants are not live DSD evidence."""

from dataclasses import dataclass

from market_data_foundation.canonical.model import DatasetDefinition, Scope

REVISION = "t08-historical-wire-r2"
PARSER = "t08-sdmx21-generic-historical-wire-r2"
MAX_ENTITY_BYTES = 2_000_000
MAX_BUNDLE_BYTES = 8_000_000
MAX_CONTENT_BYTES = 32_000_000
MAX_WINDOW_DAYS = 92


@dataclass(frozen=True)
class Surface:
    provider: str
    base: str
    agency: str
    flow: str
    key: str
    dimensions: tuple[str, ...]
    meanings: tuple[str, ...]
    unit: str


ECB = Surface(
    "ECB",
    "https://data-api.ecb.europa.eu/service",
    "ECB",
    "EXR",
    "D.USD.EUR.SP00.A",
    ("FREQ", "CURRENCY", "CURRENCY_DENOM", "EXR_TYPE", "EXR_SUFFIX"),
    ("Daily", "US dollar", "Euro", "Spot", "Average"),
    "USD per EUR",
)
# coicop18 is the 2026-09-17 MDD historical candidate, not current evidence.
# A different current DSD identifier must fail closed; there are no aliases.
EUROSTAT = Surface(
    "ESTAT",
    "https://ec.europa.eu/eurostat/api/dissemination/sdmx/2.1",
    "ESTAT",
    "PRC_HICP_MINR",
    "M.I25.TOTAL.DE",
    ("freq", "unit", "coicop18", "geo"),
    ("Monthly", "Index, 2025=100", "Total", "Germany"),
    "index 2025=100",
)


def surface_for(provider: str) -> Surface:
    # No caller-defined surface, arbitrary key or provider fallback.
    if provider == "ECB":
        return ECB
    if provider == "ESTAT":
        return EUROSTAT
    raise ValueError("sdmx_provider_not_admitted")


def definition_for(provider: str) -> DatasetDefinition:
    s = surface_for(provider)
    return DatasetDefinition(
        identity=f"mdf.macro.{provider.lower()}.sdmx.{s.flow.lower()}",
        revision=REVISION,
        domain_contract=f"official SDMX {s.agency}/{s.flow}/{s.key}",
        purpose="bounded retained SDMX snapshot and no-network local replay",
        source_qualified=False,
        key_semantics="agency|dataflow|series-key|reference-period",
        unit_semantics=s.unit,
        temporal_semantics=(
            "reference period and provider attributes are independent of MDF "
            "requested/obtained/frozen/Publication/usable instants"
        ),
        revision_semantics=(
            "latest retrieved snapshot only; provider attributes retained verbatim; "
            "no overwritten Eurostat values or historical provider vintages inferred"
        ),
        completeness_semantics=(
            "one exact series, bounded window, returned cells only; omitted periods "
            "remain absent; explicit missing cells remain unavailable"
        ),
        scope=Scope(),
    )


def roles_for(provider: str) -> tuple[str, ...]:
    surface_for(provider)
    return (
        ("flow", "dsd", "data")
        if provider == "ECB"
        else ("flow", "dsd", "unit", "data")
    )
