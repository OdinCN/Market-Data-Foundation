"""Pin the real serialized response used by frontend validation and tables."""

import copy
import json
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.claims_contract import ClaimsError
from market_data_foundation.historical.claims_release import ClaimsReleaseRecords
from tests.claims_ui_wire import responses
from tests.test_claims_release import URL, pages, parse_release_pages
from tests.test_claims_web import PREFIX, client, setup


def test_http_fixture_matches_claims_router(tmp_path, monkeypatch):
    actual = responses(tmp_path, monkeypatch)
    assert actual == json.loads(Path("tests/fixtures/claims_ui_http.json").read_text())


def test_strict_release_filters_and_http_parameters(tmp_path, monkeypatch):
    app, host, *_ = setup(tmp_path, monkeypatch)
    event = dict(parse_release_pages(pages(), URL), evidence_id="synthetic")
    monkeypatch.setattr(
        ClaimsReleaseRecords, "_rows", lambda *args: [copy.deepcopy(event)]
    )
    with client(app) as http:
        for params, count in (
            ("event_id=dol-claims-2026-10-29", 1),
            ("event_id=dol-claims-2026-10-22", 0),
            ("reference_week=2026-10-24", 1),
            ("reference_week=2026-10-10", 1),
            ("reference_week=2026-10-03", 0),
            ("source_version=2026-10-22", 0),
            (
                "source_version=2026-10-29&reference_week=2026-10-24&read_mode=source_history",
                1,
            ),
        ):
            response = http.get(PREFIX + "/releases?" + params)
            assert response.status_code == 200
            assert len(response.json()["events"]) == count
        for params in (
            "event_id=bea-gdp-2026-10-29",
            "event_id=dol-claims-2026-02-30",
            "reference_week=2026-10-23",
            "reference_week=2026-10",
            "reference_month=2026-10",
            "source_version=2026-02-30",
            "source_version=2026-10-29T08:30:00Z",
            "event_id=x&event_id=y",
        ):
            assert http.get(PREFIX + "/releases?" + params).status_code == 400
        documented = http.get(PREFIX + "/api-help").json()["release_records"][
            "parameters"
        ]
        openapi = app.openapi()["paths"][PREFIX + "/releases"]["get"]
        assert {p["name"] for p in openapi["parameters"]} == set(documented)
        assert set(documented) == {
            "as_of",
            "read_mode",
            "event_id",
            "reference_week",
            "source_version",
        }


def test_row_schedule_requires_this_series_week_actual():
    event = dict(parse_release_pages(pages(), URL), evidence_id="synthetic")
    records = ClaimsReleaseRecords(SimpleNamespace(read_snapshot=nullcontext))
    records.read = lambda **kwargs: dict(events=[event])
    result = records.link_result(
        dict(
            rows=[
                dict(series_id="ICSA", week_ending="2026-10-24"),
                dict(series_id="ICSA", week_ending="2026-10-17"),
                dict(series_id="CCSA", week_ending="2026-10-17"),
                dict(series_id="CCSA", week_ending="2026-10-24"),
            ]
        )
    )
    links = [row["related_releases"] for row in result["rows"]]
    assert links[0][0]["scheduled_publication_at"] == "2026-10-29T08:30:00-04:00"
    assert links[1][0]["scheduled_publication_at"] is None
    assert links[2][0]["roles"] == ["actual"]
    assert links[3] == []
    assert all(not link["continuous_version_bound"] for row in links for link in row)


@pytest.mark.parametrize(
    "params",
    [
        dict(event_id="bls-x"),
        dict(reference_week="2026-10-23"),
        dict(source_version="2026-10"),
    ],
)
def test_bad_filters_fail_before_storage(params):
    with pytest.raises(ClaimsError):
        ClaimsReleaseRecords(None).read(**params)
