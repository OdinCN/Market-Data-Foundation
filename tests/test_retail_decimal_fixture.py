"""Rebuild a minimal offline retail wire contract with the production serializer.

Only synthetic in-process HTTP is used; no DEV response, database or provider.
"""

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from fastapi.responses import JSONResponse

from market_data_foundation.historical.census_retail_release_schedule import (
    CALENDAR_URL,
)
from market_data_foundation.historical.retail_release_scheduler import (
    RetailReleaseScheduler,
)
from market_data_foundation.web.retail_timeseries import (
    PREFIX,
    RetailTimeSeriesHost,
    create_app,
    json_value,
)
from market_data_foundation.web.retail_timeseries_host import RetailContinuousHost
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_ppi_timeseries_web import client


def body():
    def calc(value):
        return {
            "value": value,
            "reason": "source_operand_unavailable" if value is None else None,
        }

    points = []
    for index in range(15):
        year, month = divmod(2025 * 12 + 9 + index, 12)
        missing = index >= 12
        rates = {
            str(h): calc(None if missing else Decimal("0.0123456789012345678901234567"))
            for h in (1, 3, 6, 12)
        }
        if index == 0:
            rates["1"] = calc(Decimal("0.000000000"))
        elif index == 1:
            rates["1"] = calc(Decimal("1.25E-7"))
        points.append(
            {
                "month": f"{year:04d}-{month + 1:02d}",
                "headline_level": calc(
                    None
                    if missing
                    else Decimal(
                        "745021.000000000"
                        if index == 0
                        else "12345678901234567890.123456789000"
                        if index == 1
                        else "123456.123456789"
                    )
                ),
                "control_level": calc(
                    None if missing else Decimal("1.00000000000000000001E+30")
                ),
                "headline_rates": rates,
                "control_rates": rates,
                "official_headline_mpcsm": {
                    "value": None
                    if missing
                    else Decimal(
                        {0: "0.000000000", 1: "0.800000000", 2: "-0.400000000"}.get(
                            index, "1.25E-7"
                        )
                    ),
                    "unit": "percent",
                },
                "source_cells": [
                    {
                        "value": None if missing else Decimal("1"),
                        "status": "not_provided" if missing else "available",
                    }
                    for _ in range(6)
                ],
            }
        )
    result = {
        "status": "PARTIAL",
        "source_no_content": [
            {
                "classification": "census_retail_no_content",
                "http_status": 204,
                "response_bytes": 0,
                "start_month": "2026-10",
                "end_month": "2026-12",
            }
        ],
        "source_failure": None,
        "start_month": "2025-10",
        "end_month": "2026-12",
        "persisted": False,
        "provider_requests": 1,
        "provider_attempts": 1,
        "as_of": None,
        "as_of_basis": "observed",
        "query_mode": "current",
        "units": {
            "levels": "million_current_usd",
            "rates": "ratio",
            "official_headline_mpcsm": "percent",
        },
        "points": points,
        "backend_extension": {"exact": Decimal("12345678901234567890.123456789")},
    }
    calendar = {
        "source": CALENDAR_URL,
        "sha256": "a" * 64,
        "acquired_at": "2026-11-01T03:56:22.565612+00:00",
        "events": {
            month: {"reference_month": month, "release_at": at, "status": status}
            for month, at, status in (
                ("2026-08", None, "TBA"),
                ("2026-09", "2026-10-15T12:30:00+00:00", "scheduled"),
                ("2026-10", "2026-11-17T13:30:00+00:00", "scheduled"),
                ("2026-11", "2026-12-16T13:30:00+00:00", "scheduled"),
                ("2026-12", "2027-01-15T13:30:00+00:00", "scheduled"),
            )
        },
    }
    scheduler = RetailReleaseScheduler(
        None,
        Path("/synthetic-unused-schedule"),
        clock=lambda: datetime(2026, 11, 1, 4, tzinfo=UTC),
    )
    scheduler._read = lambda: {"calendar": calendar, "targets": []}
    host = SimpleNamespace(scheduler=scheduler, official_origin=CALENDAR_URL)
    return RetailContinuousHost.publication_metadata(host, result)


def serialized_body():
    return JSONResponse(json_value(body())).body.decode("utf-8") + "\n"


def test_frontend_wire_fixture_matches_real_backend_decimal_serialization():
    sample = Path(__file__).parent / "fixtures" / "retail_decimal_response.json"
    assert sample.read_text() == serialized_body()
    wire = json.loads(sample.read_text())
    assert wire["points"][0]["official_headline_mpcsm"]["value"] == "0E-9"
    assert wire["points"][1]["headline_rates"]["1"]["value"] == "1.25E-7"

    # The frontend consumes this exact body. Exercise the actual router/response
    # serializer as well as the fixture helper, with only the service injected.
    calls = []

    def query(start, end, **options):
        calls.append((start, end, options))
        return body()

    app = create_app(
        RetailTimeSeriesHost(SimpleNamespace(query_range=query), query_allowed=True),
        authorize=lambda _: True,
    )
    with client(app) as http:
        response = http.get(
            PREFIX + "/query",
            params={"start": "2025-10", "end": "2026-12", "local_only": "true"},
        )
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.text + "\n" == sample.read_text()
    assert calls == [("2025-10", "2026-12", {"local_only": True})]


if __name__ == "__main__":
    print(serialized_body(), end="")
