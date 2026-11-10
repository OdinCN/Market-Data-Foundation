"""Ephemeral queries through actual projection/router; no private service dependency."""

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.financial_daily import DailyPoint, FinancialError
from market_data_foundation.historical.financial_daily_service import (
    FinancialDailyService,
)
from market_data_foundation.web.financial_daily_api import (
    PREFIX,
    FinancialOptions,
    financial_router,
)
from market_data_foundation.web.financial_daily_query import (
    FinancialQuery,
    availability,
    live_query_source,
)
from tests.test_financial_api_help import Store
from tests.test_financial_daily import day

NOW = datetime(2026, 11, 6, 23, tzinfo=UTC)


class ReadStore(Store):
    def __init__(self, values=()):
        super().__init__()
        self.data.update(
            values=list(values), batches=[], ranges=[], attempts=[], pending=[]
        )

    def save(self, *args, **kwargs):
        pytest.fail("query attempted persistence")

    record_attempt = save


def value(sid, at, number, origin="us_treasury", available=NOW - timedelta(days=1)):
    return (
        sid,
        day(at),
        3,
        Decimal(number),
        {},
        "held-batch",
        available,
        available,
        "held-raw",
        "held-meta",
        origin,
    )


class Source:
    def __init__(self, data=None, failure=None):
        self.data, self.failure, self.calls, self.request_count = (
            data or {},
            failure,
            [],
            0,
        )

    def fetch(self, sid, start, end):
        self.calls.append((sid, start, end))
        self.request_count += 1
        if self.failure:
            raise FinancialError(self.failure)
        return SimpleNamespace(
            points=tuple(
                DailyPoint(sid, at, Decimal(n) if n is not None else None)
                for (s, at), n in self.data.items()
                if s == sid and start <= at <= end
            ),
            origin="credit_archive"
            if sid.startswith("BAML") and end < "2026-04-01"
            else "fred"
            if sid.startswith("BAML")
            else "us_treasury",
            request={"url": "https://controlled.example", "parameters": {}},
            metadata={},
        )


def app_for(tmp_path, store, reader):
    app = FastAPI()
    app.include_router(
        financial_router(
            FinancialOptions(tmp_path / "jobs", True, True),
            service_factory=lambda: FinancialDailyService(store, clock=lambda: NOW),
            query_reader=reader,
        )
    )
    return app


def test_local_preserved_ephemeral_merge_and_page_cache_no_writes(tmp_path):
    source = Source({("DGS10", "2026-10-29"): "99", ("DGS10", "2026-10-30"): "4.1"})
    store = ReadStore([value("DGS10", "2026-10-29", "4.0")])
    original = copy.deepcopy(store.data)
    app = app_for(tmp_path, store, FinancialQuery(lambda _: source))
    params = dict(start="2026-10-29", end="2026-10-30", series="DGS10", limit=1)
    with TestClient(app) as client:
        first = client.get(PREFIX + "/query", params=params).json()
        assert first["provider_requests"] == 0 and first["rows"][0]["value"] == "4.0"
        second = client.get(PREFIX + "/query", params=params | {"offset": 1}).json()
        row = second["rows"][0]
        assert second["provider_requests"] == 1 and row["value"] == "4.1"
        assert row["origin"] == "upstream" and row["persisted"] is False
        assert all(
            row[k] is None
            for k in (
                "revision",
                "batch_id",
                "available_at",
                "observed_at",
                "raw_locator",
                "metadata_locator",
            )
        )
        again = client.get(PREFIX + "/query", params=params | {"limit": 2}).json()
        assert again["provider_requests"] == 0 and again["upstream"]["cache_hits"] == 1
        assert again["rows"][0]["value"] == "4.0" and again["rows"][1]["value"] == "4.1"
    assert len(source.calls) == 1 and store.data == original
    assert not (tmp_path / "jobs").exists()


def test_failure_retains_local_and_is_negative_cached(tmp_path):
    source = Source(failure="financial_http_failure")
    store = ReadStore([value("DGS10", "2026-10-29", "0")])
    reader = FinancialQuery(lambda _: source)
    with TestClient(app_for(tmp_path, store, reader)) as client:
        params = dict(start="2026-10-29", end="2026-10-30", series="DGS10")
        for count in (1, 0):
            r = client.get(PREFIX + "/query", params=params).json()
            assert r["provider_requests"] == count and r["rows"][0]["value"] == "0"
            assert r["rows"][1]["value"] is None
            assert (
                r["upstream"]["issues"][0]["classification"] == "financial_http_failure"
            )
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"as_of": NOW.isoformat()},
        {"local_only": "true"},
        {"as_of": NOW.isoformat(), "local_only": "false"},
    ],
)
def test_history_and_explicit_local_do_not_even_construct_source(tmp_path, extra):
    def forbidden(_):
        pytest.fail("read-only query constructed upstream")

    with TestClient(
        app_for(tmp_path, ReadStore(), FinancialQuery(forbidden))
    ) as client:
        r = client.get(
            PREFIX + "/query",
            params=dict(start="2026-10-30", end="2026-10-30", series="DGS10") | extra,
        ).json()
    assert r["provider_requests"] == 0 and r["rows"][0]["value"] is None


@pytest.mark.parametrize(
    "sid,at,state",
    [
        ("SOFR", "2017-01-03", "outside_provider_range"),
        ("DFII10", "2002-12-31", "outside_provider_range"),
        ("DFEDTARL", "2008-12-15", "outside_provider_range"),
        ("DGS10", "2026-10-31", "non_observation_day"),
        ("BAMLC0A0CM", "2026-11-01", "non_observation_day"),
        ("DGS10", "2026-11-07", "non_observation_day"),
        ("DGS10", "2026-11-09", "not_yet_available_from_provider"),
        ("EFFR", "2026-11-06", "not_yet_available_from_provider"),
    ],
)
def test_inception_calendar_and_not_published_never_fetch(sid, at, state):
    reader = FinancialQuery(lambda _: pytest.fail("noneligible day fetched"))
    service = FinancialDailyService(ReadStore(), clock=lambda: NOW)
    rows = service.query(at, at, selected=(sid,), as_of=NOW)["rows"]
    result, summary = reader.supplement(service, rows, NOW)
    assert result[0]["state"] == state and summary["provider_requests"] == 0


def test_ny_publication_next_working_day_weekend_and_monday_before_open():
    for now in (
        datetime(2026, 11, 7, 18, tzinfo=UTC),
        datetime(2026, 11, 9, 13, tzinfo=UTC),
    ):
        assert (
            availability("EFFR", "2026-11-06", now) == "not_yet_available_from_provider"
        )
    assert (
        availability("EFFR", "2026-11-06", datetime(2026, 11, 9, 15, tzinfo=UTC))
        is None
    )


def test_credit_month_end_uses_pinned_archive_and_tail_uses_fred():
    source = Source(
        {("BAMLC0A0CM", "2026-01-31"): "1.25", ("BAMLC0A0CM", "2026-10-30"): "1.5"}
    )
    kinds = []

    def factory(kind):
        kinds.append(kind)
        return source

    reader = FinancialQuery(factory)
    service = FinancialDailyService(ReadStore(), clock=lambda: NOW)
    for at in ("2026-01-31", "2026-10-30"):
        rows = service.query(at, at, selected=("BAMLC0A0CM",), as_of=NOW)["rows"]
        result, counts = reader.supplement(service, rows, NOW)
        assert result[0]["value"] is not None and result[0]["basis_points"] in (
            "125.00",
            "150.0",
        )
        assert counts["provider_requests"] == 1
    assert kinds == ["archive", "fred"]
    assert source.calls[0][1:] == ("1996-12-31", "2026-03-17")


@pytest.mark.parametrize("missing_leg", [False, True])
def test_derived_same_day_local_and_temporary_legs(missing_leg):
    data = {} if missing_leg else {("DFII10", "2026-10-30"): "1.75"}
    source = Source(data)
    service = FinancialDailyService(
        ReadStore([value("DGS10", "2026-10-30", "4.25")]), clock=lambda: NOW
    )
    rows = service.query("2026-10-30", "2026-10-30", selected=("T10YIE",), as_of=NOW)[
        "rows"
    ]
    result, counts = FinancialQuery(lambda _: source).supplement(service, rows, NOW)
    row = result[0]
    if missing_leg:
        assert row["value"] is None and row["state"] == "derived_inputs_unavailable"
        assert (
            counts["issues"][-1]["classification"]
            == "financial_derived_inputs_unavailable"
        )
    else:
        assert row["value"] == "2.50" and row["origin"] == "upstream"
        assert [r["series_id"] for r in row["provider_extra"]["inputs"]] == [
            "DGS10",
            "DFII10",
        ]
        assert row["provider_extra"]["inputs"][0]["revision"] == 3
        assert row["provider_extra"]["inputs"][1]["available_at"] is None
    assert len(source.calls) == 1 and source.calls[0][0] == "DFII10"


def test_live_factory_reuses_existing_admission_without_retention(monkeypatch):
    calls = []
    for cls in ("OriginSource", "CreditArchiveSource", "DailyFredSource"):
        monkeypatch.setattr(
            "market_data_foundation.web.financial_daily_query." + cls + ".live",
            lambda **kw: calls.append(kw),
        )
    for kind in ("origin", "archive", "fred"):
        live_query_source(kind)
    assert all(c["live_opt_in"] is True and "raw_sink" not in c for c in calls)
    assert str(calls[0]["control_directory"]).endswith(
        "transactions/MDF-MACRO-RELIABLE-UPDATE-20261104-R1/runtime/financial-daily-origin-control"
    )
    assert str(calls[1]["control_path"]).endswith(
        "runtime/financial-daily-jobs/archive-control.json"
    )


def test_empty_success_is_cached_and_bounded_failure_cannot_loop():
    timer = [0.0]
    source = Source()
    reader = FinancialQuery(lambda _: source, timer=lambda: timer[0])
    service = FinancialDailyService(ReadStore(), clock=lambda: NOW)
    rows = service.query("2026-10-29", "2026-10-30", selected=("DGS10",), as_of=NOW)[
        "rows"
    ]
    for expected in (1, 0):
        result, receipt = reader.supplement(service, rows, NOW)
        assert receipt["provider_requests"] == expected
        assert all(
            r["value"] is None and r["state"] == "not_returned_calendar_unconfirmed"
            for r in result
        )
    timer[0] = 301
    _, receipt = reader.supplement(service, rows, NOW)
    assert receipt["provider_requests"] == 1 and len(source.calls) == 2


def test_one_page_budget_and_cache_capacity_are_bounded():
    source = Source()
    reader = FinancialQuery(lambda _: source)
    service = FinancialDailyService(ReadStore(), clock=lambda: NOW)
    rows = service.query("2023-01-01", "2024-12-31", selected=("DGS10",), as_of=NOW)[
        "rows"
    ]
    result, receipt = reader.supplement(service, rows, NOW)
    assert (
        len(result) == 731
        and receipt["provider_requests"] == 20
        and len(source.calls) == 20
    )
    assert any(
        r["query_classification"] == "financial_query_request_budget_exceeded"
        for r in result
        if "query_classification" in r
    )
    assert len(reader.cache) <= 64


def test_invalid_local_only_and_empty_explicit_cutoff_rejected(tmp_path):
    with TestClient(
        app_for(
            tmp_path,
            ReadStore(),
            FinancialQuery(lambda _: pytest.fail("invalid request fetched")),
        )
    ) as client:
        for extra in ({"local_only": "1"}, {"local_only": "TRUE"}, {"as_of": ""}):
            response = client.get(
                PREFIX + "/query",
                params=dict(start="2026-10-30", end="2026-10-30") | extra,
            )
            assert (
                response.status_code == 400
                and response.json()["classification"] == "financial_query_invalid"
            )


def test_unconfirmed_hole_can_fill_but_explicit_dot_is_retained():
    source = Source({("DGS10", "2026-10-29"): "4.1", ("DGS10", "2026-10-30"): "4.2"})
    reader = FinancialQuery(lambda _: source)
    service = FinancialDailyService(ReadStore(), clock=lambda: NOW)
    rows = service.query("2026-10-29", "2026-10-30", selected=("DGS10",), as_of=NOW)[
        "rows"
    ]
    rows[0]["state"] = "not_returned_calendar_unconfirmed"
    rows[1]["state"] = "provider_missing"
    result, receipt = reader.supplement(service, rows, NOW)
    assert result[0]["value"] == "4.1" and result[1]["value"] is None
    assert (
        result[1]["state"] == "provider_missing" and receipt["provider_requests"] == 1
    )


def test_descending_order_is_global_date_major_across_pages_and_legacy_is_unchanged(
    tmp_path,
):
    store = ReadStore()
    reader = FinancialQuery(
        lambda _: pytest.fail("historical ordering constructed a source")
    )
    params = dict(
        start="2026-10-29",
        end="2026-10-31",
        series="SOFR,DGS10",
        as_of=NOW.isoformat(),
        limit=3,
        order="desc",
    )
    with TestClient(app_for(tmp_path, store, reader)) as client:
        first = client.get(PREFIX + "/query", params=params).json()
        second = client.get(PREFIX + "/query", params=params | {"offset": 3}).json()
        back = client.get(PREFIX + "/query", params=params).json()
        legacy = client.get(
            PREFIX + "/query", params=params | {"order": "asc", "limit": 6}
        ).json()

    def ids(r):
        return [(x["observation_date"], x["series_id"]) for x in r["rows"]]

    assert ids(first) == [
        ("2026-10-31", "SOFR"),
        ("2026-10-31", "DGS10"),
        ("2026-10-30", "SOFR"),
    ]
    assert ids(second) == [
        ("2026-10-30", "DGS10"),
        ("2026-10-29", "SOFR"),
        ("2026-10-29", "DGS10"),
    ]
    assert (
        ids(back) == ids(first)
        and first["next_offset"] == 3
        and second["next_offset"] is None
    )
    assert len(set(ids(first) + ids(second))) == 6
    assert ids(legacy) == [
        (at, sid)
        for sid in ("SOFR", "DGS10")
        for at in ("2026-10-29", "2026-10-30", "2026-10-31")
    ]
    assert all(r["provider_requests"] == 0 for r in (first, second, back, legacy))


def test_descending_page_roundtrip_reuses_same_upstream_gap(tmp_path):
    source = Source({("DGS10", "2026-10-30"): "4.1"})
    store = ReadStore([value("DGS10", "2026-10-29", "4.0")])
    params = dict(
        start="2026-10-29", end="2026-10-30", series="DGS10", limit=1, order="desc"
    )
    with TestClient(
        app_for(tmp_path, store, FinancialQuery(lambda _: source))
    ) as client:
        pages = [
            client.get(PREFIX + "/query", params=params | {"offset": off}).json()
            for off in (0, 1, 0)
        ]
    assert [p["provider_requests"] for p in pages] == [1, 0, 0]
    assert [p["rows"][0]["value"] for p in pages] == ["4.1", "4.0", "4.1"]
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    "extra", [{"series": ""}, {"order": "date_desc"}, {"order": ""}]
)
def test_empty_selection_or_invalid_sort_is_rejected_before_read_or_upstream(
    tmp_path, extra
):
    with TestClient(
        app_for(
            tmp_path,
            ReadStore(),
            FinancialQuery(lambda _: pytest.fail("invalid selection fetched")),
        )
    ) as client:
        response = client.get(
            PREFIX + "/query",
            params=dict(start="2026-10-29", end="2026-10-30", series="DGS10") | extra,
        )
    assert response.status_code == 400
    assert response.json()["classification"] in {
        "financial_series_invalid",
        "financial_query_invalid",
    }
