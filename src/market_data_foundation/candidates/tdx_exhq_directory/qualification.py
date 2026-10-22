"""Use the existing capability/fixture/observation contract, always offline."""

from collections.abc import Callable
from datetime import datetime
from time import perf_counter
from typing import Any

from market_data_foundation.qualification.contracts import Capability, Fixture
from market_data_foundation.qualification.harness import (
    Binding,
    Endpoint,
    Evidence,
    observe,
)

from .adapter import OfflineReceipt, build_directory
from .schema import DECODE_REVISION, require


def observe_directory(
    fixture: Fixture,
    markets: OfflineReceipt,
    count_before: OfflineReceipt,
    pages: tuple[OfflineReceipt, ...],
    count_after: OfflineReceipt,
    *,
    observed_at: datetime,
    timer: Callable[[], float] = perf_counter,
) -> dict[str, Any]:
    """Return an observation in memory; never writes raw material or observations.

    Fixture symbols, if present, are exact ``market:category:code`` strings.
    Complete refers only to bounded count/page and requested-symbol consistency.
    No callable provider/transport is accepted, and no live binding is constructed.
    """
    require(type(fixture) is Fixture, "qualification_fixture_required")
    binding = Binding(
        provider="tdx.exhq",
        implementation="market_data_foundation.candidates.tdx_exhq_directory",
        version=DECODE_REVISION,
        capability=Capability.SECURITY_UNIVERSE,
        mode="offline",
        endpoint=Endpoint(unobservable_reason="offline capture; no endpoint contacted"),
    )

    def execute(selected: Fixture) -> Evidence:
        require(
            selected.start is None and selected.end is None,
            "snapshot_cannot_claim_history",
        )
        snapshot = build_directory(markets, count_before, pages, count_after)
        require(
            all(page.request.count <= selected.page_size for page in pages),
            "fixture_page_size_exceeded",
        )
        symbols = tuple(
            f"{row.market}:{row.category}:{row.code}" for row in snapshot.instruments
        )
        complete = set(selected.symbols).issubset(symbols)
        return Evidence(
            success=complete,
            returned_symbols=symbols,
            row_count=snapshot.total_count,
            complete=complete,
            missing_fields=("currency", "lot", "multiplier", "expiry", "calendar"),
            pages=len(pages),
            exhausted=None,
            units={"economic_units": "UNKNOWN"},
            semantics={
                "scope": snapshot.completeness,
                "membership": "snapshot; historical validity UNKNOWN",
                "directory_hash": snapshot.directory_hash,
                "mapping_hash": snapshot.mapping_hash,
                "source_qualification": "UNKNOWN",
                "source_rights": "UNKNOWN",
                "market_association": (
                    "UNKNOWN"
                    if snapshot.unresolved_associations
                    else "EXACT_RESPONSE_MARKET_CATEGORY"
                ),
            },
            endpoint=binding.endpoint,
        )

    return observe(binding, fixture, execute, timestamp=observed_at, timer=timer)
