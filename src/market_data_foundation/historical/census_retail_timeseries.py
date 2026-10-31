"""Pure MARTS level/rate projection from one explicit input snapshot."""

from dataclasses import dataclass
from decimal import ROUND_HALF_EVEN, Decimal, localcontext

from market_data_foundation.historical.census_retail import (
    CATEGORY_CODES,
    FORMULA_REVISION,
)
from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailCell,
    RetailInputSnapshot,
    _range,
    _require,
)
from market_data_foundation.historical.cpi_timeseries import month_at, month_number

HORIZONS = (1, 3, 6, 12)


@dataclass(frozen=True)
class RetailCalculation:
    value: Decimal | None
    reason: str | None
    inputs: tuple[RetailCell, ...]
    formula: str
    unit: str


@dataclass(frozen=True)
class RetailMonth:
    month: str
    snapshot_identity: str
    source_cells: tuple[RetailCell, ...]
    headline_level: RetailCalculation
    control_level: RetailCalculation
    official_headline_mpcsm: RetailCell
    headline_rates: dict[int, RetailCalculation]
    control_rates: dict[int, RetailCalculation]


def calculate_snapshot(snapshot: RetailInputSnapshot) -> tuple[RetailMonth, ...]:
    """Merge the full comparison buffer before deriving unannualized ratio changes.

    Cells retain source status, percent MPCSM and raw lineage; controls never
    become provider fields. Multi-batch retrieval is a cohort, not an atomic
    official vintage. Historical cutoffs must select the input cohort first.
    """
    plan = snapshot.plan
    _require(
        tuple(batch.request for batch in snapshot.batches) == plan.requests,
        "snapshot_batches_mismatch",
    )
    cells = {}
    for batch in snapshot.batches:
        for cell in batch.cells:
            _require(cell.key not in cells, "snapshot_duplicate_target")
            cells[cell.key] = cell
    first, last = _range(plan.input_start_month, plan.end_month)
    expected = {
        (month_at(n), category, "SM")
        for n in range(first, last + 1)
        for category in CATEGORY_CODES
    } | {(month_at(n), "44X72", "MPCSM") for n in range(first, last + 1)}
    _require(set(cells) == expected, "snapshot_matrix_mismatch")

    def level(month, control):
        if (month, "44X72", "SM") not in cells:
            return RetailCalculation(
                None, "comparison_outside_input", (), "SM", "million_current_usd"
            )
        inputs = tuple(
            cells[month, category, "SM"]
            for category in (CATEGORY_CODES if control else ("44X72",))
        )
        formula = FORMULA_REVISION if control else "source_SM_44X72"
        if any(cell.value is None for cell in inputs):
            return RetailCalculation(
                None,
                "source_operand_unavailable",
                inputs,
                formula,
                "million_current_usd",
            )
        value = inputs[0].value
        if control:
            value -= sum((cell.value for cell in inputs[1:]), Decimal(0))
        return RetailCalculation(value, None, inputs, formula, "million_current_usd")

    def rate(current, previous, horizon):
        inputs = current.inputs + previous.inputs
        reason = (
            "current_level_unavailable"
            if current.value is None
            else "comparison_level_unavailable"
            if previous.value is None
            else "comparison_level_zero"
            if previous.value == 0
            else None
        )
        value = None if reason else current.value / previous.value - 1
        return RetailCalculation(
            value, reason, inputs, f"level[t]/level[t-{horizon}]-1", "ratio"
        )

    identity, result = snapshot.identity, []
    with localcontext() as context:
        context.prec, context.rounding = 28, ROUND_HALF_EVEN
        for n in range(month_number(plan.start_month), last + 1):
            month = month_at(n)
            headline, control = level(month, False), level(month, True)
            result.append(
                RetailMonth(
                    month,
                    identity,
                    tuple(cells[month, category, "SM"] for category in CATEGORY_CODES)
                    + (cells[month, "44X72", "MPCSM"],),
                    headline,
                    control,
                    cells[month, "44X72", "MPCSM"],
                    {
                        h: rate(headline, level(month_at(n - h), False), h)
                        for h in HORIZONS
                    },
                    {
                        h: rate(control, level(month_at(n - h), True), h)
                        for h in HORIZONS
                    },
                )
            )
    return tuple(result)
