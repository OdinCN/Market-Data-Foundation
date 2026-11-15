"""Browser fixture using the real query router and a simulated trading session."""

import json
import socket
import sys
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg

from tests.test_tdx_daily_query import CODES, DAY, ReadStore, Rig, at, local, native


def forbidden(*_, **__):
    raise AssertionError("external_io_forbidden")


socket.create_connection = psycopg.connect = forbidden
for operation in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, operation, forbidden)

with TemporaryDirectory(prefix="mdf-tdx-query-ui-") as directory:
    clock = (
        "10:00"
        if sys.argv[1] in {"intraday", "paging"}
        else "08:00"
        if sys.argv[1] == "preopen"
        else "16:00"
    )
    store = ReadStore([local(date(2026, 11, 13), m, c) for m, c in CODES])
    if sys.argv[1] == "paging":
        store = ReadStore(codes=[(0, f"{n:06d}") for n in range(1, 5)])
    store.summary = lambda: dict(
        markets=[
            dict(market=m, rows=sum(r["market"] == m for r in store.rows))
            for m in (0, 1, 2)
            if any(r["market"] == m for r in store.rows)
        ],
        provider_requests=0,
    )
    rig = Rig(store, now=at(clock))
    original = json.dumps(store.rows, default=str, sort_keys=True)
    query_count = 0
    with rig.client(Path(directory)) as client:
        if sys.argv[1] == "paging":
            rig.runtime.query.max_requests = 2
        for line in sys.stdin:
            message = json.loads(line)
            if message.get("control") == "stats":
                result = dict(
                    status=200,
                    body=dict(
                        simulated=True,
                        clock=rig.now.isoformat(),
                        calls=rig.calls,
                        source_requests=len(rig.calls),
                        query_count=query_count,
                        store_unchanged=json.dumps(
                            store.rows, default=str, sort_keys=True
                        )
                        == original,
                        job_files=len(list(Path(directory).rglob("*.json"))),
                        all_sources_closed=all(s.closed for s in rig.sources),
                    ),
                )
            else:
                method, path = message.get("method", "GET"), message["path"]
                assert method == "GET", "query_ui_write_forbidden"
                if path.startswith("/api/r5/tdx/daily/query?"):
                    query_count += 1
                    rig.now += timedelta(seconds=1)
                    rig.data = {
                        k: [
                            native(date(2026, 11, 13)),
                            native(DAY, 1050 + query_count * 50),
                        ]
                        for k in [
                            (m["market"], m["instrument"])
                            for m in store.snapshot["members"]
                        ]
                    }
                if sys.argv[1] == "paging":
                    rig.data[(0, "000001")] = []
                response = client.get(path)
                result = dict(status=response.status_code, body=response.json())
            print(json.dumps(result, default=str), flush=True)
