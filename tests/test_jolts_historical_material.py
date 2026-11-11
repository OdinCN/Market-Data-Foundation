"""Controlled archives, real selection/acquisition/retention; no provider IO."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_macro_release_chain import Raw

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.jolts_historical_material import (
    candidates,
    retain_original,
)
from market_data_foundation.historical.macro_release_acquisition import (
    MaterialAcquirer,
    capture_for_execution,
    select_material,
)
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from tests.test_bls_web_transport import wire as wire

AT = datetime(2026, 11, 11, 3, tzinfo=UTC)


def snapshot():
    return {
        "targets": [],
        "calendar": {
            "source": "https://www.bls.gov/schedule/news_release/bls.ics",
            "events": {
                "september": dict(
                    uid="september",
                    status="CONFIRMED",
                    summary="Job Openings and Labor Turnover Survey",
                    reference_month=None,
                    release_at="2026-11-03T15:00:00+00:00",
                ),
                "august": dict(
                    uid="august",
                    status="CONFIRMED",
                    summary="Job Openings and Labor Turnover Survey",
                    reference_month=None,
                    release_at="2026-09-29T14:00:00+00:00",
                ),
            },
        },
    }


def body(month="SEPTEMBER", day="November 3"):
    return f"""<h1>JOB OPENINGS AND LABOR TURNOVER - {month} 2026</h1>
    <p>For release 10:00 a.m. (ET) {day}, 2026</p>""".encode()


def service():
    store = InMemoryTimeSeriesStore(clock=lambda: AT, profile=JOLTS_PROFILE)
    store.raw_store = Raw(JOLTS_PROFILE)
    repo = ReleaseRecords(store)
    repo.prepare()
    return SimpleNamespace(store=store), repo


def acquire(tmp_path, svc, snap, fetcher, start="2026-09", end=None):
    return capture_for_execution(
        svc,
        snapshot=snap,
        start=start,
        end=end or start,
        state_path=tmp_path / "material.json",
        numeric_result={"points": [{"batch_id": "preexisting_numeric_batch"}]},
        fetcher=fetcher,
    )


def test_unmapped_real_shape_requires_archive_not_inferred_month(tmp_path):
    snap = snapshot()
    before = deepcopy(snap)
    svc, repo = service()
    assert select_material("jolts", snap, "2026-09", "2026-09", AT) is None
    calls = []

    def fetch(url, family):
        calls.append((url, family))
        return body()

    result = acquire(tmp_path, svc, snap, fetch)
    assert result["state"] == "retained"
    assert result["provider_requests"] == result["transport_calls"] == 1
    assert result["event_ids"] == ["bls:jolts:2026-11-03"]
    assert result["numeric_batch_ids"] == ["preexisting_numeric_batch"]
    assert calls == [
        ("https://www.bls.gov/news.release/archives/jolts_11032026.htm", "jolts")
    ]
    assert snap == before
    row = repo._rows(None)[0]
    assert row["reference_month"] == "2026-09"
    assert row["obtained_at"] == row["available_at"] == AT.isoformat()
    assert list(svc.store.raw_store.objects.values()) == [body()]
    assert repo.read(as_of=(AT - timedelta(microseconds=1)).isoformat())["events"] == []
    assert row["retention"]["acquisition_method"] == "retained_official_material_cache"
    repeated = acquire(
        tmp_path, svc, snap, lambda *_: pytest.fail("duplicate upstream request")
    )
    assert repeated["state"] == "retained"
    assert repeated["provider_requests"] == repeated["transport_calls"] == 0
    assert repo._rows(None) == [row]


def test_other_month_skips_verified_outside_archive_and_reuses_it_later(tmp_path):
    svc, repo = service()
    calls = []

    def fetch(url, _):
        calls.append(url)
        return body() if "1103" in url else body("AUGUST", "September 29")

    result = acquire(tmp_path, svc, snapshot(), fetch, "2026-08")
    assert result["state"] == "retained" and result["provider_requests"] == 2
    assert result["event_ids"] == ["bls:jolts:2026-09-29"]
    assert [r["reference_month"] for r in repo._rows(None)] == ["2026-08"]
    result = acquire(
        tmp_path, svc, snapshot(), lambda *_: pytest.fail("must reuse original")
    )
    assert result["state"] == "retained" and result["provider_requests"] == 0
    assert {r["reference_month"] for r in repo._rows(None)} == {"2026-08", "2026-09"}


@pytest.mark.parametrize(
    "change",
    [
        {"status": "CANCELLED"},
        {"status": "TENTATIVE"},
        {"absent_from_latest_calendar": True},
        {"calendar_execution_blocked": True},
        {"calendar_validation": "calendar_fallback_conflict"},
        {"classification": "calendar_ics_revision_conflict"},
        {"uid": None},
        {"summary": "State Job Openings"},
        {"release_at": "2026-12-01T15:00:00Z"},
    ],
)
def test_vetoes_do_not_fetch_or_revive_date(tmp_path, change):
    svc, repo = service()
    snap = snapshot()
    snap["calendar"]["events"] = {
        "september": {**snap["calendar"]["events"]["september"], **change}
    }
    assert (
        acquire(tmp_path, svc, snap, lambda *_: pytest.fail("vetoed request"))["state"]
        == "partial"
    )
    assert repo._rows(None) == []


def test_ambiguity_same_day_and_summaryless_cancel_win():
    snap = snapshot()
    first = snap["calendar"]["events"]["september"]
    snap["calendar"]["events"] = [first, {**first, "uid": "another"}]
    assert candidates(snap, "2026-09", "2026-09", AT) == []
    snap["calendar"]["events"] = [first, {"uid": "september", "status": "CANCELLED"}]
    assert candidates(snap, "2026-09", "2026-09", AT) == []


def test_blocked_target_and_foreign_calendar_cannot_fall_back(tmp_path):
    svc, _ = service()
    snap = snapshot()
    snap["targets"] = [
        {"reference_month": "2026-09", "calendar_execution_blocked": True}
    ]
    assert (
        acquire(tmp_path, svc, snap, lambda *_: pytest.fail("blocked target"))[
            "provider_requests"
        ]
        == 0
    )
    snap["targets"] = []
    snap["calendar"]["source"] = "https://unqualified.example/calendar.ics"
    assert (
        acquire(tmp_path, svc, snap, lambda *_: pytest.fail("foreign calendar"))[
            "provider_requests"
        ]
        == 0
    )


def test_body_must_match_explicit_calendar_reference_and_archive_date(tmp_path):
    svc, repo = service()
    snap = snapshot()
    snap["calendar"]["events"]["september"]["reference_month"] = "2026-08"
    result = acquire(tmp_path, svc, snap, lambda *_: body(), "2026-08")
    assert result["state"] == "partial" and result["reasons"] == [
        "release_material_calendar_identity_mismatch"
    ]
    assert repo._rows(None) == []
    snap = snapshot()
    result = acquire(
        tmp_path / "wrong-date", svc, snap, lambda *_: body(day="November 4")
    )
    assert result["state"] == "partial" and result["reasons"] == [
        "release_archive_header_date_conflict"
    ]
    assert repo._rows(None) == []


def test_failure_cooldown_unknown_and_no_duplicate_request(tmp_path):
    svc, repo = service()
    calls = []

    def fail(*_):
        calls.append(1)
        raise CpiTimeSeriesError("calendar_http_429")

    first = acquire(tmp_path, svc, snapshot(), fail)
    assert first["provider_requests"] == 1 and first["state"] == "partial"
    second = acquire(tmp_path, svc, snapshot(), fail)
    assert second["provider_requests"] == 0 and second["reasons"] == [
        "calendar_http_429"
    ]
    assert calls == [1] and repo._rows(None) == []


def test_corrupt_cached_bytes_fail_closed(tmp_path):
    svc, _ = service()
    assert acquire(tmp_path, svc, snapshot(), lambda *_: body())["state"] == "retained"
    path = next((tmp_path / "material.originals").glob("*.html"))
    path.write_bytes(b"changed")
    result = acquire(
        tmp_path, svc, snapshot(), lambda *_: pytest.fail("corrupt cache no retry")
    )
    assert result["reasons"] == ["jolts_original_integrity_invalid"]


def older_snapshot():
    snap = snapshot()
    for uid, at in [("july", "2026-09-01T14:00:00Z"), ("june", "2026-08-04T14:00:00Z")]:
        snap["calendar"]["events"][uid] = dict(
            snap["calendar"]["events"]["september"], uid=uid, release_at=at
        )
    return snap


def test_cached_exclusions_do_not_spend_next_execution_budget_or_hide_fourth_month(
    tmp_path,
):
    svc, repo = service()
    snap = older_snapshot()
    samples = {
        "1103": ("SEPTEMBER", "November 3"),
        "0929": ("AUGUST", "September 29"),
        "0901": ("JULY", "September 1"),
        "0804": ("JUNE", "August 4"),
    }
    calls = []

    def fetch(url, _):
        calls.append(url)
        return body(*samples[url.rsplit("_", 1)[1][:4]])

    first = acquire(tmp_path, svc, snap, fetch, "2026-06")
    assert first["state"] == "partial" and first["provider_requests"] == 3
    assert first["reasons"] == ["jolts_material_discovery_limit"]
    assert repo._rows(None) == []
    second = acquire(tmp_path, svc, snap, fetch, "2026-06")
    assert second["state"] == "retained" and second["provider_requests"] == 1
    assert second["event_ids"] == ["bls:jolts:2026-08-04"]
    assert len(calls) == len(set(calls)) == 4
    third = acquire(
        tmp_path, svc, snap, lambda *_: pytest.fail("duplicate request"), "2026-06"
    )
    assert third["state"] == "retained" and third["provider_requests"] == 0
    assert len(repo._rows(None)) == 1


@pytest.mark.parametrize(
    "block",
    [
        {"state": "calendar_removed"},
        {"publication_status": "CANCELLED"},
        {"calendar_validation": "calendar_fallback_conflict"},
    ],
)
def test_key_only_reference_month_respects_cancellation_and_conflict(tmp_path, block):
    svc, repo = service()
    snap = snapshot()
    snap["targets"] = {"2026-09": block}
    result = acquire(
        tmp_path, svc, snap, lambda *_: pytest.fail("blocked keyed target")
    )
    assert result["state"] == "partial" and result["provider_requests"] == 0
    assert repo._rows(None) == []


def test_identical_uid_duplicate_is_deduplicated_conflicting_uid_is_rejected():
    snap = snapshot()
    row = snap["calendar"]["events"]["september"]
    snap["calendar"]["events"] = [row, deepcopy(row)]
    assert len(candidates(snap, "2026-09", "2026-09", AT)) == 1
    snap["calendar"]["events"][1]["release_at"] = "2026-11-04T15:00:00Z"
    assert candidates(snap, "2026-09", "2026-09", AT) == []


def mapped_snapshot():
    snap = snapshot()
    row = dict(
        snap["calendar"]["events"]["september"],
        reference_month="2026-09",
        reference_month_evidence={
            "source": "https://www.bls.gov/schedule/news_release/jolts.htm",
            "reference_month": "2026-09",
        },
    )
    snap["targets"] = [row]
    return snap


@pytest.mark.parametrize("mapped", [False, True])
def test_named_and_discovery_paths_honor_existing_same_url_failure_cooldown(
    tmp_path, mapped
):
    svc, repo = service()
    candidate = candidates(snapshot(), "2026-09", "2026-09", AT)[0]
    selected = dict(candidate, reference_month="2026-09")
    MaterialAcquirer(tmp_path / "material.json").record_limitation(
        JOLTS_PROFILE,
        selected,
        classification="release_http_403",
        observed_at=AT,
        cooldown_until=AT + timedelta(days=1),
        evidence_locator="isolated-prior-403",
    )
    before = (tmp_path / "material.json").read_bytes()
    result = acquire(
        tmp_path,
        svc,
        mapped_snapshot() if mapped else snapshot(),
        lambda *_: pytest.fail("must respect same URL cooldown"),
    )
    assert result["provider_requests"] == 0 and result["reasons"] == [
        "release_http_403"
    ]
    assert (tmp_path / "material.json").read_bytes() == before and repo._rows(
        None
    ) == []


def test_discovery_failure_is_seen_by_named_path(tmp_path):
    svc, _ = service()

    def fail(*_):
        raise CpiTimeSeriesError("release_http_429")

    assert acquire(tmp_path, svc, snapshot(), fail)["provider_requests"] == 1
    result = acquire(
        tmp_path,
        svc,
        mapped_snapshot(),
        lambda *_: pytest.fail("must not bypass discovery cooldown"),
    )
    assert result["provider_requests"] == 0 and result["reasons"] == [
        "release_http_429"
    ]


def test_offline_original_keeps_source_time_separate_from_business_availability(
    tmp_path,
):
    svc, repo = service()
    candidate = candidates(snapshot(), "2026-09", "2026-09", AT)[0]
    original_at = AT - timedelta(days=2)
    retained = retain_original(
        tmp_path / "material.originals",
        candidate,
        body(),
        obtained_at=original_at,
        evidence_locator="verified-official-raw-and-receipt",
        clock=lambda: AT,
    )
    assert (
        retained["obtained_at"] == original_at.isoformat()
        and retained["cached_at"] == AT.isoformat()
    )
    result = acquire(
        tmp_path,
        svc,
        snapshot(),
        lambda *_: pytest.fail("offline transfer must not request"),
    )
    assert (
        result["state"] == "retained"
        and result["provider_requests"] == result["transport_calls"] == 0
    )
    row = repo._rows(None)[0]
    assert row["retention"]["original_obtained_at"] == original_at.isoformat()
    assert row["obtained_at"] == row["available_at"] == AT.isoformat()
    assert result["original_obtained_at"] == original_at.isoformat()
    assert repo.read(as_of=(AT - timedelta(microseconds=1)).isoformat())["events"] == []
    repeat = acquire(
        tmp_path, svc, snapshot(), lambda *_: pytest.fail("duplicate upstream")
    )
    assert repeat["state"] == "retained" and repeat["provider_requests"] == 0
    assert repo._rows(None) == [row]
    with pytest.raises(CpiTimeSeriesError, match="jolts_original_provenance_invalid"):
        retain_original(
            tmp_path / "future",
            candidate,
            body(),
            obtained_at=AT + timedelta(seconds=1),
            evidence_locator="future-invalid",
            clock=lambda: AT,
        )


@pytest.mark.parametrize("redirect", [False, True])
def test_default_acquisition_keeps_real_shared_bls_admission(
    tmp_path, monkeypatch, wire, redirect
):
    import json

    from market_data_foundation.historical import bls_web_transport as web
    from market_data_foundation.historical import jolts_release_schedule as schedule
    from market_data_foundation.historical.cpi_request_control import CpiRequestControl
    from market_data_foundation.historical.macro_release_acquisition import (
        fetch_official_material,
    )

    control = CpiRequestControl(
        tmp_path / "shared.json", clock=lambda: AT, min_interval_seconds=0
    )
    monkeypatch.setattr(schedule, "CpiRequestControl", lambda _: control)
    monkeypatch.setattr(
        web.HTTPSConnection, "headers", property(lambda _: {}), raising=False
    )
    scenarios, connections = wire
    if redirect:
        scenarios.append(
            {
                "status": 302,
                "location": "https://www.bls.gov/news.release/archives/jolts_11032026.htm",
            }
        )
    scenarios.append({"raw": body()})
    svc, _ = service()
    assert (
        acquire(tmp_path, svc, snapshot(), fetch_official_material)["state"]
        == "retained"
    )
    ledger = json.loads(control.state_path.read_text())
    assert len(ledger["requests"]) == 1 + int(redirect)
    assert ledger["in_flight_at"] is None
    assert all(c.events[-1] == "close" for c in connections)
    result = acquire(tmp_path, svc, snapshot(), fetch_official_material)
    assert result["provider_requests"] == 0
    assert len(connections) == 1 + int(redirect)


def test_shared_bls_exhausted_budget_rejects_before_http(tmp_path, monkeypatch, wire):
    import json

    from market_data_foundation.historical import bls_web_transport as web
    from market_data_foundation.historical import jolts_release_schedule as schedule
    from market_data_foundation.historical.cpi_request_control import CpiRequestControl
    from market_data_foundation.historical.macro_release_acquisition import (
        fetch_official_material,
    )

    control = CpiRequestControl(
        tmp_path / "shared.json", clock=lambda: AT, min_interval_seconds=0
    )
    for _ in range(400):
        with control.attempt() as attempt:
            attempt.record_response(200, {})
    monkeypatch.setattr(schedule, "CpiRequestControl", lambda _: control)
    monkeypatch.setattr(
        web.HTTPSConnection, "headers", property(lambda _: {}), raising=False
    )
    svc, repo = service()
    result = acquire(tmp_path, svc, snapshot(), fetch_official_material)
    assert result["state"] == "partial" and "budget_exhausted" in result["reasons"][0]
    assert all("request" not in c.events for c in wire[1])
    assert len(json.loads(control.state_path.read_text())["requests"]) == 400
    assert repo._rows(None) == []


def test_unresolved_cancellation_cannot_revive_legacy_history_without_uid(tmp_path):
    svc, repo = service()
    snap = snapshot()
    snap["calendar"]["display_history"] = [
        {
            "reference_month": "2026-09",
            "release_at": "2026-11-03T15:00:00+00:00",
            "reference_month_evidence": {
                "source": "https://www.bls.gov/schedule/news_release/jolts.htm",
                "reference_month": "2026-09",
            },
        }
    ]
    snap["calendar"]["events"] = [
        {"uid": "past-unmapped", "status": "CANCELLED", "release_at": None}
    ]
    assert select_material("jolts", snap, "2026-09", "2026-09", AT)
    result = acquire(
        tmp_path, svc, snap, lambda *_: pytest.fail("unresolved cancellation")
    )
    assert result["provider_requests"] == 0 and result["state"] == "partial"
    assert repo._rows(None) == []
