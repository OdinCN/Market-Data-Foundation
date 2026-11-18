"""Deterministic rule/holiday/DST/cache/fallback/retry/recovery and paused heartbeat."""

import copy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.claims_calendar import (
    BACKUP_URL,
    PRIMARY_URL,
    RULE,
    parse_official_rule,
)
from market_data_foundation.historical.claims_contract import SERIES_IDS, ClaimsError
from market_data_foundation.historical.claims_schedule import (
    ClaimsAutoStrategy,
    ClaimsReleaseScheduler,
)

AT = datetime(2026, 11, 2, 18, tzinfo=UTC)


def html():
    return (
        f"<p>{RULE}</p><table><tr><th>Release Date</th><th>Release Time</th></tr>"
        "<tr><td>Wednesday, November 25, 2026</td><td>8:30 AM EST</td></tr>"
        "<tr><td>Wednesday, November 10, 2027</td><td>8:30 AM EST</td></tr>"
        "<tr><td>Wednesday, November 24, 2027</td><td>8:30 AM EST</td></tr></table>"
    ).encode()


def setup(tmp_path, **kwargs):
    clock, calls = [AT], []
    result = [
        dict(
            state="complete",
            job_id="synthetic",
            classification=None,
            result={
                "material": dict(status="source_not_updated"),
            },
        )
    ]

    def submit(command, **options):
        calls.append((command, options))
        return copy.deepcopy(result[0])

    scheduler = ClaimsReleaseScheduler(
        SimpleNamespace(submit=submit),
        tmp_path / "claims.json",
        clock=lambda: clock[0],
        calendar_fetcher=kwargs.get("primary", html),
        fallback_fetcher=kwargs.get("backup", html),
    )
    return scheduler, clock, calls, result


def test_rule_generates_weeks_exceptions_dst_and_no_invented_reference_week():
    parsed = parse_official_rule(html(), PRIMARY_URL, AT)
    events = parsed["events"]
    assert len(events) > 50
    for date in ("2026-11-25", "2027-11-10", "2027-11-24"):
        assert events["dol-claims-" + date]["release_at"] == date + "T13:30:00+00:00"
        assert (
            events["dol-claims-" + date]["schedule_basis"] == "official_exception_table"
        )
    assert "dol-claims-2026-11-26" not in events
    assert events["dol-claims-2027-07-01"]["release_at"] == "2027-07-01T12:30:00+00:00"
    assert all(
        e["reference_week"] is None and e["planned_only"] for e in events.values()
    )
    assert parsed["rules_valid_until"] == "2027-12-31"
    for broken in (
        html().replace(b"Thursday morning", b"Friday morning"),
        html().replace(b"Wednesday, November 25", b"Thursday, November 25"),
        html().replace(b"8:30 AM EST", b"9:00 AM EST"),
    ):
        with pytest.raises(ClaimsError, match="layout_unrecognized"):
            parse_official_rule(broken, PRIMARY_URL, AT)


def test_worker_paused_heartbeat_idle_and_no_get_refresh(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    strategy = ClaimsAutoStrategy(
        scheduler, tmp_path / "strategy.json", clock=lambda: clock[0]
    )
    assert strategy.get()["worker"]["state"] == "not_started"
    assert not scheduler.state_path.exists() and not strategy.strategy_path.exists()
    assert strategy.tick()["action"] == "paused"
    assert strategy.get()["worker"]["state"] == "paused"
    assert not scheduler.state_path.exists() and not calls
    strategy.update(enabled=True, history_start=scheduler.history_start)
    assert strategy.tick()["action"] == "not_due"
    assert strategy.get()["worker"]["state"] == "idle"
    assert scheduler.status()["calendar_status"] == "fresh"
    assert not calls
    clock[0] += timedelta(seconds=121)
    assert strategy.get()["worker"]["state"] == "stale"


def test_fallback_retention_refresh_failure_uses_valid_cache_then_expires(tmp_path):
    def fail():
        raise ClaimsError("claims_http_failure", http_status=503)

    scheduler, clock, _, _ = setup(tmp_path, primary=fail)
    assert scheduler.due_check()["action"] == "not_due"
    state = scheduler.status()
    assert state["calendar"]["source"] == BACKUP_URL
    assert state["calendar"]["refresh_mode"] == "fallback"
    assert len(state["calendar"]["refresh_attempts"]) == 2
    assert (
        __import__("pathlib").Path(state["calendar"]["raw_locator"]).read_bytes()
        == html()
    )
    scheduler.fallback_fetcher = fail
    clock[0] += timedelta(hours=7)
    scheduler.due_check()
    assert scheduler.status()["calendar_status"] == "fresh"
    assert scheduler.status()["calendar"]["consecutive_refresh_failures"] == 1
    assert scheduler.status()["calendar"]["events"]
    clock[0] += timedelta(hours=18)
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert scheduler.status()["calendar_status"] == "stale"


def test_429_does_not_fallback_and_backs_off(tmp_path):
    calls = []

    def primary():
        raise ClaimsError(
            "claims_http_failure", http_status=429, retry_at=AT + timedelta(hours=1)
        )

    scheduler, _, _, _ = setup(
        tmp_path, primary=primary, backup=lambda: calls.append(1) or html()
    )
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert not calls
    assert (
        scheduler.status()["calendar"]["next_refresh_at"]
        == (AT + timedelta(hours=1)).isoformat()
    )


def test_due_old_report_retry_six_actual_weeks_and_low_priority_history(tmp_path):
    scheduler, clock, calls, result = setup(tmp_path)
    scheduler.due_check()
    clock[0] = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    first = scheduler.due_check()
    assert first["state"] == "awaiting_provider"
    command, options = calls[0]
    assert (
        command["end"] == "2026-11-05"
        and options["event_id"] == "dol-claims-2026-11-05"
    )
    assert scheduler.due_check()["action"] == "not_due"
    clock[0] += timedelta(seconds=15)
    actuals = {
        s: "2026-10-31" if s.startswith("IC") else "2026-10-24" for s in SERIES_IDS
    }
    result[0]["result"] = dict(
        material=dict(
            status="available",
            event_id=options["event_id"],
            actual_weeks=actuals,
            readback_verified=True,
        ),
        series_coverage={s: dict(latest_week=w) for s, w in actuals.items()},
        readback=dict(
            verified=True,
            as_of_basis="available",
            actuals_verified=True,
            actual_weeks=actuals,
        ),
    )
    assert scheduler.due_check()["state"] == "complete"
    assert scheduler.due_check()["state"] == "complete"
    assert calls[-1][0]["action"] == "init"
    assert calls[-1][0]["start"] == scheduler.history_start
    assert len(calls) == 3
    assert scheduler.due_check()["action"] == "not_due"


def test_interrupted_and_calendar_move_or_removal_never_silently_replays(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    target = state["targets"][0]
    target["target"].update(state="running", attempts=1)
    scheduler._save(state)
    assert scheduler.due_check()["action"] == "execution_interrupted"
    assert scheduler._read()["targets"][0]["target"]["state"] == "unknown"
    state = scheduler._read()
    state["targets"][0]["release_at"] = "2026-11-05T14:30:00+00:00"
    scheduler._accept(state, html(), AT)
    assert state["targets"][0]["calendar_execution_blocked"]
    clock[0] = datetime(2026, 11, 5, 15, tzinfo=UTC)
    scheduler.due_check()
    assert not calls


def test_calendar_refresh_preserves_cancelled_or_conflicting_event(tmp_path):
    scheduler, _, _, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    event = next(iter(state["calendar"]["events"].values()))
    event["status"] = "CANCELLED"
    scheduler._accept(state, html(), AT, source=BACKUP_URL)
    assert state["targets"][0]["calendar_execution_blocked"]


def test_rule_expiry_is_fail_closed_even_inside_24_hour_cache(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    clock[0] = datetime(2027, 12, 31, 23, tzinfo=UTC)
    scheduler.due_check()
    assert scheduler.status()["calendar_status"] == "fresh"
    clock[0] += timedelta(hours=7)
    assert scheduler.status()["calendar_status"] == "stale"
    assert scheduler.due_check()["action"] == "calendar_unavailable"
    assert not calls


def test_moved_execution_block_remains_after_repeated_refresh(tmp_path):
    scheduler, _, _, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    state["targets"][0]["target"]["attempts"] = 1
    state["targets"][0]["release_at"] = "2026-11-05T14:30:00+00:00"
    scheduler._accept(state, html(), AT)
    scheduler._accept(state, html(), AT)
    assert state["targets"][0]["calendar_execution_blocked"]


def test_thursday_waiting_target_retries_after_multiple_daily_refreshes(tmp_path):
    scheduler, clock, calls, _ = setup(tmp_path)
    clock[0] = datetime(2026, 11, 5, 13, tzinfo=UTC)
    scheduler.due_check()
    acquired = clock[0]
    clock[0] += timedelta(minutes=30)
    assert scheduler.due_check()["state"] == "awaiting_provider"
    original = scheduler.status()["targets"][0]
    original_hash = scheduler.status()["calendar"]["events"][original["event_id"]][
        "source_sha256"
    ]
    for offset in (1, 2, 3):
        clock[0] = acquired + timedelta(days=offset, hours=1)
        assert scheduler.due_check()["state"] == "awaiting_provider"
        # Re-open from durable state on every iteration.
        state = scheduler._read()
        target = next(t for t in state["targets"] if t["key"] == original["key"])
        assert not target["calendar_execution_blocked"]
        assert target["target"]["attempts"] == offset + 1
        assert calls[-1][0]["end"] == "2026-11-05"
        assert calls[-1][1]["event_id"] == original["event_id"]
        evidence = state["calendar"]["events"][original["event_id"]]
        assert evidence["acquired_at"] == acquired.isoformat()
        assert evidence["source_sha256"] == original_hash
        raw_path = (
            scheduler.state_path.parent
            / "claims-calendar-evidence"
            / (original_hash + ".bin")
        )
        assert raw_path.read_bytes() == html()
    assert scheduler.publications(acquired - timedelta(microseconds=1)) == []
    assert scheduler.publications(acquired)[0]["event_id"] == original["event_id"]
    assert not state["calendar"]["event_history"]


@pytest.mark.parametrize("guard", ["cancelled", "conflict"])
def test_repeated_fallback_and_horizon_roll_preserve_event_guards(tmp_path, guard):
    scheduler, clock, calls, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    event = state["calendar"]["events"]["dol-claims-2026-11-05"]
    if guard == "cancelled":
        event["status"] = "CANCELLED"
    else:
        event["classification"] = "calendar_fallback_conflict"
    scheduler._save(state)
    for at in (AT, AT + timedelta(hours=1), datetime(2026, 11, 6, 14, tzinfo=UTC)):
        clock[0] = at
        scheduler._accept(scheduler._read(), html(), at, source=BACKUP_URL)
        state = scheduler._read()
        target = next(t for t in state["targets"] if t["event_id"] == event["event_id"])
        assert target["calendar_execution_blocked"]
        retained = state["calendar"]["events"][event["event_id"]]
        assert retained["classification"]
        if guard == "cancelled":
            assert retained["status"] == "CANCELLED"
            publication = next(
                e
                for e in scheduler.publications()
                if e["event_id"] == event["event_id"]
            )
            assert publication["publication_status"] == "cancelled"
    assert scheduler.due_check()["action"] == "not_due"
    assert not calls


@pytest.mark.parametrize("executed", [False, True])
@pytest.mark.parametrize("legacy", [False, True])
def test_exception_date_move_matches_one_week_slot_and_preserves_attempts(
    tmp_path, executed, legacy
):
    scheduler, clock, calls, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    target = next(t for t in state["targets"] if t["source_date"] == "2026-11-25")
    if legacy:
        for t in state["targets"]:
            t.pop("rule_slot", None)
            t["key"] = t["event_id"]
        for e in state["calendar"]["events"].values():
            e.pop("rule_slot", None)
    key = target["key"]
    if executed:
        target["target"].update(attempts=1, state="complete")
    scheduler._save(state)
    changed = html().replace(
        b"<tr><td>Wednesday, November 25, 2026</td><td>8:30 AM EST</td></tr>", b""
    )
    changed_at = AT + timedelta(hours=1)
    scheduler._accept(scheduler._read(), changed, changed_at)
    scheduler._accept(scheduler._read(), changed, changed_at, source=BACKUP_URL)
    state = scheduler._read()
    matches = [
        t for t in state["targets"] if t["rule_slot"] == "dol-claims-week-2026-11-26"
    ]
    assert len(matches) == 1
    moved = matches[0]
    assert moved["key"] == key and moved["event_id"] == "dol-claims-2026-11-26"
    assert moved["target"]["attempts"] == int(executed)
    assert moved["calendar_execution_blocked"] is executed
    if executed:
        assert moved["calendar_validation"] == "calendar_moved_after_execution"
    else:
        assert moved["target"]["next_attempt_at"] == moved["release_at"]
    assert any(
        e["event_id"] == "dol-claims-2026-11-25" for e in scheduler.publications(AT)
    )
    assert not any(
        e["event_id"] == "dol-claims-2026-11-26" for e in scheduler.publications(AT)
    )
    assert len(state["calendar"]["event_history"]) == 1
    # No execution is needed to prove the persisted identity/attempt invariants.
    assert not calls


def test_fallback_schedule_conflict_cannot_be_washed_by_repeated_primary(tmp_path):
    scheduler, _, _, _ = setup(tmp_path)
    scheduler.due_check()
    changed = html().replace(
        b"<tr><td>Wednesday, November 25, 2026</td><td>8:30 AM EST</td></tr>", b""
    )
    scheduler._accept(
        scheduler._read(), changed, AT + timedelta(hours=1), source=BACKUP_URL
    )
    for _ in range(2):
        scheduler._accept(scheduler._read(), html(), AT + timedelta(hours=2))
        target = next(
            t for t in scheduler._read()["targets"] if t["source_date"] == "2026-11-25"
        )
        assert target["calendar_execution_blocked"]
        assert target["calendar_validation"] == "calendar_fallback_conflict"


@pytest.mark.parametrize(
    "proof", ["absent", "wrong_week", "five_series", "material_unread"]
)
def test_latest_week_alone_or_incomplete_exact_readback_never_completes(
    tmp_path, proof
):
    scheduler, clock, _, result = setup(tmp_path)
    scheduler.due_check()
    clock[0] = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    actuals = {s: "2026-10-24" for s in SERIES_IDS}
    verified = dict(actuals)
    if proof == "wrong_week":
        verified["ICSA"] = "2026-10-31"
    if proof == "five_series":
        actuals.pop("ICSA")
        verified.pop("ICSA")
    result[0]["result"] = dict(
        material=dict(
            status="available",
            event_id="dol-claims-2026-11-05",
            actual_weeks=actuals,
            readback_verified=proof != "material_unread",
        ),
        series_coverage={s: dict(latest_week="2026-10-31") for s in SERIES_IDS},
        readback={}
        if proof == "absent"
        else dict(
            verified=True,
            as_of_basis="available",
            actuals_verified=True,
            actual_weeks=verified,
        ),
    )
    assert scheduler.due_check()["state"] == "awaiting_provider"


def test_closed_http_reservation_settles_actuals_and_keeps_legacy_dol_upper_bound():
    job = dict(
        state="complete",
        finished_at=AT.isoformat(),
        result=dict(
            provider_requests=6, provider_attempts=8, material=dict(status="available")
        ),
    )
    charge = ClaimsReleaseScheduler.http_charge(job, 21)
    assert charge["charged_http"] == 11
    assert charge["dol_actual_http"] is None and charge["dol_held_http"] == 3
    job["result"]["material"]["provider_attempts"] = 1
    assert ClaimsReleaseScheduler.http_charge(job, 21)["charged_http"] == 9
    for state in ("running", "unknown", "failed"):
        assert ClaimsReleaseScheduler.http_charge(dict(job, state=state), 21) is None
    job["result"]["provider_attempts"] = "8"
    assert ClaimsReleaseScheduler.http_charge(job, 21) is None
    for bad in (-1, 22, None, "0"):
        assert (
            ClaimsReleaseScheduler.held_http(
                dict(reserved_http=21, charged_http=bad, accounting_state="actual")
            )
            == 21
        )


def test_legacy_four_reservations_reconcile_only_unique_closed_deterministic_jobs(
    tmp_path,
):
    from uuid import NAMESPACE_URL, uuid5

    scheduler, clock, _, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    target = state["targets"][0]
    start = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    admissions, records = [], []
    for i in range(4):
        at = start + timedelta(minutes=i)
        rid = str(uuid5(NAMESPACE_URL, f"claims:{target['key']}:target:{i + 1}"))
        admissions.append(dict(at=at.isoformat(), reserved_http=21))
        records.append(
            dict(
                request_id=rid,
                job_id=rid,
                origin="schedule",
                stage="target",
                state="complete",
                started_at=(at + timedelta(milliseconds=10)).isoformat(),
                finished_at=(at + timedelta(seconds=20)).isoformat(),
                result=dict(
                    provider_requests=6,
                    provider_attempts=6,
                    material=dict(status="available"),
                ),
            )
        )
    target["target"].update(
        state="awaiting_provider",
        attempts=4,
        last_attempt_at=(start + timedelta(minutes=3)).isoformat(),
        next_attempt_at=(start + timedelta(hours=24)).isoformat(),
    )
    state["data_admissions"] = admissions
    scheduler.runner.journal = SimpleNamespace(list=lambda: records)
    clock[0] = start + timedelta(hours=1)
    scheduler._settle_admissions(state, clock[0])
    assert sum(scheduler.held_http(e) for e in admissions) == 36
    assert all(e["reserved_http"] == 21 and e["dol_held_http"] == 3 for e in admissions)
    assert target["target"]["next_attempt_at"] == clock[0].isoformat()
    assert [r["state"] for r in records] == ["complete"] * 4  # no journal rewriting
    assert target["target"]["attempts"] == 4


def test_unclosed_or_unattributed_budget_reservations_are_not_released(tmp_path):
    scheduler, clock, _, _ = setup(tmp_path)
    scheduler.due_check()
    state = scheduler._read()
    state["data_admissions"] = [dict(at=clock[0].isoformat(), reserved_http=21)]
    scheduler.runner.journal = SimpleNamespace(list=lambda: [])
    scheduler._settle_admissions(state, clock[0] + timedelta(hours=1))
    assert state["data_admissions"] == [dict(at=clock[0].isoformat(), reserved_http=21)]


def test_verified_existing_history_does_not_submit_twenty_year_initialization(tmp_path):
    scheduler, clock, calls, result = setup(tmp_path)
    scheduler.due_check()
    clock[0] = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    actuals = {
        s: "2026-10-31" if s.startswith("IC") else "2026-10-24" for s in SERIES_IDS
    }
    receipt = dict(
        material=dict(
            status="available",
            event_id="dol-claims-2026-11-05",
            actual_weeks=actuals,
            readback_verified=True,
        ),
        readback=dict(
            verified=True,
            as_of_basis="available",
            actuals_verified=True,
            actual_weeks=actuals,
        ),
    )
    result[0]["result"] = receipt
    assert scheduler.due_check()["state"] == "complete"
    audits = []
    scheduler.runner.verify_existing_history = lambda *args: (
        audits.append(args)
        or dict(
            receipt,
            provider_requests=0,
            completion="existing_history_readback_verified",
        )
    )
    assert scheduler.due_check() == dict(
        action="existing_history_verified", state="complete"
    )
    assert len(calls) == 1 and calls[0][0]["action"] == "update"
    assert audits[0][:2] == (scheduler.history_start, "2026-11-05")
    assert (
        scheduler.status()["targets"][0]["history"]["verification"]
        == "existing_history_readback"
    )
    assert scheduler.status()["targets"][0]["reference_weeks"] == actuals
    assert scheduler.status()["targets"][1]["reference_weeks"] == {}
    assert scheduler.status()["next_due"] > clock[0].isoformat()


def test_material_write_unknown_is_not_automatically_replayed(tmp_path):
    scheduler, clock, calls, result = setup(tmp_path)
    scheduler.due_check()
    clock[0] = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    result[0]["result"] = dict(
        material=dict(
            status="unavailable", classification="claims_material_commit_unknown"
        )
    )
    assert scheduler.due_check()["state"] == "unknown"
    clock[0] += timedelta(hours=1)
    assert scheduler.due_check()["action"] == "not_due"
    assert len(calls) == 1


def test_status_separates_history_dependency_budget_and_real_material_timeout(tmp_path):
    scheduler, clock, calls, result = setup(tmp_path)
    scheduler.due_check()
    clock[0] = datetime(2026, 11, 5, 13, 30, tzinfo=UTC)
    result[0]["result"]["material"] = dict(
        status="unavailable", classification="claims_timeout"
    )
    scheduler.due_check()
    state = scheduler._read()
    target = state["targets"][0]
    target["target"]["budget_blocked_until"] = "2026-11-06T13:30:00+00:00"
    target["target"]["next_attempt_at"] = "2026-11-06T13:30:00+00:00"
    scheduler._save(state)
    before = scheduler.state_path.read_bytes()
    view = scheduler.status()["targets"][0]
    assert view["target"]["classification"] == "claims_timeout"
    assert view["target"]["budget_blocked_until"] == "2026-11-06T13:30:00+00:00"
    assert view["target"]["effective_next_attempt_at"] == "2026-11-06T13:30:00+00:00"
    assert view["history"]["state"] == "pending"
    assert view["history"]["waiting_for"] == "target_verification"
    assert view["history"]["effective_next_attempt_at"] is None
    assert view["history"]["next_attempt_at"] == "2026-11-05T13:30:00+00:00"
    assert scheduler.state_path.read_bytes() == before
    assert len(calls) == 1
