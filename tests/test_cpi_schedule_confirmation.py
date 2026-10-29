"""Post-commit failure regressions through the actual offline CPI scheduler."""

import socket
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.bls_cpi_direct import SERIES_IDS
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from market_data_foundation.historical.cpi_revision_signals import CpiSignalError
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesService,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_cli import WorkerService
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)

EMPTY = b'<table id="errata_datatable"></table>'
CALENDAR = b"""<table><tr><th>Reference Month</th><th>Release Date</th>
<th>Release Time</th></tr><tr><td>October 2026</td><td>Nov. 10, 2026</td>
<td>08:30 AM</td></tr></table>"""
SID = SERIES_IDS[0]


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)
        self.confirmations = 0
        self.fail_at = None

    def __call__(self):
        return self.at

    def confirmation(self):
        self.confirmations += 1
        if self.confirmations == self.fail_at:
            raise RuntimeError("synthetic_confirmation_failure")
        return self.at


class Source:
    registered = True

    def __init__(self, clock):
        self.clock = clock
        self.values = {}
        self.calls = []

    def fetch(self, series, start, end):
        self.calls.append((tuple(series), start, end))
        points = tuple(
            SourcePoint(s, m, self.values.get((s, m), Decimal("100")), {}, ())
            for s in series
            for m in month_range(start, end)
        )
        return (
            SourceBatch(
                points,
                repr(points).encode(),
                {"fixture": True},
                self.clock.at - timedelta(seconds=1),
                1,
            ),
        )


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("offline lifecycle tests must never contact an external service")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)


@pytest.fixture
def system(tmp_path):
    clock = Clock()
    source = Source(clock)
    store = InMemoryTimeSeriesStore(clock=clock.confirmation)
    service = CpiTimeSeriesService(store, source)

    def build():
        return CpiReleaseScheduler(
            service,
            tmp_path / "schedule.json",
            clock=clock,
            calendar_fetcher=lambda: CALENDAR,
            reference_fetcher=None,
            signal_fetcher=lambda: EMPTY,
            history_start="2026-09",
            max_attempts=4,
        )

    return build, service, store, source, clock


def strict(service, month, **options):
    return service.query_range(
        month, month, allow_upstream=False, vintage_mode="available", **options
    )


def version_counts(value):
    status = value if isinstance(value, dict) else value.status()
    return status["observation_count"], status["revision_count"]


def retry_at(stage, clock):
    clock.at = datetime.fromisoformat(stage["next_attempt_at"])


@pytest.mark.parametrize("missing", [False, True])
def test_committed_target_retry_requires_confirmation_and_strict_readback(
    system, missing
):
    build, service, store, source, clock = system
    if missing:
        source.values.update({(s, "2026-10"): None for s in SERIES_IDS})
    scheduler = build()
    clock.fail_at = clock.confirmations + 1
    failed = scheduler.run_due()
    assert failed["action"] == "awaiting_provider"
    assert not failed["targets"][0]["target_available"]
    assert len(store._history) == 4  # values really persisted before the failure
    assert strict(service, "2026-10")["points"] == []
    assert strict(service, "2026-10")["source_unavailable"] == []
    committed = service.status()
    failed_at = clock.at

    # Ordinary reads and unchanged updates must leave unknown availability alone.
    service.query_range("2026-10", "2026-10", allow_upstream=False)
    unchanged = service.update_range("2026-10", "2026-10", include_buffer=False)
    assert unchanged["saved_points"] == unchanged["saved_revisions"] == 0
    assert all(h[-1]["available_at"] is None for h in store._history.values())

    retry_at(failed["targets"][0]["target"], clock)
    clock.fail_at = clock.confirmations + 1
    retried = build().run_due()
    assert retried["action"] == "awaiting_provider"
    assert not retried["targets"][0]["target_available"]
    assert retried["targets"][0]["history"]["state"] == "blocked"
    assert strict(service, "2026-10")["coverage_complete"] is False

    retry_at(retried["targets"][0]["target"], clock)
    scheduler = build()
    scheduler.service = WorkerService(lambda: CpiTimeSeriesService(store, source))
    recovered = scheduler.run_due()
    assert recovered["action"] == ("source_unavailable" if missing else "updated")
    assert recovered["targets"][0]["target_available"] is not missing
    assert recovered["targets"][0]["history"]["state"] == "pending"
    assert strict(service, "2026-10")["coverage_complete"]
    assert all(
        h[-1]["available_at"] == clock.at.isoformat() and len(h) == 1
        for h in store._history.values()
    )
    assert strict(service, "2026-10", as_of=failed_at)["coverage_complete"] is False
    assert version_counts(service) == version_counts(committed)
    assert scheduler.run_due()["action"] == "history_updated"


@pytest.mark.parametrize("recovery", [False, True])
def test_history_and_recovery_cannot_complete_unconfirmed_revisions(system, recovery):
    build, service, store, source, clock = system
    scheduler = build()
    assert scheduler.run_due()["action"] == "updated"
    if recovery:
        assert scheduler.run_due()["action"] == "history_updated"

        def denied():
            raise CpiSignalError("signal_http_403")

        scheduler.signal_fetcher = denied
        assert scheduler.refresh_signals()["signals"]["state"] == "degraded"
        scheduler.signal_fetcher = lambda: EMPTY
        assert scheduler.refresh_signals()["signals"]["recovery"]["state"] == "pending"
    source.values[SID, "2026-09"] = Decimal("101")
    clock.at += timedelta(seconds=10)
    clock.fail_at = clock.confirmations + 1
    failed = scheduler.run_due()
    prefix = "recovery_" if recovery else ""
    assert failed["action"] == prefix + "history_awaiting_provider"
    stage = (
        failed["signals"]["recovery"] if recovery else failed["targets"][0]["history"]
    )
    assert stage["state"] == "awaiting_provider"
    assert not strict(service, "2026-09")["coverage_complete"]
    counts = service.status()
    retry_at(stage, clock)
    recovered = build().run_due()
    assert recovered["action"] == prefix + "history_updated"
    assert strict(service, "2026-09")["complete"]
    assert version_counts(service) == version_counts(counts)
    assert store._history[SID, "2026-09"][-1]["available_at"] == clock.at.isoformat()
    if recovery:
        assert recovered["signals"]["recovery"]["state"] == "complete"
    else:
        assert recovered["targets"][0]["state"] == "complete"


@pytest.mark.parametrize("month", ["2026-09", "2026-10"])
@pytest.mark.parametrize("kind", ["correction", "withdrawal"])
def test_signal_retry_requires_current_confirmation(system, kind, month):
    build, service, store, source, clock = system
    scheduler = build()
    assert scheduler.run_due()["action"] == "updated"
    assert scheduler.run_due()["action"] == "history_updated"
    clock.at += timedelta(seconds=10)
    source.values[SID, month] = Decimal("101")
    notice = dict(
        evidence_url="https://www.bls.gov/errata/cpi-correction.htm",
        kind=kind,
        verified=True,
        scope="national",
        series_ids=[SID],
        start_month=month,
        end_month=month,
        effective_at=clock.at.isoformat(),
        evidence_text=f"Synthetic CPI {kind} {SID} {month}: 100 to 101.",
        expected_points=[
            dict(series_id=SID, month=month, previous_value="100", index_value="101")
        ],
    )
    scheduler.import_signal(notice)
    # Withdrawal samples its observation clock before the post-commit clock.
    clock.fail_at = clock.confirmations + (2 if kind == "withdrawal" else 1)
    failed = scheduler.run_due()
    assert failed["action"] == "signal_pending"
    event = next(iter(failed["signals"]["events"].values()))
    assert event["state"] == "awaiting_provider"
    assert not strict(service, month)["complete"]
    if month == "2026-10":
        assert failed["targets"][0]["target_available"] is False
    counts = service.status()
    calls = len(source.calls)
    retry_at(event, clock)
    recovered = build().run_due()
    assert recovered["action"] == "signal_completed"
    assert recovered["targets"][0]["target_available"] is (
        month != "2026-10" or kind != "withdrawal"
    )
    assert version_counts(service) == version_counts(counts)
    assert len(store._history[SID, month]) == 2
    assert store._history[SID, month][-1]["available_at"] == clock.at.isoformat()
    result = strict(service, month, series_ids=(SID,))
    if kind == "withdrawal":
        assert len(result["withdrawn"]) == 1
        assert len(source.calls) == calls
    else:
        assert result["points"][0]["index_value"] == "101"


def test_successful_confirmation_still_requires_strict_readback(system, monkeypatch):
    build, service, _, _, _ = system
    original = service.query_range

    def unavailable(*args, **kwargs):
        if kwargs.get("vintage_mode") == "available":
            return {"points": [], "source_unavailable": [], "withdrawn": []}
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "query_range", unavailable)
    result = build().run_due()
    assert result["action"] == "awaiting_provider"
    target = result["targets"][0]
    assert not target["target_available"]
    assert target["history"]["state"] == "blocked"
    assert target["classification"] == "schedule_local_readback_incomplete"


def test_noop_confirmation_does_not_allow_completion(system, monkeypatch):
    build, service, store, _, clock = system
    clock.fail_at = clock.confirmations + 1
    failed = build().run_due()
    retry_at(failed["targets"][0]["target"], clock)
    calls = []

    def noop(*args, **kwargs):
        calls.append((args, kwargs))
        return {"confirmed_points": 0, "confirmed_at": None}

    monkeypatch.setattr(service, "confirm_current_availability", noop)
    result = build().run_due()
    assert len(calls) == 1
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["target_available"] is False
    assert all(h[-1]["available_at"] is None for h in store._history.values())


def test_strict_readback_of_another_revision_does_not_complete(system, monkeypatch):
    build, service, _, _, _ = system
    original = service.query_range

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if kwargs.get("vintage_mode") == "available":
            result["points"][0]["revision"] += 1
        return result

    monkeypatch.setattr(service, "query_range", changed)
    result = build().run_due()
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["target_available"] is False


@pytest.mark.parametrize("recovery", [False, True])
def test_later_history_failure_clears_current_target_availability(system, recovery):
    build, service, _, source, clock = system
    scheduler = build()
    assert scheduler.run_due()["action"] == "updated"
    if recovery:
        assert scheduler.run_due()["action"] == "history_updated"

        def denied():
            raise CpiSignalError("signal_http_403")

        scheduler.signal_fetcher = denied
        scheduler.refresh_signals()
        scheduler.signal_fetcher = lambda: EMPTY
        scheduler.refresh_signals()
    clock.at += timedelta(seconds=10)
    source.values[SID, "2026-10"] = Decimal("101")
    clock.fail_at = clock.confirmations + 1
    failed = scheduler.run_due()
    target = failed["targets"][0]
    assert not strict(service, "2026-10")["complete"]
    assert target["target"]["state"] == "complete"  # previous work stays complete
    assert target["target_available"] is False
    stage = failed["signals"]["recovery"] if recovery else target["history"]
    retry_at(stage, clock)
    clock.fail_at = clock.confirmations + 1
    repeated = build().run_due()
    assert repeated["targets"][0]["target_available"] is False
    stage = (
        repeated["signals"]["recovery"]
        if recovery
        else repeated["targets"][0]["history"]
    )
    retry_at(stage, clock)
    recovered = build().run_due()
    assert recovered["targets"][0]["target_available"] is True
    assert strict(service, "2026-10")["complete"]


@pytest.mark.parametrize("recovery", [False, True])
def test_interrupted_history_rechecks_current_target_before_retry(system, recovery):
    build, service, store, source, clock = system
    scheduler = build()
    assert scheduler.run_due()["action"] == "updated"
    if recovery:
        assert scheduler.run_due()["action"] == "history_updated"

        def denied():
            raise CpiSignalError("signal_http_403")

        scheduler.signal_fetcher = denied
        scheduler.refresh_signals()
        scheduler.signal_fetcher = lambda: EMPTY
        scheduler.refresh_signals()
    clock.at += timedelta(seconds=10)
    source.values[SID, "2026-10"] = Decimal("101")

    class CrashAfterCommit(BaseException):
        pass

    def crash():
        raise CrashAfterCommit

    store._clock = crash
    with pytest.raises(CrashAfterCommit):
        scheduler.run_due()
    store._clock = clock.confirmation
    calls = len(source.calls)
    restarted = build().run_due()
    assert restarted["action"] == (
        "recovery_interrupted" if recovery else "interrupted"
    )
    assert restarted["targets"][0]["target_available"] is False
    assert not strict(service, "2026-10")["complete"]
    assert len(source.calls) == calls
    stage = (
        restarted["signals"]["recovery"]
        if recovery
        else restarted["targets"][0]["history"]
    )
    retry_at(stage, clock)
    recovered = build().run_due()
    assert recovered["targets"][0]["target_available"] is True
    assert strict(service, "2026-10")["complete"]
