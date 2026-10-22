"""Pinned v1.0.0 native Workday fields, retained without calendar inference."""

from dataclasses import dataclass

from market_data_foundation.canonical.model import require

from .model import MaterialReceipt, State
from .parser import fields, label, verified_json

TDX_VERSION = "v1.0.0"
TDX_COMMIT = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
TDX_FIELD_REF = "workday.go:256-260/WorkdayModel"


@dataclass(frozen=True)
class TdxWorkdayObservation:
    receipt: MaterialReceipt
    native_id: int
    native_unix: int
    native_date: str
    is_workday: bool | None
    raw: bytes
    calendar_state: State = State.UNKNOWN
    clock_semantics: str = "UNKNOWN"


def parse_tdx_workday(raw: bytes, receipt: MaterialReceipt) -> TdxWorkdayObservation:
    """is_workday is a caller-retained Is result, not a WorkdayModel native field."""
    require(
        receipt.upstream_version == TDX_VERSION
        and receipt.upstream_revision == TDX_COMMIT,
        "tdx_version_pin_required",
    )
    require(
        receipt.producer_operation == "tdx.Workday", "tdx_workday_operation_required"
    )
    row = fields(verified_json(raw, receipt), {"id", "unix", "date"}, {"is_workday"})
    require(
        all(
            type(row[name]) is int and -(2**63) <= row[name] < 2**63
            for name in ("id", "unix")
        ),
        "tdx_int64_required",
    )
    label(row["date"])
    flag = row.get("is_workday")
    require(flag is None or type(flag) is bool, "tdx_bool_required")
    return TdxWorkdayObservation(
        receipt, row["id"], row["unix"], row["date"], flag, raw
    )
