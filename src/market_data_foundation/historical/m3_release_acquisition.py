"""M3 job material outcomes, independent of numeric success and source snapshots."""

import http.client
from datetime import datetime
from zoneinfo import ZoneInfo

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.m3_schedule import CALENDAR_URL
from market_data_foundation.historical.macro_release_acquisition import MaterialAcquirer
from market_data_foundation.historical.macro_release_material import official_url


def select_m3_material(snapshot, dataset, start, end, now):
    if snapshot.get("calendar_status") not in {"fresh", "degraded_cached_calendar"}:
        return None
    if snapshot.get("calendar", {}).get("source") != CALENDAR_URL:
        return None
    selected = []
    for event in snapshot.get("targets", []):
        if (
            event.get("dataset") != dataset
            or event.get("source") != CALENDAR_URL
            or not start <= event.get("reference_month", "") <= end
            or event.get("status") != "scheduled"
            or not event.get("release_at")
            or event.get("calendar_execution_blocked")
            or event.get("cancelled")
        ):
            continue
        at = datetime.fromisoformat(event["release_at"])
        if at.utcoffset() is None or at > now:
            continue
        selected.append((at, event))
    if not selected:
        return None
    at, event = max(selected, key=lambda row: row[0])
    path = "prel/pdf/s-i-o.pdf" if dataset == "m3" else "adv/pdf/durgd.pdf"
    return dict(
        source_url="https://www.census.gov/manufacturing/m3/" + path,
        source_date=at.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        reference_month=event["reference_month"],
        dataset=dataset,
        report_stage="full" if dataset == "m3" else "advance",
        selection_basis="due_official_m3_calendar_stage_and_reference",
    )


def fetch_m3_material(url, control):
    parts = official_url("m3", url)
    if control is None:
        raise CpiTimeSeriesError("m3_material_request_control_unavailable")
    connection = http.client.HTTPSConnection(parts.hostname, timeout=10)
    try:
        with control.attempt() as admission:
            connection.request(
                "GET",
                parts.path,
                headers={"Accept": "application/pdf", "Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            admission.record_response(response.status, dict(response.getheaders()))
            if response.status != 200:
                raise CpiTimeSeriesError(f"release_http_{response.status}")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise CpiTimeSeriesError("release_material_size_invalid")
            return raw
    finally:
        connection.close()


def numeric_ready(result):
    return bool(
        result
        and result.get("persisted") is True
        and result.get("readback_status") == "confirmed"
        and result.get("remaining_requests") == 0
        and not result.get("source_failure")
        and not result.get("storage_failure")
    )


def capture_m3_material(host, service, dataset, start, end, written):
    try:
        return _capture_m3_material(host, service, dataset, start, end, written)
    except Exception as error:
        return dict(
            state="partial",
            reports={},
            event_ids=[],
            evidence_ids=[],
            reasons=[
                getattr(error, "classification", "m3_material_acquisition_failed")
            ],
            transport_calls=None,
            provider_requests=None,
        )


def _capture_m3_material(host, service, dataset, start, end, written):
    snapshot = host.scheduler.status()
    reports = written.get("reports") if dataset == "both" else {dataset: written}
    results = {}
    for stage, numeric in (reports or {}).items():
        selection = select_m3_material(snapshot, stage, start, end, host.clock())
        if not numeric_ready(numeric):
            results[stage] = dict(
                state="partial",
                reasons=["numeric_result_not_confirmed"],
                selection=None,
                transport_calls=0,
                provider_requests=0,
                event_ids=[],
                evidence_ids=[],
            )
            continue
        try:
            fetcher = getattr(host, "material_fetcher", None) or (
                lambda url, _: fetch_m3_material(
                    url, getattr(service.source, "_control", None)
                )
            )
            outcome = MaterialAcquirer(
                host.scheduler.state_path.with_name("m3-release-material.json"),
                fetcher=fetcher,
            ).capture(
                service.store,
                selection,
                batch_ids=[
                    numeric.get("collection_id") or written.get("collection_id") or ""
                ],
            )
            results[stage] = {**outcome, "selection": selection}
        except Exception as error:
            results[stage] = dict(
                state="partial",
                reasons=[
                    getattr(error, "classification", "m3_material_acquisition_failed")
                ],
                selection=selection,
                transport_calls=None,
                provider_requests=None,
                event_ids=[],
                evidence_ids=[],
            )
    return dict(
        state="retained"
        if results and all(r["state"] == "retained" for r in results.values())
        else "partial",
        reports=results,
        event_ids=sorted({e for r in results.values() for e in r["event_ids"]}),
        evidence_ids=sorted({e for r in results.values() for e in r["evidence_ids"]}),
        reasons=list(
            dict.fromkeys(
                reason for r in results.values() for reason in r.get("reasons", [])
            )
        ),
        provider_requests=None
        if any(r["provider_requests"] is None for r in results.values())
        else sum(r["provider_requests"] for r in results.values()),
        transport_calls=None
        if any(r["transport_calls"] is None for r in results.values())
        else sum(r["transport_calls"] for r in results.values()),
        numeric_binding="separate_material_evidence_not_a_continuous_version_binding",
    )


def recoverable_material(job):
    material = (job.get("result") or {}).get("release_material") or {}
    return (
        numeric_ready(job.get("numeric_result"))
        and material.get("state") != "retained"
        and any(r.get("selection") for r in material.get("reports", {}).values())
    )
