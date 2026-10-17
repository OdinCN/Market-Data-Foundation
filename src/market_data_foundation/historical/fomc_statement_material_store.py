"""Provider-specific identity for T07 official HTML raw material only.

T07 admits up to 5 MiB per raw body. Base CPI MaterialIdentity retains its
100,000-byte boundary and unchanged digest, bucket, key and locator allowlist.
"""

from dataclasses import dataclass

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.bls_cpi_material_store import MaterialIdentity
from market_data_foundation.historical.fomc_statement_contract import MAX_BODY_BYTES


@dataclass(frozen=True)
class StatementMaterialIdentity(MaterialIdentity):
    """Bounded T07 official calendar or Statement HTML raw identity."""

    def __post_init__(self) -> None:
        require(
            type(self.size) is int and 0 < self.size <= MAX_BODY_BYTES,
            "statement_material_size_invalid",
        )
        # Reuse the unchanged digest/bucket/key allowlist with a bounded probe.
        address = MaterialIdentity(self.sha256, 1, self.bucket, self.key)
        object.__setattr__(self, "key", address.key)
