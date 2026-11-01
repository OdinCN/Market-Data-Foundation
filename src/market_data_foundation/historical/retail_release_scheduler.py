"""Census-only durable release queue, using shared locks and bounded retry policy."""

import hashlib
import http.client
import random
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import NAMESPACE_URL, uuid5

from market_data_foundation.historical.census_retail_release_schedule import (
    CALENDAR_URL,
    MAX_CALENDAR_BYTES,
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
    def get(self):
        from market_data_foundation.historical.census_retail_live import HttpResult

        connection = http.client.HTTPSConnection("www.census.gov", timeout=20)
        try:
            connection.request(
                "GET",
                "/retail/release_schedule.html",
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
    def fetch():
        response = CensusControlledTransport(CensusCalendarTransport(), control).get()
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

    def __init__(self, host, state_path, *, clock=now_utc, calendar_fetcher=None):
        self.host, self.clock = host, clock
        self.state_path = Path(state_path).resolve()
        self.history_start = "1992-01"
        self.calendar_fetcher = calendar_fetcher

    def _read(self):
        state = super()._read()
        if not self.state_path.exists():
            state["calendar"] = {"source": CALENDAR_URL, "events": {}}
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
            if age <= timedelta(hours=24)
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
        return {
            **state,
            "provider": "Census",
            "calendar_status": self._calendar_status(state, now),
            "calendar_coverage": "official_data_month_only",
            "next_due": min(due) if due else state["calendar"].get("next_refresh_at"),
        }

    def _accept(self, state, raw, acquired):
        events = parse_official_calendar(raw)
        previous = {t["reference_month"]: t for t in state["targets"]}
        targets, calendar = [], {}
        for event in events:
            month, release = event["reference_month"], event["release_at"]
            at = release.isoformat() if release else None
            calendar[month] = dict(
                reference_month=month,
                release_at=at,
                status=event["status"],
                actual_release_at=None,
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
                    "source": CALENDAR_URL,
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
            old.update(calendar_execution_blocked=True, state="calendar_removed")
            targets.append(old)
        state["targets"] = sorted(targets, key=lambda t: t["reference_month"])
        state["calendar"] = {
            "source": CALENDAR_URL,
            "events": calendar,
            "acquired_at": acquired.isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "next_refresh_at": (acquired + timedelta(hours=6)).isoformat(),
            "consecutive_refresh_failures": 0,
        }
        self._save(state)

    def _refresh(self, state, now):
        try:
            if self.calendar_fetcher is None:
                raise RetailSourceError("census_retail_calendar_not_bound")
            self._accept(state, self.calendar_fetcher(), now)
            return True
        except Exception as error:
            value = state["calendar"]
            failures = value.get("consecutive_refresh_failures", 0) + 1
            retry = now + timedelta(
                seconds=(300, 900, 1800, 3600)[min(failures - 1, 3)]
            )
            if getattr(error, "retry_at", None):
                retry = max(retry, _instant(str(error.retry_at)))
            value.update(
                error=getattr(
                    error, "classification", "census_retail_calendar_failure"
                ),
                last_refresh_attempt=now.isoformat(),
                consecutive_refresh_failures=failures,
                next_refresh_at=retry.isoformat(),
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
