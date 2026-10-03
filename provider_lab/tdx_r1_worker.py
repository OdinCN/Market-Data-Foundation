"""Only the four authorized R1 reconciliation retests; launched by guarded CLI."""

import argparse
import asyncio
import dataclasses
import importlib.metadata
import json
import logging
import os
import platform
import sys
from pathlib import Path
from time import monotonic

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from market_data_foundation.qualification import (  # noqa: E402
    Binding,
    Capability,
    Endpoint,
    Evidence,
    Fixture,
    observe,
)

VERSIONS = {"tdx-python": "0.1.6", "pytdxdata": "0.6.1", "mootdx-plus": "0.12.0"}
GOOD = "139.9.52.158"
DEAD = "124.70.176.52"


def plain(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if dataclasses.is_dataclass(value):
        return {k: plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, dict):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)) or type(value).__name__.startswith("Slice_"):
        return [plain(v) for v in value]
    if hasattr(value, "to_json"):
        return json.loads(value.to_json(orient="records", date_format="iso"))
    if hasattr(type(value), "__slots__"):
        return {
            k: plain(getattr(value, k))
            for k in type(value).__slots__
            if not k.startswith("_")
        }
    if hasattr(value, "handle"):
        return {
            k: plain(getattr(value, k))
            for k in dir(type(value))
            if isinstance(getattr(type(value), k), property)
        }
    if hasattr(value, "isoformat"):
        return value.isoformat()
    raise TypeError("unsupported diagnostic payload; preserve source types")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--case", required=True)
    parser.add_argument("--implementation", choices=VERSIONS, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live_provider:
        parser.error("explicit live opt-in required")
    if (
        platform.python_version() != "3.14.8"
        or importlib.metadata.version(args.implementation)
        != VERSIONS[args.implementation]
    ):
        raise RuntimeError("qualified runtime/SDK mismatch")
    out = args.output.resolve()
    if not out.is_relative_to(ROOT / "artifacts/tdx-r1"):
        raise ValueError("ignored R1 output required")
    out.mkdir(parents=True, exist_ok=True)
    home = out / "vendor-home"
    home.mkdir(exist_ok=True)
    Path.home = classmethod(lambda cls: home)
    os.environ["PYPYTDX_DATA_DISABLE_PROBE"] = "1"
    os.environ["PYPYTDX_DATA_CACHE_DIR"] = str(home / "probe-cache")
    logging.disable(logging.CRITICAL)
    client = None
    records = []
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def run(value):
        return loop.run_until_complete(asyncio.wait_for(value, 5))

    def save(
        label,
        capability,
        fn,
        *,
        semantics=None,
        symbols=(),
        actual=None,
        attempted=GOOD,
    ):
        phase = {"label": label, "started": monotonic(), "capability": capability.value}
        temp = out / "phase.tmp"
        temp.write_text(json.dumps(phase))
        temp.replace(out / "phase.json")
        fixture = Fixture(label, capability, tuple(symbols), None, None, 2, {})
        endpoint = (
            Endpoint(attempted, 7709, None, "explicit target")
            if attempted
            else Endpoint(unobservable_reason="default pools; see syscall trace")
        )
        binding = Binding(
            "TDX",
            args.implementation,
            VERSIONS[args.implementation],
            capability,
            "live",
            endpoint,
        )

        def execute(_):
            value = fn()
            payload = plain(value)
            (out / f"{label}.raw.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
            )
            if actual:
                host, port = actual()
                peer = Endpoint(host, port, "SDK command/socket peer", None)
            else:
                peer = Endpoint(
                    unobservable_reason="see OS syscall and owned-socket trace"
                )
            rows = (
                len(payload)
                if isinstance(payload, list)
                else 1
                if isinstance(payload, (dict, str)) and payload
                else 0
            )
            success = payload is not None and (
                bool(payload) if isinstance(payload, (list, dict, str)) else True
            )
            return Evidence(
                success,
                row_count=rows,
                returned_symbols=tuple(symbols),
                complete=False,
                endpoint=peer,
                semantics=semantics or {},
            )

        record = observe(binding, fixture, execute, allow_live=True)
        records.append(record)
        (out / "observations.json").write_text(json.dumps(records, indent=2) + "\n")
        return record

    name = args.implementation
    case = args.case
    try:
        if name == "tdx-python":
            from tdx_py import api

            if case in ("go-redial-off", "go-redial-on"):
                redial = case.endswith("-on")
                (out / "configuration.json").write_text(
                    json.dumps(
                        {
                            "api": "api.dial",
                            "address": DEAD + ":7709",
                            "timeout_ms": 500,
                            "redial": redial,
                        }
                    )
                )
                save(
                    case,
                    Capability.BARS_DAILY,
                    lambda: api.dial(DEAD + ":7709", 500, redial),
                    attempted=DEAD,
                    semantics={
                        "purpose": "connection failure; no bars claimed",
                        "redial": str(redial),
                    },
                )
            else:
                client = api.dial(GOOD + ":7709", 1000, case == "go-close")
                if case == "go-close":
                    save(
                        "connected",
                        Capability.BARS_DAILY,
                        lambda: client.get_kline(9, "sh600000", 0, 1),
                        symbols=("600000.XSHG",),
                    )
                    client.close()
                    save(
                        "after-explicit-close",
                        Capability.BARS_DAILY,
                        lambda: client.get_kline(9, "sh600000", 2, 1),
                        semantics={
                            "closed_client_handles": "1",
                            "redial": "True",
                            "fault": "explicit API close, not an accidental TCP loss",
                        },
                    )
                elif case == "bj-aggregate":
                    save(
                        "bj-aggregate",
                        Capability.SECURITY_UNIVERSE,
                        lambda: client.get_code_all(2),
                        semantics={
                            "api": "get_code_all(2)",
                            "source": "unverified; non-TDX traffic blocked",
                        },
                    )
                else:
                    save(
                        "bj-quote-identity",
                        Capability.SECURITY_METADATA,
                        lambda: client.get_quote(["bj920000"]),
                        symbols=("920000.XBSE",),
                    )
                    save(
                        "bj-count",
                        Capability.SECURITY_UNIVERSE,
                        lambda: client.get_count(2),
                        semantics={
                            "api": "get_count(2)",
                            "scope": "server count, not cash/status completeness",
                        },
                    )
                    save(
                        "bj-standard-list",
                        Capability.SECURITY_METADATA,
                        lambda: client.get_code(2, 0),
                        semantics={
                            "api": "get_code(2,0)",
                            "scope": "single native page",
                        },
                    )
        elif name == "mootdx-plus":
            from mootdx import config
            from mootdx.quotes import Quotes

            raising = case == "mootdx-raising" or case == "bj"
            config.setup()
            if case != "bj":
                config.set(
                    "SERVER.HQ",
                    [["recorded-dead", DEAD, 7709], ["recorded-good", GOOD, 7709]],
                )
                config.set("BESTIP.HQ", [DEAD, 7709])
            settings = {
                "timeout": 0.5,
                "heartbeat": False,
                "auto_retry": True,
                "raise_exception": raising,
            }
            if case == "bj":
                settings.update(server=(GOOD, 7709), auto_retry=False)
            (out / "configuration.json").write_text(
                json.dumps(
                    {
                        **settings,
                        "candidate_pool": config.get("SERVER.HQ"),
                        "preferred": config.get("BESTIP.HQ"),
                    }
                )
            )

            def open_client():
                nonlocal client
                client = Quotes.factory(market="std", **settings)
                return {
                    "server": list(client.server),
                    "peer": list(client.client.client.getpeername()),
                }

            start = save(
                "constructor",
                Capability.SECURITY_METADATA,
                open_client,
                semantics={
                    "purpose": "connection lifecycle; not metadata qualification"
                },
            )
            if start["success"]:

                def peer():
                    return client.client.client.getpeername()

                if case == "bj":
                    save(
                        "bj-quote-identity",
                        Capability.SECURITY_METADATA,
                        lambda: client.quotes(["BJ.920000"]),
                        symbols=("920000.XBSE",),
                        actual=peer,
                    )
                    save(
                        "bj-count",
                        Capability.SECURITY_UNIVERSE,
                        lambda: client.stock_count(2),
                        actual=peer,
                    )
                    save(
                        "bj-standard-list",
                        Capability.SECURITY_METADATA,
                        lambda: client.client.get_security_list(2, 0),
                        actual=peer,
                    )
                else:
                    save(
                        "initial-bars",
                        Capability.BARS_DAILY,
                        lambda: client.bars("SH.600000", frequency=9, offset=2),
                        actual=peer,
                    )
                    closed_fd = client.client.client.fileno()
                    client.client.client.close()
                    save(
                        "after-socket-close",
                        Capability.BARS_DAILY,
                        lambda: client.bars(
                            "SH.600000", frequency=9, start=2, offset=2
                        ),
                        actual=peer,
                        semantics={
                            "closed_socket_fd": str(closed_fd),
                            "closed_connections": "1",
                            "raising": str(raising),
                            "retry": "True",
                        },
                    )
                    save(
                        "cleanup",
                        Capability.SECURITY_METADATA,
                        lambda: {"return_type": type(client.close()).__name__},
                        semantics={"purpose": "cleanup only"},
                    )
                    client = None
        else:
            from pytdxdata import KlinePeriod, TdxData
            from pytdxdata.pool.connection import TdxConnection
            from pytdxdata.protocol.commands.security_list import GetSecurityListCmd

            peers = []
            original = TdxConnection.execute

            async def execute(connection, command):
                peer = (
                    connection._writer.get_extra_info("peername")
                    if connection._writer
                    else None
                )
                peers.append(
                    {
                        "phase": json.loads((out / "phase.json").read_text())["label"],
                        "command": type(command).__name__,
                        "peer": peer,
                    }
                )
                (out / "commands.json").write_text(json.dumps(peers, indent=2))
                return await original(connection, command)

            TdxConnection.execute = execute
            options = {
                "timeout": 0.5,
                "pool_min": 1,
                "pool_max": 2,
                "std_pool_cap": 1,
                "cache_dir": out / "data-cache",
            }
            if case == "bj":
                options.update(
                    standard_servers=[GOOD], mac_servers=[GOOD], ex_servers=[GOOD]
                )
            elif case == "py-good-dead":
                options.update(
                    standard_servers=[DEAD, GOOD], mac_servers=[GOOD], ex_servers=[GOOD]
                )
            elif case == "py-dead-only":
                options.update(
                    standard_servers=[DEAD], mac_servers=[DEAD], ex_servers=[DEAD]
                )
            config_record = {
                **options,
                "cache_dir": "isolated data-cache",
                "background_probe_disabled": True,
            }
            (out / "configuration.json").write_text(json.dumps(config_record, indent=2))
            client = TdxData(**options)
            save(
                "startup",
                Capability.SECURITY_METADATA,
                lambda: (
                    run(client.start()),
                    {
                        "std": client._std_servers,
                        "mac": client._mac_servers,
                        "ex": client._ex_servers,
                    },
                )[1],
                attempted=None
                if case == "py-default"
                else DEAD
                if case == "py-dead-only"
                else GOOD,
                semantics={"purpose": "pool lifecycle only"},
            )
            if case == "bj":
                save(
                    "bj-snapshot-identity",
                    Capability.SECURITY_METADATA,
                    lambda: run(client.get_snapshot("bj920000")),
                    symbols=("920000.XBSE",),
                )
                save(
                    "bj-count",
                    Capability.SECURITY_UNIVERSE,
                    lambda: run(client.get_security_count(2)),
                )
                save(
                    "bj-standard-list",
                    Capability.SECURITY_METADATA,
                    lambda: run(client._execute_standard(GetSecurityListCmd(2, 0))),
                    semantics={"api": "bounded underlying standard list command"},
                )
            else:

                def bars(start=0):
                    return run(
                        client.get_bars(
                            "sh600000", KlinePeriod.DAY, start=start, count=2
                        )
                    )

                first = save(
                    "uncached-first",
                    Capability.BARS_DAILY,
                    bars,
                    symbols=("600000.XSHG",),
                )
                if first["success"]:
                    save(
                        "cached-repeat",
                        Capability.BARS_DAILY,
                        bars,
                        symbols=("600000.XSHG",),
                    )
                    closed = []
                    for pc in list(client._std_pool._conns):
                        if pc.conn._writer:
                            closed.append(
                                {
                                    "host": pc.host,
                                    "peer": pc.conn._writer.get_extra_info("peername"),
                                    "alive_before": pc.alive,
                                }
                            )
                        run(pc.conn.close())
                    (out / "closed-connections.json").write_text(
                        json.dumps(closed, indent=2)
                    )
                    save(
                        "uncached-after-close",
                        Capability.BARS_DAILY,
                        lambda: bars(2),
                        symbols=("600000.XSHG",),
                        semantics={
                            "closed_connections": str(len(closed)),
                            "fault": "close recorded sockets; uncached start=2",
                        },
                    )
    finally:
        if client is not None:
            try:
                if name == "pytdxdata":
                    run(client.close())
                else:
                    client.close()
            except Exception:
                pass
        loop.close()


if __name__ == "__main__":
    main()
