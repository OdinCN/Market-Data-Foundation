"""Real roster GET protocol over isolated publications; no source, PG or jobs."""

import json
import socket
import sys
from copy import deepcopy

import psycopg
from pytest import MonkeyPatch

from tests.test_tdx_roster_query_date import (
    RosterStore,
    at,
    late_exchange_store,
    roster_client,
)


def forbidden(*_, **__):
    raise AssertionError("roster_query_external_io_forbidden")


socket.create_connection = psycopg.connect = forbidden
for operation in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, operation, forbidden)

store = late_exchange_store() if sys.argv[1] == "late" else RosterStore()
clock = "2026-11-17T09:05:00"
if sys.argv[1] == "prepublish":
    clock = "2026-11-17T09:00:31.999999"
if sys.argv[1] == "legacy":
    store.snapshots = [store.snapshots[-1]]
    clock = "2026-11-17T11:00:00"
# Exercise the real server pager, beyond the UI's fixed 100 rows.
for snapshot in store.snapshots:
    snapshot["members"].extend(
        dict(market=2, instrument=f"{920000 + n}", name=f"分页样本{n}", evidence={})
        for n in range(100)
    )
original = json.dumps(store.snapshots, default=str, sort_keys=True)
for line in sys.stdin:
    message = json.loads(line)
    if message.get("control") == "stats":
        result = dict(
            status=200,
            body=dict(
                unchanged=json.dumps(store.snapshots, default=str, sort_keys=True)
                == original,
                simulated_clock=clock,
                provider_requests=0,
                jobs=0,
            ),
        )
    elif message.get("control") == "publish":
        newer = deepcopy(
            store.snapshots[-1] if sys.argv[1] == "late" else store.snapshots[1]
        )
        newer.update(
            snapshot_id="exchange-13-later"
            if sys.argv[1] == "late"
            else "directory-new",
            available_at=at(
                "2026-11-14T12:00:00"
                if sys.argv[1] == "late"
                else "2026-11-17T09:04:00"
            ),
        )
        newer["members"][-1]["name"] = "新版不可混页"
        store.snapshots.append(newer)
        original = json.dumps(store.snapshots, default=str, sort_keys=True)
        result = dict(status=200, body={})
    else:
        assert message.get("method", "GET") == "GET", "roster_query_write_forbidden"
        with (
            MonkeyPatch.context() as patch,
            roster_client(patch, clock, store) as (client, _),
        ):
            response = client.get(message["path"])
            result = dict(status=response.status_code, body=response.json())
    print(json.dumps(result, default=str, ensure_ascii=False), flush=True)
