"""Rebuild a minimal offline retail wire contract with the production serializer.

No DEV response, database, ASGI bridge, provider or credentials are used.
"""

import json
from decimal import Decimal
from pathlib import Path

from market_data_foundation.web.retail_timeseries import json_value


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
                    None if missing else Decimal("123456.123456789")
                ),
                "control_level": calc(
                    None if missing else Decimal("1.00000000000000000001E+30")
                ),
                "headline_rates": rates,
                "control_rates": rates,
                "official_headline_mpcsm": {
                    "value": None
                    if missing
                    else Decimal("0.000000000" if index == 0 else "1.25E-7"),
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
    return {
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


def serialized_body():
    return json.dumps(json_value(body()), separators=(",", ":")) + "\n"


def test_frontend_wire_fixture_matches_real_backend_decimal_serialization():
    sample = Path(__file__).parent / "fixtures" / "retail_decimal_response.json"
    assert sample.read_text() == serialized_body()
    wire = json.loads(sample.read_text())
    assert wire["points"][0]["official_headline_mpcsm"]["value"] == "0E-9"
    assert wire["points"][1]["headline_rates"]["1"]["value"] == "1.25E-7"


if __name__ == "__main__":
    print(serialized_body(), end="")
