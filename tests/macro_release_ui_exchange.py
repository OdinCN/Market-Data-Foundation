"""Synthetic Employment archive→real HTTP wire bridge; external IO is forbidden."""

import json
import os
import socket
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import psycopg
from pytest import MonkeyPatch

from market_data_foundation.historical.macro_release_records import ReleaseRecords
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_employment_web import setup
from tests.test_macro_release_records import MATERIAL, UNKNOWN, URL, Raw
from tests.test_ppi_timeseries_web import client


def forbidden(*args, **kwargs):
    raise AssertionError("external_io_forbidden")


socket.create_connection = forbidden
socket.socket.connect = forbidden
socket.socket.bind = forbidden
socket.socket.listen = forbidden
psycopg.connect = forbidden

with (
    TemporaryDirectory(prefix="mdf-release-ui-") as directory,
    MonkeyPatch.context() as patch,
):
    app, runtime, source, store, _ = setup(Path(directory), patch)
    store.raw_store = Raw()
    records = ReleaseRecords(store)
    records.prepare()
    if sys.argv[1] == "retained":
        retained = Path(os.environ["MDF_RELEASE_RETAINED_DIR"])
        records.import_employment(
            (retained / "empsit_10022026_summary_projection.html").read_bytes(),
            "https://www.bls.gov/news.release/archives/empsit_10022026.htm",
            material_format="official_summary_projection",
        )
    else:
        records.import_employment(
            UNKNOWN if sys.argv[1] == "unknown" else MATERIAL, URL
        )
    with client(app) as http:
        for line in sys.stdin:
            message = json.loads(line)
            response = http.request(
                message.get("method", "GET"),
                message["path"],
                headers={"Origin": CANDIDATE_ORIGIN},
            )
            print(
                json.dumps(
                    {
                        "status": response.status_code,
                        "body": response.json(),
                        "text": response.text,
                        "effects": {
                            "source": len(source.calls),
                            "raw": len(store.raw_store.objects),
                            "jobs": 0,
                        },
                    }
                ),
                flush=True,
            )
