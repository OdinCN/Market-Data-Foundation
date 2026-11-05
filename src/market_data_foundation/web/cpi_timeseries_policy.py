"""Read-only product presets and latest persisted scheduler stage summaries."""

from datetime import datetime
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
from market_data_foundation.historical.cpi_timeseries import month_number
from market_data_foundation.web.ppi_timeseries import execute_profile

PRESET_ID = "cpi_latest_20y_v1"
PRESET_MONTHS = 240
EXECUTION_NAMESPACE = UUID("419d3241-27a3-59aa-9ae8-e6d7f855eff1")


def instant(value):
    try:
        result = datetime.fromisoformat(value)
        return result if result.utcoffset() is not None else None
    except ValueError, TypeError:
        return None


def valid_month(value):
    try:
        return month_number(value) >= 1901 * 12
    except RuntimeError:
        return False


def month_text(number):
    year, month = divmod(number, 12)
    return f"{year:04d}-{month + 1:02d}"


def official_releases(snapshot, *, exclude_absent=False, profile=CPI_PROFILE):
    events = snapshot.get("calendar", {}).get("events", {}).values()
    for target in [
        *snapshot.get("targets", []),
        *snapshot.get("calendar_publications", []),
        *events,
    ]:
        evidence = target.get("reference_month_evidence") or {}
        source = evidence.get("source") or evidence.get("evidence_url")
        month = target.get("reference_month")
        at = instant(target.get("release_at"))
        if (
            valid_month(month)
            and evidence.get("reference_month") == month
            and isinstance(source, str)
            and source.startswith(
                "https://www.census.gov/"
                if profile.key == "retail"
                else "https://www.bea.gov/"
                if profile.provider == "BEA"
                else "https://www.bls.gov/"
            )
            and at is not None
            and target.get("state") != "calendar_removed"
            and target.get("publication_status") != "cancelled"
            and target.get("status") != "CANCELLED"
            and not target.get("cancellation_evidence")
            and not target.get("calendar_cancelled_after_completion")
            and not target.get("calendar_execution_blocked")
            and (not exclude_absent or not target.get("absent_from_latest_calendar"))
        ):
            yield month, at


def initialization_preset(service, status, snapshot, now, *, profile=CPI_PROFILE):
    earliest = profile.earliest_month
    boundary = month_number(earliest) if earliest else 1901 * 12
    candidates = [
        (month, "official_elapsed_release/reference_month")
        for month, at in official_releases(snapshot, profile=profile)
        if at <= now
    ]
    # status.latest_months is only an upper bound: it includes unknown holdings.
    upper = max(
        (m for m in status.get("latest_months", {}).values() if valid_month(m)),
        default=None,
    )
    while upper is not None:
        floor = max(boundary, month_number(upper) - 239)
        if floor > month_number(upper):
            break
        readback = execute_profile(
            service,
            "query",
            {
                "start": month_text(floor),
                "end": upper,
                "local_only": True,
                "basket": "all",
                "adjustment": "all",
                "vintage_mode": "available",
                "query_mode": "history",
            },
            profile=profile,
        )
        confirmed = [
            point["month"]
            for point in readback.get("points", [])
            if point.get("origin") == "local"
            and point.get("availability") == "available"
            and point.get("available_at") is not None
            and point.get(
                "value"
                if profile.key in {"employment", "consumption"}
                else "index_value"
            )
            is not None
        ]
        if confirmed:
            candidates.append((max(confirmed), "confirmed_local_available"))
            break
        if floor == boundary:
            break
        upper = month_text(floor - 1)
    anchor, basis = max(candidates, default=(None, "unavailable"))
    if anchor is None and profile.key == "ppi":
        anchor = month_text(now.year * 12 + now.month - 2)
        basis = "request_calendar_month"
    if anchor is None and profile.key in {"pce", "employment", "consumption"}:
        local_now = now.astimezone(ZoneInfo("Asia/Shanghai"))
        anchor = month_text(local_now.year * 12 + local_now.month - 2)
        basis = "request_calendar_month_no_release_claim"
    available = (
        anchor is not None
        and month_number(anchor) >= boundary
        and (profile is not CPI_PROFILE or month_number(anchor) - 239 >= boundary)
    )
    start = month_text(max(boundary, month_number(anchor) - 239)) if available else None
    actual_months = month_number(anchor) - month_number(start) + 1 if available else 0
    result = {
        "preset_id": profile.key + "_latest_20y_v1",
        "months": PRESET_MONTHS,
        "start": start,
        "end": anchor if available else None,
        "available": available,
        "reason": (
            None
            if available
            else "initialization_profile_boundary_unavailable"
            if profile is not CPI_PROFILE and earliest is None
            else "initialization_anchor_unavailable"
        ),
        "anchor_basis": basis,
    }
    if profile is not CPI_PROFILE:
        result.update(
            editable=True,
            earliest_month=earliest,
            actual_months=actual_months,
            coverage_truncated=available and actual_months < PRESET_MONTHS,
            coverage_basis="profile_boundary_and_anchor_not_complete_holdings",
        )
    return result


def next_release_at(snapshot, now, *, profile=CPI_PROFILE):
    dates = [
        at
        for _, at in official_releases(snapshot, exclude_absent=True, profile=profile)
        if at > now
    ]
    # Official ICS dates remain valid even while reference-month mapping waits.
    calendar = snapshot.get("calendar", {})
    if str(calendar.get("source", "")).startswith(
        "https://www.census.gov/"
        if profile.key == "retail"
        else "https://www.bea.gov/"
        if profile.provider == "BEA"
        else "https://www.bls.gov/"
    ):
        for event in calendar.get("events", {}).values():
            at = instant(event.get("release_at"))
            if (
                at is not None
                and at > now
                and event.get("status") != "CANCELLED"
                and event.get("state") != "calendar_removed"
                and not event.get("cancelled")
                and not event.get("absent_from_latest_calendar")
            ):
                dates.append(at)
    return min(dates).isoformat() if dates else None


def automatic_executions(snapshot, *, profile=CPI_PROFILE):
    records = []

    def add(month, name, stage, *, start=None, end=None, action="update"):
        attempted = stage.get("last_attempt_at")
        # Another month's reconciliation can overwrite an old failed attempt's
        # state/completion time. That borrowed completion is not its outcome.
        if stage.get("satisfied_by_monthly_history") or instant(attempted) is None:
            return
        completed = stage.get("completed_at")
        if instant(completed) is None or instant(completed) < instant(attempted):
            completed = None
        result = {
            **(stage.get("result") or {}),
            "summary_scope": "latest_stage_attempt",
            "scheduler_attempts_total": stage.get("attempts"),
        }
        # Legacy stages can omit this evidence. Do not invent a null field or
        # overwrite a result-level observation when no stage-level one exists.
        if "availability_verified" in stage:
            result["availability_verified"] = stage["availability_verified"]
        if "release_material" in stage:
            result["release_material"] = stage["release_material"]
        if "actual_http" in stage:
            result["actual_http_total"] = stage["actual_http"]
        if "readback" in stage:
            result["readback"] = stage["readback"]
        if "error" in stage:
            result["error"] = stage["error"]
        if "buffer_source_missing" in stage:
            result["buffer_source_missing"] = stage["buffer_source_missing"]
        if profile.key in {"ppi", "pce", "consumption"}:
            if isinstance(stage.get("coverage_complete"), bool):
                result["coverage_complete"] = stage["coverage_complete"]
                if stage["coverage_complete"] is False and (
                    profile.provider != "BEA" or stage.get("readback_verified") is True
                ):
                    result["completion"] = "source_coverage_partial"
            if isinstance(stage.get("source_missing"), list):
                result["source_missing"] = stage["source_missing"]
        records.append(
            {
                "id": str(
                    uuid5(
                        EXECUTION_NAMESPACE,
                        f"{month}:{name}"
                        if profile is CPI_PROFILE
                        else f"{profile.key}:{month}:{name}",
                    )
                ),
                "origin": "automatic",
                "action": action,
                "stage": name,
                "reference_month": month or None,
                "start": start,
                "end": end,
                "created_at": None,
                "started_at": attempted,
                "finished_at": completed,
                "state": stage.get("state", "unknown"),
                "classification": stage.get("classification"),
                "result": result,
            }
        )

    for target in snapshot.get("targets", []):
        month = target.get("reference_month")
        # Automatic target attempts are persisted on auto, not target stage.
        auto = target.get("auto", {})
        target_stage = {
            **target.get("target", {}),
            **auto,
            "last_attempt_at": auto.get("last_attempt_at"),
            "attempts": target.get("target", {}).get("attempts"),
        }
        add(month, "target", target_stage, start=month, end=month)
        history = target.get("history", {})
        add(
            month,
            "history",
            history,
            start=history.get("update_start"),
            end=history.get("update_end"),
        )
    signals = snapshot.get("signals", {})
    for identity, event in signals.get("events", {}).items():
        add(
            event.get("reference_month", ""),
            "signal:" + identity,
            event,
            start=event.get("start_month"),
            end=event.get("end_month"),
            action="withdraw" if event.get("kind") == "withdrawal" else "update",
        )
    recovery = signals.get("recovery") or {}
    add(
        recovery.get("reference_month", ""),
        "recovery",
        recovery,
        start=recovery.get("update_start"),
        end=recovery.get("update_end"),
    )
    return records


def strategy_projection(value, snapshot, now, *, profile=CPI_PROFILE):
    executions = automatic_executions(snapshot, profile=profile)
    latest = max(executions, key=lambda r: instant(r["started_at"]), default=None)
    return {
        **value,
        "next_release_at": next_release_at(snapshot, now, profile=profile),
        "execution_state": value["worker"]["state"] if value["enabled"] else "paused",
        "last_result": latest,
    }
