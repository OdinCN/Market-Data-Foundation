"""Quarter/estimate-specific GDP scheduling over shared durable worker primitives."""

import copy
import hashlib
import json
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from market_data_foundation.historical.calendar_resilience import (
    failure,
    may_fallback,
    refresh_metadata,
    retry_time,
)
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    _aware,
    _instant,
    now_utc,
)
from market_data_foundation.historical.gdp_calendar import (
    GdpScheduleError,
    backup_events,
    parse_official_html,
    parse_official_ics,
)
from market_data_foundation.historical.gdp_contract import (
    shift_quarter,
    validate_quarter,
)
from market_data_foundation.historical.pce_release_schedule import (
    CALENDAR_RETRY_SECONDS,
    CALENDAR_URL,
    ICS_URL,
    RETRY_SECONDS,
    PceReleaseScheduler,
    fetch_official_calendar,
    fetch_official_html,
)
from market_data_foundation.web.gdp_jobs import GDP_PROFILE

POLICY = {
    "identity": "reference_quarter_and_native_estimate_kind",
    "event_binding": "calendar_is_not_value_evidence",
    "history_batch_quarters": 16,
    "history_priority": "low",
    "rolling_24h_reserved_http": 80,
    "stage_max_attempts": 10,
    "calendar_cache_hours": 24,
}


class GdpReleaseScheduler(PceReleaseScheduler):
    profile = GDP_PROFILE
    asset_key = "gdp"

    def __init__(
        self,
        runner,
        state_path,
        *,
        clock=now_utc,
        history_start=None,
        calendar_fetcher=fetch_official_calendar,
        fallback_fetcher=None,
    ):
        from pathlib import Path

        self.runner, self.clock = runner, clock
        self.state_path = Path(state_path).resolve()
        now = _aware(clock())
        current = f"{now.year:04d}-Q{(now.month - 1) // 3 + 1}"
        self.history_start = validate_quarter(
            history_start or shift_quarter(current, -80)
        )
        self.calendar_fetcher = calendar_fetcher
        self.fallback_fetcher = fallback_fetcher or (
            fetch_official_html if calendar_fetcher is fetch_official_calendar else None
        )
        self.auto_mode = True

    def _read(self):
        if not self.state_path.exists():
            return {
                "version": 1,
                "asset": "gdp",
                "history_start": self.history_start,
                "calendar": {"source": ICS_URL, "events": {}},
                "targets": [],
            }
        try:
            value = json.loads(self.state_path.read_text())
            self._validate_asset(value)
            if value["version"] != 1 or not isinstance(value["targets"], list):
                raise ValueError
            self.history_start = validate_quarter(value["history_start"])
            return value
        except GdpScheduleError:
            raise
        except OSError, ValueError, TypeError, KeyError:
            raise GdpScheduleError("gdp_schedule_state_invalid") from None

    def status(self):
        state, now = self._read(), _aware(self.clock())
        due = [
            t[name]["next_attempt_at"]
            for t in state["targets"]
            if not t.get("calendar_execution_blocked")
            for name in ("target", "history")
            if t[name]["state"] in {"pending", "awaiting_provider"}
        ]
        return {
            **state,
            "provider": "BEA",
            "calendar_status": self._calendar_status(state, now),
            "calendar_coverage": "explicit_quarter_and_estimate_only",
            "next_due": min(due) if due else None,
            "calendar_publications": [
                dict(
                    e,
                    acquired_at=state["calendar"].get("acquired_at"),
                    publication_status="cancelled"
                    if e["status"] == "CANCELLED"
                    else "scheduled",
                )
                for e in state["calendar"].get("events", {}).values()
            ],
        }

    def _accept(self, state, raw, acquired, *, events=None, source=ICS_URL):
        events = parse_official_ics(raw) if events is None else events
        old_events = state["calendar"].get("events", {})
        old_by_identity = {
            (e["reference_quarter"], e["estimate_kind"]): e
            for e in old_events.values()
            if e["reference_quarter"]
        }
        previous = {t["key"]: t for t in state["targets"]}
        targets = []
        for uid, event in events.items():
            old_event = old_events.get(uid)
            identity = (event["reference_quarter"], event["estimate_kind"])
            if old_event and (
                event["sequence"] < old_event["sequence"]
                or identity
                != (old_event["reference_quarter"], old_event["estimate_kind"])
            ):
                raise GdpScheduleError("gdp_calendar_sequence_or_identity_reversal")
            q = event["reference_quarter"]
            if not q:
                continue
            key = q + ":" + event["estimate_kind"]
            old = previous.pop(key, None)
            future = _instant(event["release_at"]) > acquired
            # Calendar imports do not replay past releases or initialize history.
            if old is None and not future:
                continue
            prior = old_by_identity.get(identity)
            blocked = (
                event["status"] != "CONFIRMED"
                or bool(event.get("classification"))
                or (
                    prior is not None
                    and prior["status"] == "CANCELLED"
                    and (uid != prior["uid"] or event["sequence"] <= prior["sequence"])
                )
            )
            stage = {
                "state": "pending",
                "attempts": 0,
                "next_attempt_at": event["release_at"],
            }
            target = {
                **(old or {}),
                "key": key,
                "reference_quarter": q,
                "estimate_kind": event["estimate_kind"],
                "event_id": event["event_id"],
                "uid": uid,
                "release_at": event["release_at"],
                "calendar_execution_blocked": bool(blocked),
                "calendar_validation": event.get("classification"),
                "publication_status": "cancelled"
                if event["status"] == "CANCELLED"
                else "scheduled",
                "target": old["target"] if old else dict(stage),
                "history": old["history"]
                if old
                else dict(stage, next_start=self.history_start),
            }
            if old and old["release_at"] != event["release_at"]:
                # Never reassign completed/interrupted work to a moved event.
                if any(old[n]["attempts"] for n in ("target", "history")):
                    target["calendar_execution_blocked"] = True
                    target["calendar_validation"] = "gdp_calendar_moved_after_execution"
                else:
                    for name in ("target", "history"):
                        target[name]["next_attempt_at"] = event["release_at"]
            targets.append(target)
        for old in previous.values():
            targets.append(
                dict(
                    old,
                    calendar_execution_blocked=True,
                    calendar_validation="calendar_removed",
                )
            )
        state["targets"] = sorted(targets, key=lambda t: (t["release_at"], t["key"]))
        state["calendar"] = {
            "source": source,
            "events": events,
            "acquired_at": acquired.isoformat(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "consecutive_refresh_failures": 0,
            "next_refresh_at": (acquired + timedelta(hours=6)).isoformat(),
        }
        self._save(state)

    def _refresh(self, state, now):
        attempts, errors = [], []
        for source, fetch, parser in (
            (ICS_URL, self.calendar_fetcher, parse_official_ics),
            (CALENDAR_URL, self.fallback_fetcher, parse_official_html),
        ):
            if source == CALENDAR_URL and (
                fetch is None or not may_fallback(errors[0])
            ):
                break
            try:
                if fetch is None:
                    raise GdpScheduleError("gdp_calendar_fetcher_unavailable")
                raw = fetch()
                acquired = _aware(getattr(raw, "obtained_at", self.clock()))
                if acquired > _aware(self.clock()):
                    raise GdpScheduleError("calendar_acquired_at_in_future")
                previous = state["calendar"].get("acquired_at")
                if previous and acquired < _instant(previous):
                    raise GdpScheduleError("calendar_import_older_than_current")
                events = parser(raw)
                if source == CALENDAR_URL:
                    events = backup_events(events, state["calendar"].get("events", {}))
                candidate = copy.deepcopy(state)
                self._accept(candidate, raw, acquired, events=events, source=source)
                state.clear()
                state.update(candidate)
                attempts.append({"source": source, "outcome": "accepted"})
                refresh_metadata(
                    state["calendar"],
                    now,
                    primary=ICS_URL,
                    attempts=attempts,
                    success=True,
                )
                self._save(state)
                return True
            except Exception as error:
                errors.append(error)
                attempts.append(
                    {
                        "source": source,
                        "outcome": "failed",
                        **failure(error, "gdp_calendar_unavailable"),
                    }
                )
        value = state["calendar"]
        failures = value.get("consecutive_refresh_failures", 0) + 1
        value.update(
            error=attempts[-1]["classification"],
            consecutive_refresh_failures=failures,
            next_refresh_at=retry_time(
                now, CALENDAR_RETRY_SECONDS[min(failures - 1, 3)], *errors
            ),
        )
        refresh_metadata(value, now, primary=ICS_URL, attempts=attempts, success=False)
        self._save(state)
        return False

    def due_check(self):
        now = _aware(self.clock())
        with self._locked():
            state = self._read()
            for target in state["targets"]:
                for name in ("target", "history"):
                    if target[name]["state"] == "running":
                        target[name].update(
                            state="unknown", classification="execution_interrupted"
                        )
                        self._save(state)
                        return {"action": "execution_interrupted"}
            if self._calendar_status(state, now) == "clock_reversal":
                return {"action": "clock_reversal"}
            refresh = state["calendar"].get("next_refresh_at")
            if not refresh or _instant(refresh) <= now:
                self._refresh(state, now)
            if self._calendar_status(state, now) != "fresh":
                return {"action": "calendar_unavailable"}
            eligible = [
                t
                for t in reversed(state["targets"])
                if not t.get("calendar_execution_blocked")
                and _instant(t["release_at"]) <= now
            ]
            for name in ("target", "history"):
                for target in eligible:
                    stage = target[name]
                    if name == "history" and target["target"]["state"] != "complete":
                        continue
                    if stage["state"] not in {"pending", "awaiting_provider"}:
                        continue
                    if _instant(stage["next_attempt_at"]) > now:
                        continue
                    return self._run(state, target, name, now)
        return {"action": "not_due"}

    def _run(self, state, target, name, now):
        stage = target[name]
        q = target["reference_quarter"]
        start = q if name == "target" else stage["next_start"]
        if start > q:
            stage.update(state="complete", completed_at=now.isoformat())
            self._save(state)
            return {"action": "history_not_required"}
        end = q if name == "target" else min(q, shift_quarter(start, 15))
        budget = [
            b
            for b in state.get("data_admissions", [])
            if _instant(b["at"]) > now - timedelta(hours=24)
        ]
        # Four tables, at most two five-year windows including the four-quarter buffer.
        reserve = 8
        if (
            sum(b["reserved_http"] for b in budget) + reserve
            > POLICY["rolling_24h_reserved_http"]
        ):
            stage["next_attempt_at"] = (
                _instant(budget[0]["at"]) + timedelta(hours=24)
            ).isoformat()
            self._save(state)
            return {"action": "budget_exhausted"}
        stage.update(
            state="running",
            attempts=stage["attempts"] + 1,
            last_attempt_at=now.isoformat(),
        )
        request_id = str(
            uuid5(
                NAMESPACE_URL, f"gdp:{target['key']}:{name}:{start}:{stage['attempts']}"
            )
        )
        stage["request_id"] = request_id
        state["data_admissions"] = budget + [
            {"at": now.isoformat(), "reserved_http": reserve}
        ]
        self._save(state)
        try:
            job = self.runner.submit(
                {
                    "request_id": request_id,
                    "action": "update",
                    "start": start,
                    "end": end,
                },
                origin="schedule",
                stage=name,
            )
        except Exception:
            stage.update(state="unknown", classification="execution_interrupted")
            self._save(state)
            return {"action": "execution_interrupted"}
        stage.update(
            last_job_id=job["job_id"],
            result=job["result"],
            classification=job["classification"],
        )
        if job["state"] == "unknown":
            stage["state"] = "unknown"
        elif (
            job["state"] == "complete"
            and job["result"]["completion"] != "source_coverage_partial"
        ):
            if name == "history" and end < q:
                stage.update(
                    state="pending",
                    next_start=shift_quarter(end, 1),
                    attempts=0,
                    next_attempt_at=(now + timedelta(seconds=5)).isoformat(),
                )
            else:
                stage.update(
                    state="complete", completed_at=_aware(self.clock()).isoformat()
                )
        elif stage["attempts"] >= POLICY["stage_max_attempts"]:
            stage["state"] = "pending_exhausted"
        else:
            retry = (job.get("result") or {}).get("retry_at")
            delay = now + timedelta(
                seconds=RETRY_SECONDS[min(stage["attempts"] - 1, 7)]
            )
            stage.update(
                state="awaiting_provider",
                next_attempt_at=max(
                    delay, _instant(retry) if retry else delay
                ).isoformat(),
            )
        self._save(state)
        return {"action": "executed", "job_id": job["job_id"], "state": stage["state"]}


class GdpAutoStrategy(CpiAutoStrategy):
    def _validate_period(self, value):
        return validate_quarter(value)

    def _validate_history_floor(self, value):
        validate_quarter(value)

    def _view(self, value):
        return {**super()._view(value), "policy": POLICY}
