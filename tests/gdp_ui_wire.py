"""Synthetic quarterly HTTP bodies for the shared frontend; no external IO."""

import json
from urllib.parse import urlsplit

from starlette.requests import Request

from market_data_foundation.historical.gdp_release_records import GdpReleaseRecords
from market_data_foundation.web.gdp_host import PREFIX
from market_data_foundation.web.pce_dev_host import CANDIDATE_LISTENER, CANDIDATE_ORIGIN
from tests.test_gdp_releases import URL, material
from tests.test_gdp_web import setup


def responses(tmp_path, monkeypatch):
    app, host, service, wire, _, _ = setup(tmp_path, monkeypatch)
    wire.overrides.update(
        {
            ("A191RL", "2026Q3"): "3.4",
            ("A019RY", "2026Q3"): "-0.5",
            ("A014RY", "2026Q3"): "0",
            ("A014RY", "2026Q2"): "(NA)",
        }
    )
    service.init_range("2026-Q2", "2026-Q3")
    records = GdpReleaseRecords(service.store)
    records.prepare()
    for kind in ("second", "third"):
        records.import_material(material(kind), URL.format(kind))
    host.scheduler.refresh_calendar()
    host.submit(
        {
            "request_id": "d3200000-0000-4000-8000-000000000001",
            "action": "init",
            "start": "2026-Q2",
            "end": "2026-Q3",
        }
    )
    before = service.status()
    source_calls = len(wire.calls)
    paths = {
        **{name: "/" + name for name in ("jobs", "executions", "strategy", "schedule")},
        "query": "/query?start=2026-Q2&end=2026-Q3&local_only=true",
        "history": (
            "/query?start=2026-Q2&end=2026-Q3&local_only=true"
            "&query_mode=history&as_of_basis=available&as_of=2026-11-02T11:00:00Z"
        ),
        "releases": "/releases?reference_quarter=2026-Q2",
        "source_history": (
            "/releases?reference_quarter=2026-Q2&read_mode=source_history"
        ),
    }
    result = {}

    def get(path):
        url = urlsplit(PREFIX + path)
        route = next(r for r in app.routes if getattr(r, "path", None) == url.path)
        request = Request(
            dict(
                type="http",
                http_version="1.1",
                method="GET",
                scheme="http",
                server=CANDIDATE_LISTENER,
                client=("192.168.10.25", 1234),
                path=url.path,
                root_path="",
                query_string=url.query.encode(),
                headers=[
                    (b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode()),
                    (b"origin", CANDIDATE_ORIGIN.encode()),
                ],
            )
        )
        return route.endpoint(request)

    status = get("/status")
    assert status.status_code == 200
    result["status"] = {
        key: json.loads(status.body)[key]
        for key in (
            "latest_quarters",
            "observation_count",
            "revision_count",
            "batch_count",
            "web_capabilities",
            "initialization",
            "collection_scope",
            "source_allowed",
        )
    }
    for name, path in paths.items():
        response = get(path)
        assert response.status_code == 200
        result[name] = json.loads(response.body)
    assert service.status() == before and len(wire.calls) == source_calls
    return result
