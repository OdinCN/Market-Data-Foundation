"""Frozen workflow contracts using only local stores and synthetic BLS replies."""

from copy import deepcopy
from datetime import timedelta
from uuid import UUID

import pytest

from market_data_foundation.historical.cpi_timeseries import month_range
from market_data_foundation.web.cpi_timeseries_api import PREFIX
from market_data_foundation.web.cpi_timeseries_jobs import WebError, verify_readback
from market_data_foundation.web.cpi_timeseries_policy import automatic_executions
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests import test_cpi_timeseries_web as base_web
from tests import test_cpi_timeseries_web_collection as base_collection
from tests.test_cpi_timeseries_web import NOW, client, command, no_network  # noqa: F401

fixture = base_web.fixture
collection = base_collection.collection


def target(month="2026-09", release="2026-10-13T12:30:00+00:00", **extra):
    return {
        "reference_month": month,
        "release_at": release,
        "state": "pending",
        "reference_month_evidence": {
            "source": "https://www.bls.gov/schedule/2026/10_sched.htm",
            "reference_month": month,
        },
        **extra,
    }


def calendar(monkeypatch, host, targets, **extra):
    snapshot = {"targets": targets, "signals": {"events": {}}, **extra}
    monkeypatch.setattr(host.scheduler, "status", lambda: deepcopy(snapshot))
    return snapshot


def test_empty_official_preset_has_exact_240_months(fixture, monkeypatch):
    host, store, source, _, _ = fixture
    calendar(monkeypatch, host, [target()])
    with client(host) as http:
        preset = http.get(PREFIX + "/status").json()["initialization"]
    assert preset == {
        "preset_id": "cpi_latest_20y_v1",
        "months": 240,
        "start": "2006-10",
        "end": "2026-09",
        "available": True,
        "reason": None,
        "anchor_basis": "official_elapsed_release/reference_month",
    }
    assert len(month_range(preset["start"], preset["end"])) == 240
    assert source.calls == 0 and store.status()["observation_count"] == 0
    assert not host.journal.directory.exists()
    assert not host.scheduler.state_path.parent.exists()


@pytest.mark.parametrize(
    "bad", ["absent", "future", "naive", "source", "month", "cancelled"]
)
def test_empty_without_valid_elapsed_evidence_is_unavailable(fixture, monkeypatch, bad):
    host, _, source, _, _ = fixture
    item = target()
    if bad == "future":
        item["release_at"] = (NOW + timedelta(days=1)).isoformat()
    elif bad == "naive":
        item["release_at"] = "2026-10-13T12:30:00"
    elif bad == "source":
        item["reference_month_evidence"]["source"] = "https://www.bls.gov.evil/"
    elif bad == "month":
        item["reference_month_evidence"]["reference_month"] = "2026-08"
    elif bad == "cancelled":
        item["state"] = "calendar_removed"
    calendar(monkeypatch, host, [] if bad == "absent" else [item])
    preset = host.status()["initialization"]
    assert preset["available"] is False
    assert preset["start"] is preset["end"] is None
    assert source.calls == 0 and not host.journal.directory.exists()


def test_latest_anchor_uses_confirmed_local_and_ignores_unknown(fixture, monkeypatch):
    host, store, source, _, _ = fixture
    host.read_factory().init_range("2026-08", "2026-10")
    for (sid, month), rows in store._history.items():
        if month == "2026-10":
            rows[-1]["available_at"] = None
    calendar(monkeypatch, host, [target("2026-08")])
    calls = source.calls
    preset = host.status()["initialization"]
    assert preset["end"] == "2026-09"
    assert preset["anchor_basis"] == "confirmed_local_available"
    assert store.status()["latest_months"][base_web.SERIES_IDS[0]] == "2026-10"
    assert source.calls == calls
    calendar(monkeypatch, host, [target("2026-10")])
    preset = host.status()["initialization"]
    assert preset["end"] == "2026-10"
    assert preset["anchor_basis"] == "official_elapsed_release/reference_month"


def test_unknown_only_local_holdings_never_become_anchor(fixture, monkeypatch):
    host, store, source, _, _ = fixture
    host.read_factory().init_range("2026-09", "2026-09")
    for rows in store._history.values():
        rows[-1]["available_at"] = None
    calls = source.calls
    assert host.status()["initialization"]["available"] is False
    assert source.calls == calls
    calendar(monkeypatch, host, [target()])
    assert host.status()["initialization"]["end"] == "2026-09"


def test_confirmed_local_anchor_includes_nondefault_series(fixture):
    host, _, source, _, _ = fixture
    host.read_factory().init_range(
        "2026-09", "2026-09", series_ids=(base_web.SERIES_IDS[-1],)
    )
    calls = source.calls
    preset = host.status()["initialization"]
    assert preset["end"] == "2026-09"
    assert preset["anchor_basis"] == "confirmed_local_available"
    assert source.calls == calls


def test_initialize_http_command_and_receipt(fixture, monkeypatch):
    host, _, _, _, _ = fixture
    calendar(monkeypatch, host, [target()])
    # Keep HTTP assertions deterministic; dispatch lifetime has its own tests.
    monkeypatch.setattr(
        "market_data_foundation.web.cpi_timeseries_api.dispatch_job",
        lambda submit, request: submit(request),
    )
    request = command("initialize", start="2024-01", end="2026-08")
    with client(host) as http:
        receipt = http.post(
            PREFIX + "/jobs", json=request, headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert receipt.status_code == 200
        assert receipt.json()["action"] == "initialize"
        assert receipt.json()["state"] == "complete"
        assert receipt.json()["start"] == request["start"]
        assert receipt.json()["end"] == request["end"]
        assert http.get(PREFIX + "/jobs").json()["jobs"] == [receipt.json()]
        assert (
            http.get(PREFIX + "/executions").json()["executions"][0]["origin"]
            == "manual"
        )


def test_initialize_refreshes_240_months_with_buffer_and_calendar_split(
    collection, monkeypatch
):
    host, store, transport = collection
    calendar(monkeypatch, host, [target()])
    host.max_collect_months = 240
    request = command("initialize", start="2006-10", end="2026-09")
    result = host.submit(request)
    assert result["action"] == "initialize" and result["state"] == "complete"
    assert result["result"]["readback"]["refresh_coverage_complete"]
    assert (
        result["result"]["readback"]["expected_points"]
        == len(host.profile.series_ids) * 240
    )
    assert result["result"]["saved_points"] == len(host.profile.series_ids) * 252
    assert transport.requests == [("2005", "2024"), ("2025", "2026")]
    transport.offset += 50
    transport.now += timedelta(days=1)
    refreshed = host.submit(dict(request, request_id=command()["request_id"]))
    assert refreshed["state"] == "complete"
    assert refreshed["result"]["saved_revisions"] == len(host.profile.series_ids) * 252
    assert len(transport.requests) == 4
    first = host.query(
        {"start": "2006-10", "end": "2006-10", "basket": "all", "adjustment": "all"}
    )["points"]
    assert len(first) == len(host.profile.series_ids) and all(
        p["change_12m_ratio"] is not None for p in first
    )
    assert store.status()["revision_count"] == len(host.profile.series_ids) * 252
    with pytest.raises(WebError, match="collection_range_exceeds_host_limit"):
        host.submit(command("update", start="2006-09", end="2026-09"))


@pytest.mark.parametrize(
    ("start", "end"), [("2024-01", "2026-08"), ("2000-01", "2026-08")]
)
def test_edited_initialize_range_refreshes_existing_data(
    collection, monkeypatch, start, end
):
    host, store, transport = collection
    calendar(monkeypatch, host, [target()])
    assert host.submit(command("init", start=start, end=end))["state"] == "complete"
    requests = len(transport.requests)
    # Legacy init remains inert for a fully held range.
    assert host.submit(command("init", start=start, end=end))["state"] == "complete"
    assert len(transport.requests) == requests
    transport.offset += 50
    transport.now += timedelta(days=1)
    request = command("initialize", start=start, end=end)
    result = host.submit(request)
    months = len(month_range(start, end))
    assert result["state"] == "complete" and result["action"] == "initialize"
    assert result["start"] == start and result["end"] == end
    assert (
        result["result"]["readback"]["expected_points"]
        == len(host.profile.series_ids) * months
    )
    assert result["result"]["readback"]["refresh_coverage_complete"] is True
    assert result["result"]["saved_revisions"] == len(host.profile.series_ids) * (
        months + 12
    )
    assert store.status()["revision_count"] == len(host.profile.series_ids) * (
        months + 12
    )
    assert len(transport.requests) == 2 * requests


def test_initialize_explicit_range_needs_no_preset_anchor(fixture, monkeypatch):
    host, _, source, _, _ = fixture
    assert host.status()["initialization"]["available"] is False
    monkeypatch.setattr(host, "status", lambda: pytest.fail("submit consulted preset"))
    request = command("initialize", start="2024-01", end="2026-09")
    result = host.submit(request)
    assert result["state"] == "complete" and result["action"] == "initialize"
    assert result["result"]["readback"]["verified"] is True
    assert host.submit(request) == result
    assert source.calls == 1


@pytest.mark.parametrize(
    ("start", "end", "classification"),
    [
        ("2026-10", "2026-09", "cpi_timeseries_range_invalid"),
        ("2026-13", "2026-09", "cpi_timeseries_month_invalid"),
        ("2026-01", "2026-00", "cpi_timeseries_month_invalid"),
        ("2026-1", "2026-09", "cpi_timeseries_month_invalid"),
        ("1900-12", "1901-01", "cpi_timeseries_range_invalid"),
    ],
)
def test_initialize_rejects_invalid_ranges_without_io(
    fixture, monkeypatch, start, end, classification
):
    host, _, source, closed, _ = fixture
    monkeypatch.setattr(
        "market_data_foundation.web.cpi_timeseries_api.dispatch_job",
        lambda submit, request: submit(request),
    )
    with client(host) as http:
        response = http.post(
            PREFIX + "/jobs",
            json=command("initialize", start=start, end=end),
            headers={"Origin": CANDIDATE_ORIGIN},
        )
    assert response.status_code == 400
    assert response.json()["classification"] == classification
    assert source.calls == 0 and closed == []
    assert not host.journal.directory.exists()


def test_initialize_obeys_configured_limit_including_default_240_months(fixture):
    host, _, source, _, _ = fixture
    host.max_collect_months = 36
    for start in ("2006-10", "2023-09"):
        with pytest.raises(WebError, match="collection_range_exceeds_host_limit"):
            host.submit(command("initialize", start=start, end="2026-09"))
    assert source.calls == 0 and not host.journal.directory.exists()
    result = host.submit(command("initialize", start="2023-10", end="2026-09"))
    assert result["state"] == "complete" and source.calls == 1
    assert (
        result["result"]["readback"]["expected_points"]
        == len(host.profile.series_ids) * 36
    )


def test_initialize_replay_precedes_changed_anchor_and_new_range_validation(
    collection, monkeypatch
):
    host, _, transport = collection
    snapshot = calendar(monkeypatch, host, [target()])
    request = command("initialize", start="2006-10", end="2026-09")
    result = host.submit(request)
    snapshot["targets"] = [target("2026-10")]
    assert host.status()["initialization"]["end"] == "2026-10"
    snapshot["targets"] = []
    # Replay cannot depend on a current preset or current collection permissions.
    monkeypatch.setattr(host, "status", lambda: pytest.fail("replay consulted preset"))
    host.max_collect_months = 1
    host.allow_collect = False
    assert host.submit(request) == result
    assert len(transport.requests) == 2
    with pytest.raises(WebError, match="request_id_conflict") as error:
        host.submit(dict(request, start="2006-11", end="2026-10"))
    assert error.value.status == 409
    host.allow_collect = True
    host.max_collect_months = None
    assert (
        host.submit(command("initialize", start="2006-10", end="2026-10"))["state"]
        == "complete"
    )


def test_initialize_missing_source_month_is_not_success(collection, monkeypatch):
    host, _, transport = collection
    calendar(monkeypatch, host, [target()])
    transport.missing.add("2026-09")
    job = host.submit(command("initialize", start="2006-10", end="2026-09"))
    assert job["state"] == "failed"
    assert job["classification"] == "source_range_incomplete"
    assert job["result"]["readback"]["verified"] is False
    requests = list(transport.requests)
    # Repairing the fixture source must not retry a failed request identity.
    transport.missing.clear()
    request = {key: job[key] for key in ("request_id", "action", "start", "end")}
    assert host.submit(request) == job
    assert transport.requests == requests


def test_initialize_unknown_result_is_not_retried_after_source_recovers(
    fixture, monkeypatch
):
    host, store, source, _, _ = fixture
    original_fetch = source.fetch

    def interrupted(*args):
        source.calls += 1
        raise RuntimeError("synthetic interruption")

    monkeypatch.setattr(source, "fetch", interrupted)
    request = command("initialize", start="2026-08", end="2026-09")
    result = host.submit(request)
    assert result["state"] == "unknown"
    path = host.journal.directory / (request["request_id"] + ".json")
    before = path.read_bytes()
    monkeypatch.setattr(source, "fetch", original_fetch)
    host.max_collect_months = 1
    host.allow_collect = False
    assert host.submit(request) == result
    for extra in ({"action": "init"}, {"start": "2026-09"}, {"end": "2026-10"}):
        with pytest.raises(WebError, match="request_id_conflict") as error:
            host.submit(dict(request, **extra))
        assert error.value.status == 409
    assert source.calls == 1 and store.status()["observation_count"] == 0
    assert path.read_bytes() == before


def test_initialize_requires_update_refresh_readback(fixture):
    host, _, _, _, _ = fixture
    request = command("initialize")
    written = host.read_factory().init_range("2026-09", "2026-09")
    readback = host.read_factory().query_range(
        "2026-09", "2026-09", allow_upstream=False, vintage_mode="available"
    )
    written["refresh_coverage_complete"] = False
    verified, result = verify_readback(written, readback, request)
    assert verified is False and not result["readback"]["refresh_coverage_complete"]


def test_enabled_only_preserves_persisted_baseline_and_real_worker(fixture):
    host, _, source, closed, _ = fixture
    host.auto_strategy.update(enabled=True, history_start="2006-10")
    host.auto_strategy.heartbeat("idle")
    host.scheduler.history_start = "1999-01"  # Simulate a restarted controller.
    with client(host) as http:
        response = http.put(
            PREFIX + "/strategy",
            json={"enabled": False},
            headers={"Origin": CANDIDATE_ORIGIN},
        )
        assert response.status_code == 200
        assert response.json()["history_start"] == "2006-10"
        response = http.put(
            PREFIX + "/strategy",
            json={"enabled": True},
            headers={"Origin": CANDIDATE_ORIGIN},
        )
        assert response.status_code == 200
        assert response.json()["history_start"] == "2006-10"
        worker = http.get(PREFIX + "/schedule").json()["worker"]
        assert worker == {
            "state": "idle",
            "enabled": True,
            "last_heartbeat_at": NOW.isoformat(),
        }
    assert source.calls == 0 and closed == []


def test_executions_only_attempted_stages_and_stable_ids():
    at = NOW.isoformat()
    attempted = {"state": "complete", "last_attempt_at": at, "completed_at": at}
    targets = [
        target(
            auto={
                "state": "complete",
                "last_attempt_at": at,
                "actual_http": 17,
                "classification": "auto_result",
            },
            target={
                "last_attempt_at": "fake",
                "attempts": 4,
                "completed_at": at,
                "classification": "old_stage",
            },
            classification="wrong_top_level",
            history={**attempted, "satisfied_by_monthly_history": True},
        ),
        target(
            "2026-08",
            auto={"state": "pending"},
            history={**attempted, "update_start": "2006-10", "update_end": "2026-08"},
        ),
        target("2026-07", target=attempted),  # No target.auto.last_attempt_at.
    ]
    snapshot = {
        "targets": targets,
        "signals": {
            "events": {
                "r1": {**attempted, "start_month": "2026-06", "end_month": "2026-07"}
            },
            "recovery": attempted,
        },
    }
    records = automatic_executions(snapshot)
    assert len(records) == 4
    assert len({r["id"] for r in records}) == 4
    assert all(UUID(r["id"]).version == 5 for r in records)
    latest = next(r for r in records if r["stage"] == "target")
    assert latest["started_at"] == at
    assert latest["result"]["actual_http_total"] == 17
    assert latest["result"]["scheduler_attempts_total"] == 4
    assert latest["finished_at"] == at
    assert latest["classification"] == "auto_result"
    assert "provider_attempts" not in latest["result"]
    assert next(r for r in records if r["stage"] == "recovery")["start"] is None
    snapshot["targets"][0]["release_at"] = "2026-11-01T00:00:00Z"
    snapshot["targets"][0]["auto"]["next_attempt_at"] = "2026-11-02T00:00:00Z"
    assert [r["id"] for r in automatic_executions(snapshot)] == [
        r["id"] for r in records
    ]
    snapshot["signals"]["recovery"]["end_month"] = "2026-10"
    assert [r["id"] for r in automatic_executions(snapshot)] == [
        r["id"] for r in records
    ]
    snapshot["targets"][0]["auto"]["last_attempt_at"] = (
        NOW + timedelta(seconds=1)
    ).isoformat()
    assert automatic_executions(snapshot)[0]["finished_at"] is None


def test_get_projections_do_not_write_or_access_source(fixture, monkeypatch):
    host, store, source, _, _ = fixture
    host.submit(command())
    host.auto_strategy.update(enabled=True, history_start="2006-10")
    host.auto_strategy.heartbeat("idle")
    path = host.auto_strategy.strategy_path
    before = path.read_bytes(), store.status(), source.calls
    next_at = (NOW + timedelta(days=10)).isoformat()
    snapshot = calendar(
        monkeypatch,
        host,
        [
            target(),
            target(
                "2026-10",
                next_at,
                auto={
                    "state": "running",
                    "last_attempt_at": NOW.isoformat(),
                    "actual_http": 2,
                },
            ),
        ],
        next_due=(NOW + timedelta(seconds=5)).isoformat(),
        calendar={"last_refresh_error": "calendar_http_403"},
        calendar_status="fresh",
    )
    monkeypatch.setattr(
        host.auto_strategy, "_save", lambda *_: pytest.fail("GET wrote strategy")
    )
    monkeypatch.setattr(
        host.scheduler, "_save", lambda *_: pytest.fail("GET wrote scheduler")
    )
    monkeypatch.setattr(
        store, "save_batches", lambda *_: pytest.fail("GET wrote business data")
    )
    with client(host) as http:
        for _ in range(2):
            assert http.get(PREFIX + "/status").status_code == 200
            strategy = http.get(PREFIX + "/strategy").json()
            assert strategy["next_release_at"] == next_at
            assert strategy["next_due"] == snapshot["next_due"]
            schedule = http.get(PREFIX + "/schedule").json()
            assert schedule["calendar_status"] == "fresh"
            assert schedule["calendar"]["last_refresh_error"] == "calendar_http_403"
            executions = http.get(PREFIX + "/executions").json()["executions"]
            assert len(executions) == 2
            assert {r["origin"] for r in executions} == {"manual", "automatic"}
            assert len(http.get(PREFIX + "/jobs").json()["jobs"]) == 1
    assert (path.read_bytes(), store.status(), source.calls) == before


@pytest.mark.parametrize(
    "flags",
    [
        {"calendar_cancelled_after_completion": True},
        {"absent_from_latest_calendar": True},
        {
            "cancellation_evidence": {
                "source": "https://www.bls.gov/schedule/news_release/bls.ics"
            }
        },
    ],
)
def test_next_release_excludes_removed_or_cancelled_targets(
    fixture, monkeypatch, flags
):
    host, _, source, _, _ = fixture
    future = (NOW + timedelta(days=1)).isoformat()
    calendar(monkeypatch, host, [target("2026-10", future, state="complete", **flags)])
    assert host.strategy()["next_release_at"] is None
    assert source.calls == 0


def test_official_future_ics_does_not_need_guessed_reference_month(
    fixture, monkeypatch
):
    host, _, source, _, _ = fixture
    future = (NOW + timedelta(days=3)).isoformat()
    calendar(
        monkeypatch,
        host,
        [],
        calendar={
            "source": "https://www.bls.gov/schedule/news_release/bls.ics",
            "events": {
                "cpi": {"release_at": future, "status": "CONFIRMED"},
                "cancelled": {
                    "release_at": (NOW + timedelta(days=1)).isoformat(),
                    "status": "CANCELLED",
                },
            },
        },
    )
    assert host.strategy()["next_release_at"] == future
    assert host.status()["initialization"]["available"] is False
    assert source.calls == 0
