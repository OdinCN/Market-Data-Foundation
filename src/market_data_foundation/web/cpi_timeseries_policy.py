"""Read-only product presets and latest persisted scheduler stage summaries."""

from datetime import datetime
from uuid import UUID, uuid5

from market_data_foundation.historical.cpi_timeseries import month_number
from market_data_foundation.historical.cpi_timeseries_cli import execute

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


def official_releases(snapshot, *, exclude_absent=False):
    events = snapshot.get("calendar", {}).get("events", {}).values()
    for target in [*snapshot.get("targets", []), *events]:
        evidence = target.get("reference_month_evidence") or {}
        source = evidence.get("source") or evidence.get("evidence_url")
        month = target.get("reference_month")
        at = instant(target.get("release_at"))
        if (
            valid_month(month)
            and evidence.get("reference_month") == month
            and isinstance(source, str)
            and source.startswith("https://www.bls.gov/")
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


def initialization_preset(service, status, snapshot, now):
    candidates = [
        (month, "official_elapsed_release/reference_month")
        for month, at in official_releases(snapshot)
        if at <= now
    ]
    # status.latest_months is only an upper bound: it includes unknown holdings.
    upper = max(
        (m for m in status.get("latest_months", {}).values() if valid_month(m)),
        default=None,
    )
    while upper is not None:
        floor = max(1901 * 12, month_number(upper) - 239)
        readback = execute(
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
        )
        confirmed = [
            point["month"]
            for point in readback.get("points", [])
            if point.get("origin") == "local"
            and point.get("availability") == "available"
            and point.get("available_at") is not None
            and point.get("index_value") is not None
        ]
        if confirmed:
            candidates.append((max(confirmed), "confirmed_local_available"))
            break
        if floor == 1901 * 12:
            break
        upper = month_text(floor - 1)
    anchor, basis = max(candidates, default=(None, "unavailable"))
    available = anchor is not None and month_number(anchor) - 239 >= 1901 * 12
    return {
        "preset_id": PRESET_ID,
        "months": PRESET_MONTHS,
        "start": month_text(month_number(anchor) - 239) if available else None,
        "end": anchor if available else None,
        "available": available,
        "reason": None if available else "initialization_anchor_unavailable",
        "anchor_basis": basis,
    }


def next_release_at(snapshot, now):
    dates = [
        at for _, at in official_releases(snapshot, exclude_absent=True) if at > now
    ]
    # Official ICS dates remain valid even while reference-month mapping waits.
    calendar = snapshot.get("calendar", {})
    if str(calendar.get("source", "")).startswith("https://www.bls.gov/"):
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


def automatic_executions(snapshot):
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
            "summary_scope": "latest_stage_attempt",
            "scheduler_attempts_total": stage.get("attempts"),
        }
        if "actual_http" in stage:
            result["actual_http_total"] = stage["actual_http"]
        records.append(
            {
                "id": str(uuid5(EXECUTION_NAMESPACE, f"{month}:{name}")),
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


def strategy_projection(value, snapshot, now):
    executions = automatic_executions(snapshot)
    latest = max(executions, key=lambda r: instant(r["started_at"]), default=None)
    return {
        **value,
        "next_release_at": next_release_at(snapshot, now),
        "execution_state": value["worker"]["state"] if value["enabled"] else "paused",
        "last_result": latest,
    }
