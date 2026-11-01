"""Trusted D-01 package metadata over the existing code-contract inventory.

Package membership is declared here, never inferred from a browser or domain.
The complete code inventory remains available through /api/r5/sources.
"""

from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator

from market_data_foundation.domain_contracts.kd import KD_B043_CURRENT_RANK
from market_data_foundation.domain_contracts.macro import (
    BLS_CPI_HEADLINES,
    BLS_PPI_FINAL_DEMAND,
)
from market_data_foundation.historical.bea_pce import PCE_DEFINITION
from market_data_foundation.historical.consumption_contract import (
    CONSUMPTION_DEFINITION,
)
from market_data_foundation.historical.daily_bars import RAW_DAILY_BARS
from market_data_foundation.historical.employment_contract import BLS_EMPLOYMENT
from market_data_foundation.historical.retail_contract import CENSUS_RETAIL
from market_data_foundation.web.dto import DTO
from market_data_foundation.web.source_catalog import (
    ContractKeyDTO,
    DirectoryDTO,
    DirectoryEntryDTO,
    DirectoryQuery,
    directory,
)

ContractRef = ContractKeyDTO
Permission = Literal["allowed", "denied", "unknown"]


class PackageFailure(DTO):
    status: Literal["UNKNOWN", "UNAVAILABLE", "REJECTED", "CONFLICT"]
    classification: str
    effect: Literal["NONE", "UNKNOWN"] = "NONE"


class PackageError(Exception):
    """Only public classifications; raw exceptions never enter HTTP responses."""

    def __init__(
        self, classification, http_status=503, status="UNAVAILABLE", effect="NONE"
    ):
        self.http_status = http_status
        self.failure = PackageFailure(
            status=status, classification=classification, effect=effect
        )
        super().__init__(classification)


def ref(definition) -> ContractRef:
    return ContractRef(identity=definition.identity, revision=definition.revision)


def key(reference):
    return reference.identity, reference.revision


class CapabilityView(DTO):
    capability_id: str
    title: str
    definition_ref: ContractRef
    required_sources: list[str]


@dataclass(frozen=True)
class PackageMetadata:
    package_id: str
    name: str
    domain: Literal["Macro", "TDX", "KD"]
    description: str
    capabilities: tuple[CapabilityView, ...]


# Named product scope: the other delivered code contracts are still code
# inventory, not silently enrolled capabilities or runtime-ready assets.
PACKAGES = (
    PackageMetadata(
        "macro",
        "宏观数据接入包",
        "Macro",
        "CPI、PPI、PCE价格指数、美国消费与收入、就业与零售销售；启用配置不代表来源准入或当前可执行。",
        (
            CapabilityView(
                capability_id="cpi",
                title="美国 CPI",
                definition_ref=ref(BLS_CPI_HEADLINES),
                required_sources=["BLS"],
            ),
            CapabilityView(
                capability_id="ppi",
                title="美国 PPI",
                definition_ref=ref(BLS_PPI_FINAL_DEMAND),
                required_sources=["BLS"],
            ),
            CapabilityView(
                capability_id="employment",
                title="美国就业",
                definition_ref=ref(BLS_EMPLOYMENT),
                required_sources=["BLS"],
            ),
            CapabilityView(
                capability_id="retail",
                title="美国零售销售",
                definition_ref=ref(CENSUS_RETAIL),
                required_sources=["Census"],
            ),
            CapabilityView(
                capability_id="consumption",
                title="美国消费与收入",
                definition_ref=ref(CONSUMPTION_DEFINITION),
                required_sources=["BEA"],
            ),
            CapabilityView(
                capability_id="pce",
                title="美国 PCE价格指数",
                definition_ref=ref(PCE_DEFINITION),
                required_sources=["BEA"],
            ),
        ),
    ),
    PackageMetadata(
        "tdx",
        "TDX 日线接入包",
        "TDX",
        "原始日线；保留两个具名来源变体，禁止静默互换。",
        (
            CapabilityView(
                capability_id="daily_raw",
                title="原始日线",
                definition_ref=ref(RAW_DAILY_BARS),
                required_sources=["TDX"],
            ),
        ),
    ),
    PackageMetadata(
        "kd",
        "KD 当前排名接入包",
        "KD",
        "B043 当前窗口合同；运行 descriptor 尚未装配。",
        (
            CapabilityView(
                capability_id="current_rank",
                title="当前排名",
                definition_ref=ref(KD_B043_CURRENT_RANK),
                required_sources=["KD"],
            ),
        ),
    ),
)


class PackageCatalog:
    def __init__(self, inventory: DirectoryDTO | None = None):
        self.inventory = (
            inventory if inventory is not None else directory(DirectoryQuery())
        )
        self.packages = {p.package_id: p for p in PACKAGES}
        self.entries = {}
        for package in PACKAGES:
            for cap in package.capabilities:
                rows = [
                    e for e in self.inventory.entries if e.asset == cap.definition_ref
                ]
                bound = [r for r in rows if r.binding_contract is not None]
                if not rows or set(cap.required_sources) != {
                    r.source_name for r in (bound or rows)
                }:
                    raise PackageError("package_catalog_mapping_invalid")
                for row in rows:
                    binding = row.binding_contract
                    if binding and (
                        row.source_contract is None
                        or binding.source
                        != ContractRef(
                            identity=row.source_contract.identity,
                            revision=row.source_contract.revision,
                        )
                        or binding.silent_fallback
                        or any(
                            m.definition != cap.definition_ref for m in binding.mappings
                        )
                    ):
                        raise PackageError("package_catalog_mapping_invalid")
                self.entries[package.package_id, cap.capability_id] = rows

    def package(self, package_id):
        if package_id not in self.packages:
            raise PackageError("unknown_package", 404, "REJECTED")
        return self.packages[package_id]

    def validate(self, package_id, enabled_ids):
        package = self.package(package_id)
        if len(enabled_ids) != len(set(enabled_ids)):
            raise PackageError("duplicate_capability", 422, "REJECTED")
        if not set(enabled_ids) <= {c.capability_id for c in package.capabilities}:
            raise PackageError("illegal_capability", 422, "REJECTED")


class KeepChange(DTO):
    source_id: str = Field(min_length=1, max_length=64)
    credential_action: Literal["keep"]


class ReplaceChange(DTO):
    source_id: str = Field(min_length=1, max_length=64)
    credential_action: Literal["replace"]
    credential_value: SecretStr = Field(min_length=1, max_length=4096, repr=False)

    @field_validator("credential_value")
    @classmethod
    def nonblank_value(cls, value):
        raw = value.get_secret_value()
        try:
            raw.encode("utf-8")
        except UnicodeError:
            raise ValueError("invalid_credential") from None
        if not raw.strip() or any(ord(c) < 32 or ord(c) == 127 for c in raw):
            raise ValueError("invalid_credential")
        return value


class SaveCommand(DTO):
    expected_revision: str | None = Field(pattern=r"^r[1-9][0-9]*$", max_length=32)
    enabled_capability_ids: list[str] = Field(max_length=32)
    source_changes: list[
        Annotated[KeepChange | ReplaceChange, Field(discriminator="credential_action")]
    ] = Field(default_factory=list, max_length=8)


class CredentialState(DTO):
    source_id: str
    credential_state: Literal["configured", "missing", "unknown"]
    authentication: Literal["unknown"] = "unknown"
    replace_supported: bool = False


class HistoryEvidence(DTO):
    definition_ref: ContractRef
    holding: Literal["present", "absent", "unknown"]
    metadata_view_permission: Permission = "unknown"
    read_permission: Permission = "unknown"
    publication_readable: bool | None = None


class PackageHistory(DTO):
    completeness: Literal["COMPLETE", "UNKNOWN"] = "UNKNOWN"
    history: list[HistoryEvidence] = Field(default_factory=list)


class Eligibility(DTO):
    allowed: bool | None
    reasons: list[str] = Field(min_length=1)


class AssetRevision(DTO):
    definition_ref: ContractRef
    capability_enabled: bool
    visibility_basis: list[Literal["ENABLED_MAPPING", "RETAINED_HISTORY"]]
    source_entries: list[DirectoryEntryDTO]
    metadata_view_permission: Permission
    local_holding: Literal["UNKNOWN", "ABSENT", "PRESENT"]
    local_read: Eligibility
    upstream_read: Eligibility
    collect: Eligibility
    plan: Eligibility


class AssetView(DTO):
    identity: str
    revisions: list[AssetRevision]


class PackageView(DTO):
    environment_id: str
    package_id: str
    name: str
    domain: Literal["Macro", "TDX", "KD"]
    description: str
    capabilities: list[CapabilityView]
    configuration_state: Literal["PRESENT", "ABSENT"]
    config_revision: str | None
    enabled_capability_ids: list[str]
    required_sources: list[str]
    credential_states: list[CredentialState]
    history_completeness: Literal["COMPLETE", "UNKNOWN"]
    assets: list[AssetView]
    write_eligible: bool | None
    write_reasons: list[str] = Field(min_length=1)


class PackagesView(DTO):
    status: Literal["OK"] = "OK"
    environment_id: str
    packages: list[PackageView]
