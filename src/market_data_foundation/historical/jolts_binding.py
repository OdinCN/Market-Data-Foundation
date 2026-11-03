"""Candidate BLS JOLTS capability; declaration never enables live execution."""

from dataclasses import replace

from market_data_foundation.historical.bls_employment import (
    EMPLOYMENT_BINDING_CANDIDATE,
    EMPLOYMENT_SOURCE,
)
from market_data_foundation.historical.jolts_contract import JOLTS_DEFINITION

JOLTS_SOURCE = replace(
    EMPLOYMENT_SOURCE,
    identity="mdf.source.bls.jolts",
    contract_revision="r1",
    operation="timeseries.data:JOLTS",
    product="BLS national JOLTS",
    variant="total-nonfarm-eight-SA-series",
    temporal_contract="reference, response observation and MDF availability separate",
    completeness_contract="per-series monthly coverage; no inferred missing values",
    projection_revision="bls-jolts-r1",
    acquisition_mode="bounded_monthly_capture",
    pagination_contract="eight IDs per window; registered 20 years, unregistered 10",
    freshness_contract=(
        "retrieval-bound local changes; P/R only from footnotes; "
        "bounded five-year maintenance implemented; official revision proof pending"
    ),
)
JOLTS_BINDING = replace(
    EMPLOYMENT_BINDING_CANDIDATE,
    identity="mdf.binding.bls.jolts",
    revision="r1",
    source=JOLTS_SOURCE.key,
    compatibility_policy="exact_bls_jolts_national_sa_quantity_and_rate_series",
    mappings=(
        replace(
            EMPLOYMENT_BINDING_CANDIDATE.mappings[0],
            definition=JOLTS_DEFINITION.key,
            identity="bls-national-jolts",
            semantic_revision="r1",
            temporal_revision="jolts-reference-observed-available-r1",
        ),
    ),
    limitations=(
        "definitions_and_release_material_pending",
        "no_provider_fallback",
        "no_official_vintage_reconstruction",
        "automatic_collection_requires_explicit_worker_and_strategy_opt_in",
    ),
)
