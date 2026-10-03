"""R1 evidence checks, not a production provider adapter or runtime binding."""

import math
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any, Literal

VERSIONS = {"tdx-python": "0.1.6", "pytdxdata": "0.6.1", "mootdx-plus": "0.12.0"}


def daily_rows(
    implementation: str,
    rows: Sequence[Mapping[str, Any]],
    *,
    kind: Literal["stock", "etf", "index"],
) -> list[dict[str, Any]]:
    """Check an explicit SDK unit mapping while preserving provider row order.

    Category 9 Go prices/amounts are milli-CNY; stock/ETF volume is truncated
    lots, but Go index volume is already shares. Category 4 pytdxdata volume
    is shares. Category 9 mootdx-plus volume is lots for all tested kinds.
    Lot conversion cannot recover precision lost by the SDK.
    """
    if implementation not in VERSIONS or kind not in {"stock", "etf", "index"}:
        raise ValueError("R1 requires an explicit qualified SDK and instrument kind")
    result = []
    for row in rows:
        if implementation == "tdx-python":
            day = date.fromisoformat(row["time"][:10])
            scale = 1000
            volume = row["volume"] * (1 if kind == "index" else 100)
        else:
            day = date(int(row["year"]), int(row["month"]), int(row["day"]))
            scale = 1
            volume = row["vol"] * (100 if implementation == "mootdx-plus" else 1)
        normalized = {
            "date": day.isoformat(),
            **{field: row[field] / scale for field in ("open", "high", "low", "close")},
            "volume": volume,
            "amount": row["amount"] / scale,
        }
        numbers = [v for k, v in normalized.items() if k != "date"]
        if not all(math.isfinite(v) for v in numbers):
            raise ValueError("nonfinite daily evidence")
        if normalized["volume"] < 0 or normalized["amount"] < 0:
            raise ValueError("negative quantity evidence")
        if not (
            normalized["low"]
            <= min(normalized["open"], normalized["close"])
            <= max(normalized["open"], normalized["close"])
            <= normalized["high"]
        ):
            raise ValueError("inconsistent OHLC evidence")
        result.append(normalized)
    days = [r["date"] for r in result]
    if len(set(days)) != len(days):
        raise ValueError("duplicate local daily dates")
    if days != sorted(days):
        raise ValueError("daily response is not ascending")
    return result


def cash_dividend_per_share(implementation: str, action: Mapping[str, Any]) -> float:
    """Only category 1 cash dividends; other event fields need separate mappings."""
    if action["category"] != 1:
        raise ValueError("cash dividend requires category 1")
    if implementation == "tdx-python":
        return action["c1"] / 10
    if implementation == "mootdx-plus":
        return action["fenhong"] / 10
    if implementation == "pytdxdata":
        return action["fenhong"]
    raise ValueError("unknown implementation; no fallback mapping")
