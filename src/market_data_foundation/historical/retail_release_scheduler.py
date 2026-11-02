"""Census-only durable release queue, using shared locks and bounded retry policy."""

import copy
import hashlib
import http.client
import random
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from market_data_foundation.historical.calendar_resilience import (
    failure,
    may_fallback,
    refresh_metadata,
    retry_time,
)
from market_data_foundation.historical.census_retail_release_schedule import (
    CALENDAR_URL,
    MAX_CALENDAR_BYTES,
    annual_calendar_url,
    parse_annual_calendar,
    parse_official_calendar,
)
from market_data_foundation.historical.census_retail_timeseries_source import (
    RetailSourceError,
)
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    _aware,
    _instant,
    now_utc,
)
from market_data_foundation.historical.cpi_request_control import (
    CpiRequestControlError,
    retry_after_at,
)
from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.pce_release_schedule import PceReleaseScheduler

RETAIL_PROFILE = SimpleNamespace(key="retail", label="零售销售", earliest_month=None)
RETRY_SECONDS = (15, 30, 60, 120, 300, 900, 1800, 3600)


class CensusControlledTransport:
    """Every calendar/data exchange shares one Census-only admission file."""

    def __init__(self, transport, control):
        self.transport, self.control = transport, control

    def get(self, *args, **kwargs):
        try:
            with self.control.attempt() as attempt:
                response = self.transport.get(*args, **kwargs)
                attempt.record_response(response.status, response.headers)
                return response
        except CpiRequestControlError as error:
            raise RetailSourceError(
                error.classification.replace("cpi_timeseries", "census_retail"),
                retry_at=error.retry_at,
                http_status=error.http_status,
            ) from None


class CensusCalendarTransport:
    def get(self, year=None):
        from market_data_foundation.historical.census_retail_live import HttpResult

        connection = http.client.HTTPSConnection("www.census.gov", timeout=20)
        try:
            connection.request(
                "GET",
                annual_calendar_url(year).removeprefix("https://www.census.gov")
                if year is not None
                else "/retail/release_schedule.html",
                headers={"Accept-Encoding": "identity"},
            )
            response = connection.getresponse()
            body = response.read(MAX_CALENDAR_BYTES + 1)
            if len(body) > MAX_CALENDAR_BYTES:
                raise RetailSourceError("census_retail_calendar_too_large")
            return HttpResult(response.status, body, dict(response.getheaders()))
        finally:
            connection.close()


def calendar_fetcher(control):
    def fetch(year=None):
        transport = CensusControlledTransport(CensusCalendarTransport(), control)
        response = transport.get() if year is None else transport.get(year)
        if response.status != 200:
            raise RetailSourceError(
                "census_retail_calendar_http_failure",
                http_status=response.status,
                retry_at=retry_after_at(response.headers, control._now())
                if response.status == 429
                else None,
            )
        return response.body

    return fetch


class RetailReleaseScheduler(PceReleaseScheduler):
    """Reuse atomic state/locking/import contract; never seed a historical backlog."""

    profile = RETAIL_PROFILE
    asset_key = "retail"
    calendar_url = CALENDAR_URL
    auto_mode = True

    def __init__(
        self,
        host,
        state_path,
        *,
        clock=now_utc,
        calendar_fetcher=None,
        history_calendar_fetcher=None,
    ):
        self.host, self.clock = host, clock
        self.state_path = Path(state_path).resolve()
        self.history_start = "1992-01"
        self.calendar_fetcher = calendar_fetcher
        self.history_calendar_fetcher = history_calendar_fetcher

    def _read(self):
        state = super()._read()
        if not self.state_path.exists():
            state["calendar"] = {"source": CALENDAR_URL, "events": {}}
        self.history_start = state["history_start"]
        return state

    def _calendar_status(self, state, at):
        acquired = state["calendar"].get("acquired_at")
        if not acquired:
            return "unavailable" if state["calendar"].get("error") else "missing"
        age = at - _instant(acquired)
        return (
            "clock_reversal"
            if age < timedelta(0)
            else "fresh"
            if age < timedelta(hours=24)
            else "expired"
        )

    def status(self):
        state, now = self._read(), _aware(self.clock())
        due = [
            s["next_attempt_at"]
            for t in state["targets"]
            if not t.get("calendar_execution_blocked")
            for n in ("target", "history")
            if (s := t[n]).get("state") in {"pending", "awaiting_provider"}
            and s.get("next_attempt_at")
        ]
        recent = state["calendar"].get("events", {})
        publications = [
            dict(
                event,
                publication_status=event.get("status", "unknown"),
                reference_month_evidence=event.get("reference_month_evidence")
                or {
                    "reference_month": event["reference_month"],
                    "source": state["calendar"].get("source"),
                    "sha256": state["calendar"].get("sha256"),
                    "acquired_at": state["calendar"].get("acquired_at"),
                },
            )
            for event in recent.values()
        ]
        publications += [
            event
            for cache in state["calendar"].get("history_calendars", {}).values()
            for event in cache.get("events", [])
            if event["reference_month"] not in recent
        ]
        return {
            **state,
            "calendar_publications": publications,
            "provider": "Census",
            "calendar_status": self._calendar_status(state, now),
            "calendar_coverage": "official_data_month_only",
            "next_due": min(due) if due else state["calendar"].get("next_refresh_at"),
        }

    def _accept(
        self, state, raw, acquired, *, events=None, source=CALENDAR_URL, fallback=False
    ):
        events = parse_official_calendar(raw) if events is None else events
        previous = {t["reference_month"]: t for t in state["targets"]}
        targets = []
        old_calendar = state["calendar"]
        calendar = {
            month: dict(
                event,
                acquired_at=event.get("acquired_at", old_calendar.get("acquired_at")),
                reference_month_evidence=event.get("reference_month_evidence")
                or {
                    "reference_month": month,
                    "source": old_calendar.get("source", CALENDAR_URL),
                    "sha256": old_calendar.get("sha256"),
                    "acquired_at": old_calendar.get("acquired_at"),
                },
            )
            for month, event in old_calendar.get("events", {}).items()
        }
        first_month = min(e["reference_month"] for e in events) if events else None
        present = {e["reference_month"] for e in events}
        for month, old in list(calendar.items()):
            if (
                not fallback
                and month not in present
                and (
                    not first_month
                    or month >= first_month
                    or (
                        old.get("release_at")
                        and _instant(old["release_at"]) >= acquired
                    )
                )
            ):
                calendar[month] = dict(
                    old,
                    status="cancelled",
                    release_at=None,
                    acquired_at=acquired.isoformat(),
                    reference_month_evidence={
                        "reference_month": month,
                        "source": CALENDAR_URL,
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "acquired_at": acquired.isoformat(),
                        "mapping": "removed_from_current_coverage",
                    },
                )
        for event in events:
            month, release = event["reference_month"], event["release_at"]
            at = release.isoformat() if release else None
            prior = calendar.get(month)
            conflict = bool(
                fallback
                and prior
                and (
                    prior.get("status") != "scheduled" or prior.get("release_at") != at
                )
            )
            if conflict:
                if month in previous:
                    previous[month].update(
                        calendar_execution_blocked=True,
                        calendar_validation="calendar_fallback_conflict",
                    )
                continue
            calendar[month] = dict(
                reference_month=month,
                release_at=at,
                status=event["status"],
                actual_release_at=None,
                acquired_at=acquired.isoformat(),
                reference_month_evidence={
                    "reference_month": month,
                    "source": event.get("source", source),
                    "sha256": event["sha256"],
                    "acquired_at": acquired.isoformat(),
                },
            )
            old = previous.pop(month, None)
            # Only future or tracked releases; never replay old initialization.
            if old is None and release and release < acquired:
                continue
            stage = {"state": "pending", "attempts": 0, "next_attempt_at": at}
            target = old or {
                "reference_month": month,
                "target": dict(stage),
                "history": dict(stage, state="blocked"),
                "auto": {},
            }
            changed = target.get("release_at") != at
            target.update(
                release_at=at,
                publication_status=event["status"],
                calendar_execution_blocked=release is None,
                reference_month_evidence={
                    "reference_month": month,
                    "source": event.get("source", source),
                    "sha256": event["sha256"],
                },
            )
            if changed and target["target"]["state"] in {
                "pending",
                "awaiting_provider",
            }:
                target["target"]["next_attempt_at"] = at
            target["state"] = (
                "waiting" if release is None else target["target"]["state"]
            )
            targets.append(target)
        for old in previous.values():
            if fallback and old["reference_month"] not in present:
                old.update(
                    calendar_execution_blocked=True,
                    calendar_validation="calendar_fallback_not_covered",
                )
            if calendar.get(old["reference_month"], {}).get("status") == "cancelled":
                old.update(calendar_execution_blocked=True, state="calendar_removed")
            targets.append(old)
        state["targets"] = sorted(targets, key=lambda t: t["reference_month"])
        state["calendar"] = {
            "history_calendars": old_calendar.get("history_calendars", {}),
            "source": source,
            "events": calendar,
            "acquired_at": acquired.isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "next_refresh_at": (acquired + timedelta(hours=6)).isoformat(),
            "consecutive_refresh_failures": 0,
        }
        self._save(state)

    def _accept_history(self, state, raw, acquired, year):
        events = parse_annual_calendar(raw, year=year)
        caches = state["calendar"].setdefault("history_calendars", {})
        previous = caches.get(str(year), {}).get("acquired_at")
        if previous and acquired < _instant(previous):
            raise RetailSourceError("census_retail_calendar_import_older_than_current")
        caches[str(year)] = {
            "source": annual_calendar_url(year),
            "acquired_at": acquired.isoformat(),
            "next_refresh_at": (acquired + timedelta(days=7)).isoformat(),
            "events": [
                dict(
                    event,
                    acquired_at=acquired.isoformat(),
                    reference_month_evidence=dict(
                        event["reference_month_evidence"],
                        acquired_at=acquired.isoformat(),
                    ),
                )
                for event in events
            ],
        }
        self._save(state)

    def import_history_calendar(self, raw, acquired_at, *, year):
        acquired = _aware(acquired_at)
        if acquired > _aware(self.clock()):
            raise RetailSourceError("census_retail_calendar_acquired_at_in_future")
        with self._locked():
            self._accept_history(self._read(), raw, acquired, year)
        return self.status()

    def _refresh_history(self, state, now, years=None):
        if self.history_calendar_fetcher is None:
            return
        for year in years if years is not None else (now.year - 1, now.year):
            annual_calendar_url(year)
            cache = (
                state["calendar"].setdefault("history_calendars", {}).get(str(year), {})
            )
            due = cache.get("next_refresh_at")
            if due and _instant(due) > now:
                continue
            try:
                raw = self.history_calendar_fetcher(year)
                self._accept_history(state, raw, _aware(self.clock()), year)
            except Exception as error:
                retry = now + timedelta(hours=6)
                if getattr(error, "retry_at", None):
                    retry_at = _instant(str(error.retry_at))
                    retry = (
                        retry_at
                        if getattr(error, "classification", "").startswith(
                            "census_retail_request_"
                        )
                        else max(retry, retry_at)
                    )
                cache.update(
                    error=getattr(
                        error,
                        "classification",
                        "census_retail_history_calendar_failure",
                    ),
                    last_refresh_attempt=now.isoformat(),
                    next_refresh_at=retry.isoformat(),
                )
                state["calendar"]["history_calendars"][str(year)] = cache
                self._save(state)

    def refresh_history_calendars(self, *, years=None):
        """Calendar-only maintenance, preserving strategy and execution identities."""
        with self._locked():
            self._refresh_history(self._read(), _aware(self.clock()), years)
        return self.status()

    def _refresh(self, state, now):
        attempts, errors = [], []
        try:
            if self.calendar_fetcher is None:
                raise RetailSourceError("census_retail_calendar_not_bound")
            raw = self.calendar_fetcher()
            candidate = copy.deepcopy(state)
            self._accept(candidate, raw, _aware(self.clock()))
            state.clear()
            state.update(candidate)
            attempts.append({"source": CALENDAR_URL, "outcome": "accepted"})
        except Exception as error:
            errors.append(error)
            attempts.append(
                {
                    "source": CALENDAR_URL,
                    "outcome": "failed",
                    **failure(error, "census_retail_calendar_failure"),
                }
            )
        if (
            errors
            and self.history_calendar_fetcher is not None
            and may_fallback(errors[0])
        ):
            events, raws, sources, history_caches = {}, [], [], {}
            # Q4 needs the next year's explicit December data-month mapping.
            for year in (now.year, now.year + 1) if now.month >= 10 else (now.year,):
                source = annual_calendar_url(year)
                try:
                    raw = self.history_calendar_fetcher(year)
                    incoming = parse_annual_calendar(raw, year=year)
                    year_events = {}
                    for item in incoming:
                        event = {
                            "reference_month": item["reference_month"],
                            "release_at": _instant(item["release_at"])
                            if item["release_at"]
                            else None,
                            "status": item["publication_status"],
                            "source": source,
                            "sha256": item["reference_month_evidence"]["sha256"],
                        }
                        prior = year_events.get(event["reference_month"]) or events.get(
                            event["reference_month"]
                        )
                        if prior is not None and prior != event:
                            raise RetailSourceError(
                                "census_retail_calendar_duplicate_conflict"
                            )
                        year_events[event["reference_month"]] = event
                    events.update(year_events)
                    history_caches[str(year)] = {
                        "source": source,
                        "acquired_at": _aware(self.clock()).isoformat(),
                        "next_refresh_at": (now + timedelta(days=7)).isoformat(),
                        "events": [
                            dict(
                                item,
                                acquired_at=_aware(self.clock()).isoformat(),
                                reference_month_evidence=dict(
                                    item["reference_month_evidence"],
                                    acquired_at=_aware(self.clock()).isoformat(),
                                ),
                            )
                            for item in incoming
                        ],
                    }
                    raws.append(raw)
                    sources.append(source)
                    attempts.append({"source": source, "outcome": "accepted"})
                except Exception as error:
                    errors.append(error)
                    attempts.append(
                        {
                            "source": source,
                            "outcome": "failed",
                            **failure(error, "census_retail_calendar_failure"),
                        }
                    )
                    if "duplicate_conflict" in getattr(error, "classification", ""):
                        events.clear()
                        break
                    if not may_fallback(error):
                        break
            if events:
                candidate = copy.deepcopy(state)
                candidate["calendar"].setdefault("history_calendars", {}).update(
                    history_caches
                )
                try:
                    self._accept(
                        candidate,
                        b"\n".join(raws),
                        _aware(self.clock()),
                        events=list(events.values()),
                        source=sources[0],
                        fallback=True,
                    )
                    state.clear()
                    state.update(candidate)
                    state["calendar"]["sources"] = sources
                    refresh_metadata(
                        state["calendar"],
                        now,
                        primary=CALENDAR_URL,
                        attempts=attempts,
                        success=True,
                    )
                    state["calendar"]["next_refresh_at"] = retry_time(
                        now, 21600, *errors
                    )
                    self._save(state)
                    return True
                except Exception as error:
                    errors.append(error)
                    attempts.append(
                        {
                            "source": sources[0],
                            "outcome": "failed",
                            **failure(error, "census_retail_calendar_failure"),
                        }
                    )
        if not errors:
            refresh_metadata(
                state["calendar"],
                now,
                primary=CALENDAR_URL,
                attempts=attempts,
                success=True,
            )
            self._save(state)
            return True
        value = state["calendar"]
        failures = value.get("consecutive_refresh_failures", 0) + 1
        value.update(
            error=attempts[-1]["classification"],
            consecutive_refresh_failures=failures,
            next_refresh_at=retry_time(
                now, (300, 900, 1800, 3600)[min(failures - 1, 3)], *errors
            ),
        )
        refresh_metadata(
            value, now, primary=CALENDAR_URL, attempts=attempts, success=False
        )
        self._save(state)
        return False

    def due_check(self):
        with self._locked():
            # Recheck inside the execution boundary so a paused strategy does no IO.
            if not (self.host.auto_strategy._read() or {}).get("enabled", False):
                return {"action": "paused"}
            now, state = _aware(self.clock()), self._read()
            if self._calendar_status(state, now) == "clock_reversal":
                return {"action": "clock_reversal"}
            refresh = state["calendar"].get("next_refresh_at")
            if not refresh or _instant(refresh) <= now:
                self._refresh(state, now)
            # Low-priority calendar maintenance must not delay a release attempt.
            collection_due_soon = any(
                not target.get("calendar_execution_blocked")
                and (
                    stage["state"] == "running"
                    or (
                        stage["state"] in {"pending", "awaiting_provider"}
                        and stage.get("next_attempt_at")
                        and _instant(stage["next_attempt_at"])
                        <= now + timedelta(minutes=2)
                    )
                )
                for target in state["targets"]
                for stage in (target["target"], target["history"])
            )
            if not collection_due_soon:
                self._refresh_history(state, now)
            if self._calendar_status(state, now) != "fresh":
                return {"action": "calendar_unavailable"}
            for target in state["targets"]:
                for name in ("target", "history"):
                    stage = target[name]
                    if stage["state"] == "running":
                        stage.update(
                            state="unknown",
                            classification="execution_interrupted",
                            next_attempt_at=None,
                        )
                        self._save(state)
                        return {"action": "execution_interrupted"}
            # An explicit same-id resume may reconcile a retained uncertain write.
            # Observe its durable outcome; a worker never initiates that resume.
            for target in state["targets"]:
                if target.get("calendar_execution_blocked") or not target.get(
                    "release_at"
                ):
                    continue
                for name in ("target", "history"):
                    stage = target[name]
                    if stage["state"] == "unknown" and stage.get("request_id"):
                        job = self.host.journal.read(stage["request_id"])
                        if job and job["state"] == "complete":
                            self._accept_job(stage, target, name, job, now)
                            if name == "target":
                                target["auto"] = dict(stage)
                                target["state"] = stage["state"]
                            self._save(state)
                            return {"action": "recovered", "state": stage["state"]}
            for name in ("target", "history"):
                for target in reversed(state["targets"]):
                    stage = target[name]
                    if (
                        target.get("calendar_execution_blocked")
                        or not target["release_at"]
                        or _instant(target["release_at"]) > now
                    ):
                        continue
                    if name == "history" and target["target"]["state"] != "complete":
                        continue
                    if stage["state"] not in {"pending", "awaiting_provider"} or (
                        stage.get("next_attempt_at")
                        and _instant(stage["next_attempt_at"]) > now
                    ):
                        continue
                    return self._run(state, target, name, now)
        return {"action": "not_due"}

    def _run(self, state, target, name, now):
        stage, month = target[name], target["reference_month"]
        if stage["attempts"] >= 10 or target.get("reserved_http", 0) + 3 > 30:
            stage.update(state="pending_exhausted", next_attempt_at=None)
            self._save(state)
            return {"action": "budget_exhausted"}
        # One bounded revision check of recent history, never a 20-year initialization.
        start = month if name == "target" else month_at(month_number(month) - 11)
        identity = str(
            uuid5(
                NAMESPACE_URL, f"census-retail:{month}:{name}:{stage['attempts'] + 1}"
            )
        )
        stage.update(
            state="running",
            attempts=stage["attempts"] + 1,
            last_attempt_at=now.isoformat(),
            update_start=start,
            update_end=month,
            request_id=identity,
        )
        target["reserved_http"] = target.get("reserved_http", 0) + 3
        self._save(state)
        try:
            job = self.host.submit(
                {
                    "request_id": identity,
                    "action": "update",
                    "start": start,
                    "end": month,
                },
                origin="automatic",
                stage=name,
            )
            self._accept_job(stage, target, name, job, now)
        except Exception as error:
            # Admission precedes jobs; other unknown outcomes stop.
            if getattr(error, "classification", "") == "retail_jobs_busy":
                stage.update(
                    state="awaiting_provider",
                    next_attempt_at=(now + timedelta(seconds=15)).isoformat(),
                    classification="retail_jobs_busy",
                )
            else:
                stage.update(
                    state="unknown",
                    next_attempt_at=None,
                    classification=getattr(
                        error, "classification", "retail_execution_unknown"
                    ),
                )
        target["state"] = target["target"]["state"]
        if name == "target":
            target["auto"] = dict(stage)
        self._save(state)
        return {"action": name, "state": stage["state"]}

    def _accept_job(self, stage, target, name, job, now):
        month = target["reference_month"]
        result = job.get("result") or {}
        stage.update(result=result, classification=job.get("classification"))
        if job["state"] in {"unknown", "running"} or (
            job["state"] == "failed"
            and not str(job.get("classification", "")).startswith("census_retail_")
        ):
            stage.update(state="unknown", next_attempt_at=None)
        else:
            missing_target = any(
                c["month"] == month
                for c in result.get("readback", {}).get("source_missing", [])
            )
            acquired = result.get("response_observed_at")
            fresh = acquired is not None and _instant(acquired) >= _instant(
                target["release_at"]
            )
            complete = job["state"] == "complete" and (
                name == "history" or (not missing_target and fresh)
            )
            if complete:
                stage.update(
                    state="complete",
                    completed_at=self.clock().isoformat(),
                    next_attempt_at=None,
                )
                if name == "target" and target["history"]["state"] == "blocked":
                    target["history"].update(
                        state="pending",
                        next_attempt_at=(now + timedelta(seconds=3)).isoformat(),
                    )
            else:
                retry = now + timedelta(
                    seconds=RETRY_SECONDS[min(stage["attempts"] - 1, 7)]
                    + random.uniform(0, 2)
                )
                if result.get("retry_at"):
                    retry = max(retry, _instant(result["retry_at"]))
                stage.update(
                    state="awaiting_provider",
                    next_attempt_at=retry.isoformat(),
                    classification=stage["classification"]
                    or "target_month_not_returned",
                )


class RetailAutoStrategy(CpiAutoStrategy):
    def _view(self, value):
        return {
            **super()._view(value),
            "policy": {
                "provider": "Census",
                "max_attempts_per_stage": 10,
                "event_max_reserved_http": 30,
                "rolling_24h_max_actual_http": 400,
                "retry_seconds": RETRY_SECONDS,
                "history_months": 12,
                "buffer_months": 12,
            },
        }
