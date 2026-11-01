"""Production router/serializer fixture; service/source input is synthetic only."""

from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import urlencode

from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from starlette.requests import Request

from market_data_foundation.web.pce_dev_host import CANDIDATE_LISTENER, CANDIDATE_ORIGIN
from tests.consumption_fixtures import make_host

PARAMS = dict(
    start="2026-08",
    end="2026-09",
    measure="native",
    series="DPCERC,DPCERX,DGDSRX,DSERRX,A067RC,A067RX,A072RC",
    local_only="false",
    query_mode="current",
)
PREFIX = "/api/r5/consumption/timeseries"


def router_response(app, path, params=None):
    route = next(r for r in app.routes if getattr(r, "path", None) == PREFIX + path)
    request = Request(
        dict(
            type="http",
            http_version="1.1",
            method="GET",
            scheme="http",
            server=CANDIDATE_LISTENER,
            client=("192.168.10.25", 1234),
            path=PREFIX + path,
            root_path="",
            query_string=urlencode(params or {}).encode(),
            headers=[
                (b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode()),
                (b"origin", CANDIDATE_ORIGIN.encode()),
            ],
        )
    )
    result = route.endpoint(request)
    return (
        result
        if isinstance(result, JSONResponse)
        else JSONResponse(jsonable_encoder(result))
    )


def body(directory):
    app, host, _, _ = make_host(directory)
    response = router_response(app, "/query", PARAMS)
    assert response.status_code == 200
    return response.body.decode() + "\n"


if __name__ == "__main__":
    with TemporaryDirectory(prefix="consumption-wire-") as temporary:
        print(body(Path(temporary)), end="")
