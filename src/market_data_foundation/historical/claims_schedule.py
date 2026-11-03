"""Weekly DOL targets over the existing durable macro scheduler and worker."""

import copy
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from market_data_foundation.historical.calendar_resilience import (
    failure,
    may_fallback,
    refresh_metadata,
    retry_time,
)
from market_data_foundation.historical.claims_calendar import (
    BACKUP_URL,
    NEW_YORK,
    PRIMARY_URL,
    parse_official_rule,
    rule_slot,
)
from market_data_foundation.historical.claims_contract import (
    ClaimsError,
    day,
    twenty_year_range,
)
from market_data_foundation.historical.cpi_auto_strategy import CpiAutoStrategy
from market_data_foundation.historical.cpi_release_schedule import (
    _aware,
    _instant,
    _write_atomic,
    now_utc,
)
from market_data_foundation.historical.pce_release_schedule import (
    CALENDAR_RETRY_SECONDS,
    RETRY_SECONDS,
    PceReleaseScheduler,
)
from market_data_foundation.web.claims_jobs import CLAIMS_PROFILE, actuals_ready

POLICY = dict(
    stage_max_attempts=8,
    rolling_24h_reserved_http=84,
    calendar_cache_hours=24,
    history_priority="low",
    event_binding="calendar_is_not_value_evidence",
)


class ClaimsReleaseScheduler(PceReleaseScheduler):
    profile, asset_key = CLAIMS_PROFILE, "claims"

    def __init__(
        self,
        runner,
        state_path,
        *,
        clock=now_utc,
        history_start=None,
        calendar_fetcher=None,
        fallback_fetcher=None,
    ):
        self.runner, self.clock = runner, clock
        self.state_path = Path(state_path).resolve()
        self.history_start = day(
            history_start or twenty_year_range(clock().date().isoformat())[0]
        ).isoformat()
        self.calendar_fetcher, self.fallback_fetcher = (
            calendar_fetcher,
            fallback_fetcher,
        )
        self.auto_mode = True

    def _read(self):
        if not self.state_path.exists():
            return dict(
                version=1,
                asset="claims",
                history_start=self.history_start,
                calendar=dict(source=PRIMARY_URL, events={}),
                targets=[],
            )
        try:
            state = json.loads(self.state_path.read_text())
            self._validate_asset(state)
            if state["version"] != 1 or not isinstance(state["targets"], list):
                raise ValueError
            self.history_start = day(state["history_start"]).isoformat()
            return state
        except ValueError, TypeError, KeyError, OSError:
            raise ClaimsError("claims_schedule_state_invalid") from None

    def status(self):
        state, now = self._read(), _aware(self.clock())
        due = [
            t[n]["next_attempt_at"]
            for t in state["targets"]
            if not t.get("calendar_execution_blocked")
            for n in ("target", "history")
            if t[n]["state"] in {"pending", "awaiting_provider"}
        ]
        return dict(
            state,
            provider="FRED",
            original_agency="DOL-ETA",
            calendar_status=self._calendar_status(state, now),
            next_due=min(due) if due else None,
            calendar_publications=self._publications(state),
        )

    def _publications(self, state, cutoff=None):
        calendar = state["calendar"]
        indexed = {}
        for e in calendar.get("event_history", []) + list(
            calendar.get("events", {}).values()
        ):
            event = dict(
                e, acquired_at=e.get("acquired_at", calendar.get("acquired_at"))
            )
            event["publication_status"] = (
                "cancelled" if event["status"] == "CANCELLED" else "scheduled"
            )
            if cutoff is not None and (
                not event["acquired_at"] or _instant(event["acquired_at"]) > cutoff
            ):
                continue
            slot = rule_slot(event)
            if (
                slot not in indexed
                or event["acquired_at"] >= indexed[slot]["acquired_at"]
            ):
                indexed[slot] = event
        return sorted(indexed.values(), key=lambda e: (e["release_at"], e["event_id"]))

    def publications(self, cutoff=None):
        return self._publications(self._read(), cutoff)

    def _calendar_status(self, state, at):
        value = super()._calendar_status(state, at)
        until = state["calendar"].get("rules_valid_until")
        if until and at.astimezone(NEW_YORK).date().isoformat() > until:
            return "stale"
        return value

    def _accept(self, state, raw, acquired, *, source=PRIMARY_URL):
        parsed = parse_official_rule(raw, source, acquired)
        old_calendar = state["calendar"]
        old_events = {
            rule_slot(e): dict(
                e, acquired_at=e.get("acquired_at", old_calendar.get("acquired_at"))
            )
            for e in old_calendar.get("events", {}).values()
        }
        previous = {rule_slot(t): t for t in state["targets"]}
        history = list(old_calendar.get("event_history", []))
        targets, events = [], {}
        for candidate in parsed["events"].values():
            slot = rule_slot(candidate)
            old = previous.pop(slot, None)
            prior = old_events.pop(slot, {})
            event = dict(candidate, acquired_at=acquired.isoformat())
            classification = prior.get("classification")
            if prior.get("status") == "CANCELLED":
                event = dict(prior)
                classification = "calendar_cancellation_requires_requalification"
            moved = prior and prior["release_at"] != candidate["release_at"]
            if moved and source == BACKUP_URL:
                event = dict(prior)
                classification = classification or "calendar_fallback_conflict"
            if (
                old
                and old["release_at"] != candidate["release_at"]
                and any(old[n]["attempts"] for n in ("target", "history"))
            ):
                classification = classification or "calendar_moved_after_execution"
            if classification:
                event["classification"] = classification
            # An unchanged plan keeps its original knowledge/evidence cutoff.
            fields = ("event_id", "release_at", "status", "classification")
            if prior and all(prior.get(k) == event.get(k) for k in fields):
                event = prior
            elif prior:
                history.append(prior)
                event["acquired_at"] = acquired.isoformat()
            events[event["event_id"]] = event
            if old is None and _instant(event["release_at"]) <= acquired:
                continue  # importing a calendar never replays earlier events
            stage = dict(
                state="pending", attempts=0, next_attempt_at=event["release_at"]
            )
            blocked = (
                bool((old or {}).get("calendar_execution_blocked"))
                or event.get("status") == "CANCELLED"
                or bool(event.get("classification"))
            )
            target = dict(
                old or {},
                key=old["key"] if old else slot,
                rule_slot=slot,
                event_id=event["event_id"],
                release_at=event["release_at"],
                source_date=event["source_date"],
                calendar_execution_blocked=blocked,
                calendar_validation=(old or {}).get("calendar_validation")
                or classification,
                target=copy.deepcopy(old["target"] if old else stage),
                history=copy.deepcopy(old["history"] if old else stage),
            )
            if old and old["release_at"] != event["release_at"]:
                if any(old[n]["attempts"] for n in ("target", "history")):
                    target.update(
                        calendar_execution_blocked=True,
                        calendar_validation="calendar_moved_after_execution",
                    )
                else:
                    for n in ("target", "history"):
                        target[n]["next_attempt_at"] = event["release_at"]
            targets.append(target)
        for t in previous.values():
            if t["source_date"] < parsed["rules_valid_from"]:
                # Rolling the generation horizon forward is not cancellation.
                targets.append(t)
            else:
                targets.append(
                    dict(
                        t,
                        calendar_execution_blocked=True,
                        calendar_validation="calendar_removed",
                    )
                )
        # Absence outside the rolling window is not cancellation, including plans
        # that were already in the past on their first calendar import.
        for event in old_events.values():
            events[event["event_id"]] = event
        parsed.update(events=events, event_history=history)
        sha = hashlib.sha256(raw).hexdigest()
        path = self.state_path.parent / "claims-calendar-evidence" / (sha + ".bin")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.read_bytes() != raw:
            raise ClaimsError("claims_calendar_retention_conflict")
        if not path.exists():
            _write_atomic(path, raw)
        state["targets"] = sorted(targets, key=lambda t: (t["release_at"], t["key"]))
        state["calendar"] = dict(
            parsed,
            source=source,
            acquired_at=acquired.isoformat(),
            sha256=sha,
            raw_locator=str(path),
            consecutive_refresh_failures=0,
            next_refresh_at=(acquired + timedelta(hours=6)).isoformat(),
        )
        self._save(state)

    def _refresh(self, state, now):
        attempts, errors = [], []
        for source, fetch in (
            (PRIMARY_URL, self.calendar_fetcher),
            (BACKUP_URL, self.fallback_fetcher),
        ):
            if errors and not may_fallback(errors[0]):
                break
            try:
                if fetch is None:
                    raise ClaimsError("claims_calendar_fetcher_unavailable")
                raw = fetch()
                acquired = _aware(getattr(raw, "obtained_at", self.clock()))
                if acquired > _aware(self.clock()):
                    raise ClaimsError("claims_calendar_acquired_at_in_future")
                old = state["calendar"].get("acquired_at")
                if old and acquired < _instant(old):
                    raise ClaimsError("claims_calendar_import_older_than_current")
                candidate = copy.deepcopy(state)
                self._accept(candidate, raw, acquired, source=source)
                state.clear()
                state.update(candidate)
                attempts.append(dict(source=source, outcome="accepted"))
                refresh_metadata(
                    state["calendar"],
                    now,
                    primary=PRIMARY_URL,
                    attempts=attempts,
                    success=True,
                )
                self._save(state)
                return True
            except Exception as error:
                errors.append(error)
                attempts.append(
                    dict(
                        source=source,
                        outcome="failed",
                        **failure(error, "claims_calendar_unavailable"),
                    )
                )
        value = state["calendar"]
        count = value.get("consecutive_refresh_failures", 0) + 1
        value.update(
            error=attempts[-1]["classification"],
            consecutive_refresh_failures=count,
            next_refresh_at=retry_time(
                now, CALENDAR_RETRY_SECONDS[min(count - 1, 3)], *errors
            ),
        )
        refresh_metadata(
            value, now, primary=PRIMARY_URL, attempts=attempts, success=False
        )
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
                        return dict(action="execution_interrupted")
            if self._calendar_status(state, now) == "clock_reversal":
                return dict(action="clock_reversal")
            refresh = state["calendar"].get("next_refresh_at")
            if not refresh or _instant(refresh) <= now:
                self._refresh(state, now)
            if self._calendar_status(state, now) != "fresh":
                return dict(action="calendar_unavailable")
            for name in ("target", "history"):
                for target in reversed(state["targets"]):
                    stage = target[name]
                    if (
                        target.get("calendar_execution_blocked")
                        or _instant(target["release_at"]) > now
                        or (
                            name == "history"
                            and target["target"]["state"] != "complete"
                        )
                        or stage["state"] not in {"pending", "awaiting_provider"}
                        or _instant(stage["next_attempt_at"]) > now
                    ):
                        continue
                    return self._run(state, target, name, now)
        return dict(action="not_due")

    def _run(self, state, target, name, now):
        stage = target[name]
        budget = [
            b
            for b in state.get("data_admissions", [])
            if _instant(b["at"]) > now - timedelta(hours=24)
        ]
        reserve = 21  # six FRED + one DOL logical read, at most three attempts each
        if (
            sum(b["reserved_http"] for b in budget) + reserve
            > POLICY["rolling_24h_reserved_http"]
        ):
            stage["next_attempt_at"] = (
                _instant(budget[0]["at"]) + timedelta(hours=24)
            ).isoformat()
            self._save(state)
            return dict(action="budget_exhausted")
        stage.update(
            state="running",
            attempts=stage["attempts"] + 1,
            last_attempt_at=now.isoformat(),
        )
        rid = str(
            uuid5(NAMESPACE_URL, f"claims:{target['key']}:{name}:{stage['attempts']}")
        )
        stage["request_id"] = rid
        state["data_admissions"] = budget + [
            dict(at=now.isoformat(), reserved_http=reserve)
        ]
        self._save(state)
        start = (
            self.history_start
            if name == "history"
            else (day(target["source_date"]) - timedelta(weeks=8)).isoformat()
        )
        try:
            job = self.runner.submit(
                dict(
                    request_id=rid,
                    action="init" if name == "history" else "update",
                    start=start,
                    end=target["source_date"],
                ),
                origin="schedule",
                stage=name,
                event_id=target["event_id"],
            )
        except Exception:
            stage.update(state="unknown", classification="execution_interrupted")
            self._save(state)
            return dict(action="execution_interrupted")
        result = job.get("result") or {}
        stage.update(
            last_job_id=job["job_id"],
            result=result,
            classification=job.get("classification"),
        )
        ready = (
            job["state"] == "complete"
            and result.get("material", {}).get("event_id") == target["event_id"]
            and result["material"].get("status") == "available"
        )
        if ready:
            ready = actuals_ready(result)
        if job["state"] == "unknown":
            stage["state"] = "unknown"
        elif ready:
            stage.update(
                state="complete", completed_at=_aware(self.clock()).isoformat()
            )
        elif stage["attempts"] >= POLICY["stage_max_attempts"]:
            stage["state"] = "pending_exhausted"
        else:
            delay = now + timedelta(
                seconds=RETRY_SECONDS[min(stage["attempts"] - 1, 7)]
            )
            retry = result.get("retry_at")
            stage.update(
                state="awaiting_provider",
                next_attempt_at=max(
                    delay, _instant(retry) if retry else delay
                ).isoformat(),
            )
        self._save(state)
        return dict(action="executed", job_id=job["job_id"], state=stage["state"])


class ClaimsAutoStrategy(CpiAutoStrategy):
    def _validate_period(self, value):
        return day(value).isoformat()

    def _validate_history_floor(self, value):
        day(value)

    def _view(self, value):
        return dict(super()._view(value), policy=POLICY)
