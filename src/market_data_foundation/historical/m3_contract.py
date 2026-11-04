"""Census M3 continuous asset; current snapshots do not establish event actuals."""

from market_data_foundation.canonical.model import (
    DatasetDefinition,
    EvidenceRequirement,
    Grade,
    OutputMapping,
    Scope,
    SourceBindingRevision,
    SourceOperationRef,
)

M3_DEFINITION = DatasetDefinition(
    "mdf.macro.census.durable-capital-goods",
    "r1",
    "Census M3 durable and nondefense ex-aircraft capital goods",
    "美国耐用品与核心资本品",
    False,
    "series + reference month + separate m3/advm3 dataset",
    "monthly seasonally adjusted million current USD; orders and shipments separate",
    "reference month, MDF observed/available and event identity remain distinct",
    "changed economic value; current source snapshot is not an official first release",
    "three series; gaps remain explicit",
    Scope(),
)
M3_SOURCE = SourceOperationRef(
    "mdf.source.census.m3.capital-goods",
    "r1",
    "market-data-foundation",
    "Census",
    "timeseries/eits/advm3 and timeseries/eits/m3",
    "census-m3-continuous-r1",
    "separately-selected-advance-or-full",
    None,
    None,
    None,
    "read",
    "bounded_monthly_capture",
    Scope(),
    "reference month and capture time; no inferred official event version",
    "MDM/NO, NXA/NO and NXA/VS each requested separately; missing retained",
    "one series and up to two months per request; bounded fragment plan",
    "current source snapshot, not first-release vintage",
    frozenset({Grade.ORIGINAL}),
)
M3_BINDING = SourceBindingRevision(
    "mdf.binding.census.m3.capital-goods",
    "r1",
    M3_SOURCE.key,
    (
        OutputMapping(
            "census-m3",
            M3_DEFINITION.key,
            "r1",
            "monthly-observed-available-r1",
            Scope(),
            EvidenceRequirement(frozenset({Grade.ORIGINAL}), True, True),
        ),
    ),
    "explicit dataset, series, monthly SA and nominal USD",
    ("no_provider_fallback", "advance_and_full_separate", "no_inferred_event_binding"),
    silent_fallback=False,
)
