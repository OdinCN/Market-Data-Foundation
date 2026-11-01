import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.error import HTTPError, URLError

import pytest

from market_data_foundation.historical.cpi_release_schedule import (
    CpiReleaseScheduler,
    CpiScheduleError,
)
from market_data_foundation.historical.cpi_revision_signals import (
    CpiSignalError,
    evidence_url,
    fetch_official_errata,
    parse_official_errata,
    validate_signal,
)
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesService,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    SERIES_IDS,
    InMemoryTimeSeriesStore,
)

EMPTY = b'<h1>Errata</h1><table id="errata_datatable"></table>'
NOTICE_URL = "https://www.bls.gov/errata/cpi-correction.htm"
CALENDAR = b"""<table><tr><th>Reference Month</th><th>Release Date</th>
<th>Release Time</th></tr>
<tr><td>October 2026</td><td>Nov. 10, 2026</td><td>08:30 AM</td></tr></table>"""


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)

    def __call__(self):
        return self.at

    def advance(self, seconds):
        self.at += timedelta(seconds=seconds)


class Source:
    registered = True

    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.values = {}
        self.omitted = set()
        self.error = None

    def fetch(self, series, start, end):
        self.calls.append((tuple(series), start, end))
        if self.error:
            raise self.error
        points = tuple(
            SourcePoint(s, m, self.values.get((s, m), Decimal("300")), {}, ())
            for s in series
            for m in month_range(start, end)
            if (s, m) not in self.omitted
        )
        return (
            SourceBatch(
                points,
                repr(points).encode(),
                {"seriesid": list(series)},
                self.clock(),
                1,
            ),
        )


def notice(**changes):
    sid = SERIES_IDS[0]
    result = dict(
        evidence_url=NOTICE_URL,
        kind="correction",
        verified=True,
        scope="national",
        series_ids=[sid],
        start_month="2026-09",
        end_month="2026-09",
        effective_at="2026-11-10T13:30:00+00:00",
        evidence_text=f"National correction {sid} 2026-09: 300 to 301.",
        expected_points=[
            dict(
                series_id=sid, month="2026-09", previous_value="300", index_value="301"
            )
        ],
    )
    result.update(changes)
    return result


@pytest.fixture
def setup(tmp_path):
    clock = Clock()
    source = Source(clock)
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(clock=clock), source)
    scheduler = CpiReleaseScheduler(
        service,
        tmp_path / "schedule.json",
        clock=clock,
        calendar_fetcher=lambda: CALENDAR,
        signal_fetcher=lambda: EMPTY,
        history_start="2026-01",
    )
    assert scheduler.run_due()["action"] == "updated"
    assert scheduler.run_due()["action"] == "history_updated"
    source.calls.clear()
    clock.advance(1)
    return scheduler, service, source, clock


def event(scheduler):
    return next(iter(scheduler.status()["signals"]["events"].values()))


def test_discovery_deduplicates_relevant_unparsed_notices_and_ignores_others(setup):
    scheduler, _, source, _ = setup
    row = (
        "10/19/2026",
        "Consumer Price Index",
        "Other",
        f'<a href="{NOTICE_URL}">CPI correction</a>',
        "Scope unresolved",
        "Pending",
    )
    scheduler.signal_fetcher = lambda: errata_table(
        row,
        row,
        ("10/19/2026", "Employment", "Other", "Employment correction", "Typo", "Done"),
    )
    scheduler.refresh_signals()
    scheduler.refresh_signals()
    assert len(scheduler.status()["signals"]["events"]) == 1
    assert event(scheduler)["state"] == "review_required"
    scheduler.run_due()
    assert source.calls == []
    assert scheduler.status()["revision_status"] == "pending"


def test_city_notice_never_applied_to_national_series(setup):
    scheduler, _, source, _ = setup
    scheduler.signal_fetcher = lambda: discovery_table("Correction to Boston CPI")
    scheduler.refresh_signals()
    assert event(scheduler)["state"] == "review_required"
    scheduler.run_due()
    assert source.calls == []
    with pytest.raises(CpiScheduleError, match="signal_evidence_insufficient"):
        scheduler.import_signal(notice(evidence_text="Boston CPI correction 2026-09"))
    with pytest.raises(CpiScheduleError, match="signal_scope_unverified"):
        scheduler.import_signal(notice(scope="Boston"))


def test_future_correction_waits_for_time_then_old_values_remain_pending(setup):
    scheduler, _, source, clock = setup
    scheduler.import_signal(
        notice(effective_at=(clock.at + timedelta(hours=2)).isoformat())
    )
    scheduler.run_due()
    assert source.calls == []
    clock.advance(7200)
    assert scheduler.run_due()["action"] == "signal_pending"
    assert event(scheduler)["classification"] == "signal_revision_unconfirmed"
    assert source.calls == [((SERIES_IDS[0],), "2026-09", "2026-09")]
    source.values[SERIES_IDS[0], "2026-09"] = Decimal("301")
    clock.advance(300)
    assert scheduler.run_due()["action"] == "signal_completed"
    assert event(scheduler)["state"] == "complete"
    scheduler.import_signal(notice(effective_at="2026-11-10T15:30:01+00:00"))
    scheduler.refresh_signals()
    scheduler.run_due()
    assert len(source.calls) == 2


def test_missing_revision_evidence_is_bounded_pending_not_http_success(setup):
    scheduler, _, source, clock = setup
    scheduler.import_signal(notice(expected_points=[]))
    for _ in range(5):
        assert scheduler.run_due()["action"] == "signal_pending"
        pending = event(scheduler)
        if pending["next_attempt_at"]:
            clock.at = datetime.fromisoformat(pending["next_attempt_at"])
    assert pending["state"] == "pending_exhausted"
    assert pending["classification"] == "signal_revision_evidence_incomplete"
    clock.advance(86400)
    scheduler.run_due()
    assert len(source.calls) == 5


def test_annual_notice_not_completed_by_monthly_history(setup):
    scheduler, _, source, _ = setup
    sid = next(s for s in SERIES_IDS if s.startswith("CUSR"))
    scheduler.import_signal(
        notice(
            kind="annual_revision",
            series_ids=[sid],
            start_month="2021-01",
            end_month="2025-12",
            expected_points=[],
            effective_at="2027-02-11T13:30:00+00:00",
            evidence_text=f"National annual revision {sid} 2021-01 through 2025-12.",
        )
    )
    assert scheduler.status()["targets"][0]["state"] == "complete"
    assert scheduler.status()["revision_status"] == "pending"
    scheduler.run_due()
    assert source.calls == []
    assert event(scheduler)["state"] == "pending"


def test_withdrawal_is_explicit_append_only_and_duplicates_do_not_refetch(setup):
    scheduler, service, source, _ = setup
    scheduler.import_signal(
        notice(
            kind="withdrawal",
            expected_points=[],
            evidence_text=f"National withdrawal {SERIES_IDS[0]} 2026-09.",
        )
    )
    assert scheduler.run_due()["action"] == "signal_completed"
    result = service.query_range(
        "2026-09", "2026-09", series_ids=(SERIES_IDS[0],), allow_upstream=False
    )
    assert len(result["withdrawn"]) == 1
    assert result["withdrawn"][0]["revision"] == 2
    scheduler.import_signal(
        notice(
            kind="withdrawal",
            expected_points=[],
            evidence_text=f"National withdrawal {SERIES_IDS[0]} 2026-09.",
        )
    )
    scheduler.run_due()
    assert source.calls == []
    assert service.store.status()["revision_count"] == 1


def test_api_omission_is_not_withdrawal_and_cannot_confirm_correction(setup):
    scheduler, service, source, _ = setup
    source.omitted.add((SERIES_IDS[0], "2026-09"))
    scheduler.import_signal(notice())
    assert scheduler.run_due()["action"] == "signal_pending"
    result = service.query_range("2026-09", "2026-09", allow_upstream=False)
    assert result["withdrawn"] == []
    assert result["points"][0]["index_value"] == "300"


def test_403_degraded_recovery_once_and_restart_does_not_repeat(setup):
    scheduler, service, source, clock = setup

    def denied():
        raise CpiSignalError("signal_http_403")

    scheduler.signal_fetcher = denied
    assert scheduler.refresh_signals()["signals"]["state"] == "degraded"
    assert scheduler.refresh_signals()["signals"]["error"] == "signal_http_403"
    assert source.calls == []
    restarted = CpiReleaseScheduler(
        service,
        scheduler.state_path,
        clock=clock,
        calendar_fetcher=lambda: CALENDAR,
        signal_fetcher=lambda: EMPTY,
        history_start="2026-01",
    )
    assert restarted.refresh_signals()["signals"]["recovery"]["state"] == "pending"
    assert restarted.run_due()["action"] == "recovery_history_updated"
    assert len(source.calls) == 1
    restarted.refresh_signals()
    restarted.run_due()
    again = CpiReleaseScheduler(
        service,
        scheduler.state_path,
        clock=clock,
        calendar_fetcher=lambda: CALENDAR,
        signal_fetcher=lambda: EMPTY,
        history_start="2026-01",
    )
    clock.advance(86400)
    again.refresh_signals()
    again.run_due()
    assert len(source.calls) == 1
    assert again.status()["signals"]["recovery"]["state"] == "complete"


def test_recovery_failure_marker_and_budget_survive_restart(setup):
    scheduler, _, source, clock = setup
    scheduler.signal_fetcher = lambda: (_ for _ in ()).throw(
        CpiSignalError("signal_http_403")
    )
    scheduler.refresh_signals()
    scheduler.signal_fetcher = lambda: EMPTY
    scheduler.refresh_signals()
    state = json.loads(scheduler.state_path.read_text())
    state["signals"]["recovery"].update(state="running", attempts=1)
    scheduler.state_path.write_text(json.dumps(state))
    assert scheduler.run_due()["action"] == "recovery_interrupted"
    assert source.calls == []
    clock.advance(300)
    assert scheduler.run_due()["action"] == "recovery_history_updated"
    assert len(source.calls) == 1


def test_verified_import_upgrades_discovery_and_conflict_is_atomic(setup):
    scheduler, _, _, _ = setup
    scheduler.signal_fetcher = lambda: discovery_table()
    scheduler.refresh_signals()
    identity = event(scheduler)["signal_id"]
    scheduler.import_signal(notice(discovered_signal_id=identity))
    assert event(scheduler)["verified"] is True
    scheduler.refresh_signals()
    assert event(scheduler)["state"] == "pending"
    before = scheduler.state_path.read_bytes()
    with pytest.raises(CpiScheduleError, match="signal_verified_notice_conflict"):
        scheduler.import_signals(
            [
                notice(evidence_url="https://www.bls.gov/errata/other.htm"),
                notice(
                    discovered_signal_id=identity,
                    effective_at="2026-12-01T00:00:00+00:00",
                ),
            ]
        )
    assert scheduler.state_path.read_bytes() == before


@pytest.mark.parametrize(
    "url",
    [
        "http://www.bls.gov/errata/a.htm",
        "https://bls.gov.evil.example/a",
        "https://user:password@www.bls.gov/a",
        "https://www.bls.gov:443/a",
        "https://www.bls.gov/a?secret=x",
        "https://www.bls.gov/a#x",
        "https://www.bls.gov\\@evil.example/a",
        "https://www.bls.gov/a%0a",
        "https://www.bls.gov/a\nb",
        "https://www.bls.gov",
    ],
)
def test_evidence_urls_strictly_constrained(url):
    with pytest.raises(CpiSignalError, match="signal_evidence_url_invalid"):
        evidence_url(url)


def test_invalid_evidence_and_annual_scope():
    with pytest.raises(CpiSignalError, match="signal_effective_at_invalid"):
        validate_signal(notice(effective_at="2026-11-10T13:30:00"))
    sid = next(s for s in SERIES_IDS if s.startswith("CUUR"))
    with pytest.raises(CpiSignalError, match="signal_annual_scope_invalid"):
        validate_signal(notice(kind="annual_revision", series_ids=[sid]))


@pytest.mark.parametrize(
    ("error", "classification"),
    [
        (
            HTTPError("https://www.bls.gov/errata/", 403, "private", {}, None),
            "signal_http_403",
        ),
        (TimeoutError("private"), "signal_timeout"),
        (URLError("private"), "signal_transport_failed"),
    ],
)
def test_direct_fetch_errors_are_explicit_redacted_and_offline(
    monkeypatch, error, classification
):
    def fail(*args, **kwargs):
        assert kwargs["timeout"] == 20
        raise error

    monkeypatch.setattr(
        "market_data_foundation.historical.cpi_revision_signals.fetch_bls_page", fail
    )
    with pytest.raises(CpiSignalError, match=classification) as raised:
        fetch_official_errata()
    assert "private" not in str(raised.value)


def test_discovery_rejects_access_denied_200():
    with pytest.raises(CpiSignalError, match="signal_page_unrecognized"):
        parse_official_errata(b"<html>Access denied</html>")


def test_new_evidence_can_unblock_but_duplicate_cannot_reset_budget(setup):
    scheduler, _, source, clock = setup
    scheduler.import_signal(notice(expected_points=[]))
    scheduler.run_due()
    pending = event(scheduler)
    scheduler.import_signal(notice(expected_points=[]))
    assert event(scheduler)["attempts"] == pending["attempts"] == 1
    scheduler.import_signal(notice())
    assert event(scheduler)["attempts"] == 0
    source.values[SERIES_IDS[0], "2026-09"] = Decimal("301")
    clock.advance(1)
    assert scheduler.run_due()["action"] == "signal_completed"


def test_signal_running_marker_does_not_claim_completion_after_restart(setup):
    scheduler, _, source, clock = setup
    scheduler.import_signal(notice())
    state = json.loads(scheduler.state_path.read_text())
    pending = next(iter(state["signals"]["events"].values()))
    pending.update(state="running", attempts=1)
    scheduler.state_path.write_text(json.dumps(state))
    assert scheduler.run_due()["action"] == "interrupted"
    assert source.calls == []
    source.values[SERIES_IDS[0], "2026-09"] = Decimal("301")
    clock.advance(300)
    assert scheduler.run_due()["action"] == "signal_completed"


def test_known_source_missing_target_is_terminal_and_history_handles_missing(setup):
    scheduler, _, source, clock = setup
    # New calendar release with an explicit source '-' on all four rows.
    scheduler.calendar_fetcher = lambda: CALENDAR.replace(
        b"October", b"November"
    ).replace(b"Nov. 10", b"Dec. 10")
    clock.at = datetime(2026, 12, 10, 13, 30, tzinfo=UTC)
    for sid in SERIES_IDS:
        source.values[sid, "2026-11"] = None
    scheduler.refresh_calendar()
    assert scheduler.run_due()["action"] == "source_unavailable"
    target = scheduler.status()["targets"][-1]
    assert target["target"]["state"] == "source_unavailable"
    assert target["target_available"] is False
    assert scheduler.run_due()["action"] == "history_updated"
    for _ in range(7):
        clock.advance(86400)
        scheduler.run_due()
    assert len(source.calls) == 2


def test_missing_source_rows_exhaust_history_independently(setup):
    scheduler, _, source, clock = setup
    source.omitted.add((SERIES_IDS[0], "2026-01"))
    scheduler.signal_fetcher = lambda: (_ for _ in ()).throw(
        CpiSignalError("signal_http_403")
    )
    scheduler.refresh_signals()
    scheduler.signal_fetcher = lambda: EMPTY
    scheduler.refresh_signals()
    for _ in range(5):
        assert scheduler.run_due()["action"] == "recovery_history_awaiting_provider"
        recovery = scheduler.status()["signals"]["recovery"]
        if recovery["next_attempt_at"]:
            clock.at = datetime.fromisoformat(recovery["next_attempt_at"])
    assert recovery["state"] == "pending_exhausted"
    scheduler.refresh_signals()
    scheduler.run_due()
    assert len(source.calls) == 5


def test_monthly_history_satisfies_recovery_only_once(setup):
    scheduler, _, source, clock = setup
    scheduler.signal_fetcher = lambda: (_ for _ in ()).throw(
        CpiSignalError("signal_http_403")
    )
    scheduler.refresh_signals()
    scheduler.signal_fetcher = lambda: EMPTY
    scheduler.refresh_signals()
    scheduler.calendar_fetcher = lambda: CALENDAR.replace(
        b"October", b"November"
    ).replace(b"Nov. 10", b"Dec. 10")
    clock.at = datetime(2026, 12, 10, 13, 30, tzinfo=UTC)
    scheduler.refresh_calendar()
    assert scheduler.run_due()["action"] == "updated"
    assert scheduler.run_due()["action"] == "history_updated"
    scheduler.run_due()
    assert (
        scheduler.status()["signals"]["recovery"]["satisfied_by_monthly_history"]
        == "2026-11"
    )
    assert len(source.calls) == 2


def test_direct_fetch_passes_signal_byte_limit(monkeypatch):
    from market_data_foundation.historical.cpi_revision_signals import MAX_SIGNAL_BYTES

    def fetch(url, *, headers, timeout, max_bytes):
        assert url == "https://www.bls.gov/errata/"
        assert timeout == 20
        assert max_bytes == MAX_SIGNAL_BYTES
        assert headers["User-Agent"] == "Market-Data-Foundation CPI signals/1.0"
        return EMPTY

    monkeypatch.setattr(
        "market_data_foundation.historical.cpi_revision_signals.fetch_bls_page", fetch
    )
    assert fetch_official_errata() == EMPTY


def test_expectation_without_value_evidence_is_rejected():
    with pytest.raises(CpiSignalError, match="signal_expected_evidence_insufficient"):
        validate_signal(
            notice(evidence_text=f"National correction {SERIES_IDS[0]} 2026-09.")
        )


def test_unlinked_relevant_notice_is_explicitly_review_required():
    notices = parse_official_errata(
        errata_table(
            (
                "10/19/2026",
                "Consumer Price Index",
                "Other",
                "CPI correction",
                "Correction details unavailable",
                "Pending",
            )
        )
    )
    assert len(notices) == 1
    assert notices[0]["state"] == "review_required"
    assert notices[0]["classification"] == "signal_scope_unverified"


def test_signal_import_and_refresh_share_process_lock(setup):
    import fcntl

    scheduler, _, source, _ = setup
    with scheduler.state_path.with_suffix(".json.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CpiScheduleError, match="schedule_busy"):
            scheduler.import_signal(notice())
        with pytest.raises(CpiScheduleError, match="schedule_busy"):
            scheduler.refresh_signals()
    assert source.calls == []


def test_target_visible_through_core_before_failed_history(tmp_path):
    clock = Clock()
    source = Source(clock)
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(clock=clock), source)
    scheduler = CpiReleaseScheduler(
        service,
        tmp_path / "schedule.json",
        clock=clock,
        calendar_fetcher=lambda: CALENDAR,
        signal_fetcher=lambda: EMPTY,
    )
    assert scheduler.run_due()["action"] == "updated"
    assert source.calls == [(SERIES_IDS, "2026-10", "2026-10")]
    source.error = RuntimeError("private")
    assert scheduler.run_due()["action"] == "history_awaiting_provider"
    readback = service.query_range("2026-10", "2026-10", allow_upstream=False)
    assert len(readback["points"]) == len(SERIES_IDS)
    assert readback["complete"] is True
    assert scheduler.status()["targets"][0]["target_available"] is True


def errata_table(*rows):
    return (
        '<h1>Errata</h1><table id="errata_datatable">'
        "<thead><tr><td>All years</td><td>Consumer Price Index</td></tr></thead>"
        "<tbody>"
        + "".join(
            "<tr>" + "".join("<td>" + cell + "</td>" for cell in row) + "</tr>"
            for row in rows
        )
        + "</tbody></table>"
    ).encode()


def test_real_table_scope_identity_and_navigation_exclusion():
    rows = [
        (
            "07/22/2026",
            "Consumer Price Index",
            "Database",
            '<a href="https://data.bls.gov/PDQWeb/cu">All Urban Consumers database</a>',
            "Energy in Tampa removed. Affected series CUURS35DSA0E and CWURS35DSA0E.",
            "Corrections made on 08/12/2026.",
        ),
        (
            "10/19/2026",
            "Consumer Price Index",
            "Database",
            '<a href="/cpi/data.htm">Database</a>',
            f"National correction {SERIES_IDS[0]} 2026-09: 300 to 301.",
            "Pending",
        ),
        (
            "10/20/2026",
            "Consumer Price Index",
            "Database",
            '<a href="/cpi/data.htm">Database</a>',
            f"National correction {SERIES_IDS[0]} 2026-08: 300 to 302.",
            "Pending",
        ),
    ]
    raw = (
        b'<a href="/cpi/calculator.htm">CPI calculator</a>'
        b'<a href="/cpi/covid.htm">CPI COVID</a>' + errata_table(*rows, rows[1])
    )
    notices = parse_official_errata(raw)
    assert len(notices) == 2
    assert len({n["signal_id"] for n in notices}) == 2
    assert {n["notice_date"] for n in notices} == {"2026-10-19", "2026-10-20"}
    assert all(n["mentioned_series_ids"] == [SERIES_IDS[0]] for n in notices)
    assert all(
        n["related_urls"] == ["https://www.bls.gov/cpi/data.htm"] for n in notices
    )
    assert notices[0]["correction_status"] == "Pending"
    assert parse_official_errata(errata_table(rows[0])) == []


def test_specific_dated_row_import_upgrades_only_that_notice(setup):
    scheduler, _, source, clock = setup
    scheduler.signal_fetcher = lambda: errata_table(
        (
            "10/19/2026",
            "Consumer Price Index",
            "Database",
            '<a href="/cpi/data.htm">Database</a>',
            f"National correction {SERIES_IDS[0]} 2026-09: 300 to 301.",
            "Pending",
        ),
        (
            "10/20/2026",
            "Consumer Price Index",
            "Database",
            '<a href="/cpi/data.htm">Database</a>',
            f"National correction {SERIES_IDS[0]} 2026-08: 300 to 302.",
            "Pending",
        ),
    )
    scheduler.refresh_signals()
    first = next(iter(scheduler.status()["signals"]["events"].values()))
    scheduler.import_signal(
        notice(
            discovered_signal_id=first["signal_id"],
            evidence_url="https://www.bls.gov/cpi/data.htm",
        )
    )
    source.values[SERIES_IDS[0], "2026-09"] = Decimal("301")
    clock.advance(1)
    assert scheduler.run_due()["action"] == "signal_completed"
    assert len(scheduler.status()["signals"]["events"]) == 2
    scheduler.refresh_signals()
    assert (
        sum(
            e["state"] == "review_required"
            for e in scheduler.status()["signals"]["events"].values()
        )
        == 1
    )
    assert (
        sum(
            e["state"] == "complete"
            for e in scheduler.status()["signals"]["events"].values()
        )
        == 1
    )


def test_recovery_uses_existing_baseline_with_future_only_calendar(tmp_path):
    clock = Clock()
    source = Source(clock)
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(clock=clock), source)
    service.update_range("2026-01", "2026-09", include_buffer=False)
    source.calls.clear()
    scheduler = CpiReleaseScheduler(
        service,
        tmp_path / "state.json",
        clock=clock,
        history_start="2026-01",
        calendar_fetcher=lambda: CALENDAR.replace(b"Nov. 10", b"Nov. 11"),
        signal_fetcher=lambda: (_ for _ in ()).throw(CpiSignalError("signal_http_403")),
    )
    scheduler.refresh_calendar()
    scheduler.refresh_signals()
    scheduler.signal_fetcher = lambda: EMPTY
    scheduler.refresh_signals()
    assert scheduler.run_due()["action"] == "recovery_history_updated"
    assert source.calls == [(SERIES_IDS, "2026-01", "2026-09")]
    assert scheduler.status()["signals"]["recovery"]["end_month"] == "2026-09"
    scheduler.run_due()
    assert len(source.calls) == 1


def test_verified_correction_restores_withdrawal_with_explicit_evidence(setup):
    scheduler, service, source, clock = setup
    service.withdraw_range(
        "2026-09",
        "2026-09",
        series_ids=(SERIES_IDS[0],),
        evidence_url="https://www.bls.gov/errata/withdrawal.htm",
    )
    clock.advance(1)
    source.values[SERIES_IDS[0], "2026-09"] = Decimal("301")
    scheduler.import_signal(notice())
    assert scheduler.run_due()["action"] == "signal_completed"
    result = service.query_range(
        "2026-09", "2026-09", series_ids=(SERIES_IDS[0],), allow_upstream=False
    )
    assert result["withdrawn"] == []
    assert result["points"][0]["revision"] == 3
    assert result["points"][0]["evidence_url"] == NOTICE_URL


def test_unchanged_api_values_never_restore_withdrawal(setup):
    scheduler, service, _, clock = setup
    service.withdraw_range(
        "2026-09",
        "2026-09",
        series_ids=(SERIES_IDS[0],),
        evidence_url="https://www.bls.gov/errata/withdrawal.htm",
    )
    clock.advance(1)
    scheduler.import_signal(notice())
    assert scheduler.run_due()["action"] == "signal_pending"
    result = service.query_range(
        "2026-09", "2026-09", series_ids=(SERIES_IDS[0],), allow_upstream=False
    )
    assert len(result["withdrawn"]) == 1
    assert result["withdrawn"][0]["revision"] == 2


def test_natural_official_prose_and_separate_verified_scope_mapping():
    sid = SERIES_IDS[0]
    parsed = validate_signal(
        notice(
            kind="withdrawal",
            expected_points=[],
            start_month="2026-06",
            end_month="2026-06",
            evidence_text=(
                f"Index data for {sid} for June 2026 were removed from the database."
            ),
        )
    )
    assert parsed["start_month"] == "2026-06"
    excerpt = (
        "The Consumer Price Index for All Urban Consumers (CPI-U), U.S. city average, "
        "was revised for the prior five calendar years."
    )
    mapped = notice(
        evidence_text=excerpt,
        scope_mapping={
            "verified": True,
            "series_ids": [sid],
            "start_month": "2026-09",
            "end_month": "2026-09",
        },
    )
    assert validate_signal(mapped)["evidence_text"] == excerpt
    with pytest.raises(CpiSignalError, match="signal_evidence_scope_mismatch"):
        validate_signal(
            dict(mapped, evidence_text="Consumer Price Index for Boston was revised.")
        )


def test_annual_full_confirmation_allows_unchanged_cells(setup):
    scheduler, _, source, clock = setup
    sid = next(s for s in SERIES_IDS if s.startswith("CUSR"))
    scheduler.import_signal(
        notice(
            kind="annual_revision",
            series_ids=[sid],
            start_month="2026-08",
            end_month="2026-09",
            evidence_text=(
                f"National revision {sid}, August 2026 through September 2026: "
                "300 to 301."
            ),
            expected_points=[
                dict(
                    series_id=sid,
                    month="2026-08",
                    previous_value="300",
                    index_value="300",
                ),
                dict(
                    series_id=sid,
                    month="2026-09",
                    previous_value="300",
                    index_value="301",
                ),
            ],
        )
    )
    source.values[sid, "2026-09"] = Decimal("301")
    clock.advance(1)
    assert scheduler.run_due()["action"] == "signal_completed"


def test_all_unchanged_expectations_remain_pending(setup):
    scheduler, _, _, _ = setup
    scheduler.import_signal(
        notice(
            expected_points=[
                dict(
                    series_id=SERIES_IDS[0],
                    month="2026-09",
                    previous_value="300",
                    index_value="300",
                )
            ]
        )
    )
    assert scheduler.run_due()["action"] == "signal_pending"
    assert event(scheduler)["classification"] == "signal_revision_change_unconfirmed"


def test_recovery_without_local_range_is_bounded(tmp_path):
    clock = Clock()
    service = CpiTimeSeriesService(InMemoryTimeSeriesStore(clock=clock), Source(clock))
    scheduler = CpiReleaseScheduler(
        service,
        tmp_path / "state.json",
        clock=clock,
        calendar_fetcher=lambda: CALENDAR.replace(b"2026", b"2027"),
        signal_fetcher=lambda: (_ for _ in ()).throw(CpiSignalError("signal_http_403")),
    )
    scheduler.refresh_signals()
    scheduler.signal_fetcher = lambda: EMPTY
    scheduler.refresh_signals()
    for _ in range(5):
        assert scheduler.run_due()["action"] == "recovery_awaiting_local_range"
        recovery = scheduler.status()["signals"]["recovery"]
        if recovery["next_attempt_at"]:
            clock.at = datetime.fromisoformat(recovery["next_attempt_at"])
    assert recovery["state"] == "pending_exhausted"


def test_all_unchanged_expectations_cannot_restore_old_withdrawn_value(setup):
    scheduler, service, _, clock = setup
    service.withdraw_range(
        "2026-09",
        "2026-09",
        series_ids=(SERIES_IDS[0],),
        evidence_url="https://www.bls.gov/errata/withdrawal.htm",
    )
    clock.advance(1)
    scheduler.import_signal(
        notice(
            expected_points=[
                dict(
                    series_id=SERIES_IDS[0],
                    month="2026-09",
                    previous_value="300",
                    index_value="300",
                )
            ]
        )
    )
    assert scheduler.run_due()["action"] == "signal_pending"
    result = service.query_range(
        "2026-09", "2026-09", series_ids=(SERIES_IDS[0],), allow_upstream=False
    )
    assert len(result["withdrawn"]) == 1


def test_recovery_does_not_truncate_newer_local_baseline_to_old_target(setup):
    scheduler, service, source, clock = setup
    clock.advance(1)
    service.update_range("2026-11", "2026-11", include_buffer=False)
    source.calls.clear()
    scheduler.signal_fetcher = lambda: (_ for _ in ()).throw(
        CpiSignalError("signal_http_403")
    )
    scheduler.refresh_signals()
    scheduler.signal_fetcher = lambda: EMPTY
    scheduler.refresh_signals()
    assert scheduler.run_due()["action"] == "recovery_history_updated"
    assert source.calls == [(SERIES_IDS, "2026-01", "2026-11")]


def discovery_table(title="CPI correction"):
    return errata_table(
        (
            "10/19/2026",
            "Consumer Price Index",
            "Other",
            f'<a href="{NOTICE_URL}">{title}</a>',
            "Scope unresolved",
            "Pending",
        )
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"<html><title>Errata</title><p>Service unavailable</p></html>",
        b'<h1>Errata</h1><a href="/errata/cpi.htm">CPI correction</a>',
        b'<h1>Errata</h1><table id="other"><tr><td>CPI</td></tr></table>',
        b"<h1>Errata</h1><p>Consumer Price Index correction</p>",
    ],
)
def test_missing_official_table_cannot_make_channel_healthy(setup, raw):
    scheduler, _, source, _ = setup
    with pytest.raises(CpiSignalError, match="signal_page_unrecognized"):
        parse_official_errata(raw)
    scheduler.signal_fetcher = lambda: (_ for _ in ()).throw(
        CpiSignalError("signal_http_403")
    )
    scheduler.refresh_signals()
    before = scheduler.status()["signals"]["fetched_at"]
    scheduler.signal_fetcher = lambda: raw
    state = scheduler.refresh_signals()["signals"]
    assert state["state"] == "degraded"
    assert state["error"] == "signal_page_unrecognized"
    assert state["fetched_at"] == before
    assert "recovery" not in state
    scheduler.run_due()
    assert source.calls == []


def test_dismiss_persists_review_and_dedup_across_restart_without_service(setup):
    scheduler, _, source, clock = setup
    scheduler.signal_fetcher = lambda: discovery_table()
    scheduler.refresh_signals()
    original = event(scheduler).copy()
    identity = original["signal_id"]
    scheduler.service = None
    result = scheduler.dismiss_signal(identity, "  Reviewed: image caption only  ")
    dismissed = result["signals"]["events"][identity]
    assert dismissed["state"] == "dismissed"
    assert dismissed["dismiss_reason"] == "Reviewed: image caption only"
    assert dismissed["dismissed_at"] == clock.at.isoformat()
    assert dismissed["next_attempt_at"] is None
    assert result["revision_status"] == "no_pending_known_notices"
    for key, value in original.items():
        if key != "state":
            assert dismissed[key] == value
    before = scheduler.state_path.read_bytes()
    clock.advance(10)
    scheduler.dismiss_signal(identity, "Reviewed: image caption only")
    assert scheduler.state_path.read_bytes() == before
    restarted = CpiReleaseScheduler(
        None,
        scheduler.state_path,
        clock=clock,
        history_start="2026-01",
        signal_fetcher=lambda: discovery_table(),
    )
    restarted.refresh_signals()
    assert event(restarted) == dismissed
    assert restarted.status()["revision_status"] == "no_pending_known_notices"
    assert source.calls == []
    with pytest.raises(CpiScheduleError, match="signal_dismissal_conflict"):
        restarted.dismiss_signal(identity, "Another reason")
    with pytest.raises(CpiScheduleError, match="signal_dismissal_not_allowed"):
        restarted.import_signal(notice(discovered_signal_id=identity))
    assert event(restarted) == dismissed


@pytest.mark.parametrize(
    "change",
    [
        {"state": "pending"},
        {"state": "complete"},
        {"state": "running"},
        {"state": "pending_exhausted"},
        {"verified": True},
        {"attempts": 1},
        {"last_attempt_at": "2026-11-10T13:30:00+00:00"},
    ],
)
def test_dismiss_only_unexecuted_unverified_review_items(setup, change):
    scheduler, _, source, _ = setup
    scheduler.signal_fetcher = lambda: discovery_table()
    scheduler.refresh_signals()
    identity = event(scheduler)["signal_id"]
    document = json.loads(scheduler.state_path.read_text())
    document["signals"]["events"][identity].update(change)
    scheduler.state_path.write_text(json.dumps(document))
    before = scheduler.state_path.read_bytes()
    with pytest.raises(CpiScheduleError, match="signal_dismissal_not_allowed"):
        scheduler.dismiss_signal(identity, "Reviewed unrelated")
    assert scheduler.state_path.read_bytes() == before
    assert source.calls == []


@pytest.mark.parametrize(
    ("identity", "reason", "classification"),
    [
        ("bad", "Unrelated", "signal_id_invalid"),
        ("A" * 64, "Unrelated", "signal_id_invalid"),
        ("0" * 64, "", "signal_dismiss_reason_invalid"),
        ("0" * 64, "   ", "signal_dismiss_reason_invalid"),
        ("0" * 64, None, "signal_dismiss_reason_invalid"),
        ("0" * 64, "x" * 2001, "signal_dismiss_reason_invalid"),
        ("0" * 64, "x\ny", "signal_dismiss_reason_invalid"),
        ("0" * 64, "Unrelated", "signal_not_found"),
    ],
)
def test_dismiss_input_validation_preserves_state(
    setup, identity, reason, classification
):
    scheduler, _, source, _ = setup
    before = scheduler.state_path.read_bytes()
    with pytest.raises(CpiScheduleError, match=classification):
        scheduler.dismiss_signal(identity, reason)
    assert scheduler.state_path.read_bytes() == before
    assert source.calls == []


def test_dismiss_uses_state_lock_and_failed_save_keeps_review_pending(setup):
    import fcntl

    scheduler, _, source, _ = setup
    scheduler.signal_fetcher = lambda: discovery_table()
    scheduler.refresh_signals()
    identity = event(scheduler)["signal_id"]
    before = scheduler.state_path.read_bytes()
    with scheduler.state_path.with_suffix(".json.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(CpiScheduleError, match="schedule_busy"):
            scheduler.dismiss_signal(identity, "Unrelated")
    scheduler._save = lambda _: (_ for _ in ()).throw(OSError("write failed"))
    with pytest.raises(OSError):
        scheduler.dismiss_signal(identity, "Unrelated")
    assert scheduler.state_path.read_bytes() == before
    assert event(scheduler)["state"] == "review_required"
    assert source.calls == []


def test_errata_requests_html_media_type(monkeypatch):
    from market_data_foundation.historical import cpi_revision_signals as signals

    captured = {}

    def fetch(url, **kwargs):
        captured.update(url=url, **kwargs)
        return b"official errata response"

    monkeypatch.setattr(signals, "fetch_bls_page", fetch)
    assert signals.fetch_official_errata() == b"official errata response"
    assert captured["url"] == signals.ERRATA_URL
    assert captured["headers"]["Accept"] == "text/html"
    assert captured["headers"]["User-Agent"].startswith("Market-Data-Foundation")
