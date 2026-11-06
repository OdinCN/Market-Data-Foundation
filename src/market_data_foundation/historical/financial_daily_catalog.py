"""Named daily paths shared by explicit manual jobs and periodic maintenance."""

from market_data_foundation.canonical.model import (
    EvidenceRequirement,
    Grade,
    OutputMapping,
    SourceBindingRevision,
    SourceOperationRef,
)
from market_data_foundation.historical.financial_daily import DEFINITION

# Keep installed binding identities for both callers; never imply an all-FRED source.
PATHS = (
    ("board", "Federal Reserve Board", "policy effective lower/upper projection"),
    ("nyfed", "New York Fed", "EFFR and SOFR official daily observations"),
    ("treasury", "US Treasury", "2Y/10Y nominal and 10Y real daily curves"),
    ("derived", "MDF", "same-day, same-cutoff DGS10 minus DFII10; both input versions"),
    (
        "archive",
        "Credit archive",
        "Owner-pinned PStoclet GitHub archive; ICE IG/HY indices",
    ),
    ("fred", "FRED", "ICE BAMLC0A0CM/BAMLH0A0HYM2 explicit tail and incremental reads"),
)


def contracts():
    result = []
    for slug, distributor, product in PATHS:
        source = SourceOperationRef(
            "mdf.source.financial-conditions.manual." + slug,
            "r1",
            "market-data-foundation",
            distributor,
            product,
            "financial-daily-manual-r1",
            slug,
            None,
            None,
            None,
            "read",
            "explicit_manual_or_periodic_daily_capture",
            DEFINITION.scope,
            "observation day, source availability and MDF cutoff are distinct",
            "per-series partial results; absent values remain absent",
            "bounded source segments within one resumable daily job",
            "manual init/update and source-group periodic maintenance; "
            "actual strategy and worker state observed separately",
            frozenset({Grade.ORIGINAL}),
        )
        binding = SourceBindingRevision(
            "mdf.binding.financial-conditions.manual." + slug,
            "r1",
            source.key,
            (
                OutputMapping(
                    slug,
                    DEFINITION.key,
                    "r1",
                    "daily-observed-available-r1",
                    DEFINITION.scope,
                    EvidenceRequirement(frozenset({Grade.ORIGINAL}), True, True),
                ),
            ),
            "exact named path, observation date, units and retained evidence",
            (
                "internal personal noncommercial DEV only",
                "no inferred historical first availability",
                "MDF derived is not a FRED capture",
                "no silent fallback",
            ),
            False,
        )
        result.append((distributor, source, binding))
    return tuple(result)
