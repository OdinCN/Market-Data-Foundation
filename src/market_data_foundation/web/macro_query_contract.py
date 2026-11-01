"""OpenAPI parameters for the existing Request-based monthly query routes."""

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.macro_research_fields import query_columns


def query_openapi(family):
    descriptions = {
        "start": "Inclusive reference month YYYY-MM.",
        "end": "Inclusive reference month YYYY-MM; descending result presentation.",
        "local_only": (
            "true/false; CPI default true, other families false. History "
            "always local; GET never writes."
        ),
        "query_mode": (
            "current/history; inferred history with as_of or available basis."
        ),
        "as_of": (
            "Aware ISO8601 inclusive MDF cutoff; every operand and comparison "
            "uses this same cutoff. +08:00 is accepted."
        ),
        "as_of_basis": (
            "observed/available; history defaults available. Must agree with "
            "vintage_mode."
        ),
        "vintage_mode": "Alias of as_of_basis. Never official vintage.",
        "columns": "Optional comma-separated numeric fields: "
        + ",".join(query_columns(family))
        + ". Omitted preserves existing fields and metadata.",
    }
    if family == "retail":
        descriptions["basket"] = (
            "CSV headline/control/ex_auto or all; core aliases control. "
            "Default headline,control. Six shared source inputs; ex_auto=44X72-441."
        )
    elif family == "employment":
        descriptions["series"] = (
            "CSV allowlisted CES/CPS IDs; omitted preserves original five. "
            "Includes private/EPOP/eleven non-overlapping major sectors; "
            "no summation of aggregates."
        )
        descriptions["measure"] = (
            "index only; native value and per-series units, not a price index."
        )
    else:
        profile = {"cpi": CPI_PROFILE, "ppi": PPI_PROFILE, "pce": PCE_PROFILE}[family]
        descriptions["basket"] = (
            "CSV " + ",".join(profile.baskets) + " or all; default headline."
        )
        descriptions["adjustment"] = (
            "sa/nsa/all; default sa. PCE has SA only. "
            "CPI SA short-term and NSA annual stay separate series."
        )
        descriptions["measure"] = (
            "index/mom/yoy/change_{1,3,6,12}m_ratio; default index."
            + (
                " PCE also annualized_3m_ratio/annualized_6m_ratio: "
                "unrounded same-cutoff index ratios raised to 4/2, minus one."
                if profile is PCE_PROFILE
                else ""
            )
        )
    return {
        "parameters": [
            {
                "in": "query",
                "name": name,
                "required": name in {"start", "end"},
                "schema": {
                    "type": "string",
                    **(
                        {"pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"}
                        if name in {"start", "end"}
                        else {}
                    ),
                },
                "description": description,
            }
            for name, description in descriptions.items()
        ]
    }


def contract_app():
    from fastapi import FastAPI

    from market_data_foundation.web.cpi_timeseries_api import timeseries_router

    app = FastAPI()
    for profile in (CPI_PROFILE, PPI_PROFILE, PCE_PROFILE, EMPLOYMENT_PROFILE):
        app.include_router(timeseries_router(None, profile=profile))
    return app


def job_openapi(family):
    properties = {
        "request_id": {"type": "string", "format": "uuid"},
        "action": {"type": "string", "enum": ["init", "initialize", "update"]},
        "start": {"type": "string", "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
        "end": {"type": "string", "pattern": r"^[0-9]{4}-(0[1-9]|1[0-2])$"},
    }
    if family in {"cpi", "ppi", "employment"}:
        properties["series"] = {
            "type": "string",
            "description": (
                "Optional allowlisted CSV subset; omitted collects every "
                "profile input. Use only newly admitted IDs for historical backfill."
            ),
        }
    return {
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["request_id", "action", "start", "end"],
                        "properties": properties,
                    }
                }
            },
        }
    }
