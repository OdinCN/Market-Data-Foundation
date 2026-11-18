"""Actual refresh jobs and roster GETs on a temporary store; no network or PG."""

import json
import signal
import socket
import sys
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory
from uuid import uuid4

import psycopg
from pytest import MonkeyPatch

from tests.test_tdx_roster_query_date import at
from tests.test_tdx_roster_refresh import Rig


def forbidden(*_, **__):
    raise AssertionError("roster_refresh_external_io_forbidden")


socket.create_connection = psycopg.connect = forbidden
for operation in ("connect", "connect_ex", "bind", "listen"):
    setattr(socket.socket, operation, forbidden)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

with (
    TemporaryDirectory(prefix="mdf-roster-refresh-ui-") as directory,
    MonkeyPatch.context() as patch,
):
    rig = Rig(Path(directory), patch)
    if sys.argv[1] == "paging":
        rig.codes[0].update({f"{n:06d}": f"分页证券{n}" for n in range(100, 220)})
    if sys.argv[1] == "legacy_history":
        rig.current = at("2026-11-17T09:01:00")
        rig.manual()
        legacy = rig.store.snapshots[-1]
        legacy["details"].pop("producer")
        legacy["details"].pop("directory_verification")
        for m in [*legacy["members"], *legacy["details"]["directory_only_candidates"]]:
            m["evidence"].pop("directory_verification", None)
        rig.current = at("2026-11-18T09:01:00")
        rig.codes[0]["000001"] = "十八日名称"
    job = rig.manual()
    if sys.argv[1] == "legacy_ambiguous":
        legacy = rig.store.snapshots[-1]
        legacy["details"].pop("producer")
        legacy["details"].pop("directory_verification")
        other = deepcopy(job)
        other["request_id"] = str(uuid4())
        rig.runtime.jobs.save(other)
    for line in sys.stdin:
        message = json.loads(line)
        control = message.get("control")
        if control in {"manual", "fail"}:
            rig.current = at("2026-11-18T14:01:00")
            rig.codes[0]["000001"] = "新版平安银行"
            if control == "fail":
                rig.fail.add(2)
            job = rig.manual()
            result = dict(
                status=200,
                body={"request_id": job["request_id"], "state": job["state"]},
            )
        elif control == "stats":
            result = dict(
                status=200,
                body=dict(provider_calls=len(rig.calls), get_source_requests=0),
            )
        else:
            assert message.get("method", "GET") == "GET", "roster_ui_write_forbidden"
            before = len(rig.calls)
            response = rig.client.get(message["path"])
            assert len(rig.calls) == before, "roster_get_contacted_source"
            result = dict(status=response.status_code, body=response.json())
        print(json.dumps(result, default=str, ensure_ascii=False), flush=True)
