"""Explicit I1 physical address binding for the simple I3 material store."""

from market_data_foundation.domain_contracts.market_universe import content_id

from .market_vertical import PhysicalIntake


def simple_intake(store, *, level: str) -> PhysicalIntake:
    """Bind the original receipt before retain; no IO and no receipt rewriting."""

    def locator(receipt, physical_level):
        return store.locator(
            physical_level,
            content_id(receipt.source),
            content_id(receipt.binding),
            content_id(
                (
                    receipt.mapping_revision,
                    receipt.upstream_revision,
                    receipt.decode_revision,
                )
            ),
            receipt.digest,
        )

    return PhysicalIntake(level, locator)
