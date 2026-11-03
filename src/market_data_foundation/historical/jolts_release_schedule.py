"""National JOLTS calendar and bounded maintenance over shared execution ports."""

from datetime import timedelta
from urllib.parse import urlsplit
from uuid import NAMESPACE_URL, uuid5

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_release_schedule import (
    ICS_URL,
    CpiReleaseScheduler,
    _aware,
    _fetch_calendar,
    _instant,
    _keys,
    now_utc,
    parse_official_calendar,
)
from market_data_foundation.historical.cpi_release_schedule import (
    parse_official_ics as _parse_ics,
)
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries import (
    DEFAULT_REQUEST_CONTROL_PATH,
)

CALENDAR_URL = "https://www.bls.gov/schedule/news_release/jolts.htm"
POLICY = {
    "release_update": "latest explicitly mapped due national JOLTS event",
    "annual_rule": (
        "initial once per calendar year and once after each numerically confirmed "
        "mapped release; previous five complete calendar years"
    ),
    "annual_basis": "MDF conservative maintenance rule, not an official benchmark date",
    "annual_event_dedup": "reference month + scheduled release instant",
    "release_completion": "all eight targets returned this attempt and locally numeric",
    "annual_target_months": 60,
    "comparison_buffer_months": 12,
    "max_attempts": 3,
    "retry_seconds": [300, 900],
    "admission": (
        "one job per tick; shared BLS single-flight, rolling quota and cooldown"
    ),
    "default_series": list(JOLTS_PROFILE.series_ids),
}


def fetch_official_page(url, *, observer=None):
    return _fetch_calendar(
        url,
        "text/calendar" if url == ICS_URL else "text/html",
        title="JOLTS",
        allowed_path=urlsplit(url).path,
        request_control=CpiRequestControl(DEFAULT_REQUEST_CONTROL_PATH),
        observer=observer,
    )


def parse_official_ics(raw, *, known_uids=()):
    return _parse_ics(raw, profile=JOLTS_PROFILE, known_uids=known_uids)


def annual_window(year):
    return f"{year - 5:04d}-01", f"{year - 1:04d}-12"


class JoltsReleaseScheduler(CpiReleaseScheduler):
    """Calendar mutation remains explicit; GET/status only read existing files.

    Execution uses Host.submit so permissions, durable journal, source admission,
    changed-only writes and available readback are shared with manual collection.
    """

    def __init__(
        self, state_path, *, history_start="2000-12", clock=now_utc, **options
    ):
        options.setdefault("calendar_fetcher", lambda: fetch_official_page(ICS_URL))
        options.setdefault(
            "reference_fetcher", lambda: fetch_official_page(CALENDAR_URL)
        )
        options.setdefault("fallback_fetcher", options["reference_fetcher"])
        super().__init__(
            None,
            state_path,
            profile=JOLTS_PROFILE,
            history_start=history_start,
            clock=clock,
            auto_mode=True,
            **options,
        )
        self.submit = None
        self.maintenance_only = False

    def _accept_calendar(self, state, at, raw, **options):
        # Acquisition is sampled after transport, never the tick's start time.
        options.setdefault("acquired_at", _aware(self.clock()))
        super()._accept_calendar(state, at, raw, **options)
        available = _aware(self.clock()).isoformat()
        state["calendar"]["available_at"] = available
        reference = state["calendar"].get("reference_calendar")
        if reference:
            reference["available_at"] = available
        self._save(state)
        return self._snapshot(state, _aware(self.clock()))

    def _snapshot(self, state, at):
        result = {
            **super()._snapshot(state, at),
            "policy": POLICY,
            "work": state.get("work", {}),
        }
        # Older DEV observations predate the shared refresh envelope. Derive
        # display metadata without changing acquisition times or writing on GET.
        calendar = result["calendar"] = dict(result["calendar"])
        acquired = calendar.get("acquired_at") or calendar.get("fetched_at")
        calendar.setdefault("primary_source", self.ics_url)
        calendar.setdefault("source", self.ics_url)
        calendar.setdefault(
            "refresh_mode",
            "unavailable"
            if not acquired
            else "cache"
            if calendar.get("error")
            else "primary",
        )
        calendar.setdefault(
            "cache_valid_until",
            (_instant(acquired) + timedelta(hours=24)).isoformat()
            if acquired
            else None,
        )
        calendar["refresh_attempts"] = [
            {
                "source": row.get("source") or row.get("source_url"),
                "outcome": row.get("outcome")
                or ("accepted" if row.get("success") else "failed"),
                **{
                    key: row[key]
                    for key in ("classification", "http_status")
                    if key in row
                },
            }
            for row in calendar.get("refresh_attempts", [])
        ]
        annual = state.get("work", {}).get(f"annual:{at.year}", {})
        result["annual_maintenance"] = dict(
            start=annual_window(at.year)[0],
            end=annual_window(at.year)[1],
            due_at=f"{at.year}-01-01T00:00:00+00:00",
            state=annual.get("state", "pending"),
            attempts=annual.get("attempts", 0),
        )
        due = []
        if annual.get("state") != "complete" and annual.get("attempts", 0) < 3:
            due.append(
                max(
                    at,
                    _instant(
                        annual.get("next_attempt_at")
                        or result["annual_maintenance"]["due_at"]
                    ),
                )
            )
        for month, target in state["targets"].items():
            if (
                self._calendar_status(state, at) != "fresh"
                or target.get("state") == "calendar_removed"
                or target.get("calendar_execution_blocked")
            ):
                continue
            key = "release:" + month + ":" + target["release_at"]
            record = state.get("work", {}).get(key, {})
            if record.get("state") != "complete" and record.get("attempts", 0) < 3:
                due.append(
                    max(
                        at,
                        _instant(record.get("next_attempt_at") or target["release_at"]),
                    )
                )
        for record in state.get("work", {}).values():
            if (
                record.get("kind") == "post_release_maintenance"
                and record.get("state") != "complete"
                and record.get("attempts", 0) < POLICY["max_attempts"]
            ):
                due.append(max(at, _instant(record["next_attempt_at"])))
        result["next_due"] = min(due).isoformat() if due else None
        return result

    def _refresh(self, state, at):
        super()._refresh(state, at)
        if any(
            row.get("http_status") == 403
            for row in state["calendar"].get("refresh_attempts", [])
        ):
            state["calendar"]["next_refresh_at"] = (
                _aware(self.clock()) + timedelta(days=1)
            ).isoformat()
            self._save(state)
        return self._snapshot(state, _aware(self.clock()))

    def due_check(self):
        at = _aware(self.clock())
        with self._locked():
            state = self._read()
            calendar = state["calendar"]
            next_refresh = calendar.get("next_refresh_at")
            if not calendar or next_refresh is None or _instant(next_refresh) <= at:
                self._refresh(state, at)
                at = _aware(self.clock())
            work = state.setdefault("work", {})
            candidate = None
            if (
                not self.maintenance_only
                and self._calendar_status(state, at) == "fresh"
            ):
                due = [
                    (month, target)
                    for month, target in state["targets"].items()
                    if target.get("state") != "calendar_removed"
                    and not target.get("calendar_execution_blocked")
                    and _instant(target["release_at"]) <= at
                    and (target.get("reference_month_evidence") or {}).get("source")
                ]
                if due:
                    month, target = max(due)
                    key = "release:" + month + ":" + target["release_at"]
                    candidate = (
                        key,
                        month,
                        month,
                        "release_catch_up"
                        if at - _instant(target["release_at"]) > timedelta(hours=2)
                        else "release_observation",
                    )
            annual_key = f"annual:{at.year}"
            choices = [candidate] if candidate else []
            # A pending release retains priority while waiting for its retry.
            # Completed annual work cannot suppress a later event's five-year check.
            release_record = work.get(candidate[0], {}) if candidate else {}
            release_pending = (
                candidate
                and release_record.get("state") != "complete"
                and release_record.get("attempts", 0) < POLICY["max_attempts"]
            )
            if not release_pending:
                choices.extend(
                    (key, record["start"], record["end"], record["kind"])
                    for key, record in sorted(work.items())
                    if record.get("kind") == "post_release_maintenance"
                )
                choices.append(
                    (annual_key, *annual_window(at.year), "annual_maintenance")
                )
            for key, start, end, kind in choices:
                record = work.get(key, {})
                if record.get("state") == "complete" or (
                    record.get("attempts", 0) >= 3 and record.get("state") != "running"
                ):
                    continue
                if (
                    record.get("next_attempt_at")
                    and _instant(record["next_attempt_at"]) > at
                ):
                    continue
                # A running reservation reuses its UUID after restart. The journal
                # decides whether a write completed or became unknown.
                attempt = record.get("attempts", 0) + (record.get("state") != "running")
                request_id = str(
                    uuid5(NAMESPACE_URL, str(self.state_path) + key + str(attempt))
                )
                if request_id not in record.setdefault("request_ids", []):
                    record["request_ids"].append(request_id)
                record.update(
                    kind=kind,
                    start=start,
                    end=end,
                    attempts=attempt,
                    request_id=request_id,
                    state="running",
                    started_at=at.isoformat(),
                )
                work[key] = record
                self._save(state)
                try:
                    job = self.submit(
                        dict(
                            request_id=request_id, action="update", start=start, end=end
                        )
                    )
                    record.update(
                        state=job["state"],
                        classification=job.get("classification"),
                        result=job.get("result"),
                        finished_at=job.get("finished_at"),
                    )
                    if kind.startswith("release_") and record["state"] == "complete":
                        result = job.get("result") or {}
                        evidence = result.get("target_evidence") or {}
                        expected = {(series, end) for series in self.series_ids}
                        ready = (
                            result.get("readback", {}).get("verified") is True
                            and expected
                            <= _keys(evidence.get("source_returned_series_months", []))
                            and expected
                            <= _keys(evidence.get("available_series_months", []))
                        )
                        if not ready:
                            record.update(
                                state="awaiting_provider",
                                classification="target_source_unavailable"
                                if evidence.get("source_unavailable")
                                else "target_month_not_available",
                            )
                except Exception as error:
                    record.update(
                        state="unknown",
                        classification=getattr(
                            error, "classification", type(error).__name__
                        ),
                        finished_at=_aware(self.clock()).isoformat(),
                    )
                record["next_attempt_at"] = (
                    None
                    if record["state"] == "complete" or attempt >= 3
                    else (
                        _aware(self.clock())
                        + timedelta(seconds=POLICY["retry_seconds"][attempt - 1])
                    ).isoformat()
                )
                history = record.setdefault("attempt_history", [])
                history[:] = [r for r in history if r["request_id"] != request_id]
                history.append(
                    {
                        k: record.get(k)
                        for k in (
                            "request_id",
                            "attempts",
                            "state",
                            "classification",
                            "started_at",
                            "finished_at",
                            "next_attempt_at",
                        )
                    }
                )
                if kind.startswith("release_") and record["state"] == "complete":
                    target = state["targets"][end]
                    target.update(
                        state="complete",
                        target_available=True,
                        classification=None,
                        next_attempt_at=None,
                        target_refreshed=True,
                        verified_target_series=list(self.series_ids),
                    )
                    target["target"].update(
                        state="complete",
                        next_attempt_at=None,
                        attempts=attempt,
                        availability_verified=True,
                    )
                    target["history"].update(
                        state="separate_annual_maintenance", next_attempt_at=None
                    )
                    work.setdefault(
                        "annual_after:" + key,
                        dict(
                            kind="post_release_maintenance",
                            release_work_key=key,
                            start=annual_window(_instant(target["release_at"]).year)[0],
                            end=annual_window(_instant(target["release_at"]).year)[1],
                            state="pending",
                            attempts=0,
                            next_attempt_at=_aware(self.clock()).isoformat(),
                        ),
                    )
                elif kind.startswith("release_"):
                    target = state["targets"][end]
                    target.update(
                        state=record["state"],
                        target_available=False,
                        classification=record.get("classification"),
                        next_attempt_at=record["next_attempt_at"],
                    )
                    target["target"].update(
                        state=record["state"],
                        attempts=attempt,
                        next_attempt_at=record["next_attempt_at"],
                    )
                self._save(state)
                return {
                    "action": kind,
                    "execution": record,
                    **self._snapshot(state, at),
                }
            return {"action": "not_due", **self._snapshot(state, at)}


__all__ = [
    "CALENDAR_URL",
    "ICS_URL",
    "JoltsReleaseScheduler",
    "parse_official_calendar",
    "parse_official_ics",
]
