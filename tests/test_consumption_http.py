"""Actual router and normal TestClient byte contract consumed by shared UI."""

from pathlib import Path

from market_data_foundation.web.macro_query_contract import contract_app
from tests.consumption_fixtures import make_host
from tests.consumption_wire import PARAMS, PREFIX, body, router_response
from tests.test_ppi_timeseries_web import client


def test_router_serializer_fixture_and_native_openapi(tmp_path):
    expected = Path("tests/fixtures/consumption_http_response.json").read_text()
    assert body(tmp_path) == expected
    doc = contract_app().openapi()
    params = doc["paths"][PREFIX + "/query"]["get"]["parameters"]
    assert {"series", "columns", "measure", "as_of", "local_only"} <= {
        p["name"] for p in params
    }
    assert "native" in next(p["description"] for p in params if p["name"] == "measure")


def test_actual_http_body_matches_shared_frontend_input(tmp_path):
    app, host, source, store = make_host(tmp_path)
    before = store.status()
    with client(app) as http:
        response = http.get(PREFIX + "/query", params=PARAMS)
        help_doc = http.get(PREFIX + "/api-help").json()
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert (
        response.text + "\n"
        == Path("tests/fixtures/consumption_http_response.json").read_text()
    )
    assert help_doc["query_defaults"]["local_only"] is False
    assert help_doc["measures"] == ["native"]
    # August's 12-month endpoint is deliberately absent from local fixtures.
    assert len(source.calls) == 1
    assert source.calls[0][1:] == ("2025-08", "2025-08")
    assert response.json()["provider_requests"] == 2
    assert response.json()["persisted"] is False
    assert store.status() == before


def test_router_column_projection_keeps_native_selector_and_saving_rate(tmp_path):
    app, _, _, _ = make_host(tmp_path)
    response = router_response(
        app, "/query", dict(PARAMS, columns="value,change_1m_percentage_points")
    )
    assert response.status_code == 200
    data = __import__("json").loads(response.body)
    assert data["selected_columns"] == ["value", "change_1m_percentage_points"]
    assert all("change_1m_ratio" not in p for p in data["points"])
    response = router_response(app, "/query", dict(PARAMS, columns="index_value"))
    assert response.status_code == 400
