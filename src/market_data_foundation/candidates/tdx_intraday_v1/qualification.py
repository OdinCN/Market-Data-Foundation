"""Qualification contract wrapper; structural success never grants Source admission."""

from datetime import datetime

from market_data_foundation.qualification.contracts import Fixture
from market_data_foundation.qualification.harness import (
    Binding,
    Endpoint,
    Evidence,
    observe,
)

from .decode import decode
from .model import REVISION, Material, require


def observe_material(
    material: Material, fixture: Fixture, *, observed_at: datetime
) -> dict:
    require(
        fixture.capability == material.request.capability, "fixture_capability_mismatch"
    )
    req = material.request
    symbol = (
        f"{req.market}:{req.category}:{req.code}"
        if req.operation.value.startswith("Ex")
        else req.code
    )
    binding = Binding(
        "tdx.i4",
        "source-native-payload",
        REVISION,
        fixture.capability,
        "offline",
        Endpoint(unobservable_reason="retained/offline input; no connection"),
    )

    def execute(_: Fixture) -> Evidence:
        page = decode(material)
        require(fixture.symbols == (symbol,), "exact_fixture_symbol_required")
        require(len(page.rows) <= fixture.page_size, "fixture_row_budget")
        if fixture.start:
            require(
                bool(req.date)
                and fixture.start.strftime("%Y%m%d")
                <= req.date
                <= fixture.end.strftime("%Y%m%d"),
                "fixture_date_mismatch",
            )
        return Evidence(
            success=True,
            returned_symbols=(symbol,) if page.rows else (),
            row_count=len(page.rows),
            complete=None,
            pages=1,
            exhausted=None,
            units={"economic_units": "UNKNOWN_UNLESS_OPERATION_FIELD_EVIDENCE"},
            semantics={
                "qualification": "STRUCTURE_ONLY_NOT_SOURCE_ADMISSION",
                "source_freshness": "UNKNOWN",
                "findings": ",".join(page.findings),
            },
        )

    return observe(binding, fixture, execute, timestamp=observed_at)
