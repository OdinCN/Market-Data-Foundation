"""Invented one-day physical-port fixture; never a provider or admission claim."""

from dataclasses import replace
from datetime import UTC, date, datetime

from market_data_foundation.canonical.market_material import (
    DailyBar,
    FieldSemantics,
    MaterialReceipt,
    MemoryMaterialBytes,
    NormalizationContract,
    RowLocator,
    SyntheticDailyDecoder,
    assess_daily,
    byte_digest,
)
from market_data_foundation.canonical.market_persistence import required_admissions
from market_data_foundation.canonical.market_projection import MonthKey
from market_data_foundation.canonical.market_publication import (
    MarketPublicationRequest,
    MarketVersion,
)
from market_data_foundation.canonical.model import Decision, Key, Kind
from market_data_foundation.domain_contracts.market_coverage import (
    CoverageClass,
    PointEvidence,
)
from market_data_foundation.domain_contracts.market_planning import (
    DAILY_DEFINITIONS,
    GridPoint,
    PublicationUnit,
)
from market_data_foundation.domain_contracts.market_universe import (
    DateWindow,
    EvidenceMode,
    InstrumentIdentity,
    canonical_bytes,
)
from market_data_foundation.qualification.contracts import Capability


def synthetic_fixture(run_id, locator_for):
    at = datetime(2024, 2, 8, tzinfo=UTC)
    day = date(2024, 1, 1)
    source = Key(Kind.SOURCE, "i3-invented-source", "r1")
    binding = Key(Kind.BINDING, "i3-invented-binding", "r1")
    daily = DAILY_DEFINITIONS[0]
    instrument = InstrumentIdentity("i3.synthetic", "invented")
    contract = NormalizationContract(
        source,
        binding,
        "invented-mapping-r1",
        daily,
        Capability.BARS_DAILY,
        "synthetic.daily",
        "invented-upstream-r1",
        "synthetic-json-r2",
        (("period", "daily"),),
        tuple(
            FieldSemantics(
                name,
                "decimal-text",
                unit,
                name,
                unit,
                "EXPLICIT",
                "synthetic-field",
                "identity.synthetic.r1",
            )
            for name, unit in (
                ("open", "CNY"),
                ("high", "CNY"),
                ("low", "CNY"),
                ("close", "CNY"),
                ("volume", "shares"),
                ("amount", "CNY"),
            )
        ),
    )
    decoder = SyntheticDailyDecoder(contract)
    raw = canonical_bytes(DailyBar(day, "10", "12", "9", "11", "0", "0"))
    receipt = MaterialReceipt(
        Key(Kind.MATERIAL, run_id + ".material"),
        run_id,
        "invented-shard",
        "invented-attempt",
        "invented-request",
        source,
        binding,
        "invented-mapping-r1",
        daily.definition,
        instrument,
        Capability.BARS_DAILY,
        "synthetic.daily",
        "invented-upstream-r1",
        "synthetic-json-r2",
        contract.request_fields,
        EvidenceMode.SYNTHETIC,
        byte_digest(raw),
        len(raw),
        locator_for(source, binding, contract, raw),
        ("invented-page",),
        (RowLocator("invented-row", "invented-page", 0, len(raw), byte_digest(raw)),),
        at,
        at,
        at,
        fixture_id="i3-physical-synthetic-r1",
        requested_at=at,
    )
    unit = PublicationUnit(
        daily.definition,
        "invented-universe",
        "invented-calendar",
        instrument,
        "Asia/Shanghai",
        DateWindow(day, date(2024, 1, 2)),
    )
    evidence = (
        PointEvidence(
            GridPoint(instrument, day),
            CoverageClass.OBSERVED_VALID,
            ("invented-evidence",),
            (receipt.shard,),
            (receipt.attempt,),
            (receipt.request,),
            receipt.key,
            "invented-row",
        ),
    )
    store = MemoryMaterialBytes()
    store.put(receipt.locator, raw)
    assessment = assess_daily(unit, (receipt,), evidence, decoder, store, at)
    version = MarketVersion(
        Key(Kind.VERSION, run_id + ".version"), assessment, at, ("temporary",)
    )
    version = replace(version, admission_subjects=required_admissions(version))
    request = MarketPublicationRequest(
        Key(Kind.PUBLICATION, run_id + ".publication"),
        run_id + ".occurrence",
        version.key,
        Decision.PUBLISH,
        None,
        ("synthetic-dev-only",),
        "synthetic-rule-r1",
    )
    return (
        receipt,
        raw,
        decoder,
        version,
        request,
        MonthKey(daily.definition, instrument, 2024, 1),
    )
