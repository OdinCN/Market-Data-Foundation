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


def release_openapi():
    """Wire schema for the five-family evidence read route."""
    string = {"type": "string"}
    nullable = {"type": ["string", "null"]}
    scope = {
        k: string
        for k in (
            "series_id",
            "reference_month",
            "statistic",
            "unit",
            "adjustment",
            "role",
            "value",
            "value_identity",
            "evidence_id",
        )
    }
    source = {
        "type": "object",
        "required": ["value", "precision", "basis", "source"],
        "properties": {
            "value": string,
            "precision": {"type": "string", "enum": ["date", "instant", "interval"]},
            "basis": string,
            "source": string,
        },
    }
    claim = {
        "type": "object",
        "required": [k for k in scope if k != "evidence_id"],
        "properties": {
            **scope,
            "continuous_version": {
                "type": ["object", "null"],
                "additionalProperties": True,
            },
            "value_basis": string,
            "publisher_reported_rate": {"type": "boolean"},
            "confirmation_state": string,
            "mdf_first_captured_missing": nullable,
            "mdf_pre_event_previous_missing": nullable,
            "mdf_pre_event_previous_basis": string,
            "mdf_pre_event_guaranteed_previous": {
                "type": ["object", "null"],
                "additionalProperties": True,
            },
            "association": {"type": ["object", "null"], "additionalProperties": True},
            "association_missing": nullable,
            "mdf_first_captured": {
                "type": ["object", "null"],
                "additionalProperties": True,
            },
            "mdf_pre_event_last_consumable_previous": {
                "type": ["object", "null"],
                "additionalProperties": True,
            },
            "consensus": {"type": ["object", "null"], "additionalProperties": True},
            "surprise": nullable,
            "surprise_missing": {"type": "array", "items": string},
            "locator": {"type": "object", "additionalProperties": True},
        },
    }
    evidence = {
        "type": "object",
        "properties": {
            **{
                k: string
                for k in (
                    "evidence_id",
                    "raw_sha256",
                    "raw_locator",
                    "obtained_at",
                    "available_at",
                    "parser_revision",
                    "source_url",
                )
            },
            "source_version": source,
            "retention": {"type": "object", "additionalProperties": True},
            "claims": {"type": "array", "items": claim},
        },
    }
    event = {
        "type": "object",
        "required": [
            "event_id",
            "reference_months",
            "source_version",
            "claims",
            "evidence",
            "state",
        ],
        "properties": {
            "event_id": string,
            "family": string,
            "reference_month": string,
            "reference_months": {"type": "array", "items": string},
            "actual_at": nullable,
            "source_version": source,
            "claims": {"type": "array", "items": claim},
            "evidence": {"type": "array", "items": evidence},
            "state": {"type": "string", "enum": ["confirmed", "unknown"]},
            "missing": {"type": "array", "items": string},
            "missing_scopes": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": [
                        "series_id",
                        "reference_month",
                        "statistic",
                        "unit",
                        "adjustment",
                        "reason",
                    ],
                    "properties": {
                        **{
                            k: string
                            for k in (
                                "series_id",
                                "reference_month",
                                "statistic",
                                "unit",
                                "adjustment",
                                "reason",
                                "coverage_basis",
                                "evidence_id",
                            )
                        }
                    },
                },
            },
            "conflicts": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "consensus_snapshots": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
        },
    }
    response = {
        "type": "object",
        "required": [
            "events",
            "missing",
            "as_of",
            "as_of_basis",
            "provider_requests",
            "persisted",
        ],
        "properties": {
            "read_mode": {"type": "string", "enum": ["event", "source_history"]},
            "as_of": {"type": "string", "format": "date-time"},
            "as_of_basis": {"type": "string", "const": "available"},
            "events": {"type": "array", "items": event},
            "consensus_snapshots": {
                "type": "array",
                "items": {"type": "object", "additionalProperties": True},
            },
            "missing": {"type": "array", "items": string},
            "provider_requests": {"type": "integer", "const": 0},
            "provider_attempts": {"type": "integer", "const": 0},
            "persisted": {"type": "boolean", "const": False},
            "history_basis": string,
            "coverage": string,
        },
    }
    descriptions = {
        "event_id": (
            "Exact canonical official event ID; BEA PIO "
            "shared across metrics and reference months."
        ),
        "reference_month": (
            "Optional YYYY-MM event COVERAGE filter, not only primary reference month."
        ),
        "as_of": (
            "Inclusive aware MDF availability cutoff, "
            "also applied to consensus and version "
            "associations."
        ),
        "read_mode": (
            "event (default) / source_history; retained "
            "official archives only; ALFRED not "
            "implemented and no latest fallback."
        ),
        "source_version": (
            "Exact source version DATE YYYY-MM-DD; "
            "remains date precision, never midnight "
            "publication time."
        ),
    }
    return {
        "parameters": [
            {
                "in": "query",
                "name": k,
                "schema": {"type": "string"},
                "required": False,
                "description": v,
            }
            for k, v in descriptions.items()
        ],
        "responses": {
            "200": {
                "description": (
                    "Local retained evidence and per-scope "
                    "absence; never creates schema or requests "
                    "upstream."
                ),
                "content": {"application/json": {"schema": response}},
            }
        },
    }
