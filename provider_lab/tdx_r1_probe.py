"""Reproduce one equivalent two-row R1 request; SDKs remain lab dependencies."""

import argparse
import asyncio
import importlib.metadata
import json
import logging
import os
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
from market_data_foundation.qualification.tdx_semantics import VERSIONS, daily_rows

HOST = "139.9.52.158"
ROOT = Path(__file__).resolve().parents[1]


def probe(implementation: str, output: Path, *, allow_live: bool = False) -> dict:
    if not allow_live:
        raise PermissionError("R1 live execution requires explicit opt-in")
    if platform.python_version() != "3.14.8":
        raise ValueError("R1 requires the unchanged Python 3.14.8 baseline")
    if importlib.metadata.version(implementation) != VERSIONS[implementation]:
        raise ValueError("R1 SDK version mismatch")
    output = output.resolve()
    if not output.is_relative_to(ROOT / "artifacts/tdx-r1"):
        raise ValueError("R1 output must remain under ignored artifacts/tdx-r1")
    output.mkdir(parents=True, exist_ok=True)
    # Vendor configuration must never read or write the user's actual home.
    original_home = Path.home
    Path.home = classmethod(lambda cls: output / "vendor-home")
    old_probe = os.environ.get("PYPYTDX_DATA_DISABLE_PROBE")
    os.environ["PYPYTDX_DATA_DISABLE_PROBE"] = "1"
    logging.disable(logging.CRITICAL)
    fixture = Fixture(
        "tdx-r1-common-endpoint-recent-two",
        Capability.BARS_DAILY,
        ("600000.XSHG",),
        None,
        None,
        2,
        {},
    )
    binding = Binding(
        "TDX",
        implementation,
        VERSIONS[implementation],
        fixture.capability,
        "live",
        Endpoint(HOST, 7709, None, "explicit configured target"),
    )

    def execute(_):
        endpoint = Endpoint(unobservable_reason="Go peer not exposed by Python API")
        if implementation == "tdx-python":
            from tdx_py import api

            client = api.dial(f"{HOST}:7709", 2000, False)
            try:
                page = client.get_kline(9, "sh600000", 0, 2)
                raw = [
                    {
                        k: getattr(row, k)
                        for k in (
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
        elif implementation == "mootdx-plus":
            from mootdx.quotes import Quotes

            client = Quotes.factory(
                market="std",
                server=(HOST, 7709),
                timeout=2,
                heartbeat=False,
                auto_retry=False,
                raise_exception=True,
            )
            try:
                raw = json.loads(
                    client.bars(
                        "SH.600000",
                        frequency=9,
                        start=0,
                        offset=2,
                    ).to_json(orient="records")
                )
                host, port = client.client.client.getpeername()
                endpoint = Endpoint(host, port, "socket.getpeername", None)
            finally:
                client.close()
        else:
            from dataclasses import asdict

            from pytdxdata import KlinePeriod, TdxData
            from pytdxdata.pool.connection import TdxConnection

            peers = []
            original_execute = TdxConnection.execute

            async def traced_execute(connection, command):
                if connection._writer is not None:
                    peers.append(connection._writer.get_extra_info("peername"))
                return await original_execute(connection, command)

            async def fetch():
                client = TdxData(
                    standard_servers=[HOST],
                    mac_servers=[HOST],
                    ex_servers=[HOST],
                    timeout=2,
                    pool_min=1,
                    pool_max=1,
                    cache_dir=None,
                )
                try:
                    await asyncio.wait_for(client.start(), 12)
                    bars = await asyncio.wait_for(
                        client.get_bars(
                            "sh600000",
                            KlinePeriod.DAY,
                            start=0,
                            count=2,
                        ),
                        8,
                    )
                    return [asdict(row) for row in bars]
                finally:
                    await client.close()

            TdxConnection.execute = traced_execute
            try:
                raw = asyncio.run(fetch())
            finally:
                TdxConnection.execute = original_execute
            if peers:
                host, port = peers[-1]
                endpoint = Endpoint(host, port, "peer at command execution", None)
        (output / "raw.json").write_text(json.dumps(raw, indent=2) + "\n")
        rows = daily_rows(implementation, raw, kind="stock")
        return Evidence(
            len(rows) == 2,
            returned_symbols=fixture.symbols if rows else (),
            row_count=len(rows),
            complete=False,
            pages=1,
            exhausted=None,
            observed_start=date.fromisoformat(rows[0]["date"]) if rows else None,
            observed_end=date.fromisoformat(rows[-1]["date"]) if rows else None,
            endpoint=endpoint,
            units={"price": "CNY", "amount": "CNY", "volume": "shares"},
            semantics={
                "origin": "SDK daily response with explicit R1 unit mapping",
                "adjustment": "none",
                "timezone": "Asia/Shanghai local date",
                "volume_precision": "native shares"
                if implementation == "pytdxdata"
                else "approximate shares reconstructed from truncated native lots",
                "retry": "no harness retry; pytdxdata may internally retry",
                "discovery": "explicit servers; pytdxdata public probe-disable switch",
            },
        )

    try:
        record = observe(binding, fixture, execute, allow_live=True)
        (output / "observation.json").write_text(json.dumps(record, indent=2) + "\n")
        return record
    finally:
        Path.home = original_home
        if old_probe is None:
            os.environ.pop("PYPYTDX_DATA_DISABLE_PROBE", None)
        else:
            os.environ["PYPYTDX_DATA_DISABLE_PROBE"] = old_probe


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--implementation", choices=VERSIONS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live_provider:
        parser.error("explicit --live-provider is required")
    record = probe(args.implementation, args.output, allow_live=True)
    raise SystemExit(0 if record["success"] else 1)


if __name__ == "__main__":
    main()
