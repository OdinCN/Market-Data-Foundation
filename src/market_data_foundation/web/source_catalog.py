"""Explicit code inventory, never a runtime descriptor or a service registry.

Only named, repository-owned contracts are projected. No ledger, transport,
credential loader, scheduler, or host probe is used by this directory.
"""

from typing import Literal

from pydantic import Field

from market_data_foundation.canonical.model import (
    DatasetDefinition,
    SourceBindingRevision,
    SourceOperationRef,
)
from market_data_foundation.domain_contracts.kd import (
    KD_B043,
    KD_B043_CURRENT_RANK,
    KD_B044_DISPOSITION,
)
from market_data_foundation.domain_contracts.macro import (
    BLS_CPI_HEADLINES,
    BLS_CPI_SOURCE_CANDIDATE,
    BLS_PPI_FINAL_DEMAND,
)
from market_data_foundation.historical.bea_pce import (
    PCE_BINDING_CANDIDATE,
    PCE_DEFINITION,
    PCE_SOURCE,
)
from market_data_foundation.historical.bls_cpi_direct import (
    BLS_CPI_DIRECT_BINDING_CANDIDATE,
    BLS_CPI_DIRECT_SOURCE,
    BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
    BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE,
)
from market_data_foundation.historical.bls_employment import (
    BLS_EMPLOYMENT,
    EMPLOYMENT_BINDING_CANDIDATE,
    EMPLOYMENT_SOURCE,
)
from market_data_foundation.historical.bls_ppi_final_demand import (
    PPI_BINDING_CANDIDATE,
    PPI_SOURCE,
)
from market_data_foundation.historical.census_retail import (
    CENSUS_RETAIL,
    RETAIL_BINDING_CANDIDATE,
    RETAIL_SOURCE,
)
from market_data_foundation.historical.consumption_contract import (
    CONSUMPTION_BINDING,
    CONSUMPTION_DEFINITION,
    CONSUMPTION_SOURCE,
)
from market_data_foundation.historical.cpi_history import source_binding
from market_data_foundation.historical.daily_bars import (
    RAW_DAILY_BARS,
    TDX_R1_DAILY_BINDING,
    TDX_R1_DAILY_SOURCE,
)
from market_data_foundation.historical.fomc_statement import (
    STATEMENT_BINDING_CANDIDATE,
    STATEMENT_DOCUMENT,
    STATEMENT_SOURCE,
)
from market_data_foundation.historical.fred_alfred_vintage import (
    VINTAGE_BINDING_CANDIDATE,
    VINTAGE_BUNDLE,
    VINTAGE_SOURCE,
)
from market_data_foundation.historical.sdmx_official import _contracts as sdmx_contracts
from market_data_foundation.historical.tdx_recent_daily import (
    RECENT_BINDING,
    RECENT_SOURCE,
)
from market_data_foundation.web.dto import DTO

MAX_ENTRIES = 32
Domain = Literal["Macro", "TDX", "KD"]


class DirectoryQuery(DTO):
    domain: Domain | None = None


class ContractKeyDTO(DTO):
    identity: str = Field(min_length=1, max_length=256)
    revision: str = Field(min_length=1, max_length=256)


class SourceContractDTO(ContractKeyDTO):
    product: str
    projection_revision: str
    variant: str
    build_revision: str | None
    registry_revision: str | None
    provider_app_revision: str | None
    acquisition_mode: str
    temporal_contract: str
    completeness_contract: str
    pagination_contract: str
    freshness_contract: str
    evidence_capability: list[str] = Field(max_length=8)


class MappingDTO(DTO):
    identity: str
    definition: ContractKeyDTO
    semantic_revision: str
    temporal_revision: str
    evidence_grades: list[str] = Field(max_length=8)
    durable_required: bool
    replayable_required: bool


class BindingContractDTO(ContractKeyDTO):
    source: ContractKeyDTO
    mappings: list[MappingDTO] = Field(min_length=1, max_length=16)
    compatibility_policy: str
    limitations: list[str] = Field(max_length=32)
    silent_fallback: Literal[False] = False


class DirectoryEntryDTO(DTO):
    identity: str = Field(min_length=1, max_length=512)
    domain: Domain
    source_name: str
    component_identity: str
    capability_identity: str
    operation: str
    code_delivery: Literal["DELIVERED", "CONTRACT_ONLY"]
    contract_disposition: Literal[
        "BOUNDED_CONTRACT", "CANDIDATE", "NOT_ADMITTED_SELF_BLOCKED"
    ]
    source_contract: SourceContractDTO | None
    binding_contract: BindingContractDTO | None
    asset: ContractKeyDTO | None
    source_qualified_contract: bool | None
    web_read: Literal["CPI_REQUEST_QUALIFIED", "DOMAIN_SPECIFIC_ONLY", "UNSUPPORTED"]
    source_qualification: Literal["UNKNOWN"] = "UNKNOWN"
    current_binding: Literal["UNKNOWN"] = "UNKNOWN"
    runtime_observation: Literal["UNKNOWN"] = "UNKNOWN"
    local_holding: Literal["UNKNOWN"] = "UNKNOWN"
    reasons: list[str] = Field(min_length=1, max_length=40)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class DirectoryDTO(DTO):
    status: Literal["OK"] = "OK"
    evidence_mode: Literal["CODE_CONTRACTS_ONLY"] = "CODE_CONTRACTS_ONLY"
    scope: Literal["NAMED_R5_I2_ALLOWLIST"] = "NAMED_R5_I2_ALLOWLIST"
    entries: list[DirectoryEntryDTO] = Field(max_length=MAX_ENTRIES)


def _key(record) -> ContractKeyDTO:
    return ContractKeyDTO(identity=record.key.identity, revision=record.key.revision)


def _entry(
    domain: Domain,
    name: str,
    module: str,
    symbol: str,
    source: SourceOperationRef,
    binding: SourceBindingRevision,
    definition: DatasetDefinition,
) -> DirectoryEntryDTO:
    # Pin each row to the real source/revision and exact Definition mapping.
    if (
        binding.source != source.key
        or binding.silent_fallback
        or not binding.mappings
        or any(m.definition != definition.key for m in binding.mappings)
    ):
        raise ValueError("catalog_contract_mismatch")
    return DirectoryEntryDTO(
        identity=f"{source.identity}@{source.contract_revision}",
        domain=domain,
        source_name=name,
        component_identity=f"market_data_foundation.historical.{module}",
        capability_identity=definition.identity,
        operation=source.operation,
        code_delivery="DELIVERED",
        contract_disposition="BOUNDED_CONTRACT",
        source_contract=SourceContractDTO(
            **_key(source).model_dump(),
            product=source.product,
            projection_revision=source.projection_revision,
            variant=source.variant,
            build_revision=source.build_revision,
            registry_revision=source.registry_revision,
            provider_app_revision=source.provider_app_revision,
            acquisition_mode=source.acquisition_mode,
            temporal_contract=source.temporal_contract,
            completeness_contract=source.completeness_contract,
            pagination_contract=source.pagination_contract,
            freshness_contract=source.freshness_contract,
            evidence_capability=sorted(g.value for g in source.evidence_capability),
        ),
        binding_contract=BindingContractDTO(
            **_key(binding).model_dump(),
            source=_key(source),
            mappings=[
                MappingDTO(
                    identity=m.identity,
                    definition=_key(definition),
                    semantic_revision=m.semantic_revision,
                    temporal_revision=m.temporal_revision,
                    evidence_grades=sorted(g.value for g in m.evidence.allowed),
                    durable_required=m.evidence.durable,
                    replayable_required=m.evidence.replayable,
                )
                for m in sorted(binding.mappings, key=lambda m: m.identity)
            ],
            compatibility_policy=binding.compatibility_policy,
            limitations=list(binding.limitations),
        ),
        asset=_key(definition),
        source_qualified_contract=definition.source_qualified,
        web_read=(
            "CPI_REQUEST_QUALIFIED"
            if definition.key == BLS_CPI_HEADLINES.key
            else "DOMAIN_SPECIFIC_ONLY"
        ),
        reasons=["code_contract_is_not_current_qualification_or_activation"],
        evidence_refs=[f"src/market_data_foundation/historical/{module}.py:{symbol}"],
    )


def directory(query: DirectoryQuery) -> DirectoryDTO:
    """Deterministic bounded allowlist; unknown/unlisted contracts are absent."""
    history_source, history_binding = source_binding()
    specs = (
        (
            "Macro",
            "BLS",
            "cpi_history",
            "CPI_HISTORY_INDEX_SOURCE",
            history_source,
            history_binding,
            BLS_CPI_HEADLINES,
        ),
        (
            "Macro",
            "BLS",
            "bls_cpi_direct",
            "BLS_CPI_DIRECT_SOURCE",
            BLS_CPI_DIRECT_SOURCE,
            BLS_CPI_DIRECT_BINDING_CANDIDATE,
            BLS_CPI_HEADLINES,
        ),
        (
            "Macro",
            "BLS",
            "bls_cpi_direct",
            "BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE",
            BLS_CPI_SEPTEMBER_RELEASE_SOURCE_CANDIDATE,
            BLS_CPI_SEPTEMBER_RELEASE_BINDING_CANDIDATE,
            BLS_CPI_HEADLINES,
        ),
        (
            "Macro",
            "BLS",
            "bls_ppi_final_demand",
            "PPI_SOURCE",
            PPI_SOURCE,
            PPI_BINDING_CANDIDATE,
            BLS_PPI_FINAL_DEMAND,
        ),
        (
            "Macro",
            "BLS",
            "bls_employment",
            "EMPLOYMENT_SOURCE",
            EMPLOYMENT_SOURCE,
            EMPLOYMENT_BINDING_CANDIDATE,
            BLS_EMPLOYMENT,
        ),
        (
            "Macro",
            "BEA",
            "bea_consumption",
            "CONSUMPTION_SOURCE",
            CONSUMPTION_SOURCE,
            CONSUMPTION_BINDING,
            CONSUMPTION_DEFINITION,
        ),
        (
            "Macro",
            "BEA",
            "bea_pce",
            "PCE_SOURCE",
            PCE_SOURCE,
            PCE_BINDING_CANDIDATE,
            PCE_DEFINITION,
        ),
        (
            "Macro",
            "Census",
            "census_retail",
            "RETAIL_SOURCE",
            RETAIL_SOURCE,
            RETAIL_BINDING_CANDIDATE,
            CENSUS_RETAIL,
        ),
        (
            "Macro",
            "Federal Reserve",
            "fomc_statement",
            "STATEMENT_SOURCE",
            STATEMENT_SOURCE,
            STATEMENT_BINDING_CANDIDATE,
            STATEMENT_DOCUMENT,
        ),
        (
            "Macro",
            "FRED/ALFRED",
            "fred_alfred_vintage",
            "VINTAGE_SOURCE",
            VINTAGE_SOURCE,
            VINTAGE_BINDING_CANDIDATE,
            VINTAGE_BUNDLE,
        ),
        (
            "TDX",
            "TDX",
            "daily_bars",
            "TDX_R1_DAILY_SOURCE",
            TDX_R1_DAILY_SOURCE,
            TDX_R1_DAILY_BINDING,
            RAW_DAILY_BARS,
        ),
        (
            "TDX",
            "TDX",
            "tdx_recent_daily",
            "RECENT_SOURCE",
            RECENT_SOURCE,
            RECENT_BINDING,
            RAW_DAILY_BARS,
        ),
    )
    entries = [_entry(*spec) for spec in specs]
    for provider in ("ECB", "ESTAT"):
        source, definition, binding = sdmx_contracts(provider)
        entries.append(
            _entry(
                "Macro",
                provider,
                "sdmx_official",
                "_contracts",
                source,
                binding,
                definition,
            )
        )
    for domain, name, symbol, operation, definition, reasons in (
        (
            "Macro",
            "MDD",
            "BLS_CPI_SOURCE_CANDIDATE",
            BLS_CPI_SOURCE_CANDIDATE.operation,
            BLS_CPI_HEADLINES,
            list(BLS_CPI_SOURCE_CANDIDATE.missing_runtime_evidence),
        ),
        (
            "KD",
            "KD",
            "KD_B043",
            KD_B043.operation,
            KD_B043_CURRENT_RANK,
            [
                "kd_runtime_descriptor_required",
                KD_B043.source_variant_basis,
                KD_B043.completeness,
                "sampled_current_state_history_only",
                "no_silent_fallback",
            ],
        ),
        (
            "KD",
            "KD",
            "KD_B044_DISPOSITION",
            KD_B044_DISPOSITION.operation,
            None,
            [KD_B044_DISPOSITION.reason],
        ),
    ):
        module = "macro" if domain == "Macro" else "kd"
        entries.append(
            DirectoryEntryDTO(
                identity=f"market_data_foundation.domain_contracts.{module}:{symbol}",
                domain=domain,
                source_name=name,
                component_identity=f"market_data_foundation.domain_contracts.{module}",
                capability_identity=definition.identity if definition else operation,
                operation=operation,
                code_delivery="CONTRACT_ONLY",
                contract_disposition=(
                    KD_B044_DISPOSITION.status if definition is None else "CANDIDATE"
                ),
                source_contract=None,
                binding_contract=None,
                asset=None if definition is None else _key(definition),
                source_qualified_contract=(
                    None if definition is None else definition.source_qualified
                ),
                web_read="UNSUPPORTED",
                reasons=reasons,
                evidence_refs=[
                    f"src/market_data_foundation/domain_contracts/{module}.py:{symbol}"
                ],
            )
        )
    if len(entries) > MAX_ENTRIES or len({e.identity for e in entries}) != len(entries):
        raise ValueError("catalog_bound_or_identity_invalid")
    return DirectoryDTO(
        entries=sorted(
            (e for e in entries if query.domain is None or e.domain == query.domain),
            key=lambda e: (e.domain, e.source_name, e.component_identity, e.identity),
        )
    )
