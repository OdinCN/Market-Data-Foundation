"""Census M3 source contract; parsed snapshots never establish release actuals.

MDM/NO and NXA/NO, NXA/VS rows observed on DEV for advm3 and m3.
New orders are net of cancellations: signed finite amounts are valid.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.cpi_timeseries import month_at, month_number

Dataset = Literal["advm3", "m3"]
MAX_RESPONSE_BYTES = 1024 * 1024
QUALIFICATION = "parsed_contract_only_not_event_qualified"
SERIES = {
    ("MDM", "NO"): "durable_goods_new_orders",
    ("NXA", "NO"): "nondefense_ex_aircraft_capital_goods_new_orders",
    ("NXA", "VS"): "nondefense_ex_aircraft_capital_goods_shipments",
}
FIELDS = (
    "data_type_code",
    "time_slot_id",
    "seasonally_adj",
    "category_code",
    "cell_value",
    "error_data",
)
HEADERS = (*FIELDS, "time", "us")
REPEATED = {"category_code", "data_type_code", "seasonally_adj", "error_data"}
_NUMBER = re.compile(r"[+-]?[0-9]{1,18}(?:\.[0-9]{1,9})?")


def check(condition, reason):
    require(condition, "census_m3_" + reason)


@dataclass(frozen=True)
class M3Request:
    """Two months maximum; slot is selected in get, never a guessed predicate."""

    dataset: Dataset
    category_code: Literal["MDM", "NXA"]
    start_month: str
    end_month: str
    data_type_code: Literal["NO", "VS"] = "NO"

    def __post_init__(self):
        check(
            type(self.dataset) is str and self.dataset in {"advm3", "m3"},
            "dataset_invalid",
        )
        check(
            type(self.category_code) is str and self.category_code in {"MDM", "NXA"},
            "category_invalid",
        )
        try:
            first, last = month_number(self.start_month), month_number(self.end_month)
        except Exception:
            raise ContractError("census_m3_month_invalid") from None
        check(0 <= last - first <= 1, "month_window_invalid")
        check((self.category_code, self.data_type_code) in SERIES, "type_invalid")

    @property
    def endpoint(self):
        return "https://api.census.gov/data/timeseries/eits/" + self.dataset

    @property
    def report_stage(self):
        return "advance" if self.dataset == "advm3" else "full"

    @property
    def months(self):
        return tuple(
            month_at(n)
            for n in range(
                month_number(self.start_month), month_number(self.end_month) + 1
            )
        )

    @property
    def kinds(self):
        return (self.data_type_code,)

    def persisted(self):
        return {
            "endpoint": self.endpoint,
            "method": "GET",
            "parameters": {
                "get": ",".join(FIELDS),
                "category_code": self.category_code,
                "data_type_code": ",".join(self.kinds),
                "seasonally_adj": "yes",
                "error_data": "no",
                "for": "us:*",
                "time": f"from {self.start_month} to {self.end_month}",
            },
        }


@dataclass(frozen=True)
class M3Cell:
    series: str
    reference_month: str
    category_code: str
    data_type_code: str
    time_slot_id: str | None
    value: Decimal | None
    status: str
    reason: str | None
    source_pointer: str | None
    source_value: str | None
    unit: str = field(default="million_current_usd", init=False)
    frequency: str = field(default="monthly", init=False)
    seasonal_adjustment: str = field(default="SA", init=False)
    annualized: bool = field(default=False, init=False)


@dataclass(frozen=True)
class M3Snapshot:
    request: M3Request
    cells: tuple[M3Cell, ...]
    obtained_at: datetime
    raw_sha256: str
    raw: bytes = field(repr=False)
    qualification: str = field(default=QUALIFICATION, init=False)
    version_basis: str = field(default="current_source_snapshot", init=False)
    official_release_event: None = field(default=None, init=False)
    source_version_date: None = field(default=None, init=False)

    @property
    def binding_evidence(self):
        return "bounded_DEV_rows_observed_single_type_request"

    @property
    def complete(self):
        return all(cell.value is not None for cell in self.cells)


def parse_response(raw, request: M3Request, *, obtained_at, forbidden_secret=None):
    """Validate the observed table contract, preserving every missing target cell.

    Only slot 0 has been observed. It is retained as a source dimension, without
    interpreting it as a release version. Unknown slots fail closed for review.
    Each request selects one type; comma-separated NO,VS returned 204 on DEV.
    """
    check(type(request) is M3Request, "request_invalid")
    check(
        isinstance(obtained_at, datetime) and obtained_at.utcoffset() is not None,
        "aware_capture_required",
    )
    check(type(raw) is bytes and 0 < len(raw) <= MAX_RESPONSE_BYTES, "body_invalid")
    if forbidden_secret is not None:
        check(type(forbidden_secret) is str and bool(forbidden_secret), "key_invalid")
        check(forbidden_secret.encode() not in raw, "credential_echo_forbidden")
    try:
        table = json.loads(raw)
    except Exception:
        raise ContractError("census_m3_json_invalid") from None
    check(type(table) is list and bool(table), "table_invalid")
    header = table[0]
    check(
        type(header) is list
        and all(type(name) is str for name in header)
        and set(header) == set(HEADERS),
        "header_invalid",
    )
    indices = {name: [i for i, v in enumerate(header) if v == name] for name in HEADERS}
    check(
        all(
            len(ix) in ((1, 2) if name in REPEATED else (1,))
            for name, ix in indices.items()
        ),
        "header_invalid",
    )
    expected = {(month, kind) for month in request.months for kind in request.kinds}
    cells = {}
    for n, values in enumerate(table[1:], 1):
        check(
            type(values) is list
            and len(values) == len(header)
            and all(
                type(v) is str or (v is None and header[i] == "cell_value")
                for i, v in enumerate(values)
            ),
            "row_invalid",
        )
        if forbidden_secret:
            check(
                all(forbidden_secret not in v for v in values if isinstance(v, str)),
                "credential_echo_forbidden",
            )
        check(
            all(
                len(ix) == 1 or values[ix[0]] == values[ix[1]]
                for ix in indices.values()
            ),
            "duplicate_dimension_conflict",
        )
        row = {name: values[ix[0]] for name, ix in indices.items()}
        check(
            row["us"] == "1"
            and row["seasonally_adj"] == "yes"
            and row["error_data"] == "no"
            and row["category_code"] == request.category_code
            and row["data_type_code"] in request.kinds,
            "dimensions_invalid",
        )
        check(row["time"] in request.months, "reference_month_invalid")
        check(row["time_slot_id"] == "0", "slot_unrecognized")
        key = row["time"], row["data_type_code"]
        check(key in expected and key not in cells, "duplicate_or_unrequested_cell")
        value = row["cell_value"]
        if value is not None and _NUMBER.fullmatch(value):
            number, status, reason = Decimal(value), "available", None
        elif value in {None, "", "NA", "N/A", "(NA)", "(S)", "(D)"}:
            number, status = None, "missing"
            reason = "source_null" if value is None else "source_missing_marker"
        else:
            raise ContractError("census_m3_value_unrecognized")
        cells[key] = M3Cell(
            SERIES[(request.category_code, key[1])],
            key[0],
            request.category_code,
            key[1],
            row["time_slot_id"],
            number,
            status,
            reason,
            f"/rows/{n}/cell_value",
            value,
        )
    for month, kind in sorted(expected - cells.keys()):
        cells[(month, kind)] = M3Cell(
            SERIES[(request.category_code, kind)],
            month,
            request.category_code,
            kind,
            None,
            None,
            "missing",
            "source_row_absent",
            None,
            None,
        )
    return M3Snapshot(
        request,
        tuple(cells[k] for k in sorted(cells)),
        obtained_at.astimezone(UTC),
        hashlib.sha256(raw).hexdigest(),
        raw,
    )


def month_over_month(snapshot: M3Snapshot, series: str):
    """Same snapshot and series; calculated percent, never an event actual."""
    cells = sorted(
        (c for c in snapshot.cells if c.series == series),
        key=lambda c: c.reference_month,
    )
    check(len(cells) == 2, "mom_pair_required")
    previous, current = cells
    if previous.value is None or current.value is None:
        return None, "input_missing"
    if previous.value == 0:
        return None, "zero_denominator"
    return (current.value / previous.value - 1) * 100, None
