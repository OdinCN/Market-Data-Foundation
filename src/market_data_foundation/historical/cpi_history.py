"""CPI index observations fetched now, never historical first-release vintages.

Each bounded batch is one reference month plus its comparison years. Raw index
requests need neither catalog nor provider calculation privileges. Qualification
is supplied by the trusted host, never inferred from a key's presence.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

from market_data_foundation.canonical.model import (
    AcquisitionOccurrence,
    DataRun,
    DatasetVersion,
    Decision,
    EvidenceMaterial,
    FiniteScope,
    Grade,
    PublicationRequest,
    RunKind,
    Scope,
    Transform,
    digest,
    instant,
    require,
)
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.bls_cpi_direct import (
    ENDPOINT,
    MAX_RESPONSE_BYTES,
    SERIES_IDS,
    DirectCpiLivePlan,
    DirectCpiReferenceContract,
    _binding,
    _decode,
    _encode,
    _footnotes,
    _source,
)
from market_data_foundation.historical.bls_cpi_direct_live import (
    TIMEOUT_SECONDS,
    LiveExecutionCandidate,
    LiveExecutionError,
    _request_bls,
    load_registration_key,
)
from market_data_foundation.historical.cpi_job_journal import valid_identity
from market_data_foundation.historical.cpi_retained_handoff import (
    MEASURES,
    CpiReferenceSemantics,
)

REVISION = "r5-cpi-history-index-v1"
SOURCE = "mdf.source-candidate.bls-v2.cpi.history-index"
COMPLETENESS = "four_cpi_index_derived_changes_complete"
RULE = "r5-cpi-history-index-admission-v1"


def month_number(value: str) -> int:
    require(
        type(value) is str and re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value),
        "cpi_history_month_invalid",
    )
    year, month = map(int, value.split("-"))
    require(2 <= year <= 9998, "cpi_history_month_invalid")
    return year * 12 + month - 1


def month_text(value: int) -> str:
    year, month = divmod(value, 12)
    return f"{year:04d}-{month + 1:02d}"


def months(start: str, end: str) -> tuple[str, ...]:
    first, last = month_number(start), month_number(end)
    require(0 <= last - first < 360, "cpi_history_range_budget_exceeded")
    return tuple(month_text(n) for n in range(first, last + 1))


def contract(reference: str) -> DirectCpiReferenceContract:
    n = month_number(reference)
    return DirectCpiReferenceContract(
        CpiReferenceSemantics(
            reference,
            month_text(n - 1),
            month_text(n - 12),
            "current_observation_not_historical_vintage",
        ),
        REVISION,
        RULE,
        COMPLETENESS,
        SOURCE,
        "mdf.binding-candidate.bls-v2.cpi.history-index",
        REVISION,
        "historical_index_observation",
        "actual obtained_at; release/vintage unknown",
        "explicit_index_arithmetic_not_provider_pct_changes",
    )


def source_binding():
    c = contract("2000-01")  # identities and source metadata are reference invariant
    source = replace(
        _source(c), variant="four-index-series-comparison-years-no-options"
    )
    binding = _binding(c, source)
    mapping = replace(binding.mappings[0], temporal_revision=REVISION)
    return source, replace(binding, mappings=(mapping,))


@dataclass(frozen=True)
class HistoryQualification:
    """Host attests current tier and shared-account remaining daily wire budget.

    Conservative two-year requests work within either currently documented tier.
    The host must account for other processes sharing its key/IP. This local
    scheduler charges each physical transport attempt before dispatch, including
    retries, against that attempt's UTC day. Ambiguous charges are never refunded.
    """

    tier: str
    max_years: int
    daily_wire_budget: int
    minimum_spacing_seconds: float = 1.0

    def __post_init__(self):
        require(self.tier in {"unregistered", "registered"}, "cpi_history_tier_invalid")
        require(
            type(self.max_years) is int
            and 2 <= self.max_years <= (20 if self.tier == "registered" else 10),
            "cpi_history_year_qualification_invalid",
        )
        require(
            type(self.daily_wire_budget) is int
            and 3
            <= self.daily_wire_budget
            <= (500 if self.tier == "registered" else 25),
            "cpi_history_quota_invalid",
        )
        require(
            type(self.minimum_spacing_seconds) in {int, float}
            and 1 <= self.minimum_spacing_seconds <= 86400,
            "cpi_history_rate_invalid",
        )


def request(reference: str) -> dict:
    month_number(reference)
    year = int(reference[:4])
    return {
        "endpoint": ENDPOINT,
        "method": "POST",
        "body": {
            "seriesid": list(SERIES_IDS),
            "startyear": str(year - 1).zfill(4),
            "endyear": str(year).zfill(4),
            "catalog": False,
            "calculations": False,
            "annualaverage": False,
        },
    }


def project(raw: bytes, reference: str) -> dict:
    c = contract(reference)
    payload = _decode(raw)
    require(
        payload.get("status") == "REQUEST_SUCCEEDED", "cpi_history_provider_rejected"
    )
    # Provider warnings can signify truncation or tier denial despite HTTP 200.
    require(payload.get("message") == [], "cpi_history_provider_warning")
    series = payload.get("Results", {}).get("series")
    require(type(series) is list and len(series) == 4, "cpi_history_series_invalid")
    by_series = {}
    allowed_years = {request(reference)["body"][k] for k in ("startyear", "endyear")}
    for item in series:
        require(
            type(item) is dict and item.get("seriesID") in SERIES_IDS,
            "cpi_history_series_invalid",
        )
        sid = item["seriesID"]
        require(
            sid not in by_series and type(item.get("data")) is list,
            "cpi_history_series_invalid",
        )
        rows = {}
        for row in item["data"]:
            require(
                type(row) is dict and row.get("year") in allowed_years,
                "cpi_history_row_invalid",
            )
            period = row.get("period")
            require(
                type(period) is str and re.fullmatch(r"M(0[1-9]|1[0-2])", period),
                "cpi_history_period_invalid",
            )
            pointer = row["year"] + "-" + period[1:]
            require(pointer not in rows, "cpi_history_duplicate_period")
            value = row.get("value")
            require(
                type(value) is str
                and (value == "-" or re.fullmatch(r"[0-9]{1,9}(\.[0-9]{1,6})?", value)),
                "cpi_history_index_invalid",
            )
            notes = _footnotes(row.get("footnotes"))
            rows[pointer] = (None if value == "-" else Decimal(value), notes)
        by_series[sid] = rows
    measurements = []
    indices = []
    for name, (sid, meaning, lag) in MEASURES.items():
        comparison = (
            c.semantics.mom_comparison_period
            if lag == 1
            else c.semantics.yoy_comparison_period
        )
        target, notes = by_series[sid].get(reference, (None, []))
        prior, prior_notes = by_series[sid].get(comparison, (None, []))
        available = target is not None and prior is not None and prior > 0
        with localcontext() as context:
            context.prec = 28
            value = (
                str(
                    ((target / prior - 1) * 100).quantize(
                        Decimal("0.1"), rounding=ROUND_HALF_UP
                    )
                )
                if available
                else None
            )
        measurements.append(
            {
                "name": name,
                "measure_identity": "bls:cpi:" + name,
                "series_id": sid,
                "meaning": meaning,
                "change_months": lag,
                "comparison_period": comparison,
                "seasonal_adjustment": "Seasonally Adjusted"
                if lag == 1
                else "Not Seasonally Adjusted",
                "reference_period": reference,
                "unit": "percent",
                "classification": "mdf_index_derived_change",
                "value": value,
                "availability": "available" if available else "unavailable",
                "unavailable_reason": None
                if available
                else "target_or_comparison_index_missing_or_zero",
                "preliminary": any(n["code"] == "P" for n in notes + prior_notes),
                "finality": "not_asserted",
                "footnotes": notes,
            }
        )
        indices.append(
            {
                "series_id": sid,
                "unit": "index_1982_1984_100",
                "reference_period": reference,
                "value": str(target) if target is not None else None,
                "comparison_period": comparison,
                "comparison_value": str(prior) if prior is not None else None,
                "comparison_footnotes": prior_notes,
            }
        )
    return {
        "product": "CPI",
        "reference_period": reference,
        "measurements": measurements,
        "availability": {
            "state": "available"
            if all(m["value"] is not None for m in measurements)
            else "incomplete",
            "basis": c.semantics.availability_basis,
        },
        "indices": indices,
        "calculation": (
            "(index/reference_comparison_index-1)*100; "
            "Decimal; half-up 0.1 percentage point"
        ),
    }


@dataclass(frozen=True)
class HistoryEvidence:
    raw_bytes: bytes
    reference: str
    requested_at: datetime
    obtained_at: datetime
    provider_data_attempts: int
    controlled_external_receipt: str


def build_plan(
    evidence: HistoryEvidence,
    job_id: str,
    *,
    finished_at: datetime,
    frozen_at: datetime,
) -> DirectCpiLivePlan:
    valid_identity(job_id)
    require(type(evidence) is HistoryEvidence, "cpi_history_evidence_required")
    for at in (evidence.requested_at, evidence.obtained_at, finished_at, frozen_at):
        instant(at)
    require(
        evidence.requested_at <= evidence.obtained_at <= finished_at <= frozen_at,
        "cpi_history_clock_invalid",
    )
    require(
        type(evidence.provider_data_attempts) is int
        and 1 <= evidence.provider_data_attempts <= 3,
        "cpi_history_attempts_invalid",
    )
    data = project(evidence.raw_bytes, evidence.reference)
    require(data["availability"]["state"] == "available", "cpi_history_incomplete")
    source, binding = source_binding()
    identity = "r5-cpi-history-" + job_id + "-a1"
    req = request(evidence.reference)
    req_id = "bls-cpi-history:" + digest(_encode(req))
    transform = Transform(
        "mdf.cpi.history.index-arithmetic", REVISION, (("request_identity", req_id),)
    )
    run = DataRun(
        identity + ":run",
        RunKind.COLLECT,
        Scope(),
        evidence.requested_at,
        finished_at,
        "success",
        binding=binding.key,
        transform=transform,
        trigger="explicit_bounded_history_observation",
        budget=1,
    )
    acquisition = AcquisitionOccurrence(
        identity + ":acquisition",
        run.key,
        req_id,
        source.key,
        binding.key,
        "bls-cpi-headlines",
        Scope(),
        (
            ("request", _encode(req).decode()),
            ("job_identity", job_id),
            ("attempt_identity", job_id + "-a1"),
        ),
        evidence.requested_at,
        evidence.obtained_at,
        None,
        None,
        "historical_index_observation",
        COMPLETENESS,
        "provider_release_time_and_vintage_unknown",
        "success",
        external_receipt=evidence.controlled_external_receipt,
        provider_data_attempts=evidence.provider_data_attempts,
        pagination="single two-year response",
        recovery_summary="no fallback; no automatic replay",
    )
    content = _encode(
        {
            "schema": RULE,
            "data": data,
            "request": req,
            "obtained_at": evidence.obtained_at.isoformat(),
            "provider_released_at": None,
            "provider_vintage": None,
            "provider_revision": {"state": "not_declared", "value": None},
            "mode": "historical_index_observation",
            "historical_as_of_verified": False,
        }
    )
    raw = EvidenceMaterial(
        identity + ":raw",
        Grade.ORIGINAL,
        digest(evidence.raw_bytes),
        Scope(),
        acquired_from=acquisition.key,
        controlled_ref=evidence.controlled_external_receipt + "/raw-response.bin",
        durable=False,
        replayable=False,
        named_class="bls_index_response_observed_now",
    )
    semantic = EvidenceMaterial(
        identity + ":semantic",
        Grade.PROJECTED,
        digest(content),
        Scope(),
        acquired_from=acquisition.key,
        controlled_ref="mdf-content://" + identity + ":version",
        durable=True,
        replayable=True,
        named_class="mdf_index_derived_changes",
    )
    version = DatasetVersion(
        identity + ":version",
        BLS_CPI_HEADLINES.key,
        FiniteScope(
            tuple(f"{evidence.reference}:bls:cpi:{n}" for n in sorted(MEASURES))
        ),
        Scope(),
        frozen_at,
        run.key,
        digest(content),
        binding=binding.key,
        mapping="bls-cpi-headlines",
        acquisitions=(acquisition.key,),
        materials=(raw.key, semantic.key),
        transform=transform,
        completeness=COMPLETENESS,
    )
    publication = PublicationRequest(
        identity + ":publication",
        identity + ":occurrence",
        version.key,
        RULE,
        Decision.PUBLISH,
        Scope(),
        version.materials,
        frozenset({Grade.ORIGINAL, Grade.PROJECTED}),
        "current-observation-no-historical-vintage-claim",
        required_completeness=COMPLETENESS,
        claims_replayable=True,
    )
    return DirectCpiLivePlan(
        source, binding, run, acquisition, raw, semantic, version, publication, content
    )


def acquire(
    *,
    reference: str,
    qualification: HistoryQualification,
    secret_path: Path,
    transport,
    clock: Callable[[], datetime],
    artifact_root: Path,
    job_id: str,
):
    require(
        type(qualification) is HistoryQualification,
        "cpi_history_qualification_required",
    )
    valid_identity(job_id)
    req = request(reference)
    body = dict(req["body"])
    key = (
        load_registration_key(secret_path)
        if qualification.tier == "registered"
        else None
    )
    if key is not None:
        body["registrationkey"] = key
    started = clock()
    import time

    result, attempts = _request_bls(
        lambda: transport.post(
            ENDPOINT,
            _encode(body),
            {"Content-Type": "application/json", "Accept": "application/json"},
            timeout=TIMEOUT_SECONDS,
            max_response_bytes=MAX_RESPONSE_BYTES,
        ),
        prefix="bls_live_",
        sleep=time.sleep,
    )
    obtained = clock()
    require(
        not result.headers.get("content-encoding")
        or result.headers["content-encoding"].lower() == "identity",
        "cpi_history_encoding_invalid",
    )
    if key is not None and key.encode() in result.body:
        raise LiveExecutionError("bls_live_credential_persistence_forbidden", attempts)
    if key is not None:
        try:
            decoded = _encode(_decode(result.body))
        except Exception:
            raise LiveExecutionError(
                "bls_live_response_contract_failed", attempts
            ) from None
        # JSON escapes can hide echoes anywhere in the retained raw response,
        # including fields omitted by the business projection. Reject before
        # constructing a plan or writing any acquisition artifacts.
        if key.encode() in decoded:
            raise LiveExecutionError(
                "bls_live_credential_persistence_forbidden", attempts
            )
    evidence = HistoryEvidence(
        result.body,
        reference,
        started,
        obtained,
        attempts,
        "mdf-cpi-history://" + job_id + "-a1",
    )
    try:
        plan = build_plan(evidence, job_id, finished_at=clock(), frozen_at=clock())
    except Exception:
        raise LiveExecutionError(
            "bls_live_response_contract_failed", attempts
        ) from None
    directory = artifact_root / (job_id + "-a1")
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=False)
        (directory / "raw-response.bin").write_bytes(result.body)
        (directory / "observation.json").write_bytes(
            _encode(
                {
                    "capability": "bls.cpi.history.indices",
                    "fixture": reference,
                    "classification": "success",
                    "requested_at": started.isoformat(),
                    "obtained_at": obtained.isoformat(),
                    "provider_data_attempts": attempts,
                    "request": req,
                    "raw_sha256": digest(result.body),
                }
            )
        )
    except OSError:
        raise LiveExecutionError("bls_live_artifact_write_failed", attempts) from None
    return LiveExecutionCandidate(evidence, plan, directory)


def validate_content(content: dict, reference: str) -> None:
    """Recompute arithmetic/descriptors from carried indices on every CPI read."""
    require(
        set(content)
        == {
            "schema",
            "data",
            "request",
            "obtained_at",
            "provider_released_at",
            "provider_vintage",
            "provider_revision",
            "mode",
            "historical_as_of_verified",
        },
        "cpi_history_payload_invalid",
    )
    require(
        content["schema"] == RULE and content["mode"] == "historical_index_observation",
        "cpi_history_payload_invalid",
    )
    require(content["request"] == request(reference), "cpi_history_request_invalid")
    instant(datetime.fromisoformat(content["obtained_at"]))
    require(
        content["provider_released_at"] is None
        and content["provider_vintage"] is None
        and content["historical_as_of_verified"] is False
        and content["provider_revision"] == {"state": "not_declared", "value": None},
        "cpi_history_vintage_invalid",
    )
    data = content["data"]
    require(
        data["reference_period"] == reference
        and len(data["indices"]) == 4
        and len(data["measurements"]) == 4,
        "cpi_history_payload_invalid",
    )
    items = []
    for (name, (sid, _meaning, _lag)), index, measurement in zip(
        MEASURES.items(), data["indices"], data["measurements"], strict=True
    ):
        require(
            measurement["name"] == name and index["series_id"] == sid,
            "cpi_history_payload_invalid",
        )
        rows = []
        for pointer, value, notes in (
            (reference, index["value"], measurement["footnotes"]),
            (
                index["comparison_period"],
                index["comparison_value"],
                index["comparison_footnotes"],
            ),
        ):
            rows.append(
                {
                    "year": pointer[:4],
                    "period": "M" + pointer[5:],
                    "value": value,
                    "footnotes": notes,
                }
            )
        items.append({"seriesID": sid, "data": rows})
    expected = project(
        _encode(
            {"status": "REQUEST_SUCCEEDED", "message": [], "Results": {"series": items}}
        ),
        reference,
    )
    require(
        data == expected and expected["availability"]["state"] == "available",
        "cpi_history_payload_invalid",
    )


CPI_HISTORY_INDEX_SOURCE, CPI_HISTORY_INDEX_BINDING = source_binding()
