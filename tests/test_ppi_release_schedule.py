"""Deterministic PPI calendar and collection checks; no transport or database."""

import json
from pathlib import Path

import pytest
from test_cpi_release_schedule import Clock, Service, calendar_html
from test_cpi_release_schedule_ics import event, ics

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import (
    CALENDAR_MAX_AGE,
    CpiReleaseScheduler,
    CpiScheduleError,
)
from market_data_foundation.historical.ppi_release_schedule import (
    CALENDAR_URL,
    ICS_URL,
    PpiReleaseScheduler,
    parse_official_calendar,
    parse_official_ics,
)

OCTOBER = ("October 2026", "Nov. 13, 2026", "08:30 AM")


class PpiService(Service):
    profile = PPI_PROFILE

    def __init__(self):
        super().__init__()
        self.available = True
        self.returned_series = PPI_PROFILE.series_ids

    def points(self, month):
        return [
            {
                "series_id": series,
                "month": month,
                "index_value": "150.000",
                "available_at": "2026-11-13T13:30:00+00:00",
            }
            for series in self.returned_series
        ]

    def update_range(self, start, end, **kwargs):
        result = super().update_range(start, end, **kwargs)
        result["source_returned_series_months"] = [
            {"series_id": p["series_id"], "month": p["month"]} for p in result["points"]
        ]
        return result


def ppi_event(uid="ppi-oct", at="20261113T083000", sequence=1, **options):
    options.setdefault("summary", "Producer Price Index")
    return event(uid, at, sequence, **options)


def make(tmp_path, *, at="2026-11-13T13:30:00+00:00", **options):
    clock, service = Clock(at), PpiService()
    options.setdefault("calendar_fetcher", lambda: calendar_html(OCTOBER))
    options.setdefault("reference_fetcher", None)
    options.setdefault("history_start", "2010-04")
    scheduler = PpiReleaseScheduler(
        service, tmp_path / "ppi-schedule.json", clock=clock, **options
    )
    return scheduler, service, clock


def test_mixed_ics_selects_only_exact_ppi_titles_and_preserves_dst():
    parsed = parse_official_ics(
        ics(
            event(),
            ppi_event("summer", "20260312T083000"),
            ppi_event("winter", summary="Producer Price Indexes"),
            ppi_event("other", summary="Producer Price Index (Other)"),
        )
    )
    assert {item["uid"] for item in parsed} == {"summer", "winter"}
    assert parsed[0]["release_at"] == "2026-03-12T12:30:00+00:00"
    assert parsed[1]["release_at"] == "2026-11-13T13:30:00+00:00"
    assert all("reference_month" not in item for item in parsed)


def test_summaryless_cancellation_requires_previously_bound_ppi_uid():
    raw = ics(ppi_event(at=None, summary=None), method="CANCEL")
    assert parse_official_ics(raw) == []
    assert parse_official_ics(raw, known_uids={"ppi-oct"})[0]["status"] == "CANCELLED"


def test_reference_html_maps_delayed_release_without_subtracting_month(tmp_path):
    delayed = ("September 2026", "Nov. 13, 2026", "08:30 AM")
    scheduler, _, _ = make(
        tmp_path,
        calendar_fetcher=lambda: ics(event(), ppi_event()),
        reference_fetcher=lambda: calendar_html(delayed),
    )
    result = scheduler.refresh_calendar()
    assert result["calendar"]["source"] == ICS_URL
    assert result["calendar"]["release_count"] == 1
    assert result["calendar"]["reference_calendar"]["source"] == CALENDAR_URL
    assert [t["reference_month"] for t in result["targets"]] == ["2026-09"]
    evidence = result["targets"][0]["reference_month_evidence"]
    assert evidence["source"] == CALENDAR_URL
    assert Path(evidence["raw_path"]).read_bytes() == calendar_html(delayed)
    assert parse_official_calendar(calendar_html(delayed))[0]["reference_month"] == (
        "2026-09"
    )


def test_ics_without_html_remains_unbound_instead_of_guessing(tmp_path):
    scheduler, service, _ = make(tmp_path, calendar_fetcher=lambda: ics(ppi_event()))
    result = scheduler.due_check()
    assert result["targets"] == []
    assert result["calendar"]["reference_mapping_status"] == "pending"
    assert service.updates == []


def test_retime_and_summaryless_cancellation_keep_bound_ppi_identity(tmp_path):
    scheduler, service, clock = make(tmp_path, at="2026-11-01T00:00:00+00:00")
    scheduler.import_calendar(calendar_html(OCTOBER), clock())
    scheduler.import_calendar(ics(ppi_event()), clock())
    result = scheduler.import_calendar(
        ics(ppi_event(at="20261117T083000", sequence=2)), clock()
    )
    assert result["targets"][0]["release_at"] == "2026-11-17T13:30:00+00:00"
    assert result["calendar"]["changes"]["retimed"] == ["2026-10"]
    result = scheduler.import_calendar(
        ics(ppi_event(at=None, sequence=3, summary=None), method="CANCEL"), clock()
    )
    assert result["targets"][0]["state"] == "calendar_removed"
    clock.advance(20 * 86400)
    scheduler.calendar_fetcher = lambda: ics(
        ppi_event(at=None, sequence=3, summary=None), method="CANCEL"
    )
    scheduler.due_check()
    assert service.updates == []


@pytest.mark.parametrize(
    "returned", [CPI_PROFILE.series_ids, PPI_PROFILE.series_ids[:3]]
)
@pytest.mark.parametrize("auto_mode", [False, True])
def test_target_requires_all_four_ppi_series(tmp_path, returned, auto_mode):
    scheduler, service, _ = make(tmp_path, auto_mode=auto_mode)
    service.returned_series = returned
    result = scheduler.due_check()
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["target"]["state"] != "complete"
    assert service.updates[0][2] == PPI_PROFILE.series_ids


def test_monthly_history_runs_once_with_signals_unsupported(tmp_path):
    scheduler, service, _ = make(tmp_path)
    first = scheduler.due_check()
    assert first["action"] == "updated"
    assert first["targets"][0]["verified_target_series"] == list(PPI_PROFILE.series_ids)
    result = scheduler.due_check()
    assert result["action"] == "history_updated"
    assert service.updates[-1] == ("2010-04", "2026-10", PPI_PROFILE.series_ids)
    assert result["signals"] == {"state": "unsupported", "events": {}}
    assert result["revision_status"] == "unsupported"
    scheduler.due_check()
    scheduler.refresh_signals()
    assert len(service.updates) == 2
    assert scheduler.status()["signals"].get("next_refresh_at") is None
    assert "recovery" not in scheduler.status()["signals"]


def test_ppi_rejects_cpi_errata_fetcher_and_explicit_imports(tmp_path):
    with pytest.raises(CpiScheduleError, match="schedule_signals_unsupported"):
        make(tmp_path, signal_fetcher=lambda: b"CPI Errata")
    scheduler, _, _ = make(tmp_path)
    with pytest.raises(CpiScheduleError, match="schedule_signals_unsupported"):
        scheduler.import_signals([])
    with pytest.raises(CpiScheduleError, match="schedule_signals_unsupported"):
        scheduler.import_signal({"series_ids": list(CPI_PROFILE.series_ids)})


@pytest.mark.parametrize("asset", [None, "cpi"])
def test_ppi_state_refuses_cpi_or_unlabelled_state(tmp_path, asset):
    scheduler, _, _ = make(tmp_path)
    value = scheduler._read()
    value.pop("asset")
    if asset:
        value["asset"] = asset
    scheduler.state_path.write_text(json.dumps(value))
    before = scheduler.state_path.read_bytes()
    with pytest.raises(CpiScheduleError, match="schedule_asset_mismatch"):
        scheduler.refresh_calendar()
    assert scheduler.state_path.read_bytes() == before


def test_cpi_legacy_state_is_accepted_but_ppi_state_is_rejected(tmp_path):
    scheduler = CpiReleaseScheduler(None, tmp_path / "cpi.json")
    state = scheduler._read()
    state.pop("asset")
    scheduler.state_path.write_text(json.dumps(state))
    assert scheduler.status()["history_start"] == "1999-01"
    state["asset"] = "ppi"
    scheduler.state_path.write_text(json.dumps(state))
    with pytest.raises(CpiScheduleError, match="schedule_asset_mismatch"):
        scheduler.status()


def test_stale_calendar_keeps_cached_ppi_targets_and_source(tmp_path):
    scheduler, service, clock = make(tmp_path)
    scheduler.refresh_calendar()
    clock.advance(CALENDAR_MAX_AGE.total_seconds())

    def unavailable():
        raise CpiScheduleError("calendar_timeout")

    scheduler.calendar_fetcher = unavailable
    result = scheduler.due_check()
    assert result["calendar_status"] == "stale"
    assert result["calendar"]["source"] == CALENDAR_URL
    assert result["action"] == "calendar_unavailable"
    assert service.updates == []


def test_service_profile_cannot_silently_cross_assets(tmp_path):
    with pytest.raises(CpiScheduleError, match="schedule_service_profile_mismatch"):
        CpiReleaseScheduler(PpiService(), tmp_path / "cpi.json")


def test_default_history_uses_profile_or_requires_explicit_qualified_start(tmp_path):
    if PPI_PROFILE.earliest_month is None:
        with pytest.raises(CpiScheduleError, match="schedule_history_start_required"):
            PpiReleaseScheduler(None, tmp_path / "ppi.json")
    else:
        scheduler = PpiReleaseScheduler(None, tmp_path / "ppi.json")
        assert scheduler.history_start == PPI_PROFILE.earliest_month
        with pytest.raises(
            CpiScheduleError, match="schedule_history_start_before_profile"
        ):
            PpiReleaseScheduler(None, tmp_path / "early.json", history_start="1999-01")
