"""Explicit saved-source probes; one admitted exchange, no collection or DB write.

Uses the product transports and their existing shared admission state. Only safe
facts escape this module; provider messages, bodies, keys and URLs do not.
"""

import json
from decimal import Decimal
from urllib.parse import urlencode

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.bls_cpi_direct import ENDPOINT as BLS_ENDPOINT
from market_data_foundation.historical.bls_cpi_direct_live import ExactOneHttpsTransport
from market_data_foundation.historical.census_retail import _DECIMAL as CENSUS_DECIMAL
from market_data_foundation.historical.census_retail import ENDPOINT as CENSUS_ENDPOINT
from market_data_foundation.historical.census_retail_live import ExactCensusTransport
from market_data_foundation.historical.claims_source import (
    ClaimsHttpsTransport,
    ClaimsRequest,
)
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControl,
    retry_after_at,
)
from market_data_foundation.historical.cpi_timeseries_source import _daily_limit
from market_data_foundation.historical.pce_timeseries_source import (
    ENDPOINT as BEA_ENDPOINT,
)
from market_data_foundation.historical.pce_timeseries_source import (
    BeaHttpsTransport,
    _decode,
)
from market_data_foundation.source_request_policy import classify_source_exception
from market_data_foundation.web.package_catalog import VerificationFacts


def _valid_census_cell(doc):
    # MARTS appends predicate dimensions already selected in get. The actual
    # one-cell response repeats these four dimensions, with identical values.
    targets = {
        "time": "2025-01",
        "category_code": "44X72",
        "data_type_code": "SM",
        "seasonally_adj": "yes",
        "error_data": "no",
        "us": "1",
    }
    if not isinstance(doc, list) or len(doc) != 2:
        return False
    header, row = doc
    if (
        not isinstance(header, list)
        or not all(isinstance(field, str) for field in header)
        or set(header) != {*targets, "cell_value"}
        or not isinstance(row, list)
        or len(row) != len(header)
        or not all(isinstance(value, str) for value in row)
    ):
        return False
    repeated = {"category_code", "data_type_code", "seasonally_adj", "error_data"}
    for field in {*targets, "cell_value"}:
        indices = [i for i, name in enumerate(header) if name == field]
        if len(indices) not in ((1, 2) if field in repeated else (1,)):
            return False
        if any(row[i] != row[indices[0]] for i in indices):
            return False
        if field in targets and row[indices[0]] != targets[field]:
            return False
    value = row[header.index("cell_value")]
    return CENSUS_DECIMAL.fullmatch(value) is not None and Decimal(value) > 0


def response_facts(source, status, body):
    if status != 200:
        return VerificationFacts(
            request_status="failed",
            http_status=status,
            reason={
                401: "http_unauthorized",
                403: "http_forbidden",
                429: "rate_limited",
            }.get(status, "http_failure"),
        )
    try:
        doc = json.loads(body)
    except ValueError, UnicodeError:
        return VerificationFacts(
            request_status="failed", http_status=200, reason="response_invalid"
        )
    valid = False
    business_error = False
    if source == "FRED" and isinstance(doc, dict):
        business_error = "error_code" in doc or "error_message" in doc
        rows = doc.get("seriess")
        valid = (
            isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], dict)
            and rows[0].get("id") == "ICSA"
        )
    elif source == "BLS" and isinstance(doc, dict):
        business_error = doc.get("status") != "REQUEST_SUCCEEDED"
        results = doc.get("Results")
        rows = results.get("series") if isinstance(results, dict) else None
        valid = (
            isinstance(rows, list)
            and len(rows) == 1
            and isinstance(rows[0], dict)
            and rows[0].get("seriesID") == "CUUR0000SA0"
            and bool(rows[0].get("data"))
        )
    elif source == "BEA" and isinstance(doc, dict):
        api = doc.get("BEAAPI", {})
        if isinstance(api, dict):
            results = api.get("Results", {})
            business_error = "Error" in api or (
                isinstance(results, dict) and "Error" in results
            )
            error = api.get("Error") or (
                results.get("Error") if isinstance(results, dict) else None
            )
            if isinstance(error, dict):
                # Official BEA API guide: error 1 is invalid UserID; 7 is quota.
                # Other business/HTTP failures do not establish Key rejection.
                code = error.get("APIErrorCode")
                if type(code) in (str, int) and str(code) in {"1", "7"}:
                    return VerificationFacts(
                        request_status="failed",
                        http_status=200,
                        reason="business_error" if str(code) == "1" else "rate_limited",
                        authentication="rejected" if str(code) == "1" else "unknown",
                    )
            valid = (
                isinstance(results, dict)
                and isinstance(results.get("Dataset"), list)
                and bool(results["Dataset"])
                and all(
                    isinstance(row, dict)
                    and isinstance(row.get("DatasetName"), str)
                    and bool(row["DatasetName"].strip())
                    for row in results["Dataset"]
                )
            )
    elif source == "Census":
        valid = _valid_census_cell(doc)
        business_error = isinstance(doc, dict) and ("error" in doc or "errors" in doc)
    if business_error or not valid:
        return VerificationFacts(
            request_status="failed",
            http_status=200,
            reason="business_error" if business_error else "response_invalid",
        )
    # BEA GetDatasetList requires a valid UserID (official API guide).
    # Census requires a key (official MARTS examples); only a valid requested
    # numeric cell, obtained with the saved key, establishes confirmation.
    # BLS success alone is insufficient.
    return VerificationFacts(
        request_status="succeeded",
        http_status=200,
        reason="request_succeeded",
        authentication="confirmed"
        if source in {"FRED", "BEA", "Census"}
        else "unknown",
    )


def bind_source_verifier(runtime, claims_options):
    controls = {"BLS": runtime.request_control}
    if hasattr(runtime, "bea_request_control"):
        controls["BEA"] = runtime.bea_request_control
    if hasattr(runtime, "retail_request_control"):
        controls["Census"] = runtime.retail_request_control
    if claims_options is not None:
        controls["FRED"] = CpiRequestControl(
            claims_options.request_control_path
            or runtime.settings.config_directory / "claims-fred-request-control.json",
            clock=runtime.clock,
            min_interval_seconds=1.1,
        )

    def verify(source, credential, record):
        hosts = [
            h
            for h in runtime.packages.timeseries_hosts.values()
            if h is not None and h.source_id == source
        ]
        if source not in controls:
            return VerificationFacts(reason="verification_not_assembled")
        if not any(h.upstream_access(h.source(record))["allowed"] for h in hosts):
            return VerificationFacts(reason="source_not_admitted")
        control = controls[source]
        attempted = False
        try:
            with control.attempt() as admission:
                attempted = True
                if source == "FRED":
                    response = ClaimsHttpsTransport(credential)(
                        ClaimsRequest(
                            "metadata", {"series_id": "ICSA", "file_type": "json"}
                        )
                    )
                elif source == "BLS":
                    response = ExactOneHttpsTransport().post(
                        BLS_ENDPOINT,
                        json.dumps(
                            {
                                "seriesid": ["CUUR0000SA0"],
                                "startyear": "2025",
                                "endyear": "2025",
                                "registrationkey": credential,
                            }
                        ).encode(),
                        {
                            "Content-Type": "application/json",
                            "Accept": "application/json",
                        },
                        timeout=20,
                        max_response_bytes=1024 * 1024,
                    )
                elif source == "BEA":
                    response = BeaHttpsTransport().post(
                        BEA_ENDPOINT,
                        urlencode(
                            {
                                "UserID": credential,
                                "method": "GetDatasetList",
                                "ResultFormat": "JSON",
                            }
                        ).encode(),
                        {"Accept": "application/json"},
                        timeout=20,
                        max_response_bytes=1024 * 1024,
                    )
                else:
                    response = ExactCensusTransport().get(
                        CENSUS_ENDPOINT,
                        {
                            "get": (
                                "data_type_code,seasonally_adj,category_code,"
                                "cell_value,error_data"
                            ),
                            "time": "2025-01",
                            "for": "us:*",
                            "category_code": "44X72",
                            "data_type_code": "SM",
                            "seasonally_adj": "yes",
                            "error_data": "no",
                            "key": credential,
                        },
                        timeout=20,
                        max_response_bytes=1024 * 1024,
                    )
                body = response.body
                if source == "BEA":
                    if response.status == 429:
                        admission(
                            retry_after_at(
                                response.headers, runtime.clock(), minimum_seconds=300
                            )
                        )
                else:
                    daily_limit = False
                    if source == "BLS" and response.status == 200:
                        try:
                            daily_limit = _daily_limit(json.loads(body))
                        except ValueError, TypeError:
                            pass
                    admission.record_response(
                        response.status,
                        response.headers,
                        provider_daily_limit=daily_limit,
                    )
                    if daily_limit:
                        return VerificationFacts(
                            request_status="failed",
                            http_status=200,
                            reason="rate_limited",
                        )
                if source == "BEA" and response.status == 200:
                    # The existing BEA contract permits UserID in Request only.
                    # Discard that envelope before scanning or classifying Results.
                    document = _decode(body)
                    envelope = document.get("BEAAPI")
                    if isinstance(envelope, dict):
                        body = json.dumps(
                            {
                                "BEAAPI": {
                                    k: v for k, v in envelope.items() if k != "Request"
                                }
                            }
                        ).encode()
                if credential.casefold().encode() in body.lower():
                    return VerificationFacts(
                        request_status="failed",
                        http_status=response.status,
                        reason="response_invalid",
                    )
                facts = response_facts(source, response.status, body)
                if (
                    source == "BEA"
                    and response.status == 200
                    and facts.reason == "rate_limited"
                ):
                    # Explicit BEA business error 7 shares the HTTP429 cooldown.
                    admission(
                        retry_after_at(
                            response.headers, runtime.clock(), minimum_seconds=300
                        )
                    )
                return facts
        except Exception as error:
            classification, _ = classify_source_exception(error)
            if isinstance(error, ContractError):
                classification = "protocol_failure"
            if not attempted:
                control_reason = getattr(error, "classification", "")
                classification = (
                    "rate_limited"
                    if control_reason.endswith(
                        (
                            "request_cooldown",
                            "provider_daily_limit",
                            "request_budget_exhausted",
                        )
                    )
                    else "admission_denied"
                )
            return VerificationFacts(
                request_status="failed" if attempted else "not_run",
                reason=classification,
                http_status=getattr(error, "http_status", None)
                if isinstance(error, ContractError)
                else None,
            )

    runtime.packages.source_verifier = verify
