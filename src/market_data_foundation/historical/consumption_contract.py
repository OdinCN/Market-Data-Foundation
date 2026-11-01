"""Independent logical asset, bound only to qualified native BEA NIPA inputs."""

from dataclasses import replace

from market_data_foundation.canonical.model import DatasetDefinition, Scope
from market_data_foundation.historical.bea_pce import PCE_BINDING_CANDIDATE, PCE_SOURCE

CONSUMPTION_DEFINITION = DatasetDefinition(
    "mdf.macro.bea.consumption-income",
    "r1",
    "BEA NIPA T20600/T20806 monthly seven native series",
    "美国消费与收入",
    True,
    "table + line + SeriesCode + month",
    "millions USD/chained 2017 USD SAAR; saving rate percent SA",
    "reference, observed, available, official event separate",
    "changed-only local versions; no reconstructed unobserved vintage",
    "actual bounded source coverage, no inferred native start",
    Scope(),
)
CONSUMPTION_SOURCE = replace(
    PCE_SOURCE,
    identity="mdf.source.bea.nipa.consumption-income",
    contract_revision="r1",
    operation="GetData:NIPA:T20600:T20806:M",
    product="BEA NIPA consumption/income",
    variant="seven-native-monthly-series",
    temporal_contract=(
        "separate response completion; MDF observed/available; "
        "official event only from retained proof"
    ),
    completeness_contract=(
        "seven native series per returned reference month; explicit missing rows"
    ),
    pagination_contract=(
        "five calendar years per table request; two separately completed requests"
    ),
    projection_revision="bea-consumption-income-v1",
)
CONSUMPTION_BINDING = replace(
    PCE_BINDING_CANDIDATE,
    identity="mdf.binding.bea.nipa.consumption-income",
    revision="r1",
    source=CONSUMPTION_SOURCE.key,
    limitations=(
        "credential_free_results_projection_only",
        "no_provider_fallback",
        "no_unobserved_vintage_reconstruction",
        "two_responses_not_atomic_snapshot",
        "chained_components_nonadditive",
    ),
    mappings=(
        replace(
            PCE_BINDING_CANDIDATE.mappings[0],
            definition=CONSUMPTION_DEFINITION.key,
            identity="bea-consumption-income",
            semantic_revision="r1",
        ),
    ),
)
