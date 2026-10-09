"""Exact-one P6 count-30 probe; only the explicitly authorized TDX SDK."""

import argparse
import importlib.metadata
import json
import logging
import platform
from datetime import date
from pathlib import Path

from market_data_foundation.qualification import (
    Binding,
    Capability,
    Endpoint,
    Evidence,
    Fixture,
    observe,
)
from market_data_foundation.qualification.tdx_semantics import daily_rows

HOST = "139.9.52.158"
FIXTURE = "mdf-p6-real-cognition-r1-recent-count30"
REQUEST = {
    "api": "get_kline",
    "period": 9,
    "symbol": "sh600000",
    "offset": 0,
    "count": 30,
    "redial": False,
    "dial_ms": 2000,
}


def probe(output: Path, *, allow_live: bool = False) -> dict:
    if not allow_live:
        raise PermissionError("explicit live opt-in required")
    if platform.python_version() != "3.14.8":
        raise ValueError("qualified Python required")
    if importlib.metadata.version("tdx-python") != "0.1.6":
        raise ValueError("qualified SDK required")
    root = Path(__file__).resolve().parents[1] / "artifacts/tdx-r1"
    output = output.resolve()
    if not output.is_relative_to(root):
        raise ValueError("ignored evidence directory required")
    output.mkdir(parents=True, exist_ok=True)
    fixture = Fixture(
        FIXTURE, Capability.BARS_DAILY, ("600000.XSHG",), None, None, 30, {}
    )
    binding = Binding(
        "TDX", "tdx-python", "0.1.6", fixture.capability, "live", Endpoint(HOST, 7709)
    )

    def execute(_fixture):
        from tdx_py import api

        client = api.dial(f"{HOST}:7709", 2000, False)
        try:
            page = client.get_kline(9, "sh600000", 0, 30)
            raw = [
                {
                    name: getattr(row, name)
                    for name in (
                        "time",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                        "amount",
                    )
                }
                for row in page.list
            ]
        finally:
            client.close()
        (output / "raw.json").write_text(json.dumps(raw, indent=2) + "\n")
        rows = daily_rows("tdx-python", raw, kind="stock")
        return Evidence(
            success=1 <= len(rows) <= 30,
            returned_symbols=fixture.symbols if rows else (),
            row_count=len(rows),
            complete=False,
            pages=1,
            exhausted=None,
            observed_start=date.fromisoformat(rows[0]["date"]) if rows else None,
            observed_end=date.fromisoformat(rows[-1]["date"]) if rows else None,
            endpoint=Endpoint(unobservable_reason="Go peer not exposed by Python API"),
            units={"price": "CNY", "volume": "shares", "amount": "CNY"},
            semantics={
                "adjustment": "none",
                "timezone": "Asia/Shanghai local date",
                "retry": "no harness retry; redial=false",
                "volume_precision": "approximate shares from truncated native lots",
            },
        )

    logging.disable(logging.CRITICAL)
    original_home = Path.home
    Path.home = classmethod(lambda cls: output / "vendor-home")
    try:
        record = observe(binding, fixture, execute, allow_live=True)
        record["request"] = REQUEST
        (output / "observation.json").write_text(json.dumps(record, indent=2) + "\n")
        return record
    finally:
        Path.home = original_home


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live_provider:
        parser.error("explicit --live-provider required")
    raise SystemExit(0 if probe(args.output, allow_live=True)["success"] else 1)
