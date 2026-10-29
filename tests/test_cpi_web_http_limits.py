"""The new time-series preview serves full history within an explicit bound."""

import importlib
import io
from email.message import Message
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize(
    ("candidate", "size", "expected"),
    [
        (False, 3_000_000, 503),
        (True, 3_000_000, 200),
        (True, 32 * 1024 * 1024 + 1, 503),
    ],
)
def test_response_limit_preserves_old_default_and_bounds_new_history(
    monkeypatch, candidate, size, expected
):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    original = importlib.import_module("mdf_b3_pce_dev")
    web = importlib.import_module("mdf_cpi_timeseries_web")
    handler_type = web.TimeSeriesHandler if candidate else original.PreviewHandler
    response = SimpleNamespace(
        status_code=200,
        content=b"x" * size,
        headers={"content-type": "application/json"},
    )

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def request(self, *args, **kwargs):
            return response

    monkeypatch.setattr(original, "TestClient", Client)
    handler = object.__new__(handler_type)
    handler.path = "/api/r5/cpi/timeseries/query"
    handler.command = "GET"
    handler.headers = Message()
    handler.headers["Host"] = handler.origin.removeprefix("http://")
    handler.client_address = ("127.0.0.1", 12345)
    handler.rfile = io.BytesIO(b"")
    handler.server = SimpleNamespace(app=object())
    captured = []
    handler.respond = lambda status, *args: captured.append(status)
    handler.dispatch()
    assert captured == [expected]
