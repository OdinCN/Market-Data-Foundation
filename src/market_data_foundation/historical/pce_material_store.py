"""Named, bounded sanitized PCE carrier using the existing verified raw store.

This identity is explicitly selected by PCE paths. CPI's MaterialIdentity keeps
its 100,000-byte limit. Locators and writer-lock keys remain content addressed:
the same bytes have the same address, never semantic-equivalence aliases.
"""

from dataclasses import dataclass

from market_data_foundation.canonical.model import require
from market_data_foundation.historical.bea_pce import MAX_RESPONSE_BYTES
from market_data_foundation.historical.bls_cpi_material_store import MaterialIdentity


@dataclass(frozen=True)
class PceMaterialIdentity(MaterialIdentity):
    """At most 2,000,000 sanitized bundle bytes; never BEA original identity."""

    def __post_init__(self) -> None:
        require(
            type(self.size) is int and 0 < self.size <= MAX_RESPONSE_BYTES,
            "pce_material_size_invalid",
        )
        # Reuse the unchanged digest/bucket/key allowlist, with a bounded probe.
        address = MaterialIdentity(self.sha256, 1, self.bucket, self.key)
        object.__setattr__(self, "key", address.key)
