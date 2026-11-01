"""Real CPI service/store/scheduler through adapters; only provider I/O is synthetic."""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesService,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_cli import execute, main
from market_data_foundation.historical.cpi_timeseries_http import handle_request
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    SERIES_IDS,
    InMemoryTimeSeriesStore,
)

HEADERS = {"Host": "127.0.0.1:18766"}
SERIES = "CUSR0000SA0"


class Clock:
    def __init__(self):
        self.at = datetime(2026, 11, 10, 13, 30, tzinfo=UTC)

    def __call__(self):
        return self.at


class OfflineSource:
    def __init__(self, clock):
        self.clock = clock
        self.calls = []
        self.values = {}
        self.fail = False

    def fetch(self, series, start, end):
        assert not self.fail, "unexpected provider request"
        self.calls.append((tuple(series), start, end))
        points = tuple(
            SourcePoint(
                sid,
                month,
                self.values.get((sid, month), Decimal("100")),
                {"pct_changes": {"1": "0.1", "12": "3.1"}},
                (),
            )
            for sid in series
            for month in month_range(start, end)
        )
        return (
            SourceBatch(
                points,
                json.dumps(
                    [(p.series_id, p.month, str(p.index_value)) for p in points]
                ).encode(),
                {"seriesid": list(series)},
                self.clock.at - timedelta(seconds=5),
                1,
            ),
        )


@pytest.fixture
def actual():
    clock = Clock()
    source = OfflineSource(clock)
    store = InMemoryTimeSeriesStore(clock=clock)
    return CpiTimeSeriesService(store, source), store, source, clock


def request(service, params):
    from urllib.parse import urlencode

    return handle_request(
        service, "GET", "/api/cpi/query?" + urlencode(params), HEADERS, port=18766
    )


@pytest.mark.parametrize("months", [1, 3, 6, 12])
def test_http_real_core_ratio_contract_retains_source_and_legacy_units(actual, months):
    service, _, source, _ = actual
    source.values[SERIES, "2026-09"] = Decimal("103.125")
    execute(service, "init", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    source.fail = True
    code, result = request(
        service,
        {"start": "2026-09", "end": "2026-09", "measure": f"change_{months}m_ratio"},
    )
    assert code == 200
    point = result["points"][0]
    assert point["month"] == "2026-09"
    assert point["value"] == point[f"change_{months}m_ratio"] == "0.0313"
    assert result["unit"] == result["units"][f"change_{months}m_ratio"] == "ratio"
    assert point["mom_pct"] == point["yoy_pct"] == "3.13"
    assert point["provider_calculations"]["pct_changes"]["12"] == "3.1"
    assert result["measure_complete"] and result["provider_requests"] == 0
    legacy_code, legacy = request(
        service, {"start": "2026-09", "end": "2026-09", "measure": "yoy"}
    )
    assert legacy_code == 200
    assert legacy["unit"] == "percent" and legacy["points"][0]["value"] == "3.13"


def test_real_core_missing_denominator_does_not_trigger_refetch(actual):
    service, _, source, _ = actual
    source.values[SERIES, "2025-10"] = None
    execute(service, "init", {"start": "2025-10", "end": "2026-10", "series": SERIES})
    source.fail = True
    for month, interval in [
        ("2025-11", 1),
        ("2026-01", 3),
        ("2026-04", 6),
        ("2026-10", 12),
    ]:
        code, result = request(
            service,
            {"start": month, "end": month, "measure": f"change_{interval}m_ratio"},
        )
        assert code == 200
        assert result["points"][0]["value"] is None
        assert result["complete"] and not result["measure_complete"]
        assert result["provider_requests"] == 0
    code, result = request(
        service, {"start": "2026-01", "end": "2026-01", "measure": "change_6m_ratio"}
    )
    assert code == 200 and result["points"][0]["value"] == "0.0000"


def test_real_core_observed_available_cutoffs_and_official_rejection(actual):
    service, _, source, clock = actual
    execute(service, "init", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    source.fail = True
    cutoff = (clock.at - timedelta(seconds=2)).isoformat()
    base = {"start": "2026-09", "end": "2026-09", "as_of": cutoff}
    code, default = request(service, base)
    assert (
        code == 200
        and default["as_of_basis"] == "available"
        and not default["complete"]
    )
    code, observed = request(service, {**base, "vintage_mode": "observed"})
    assert code == 200 and observed["points"][0]["index_value"] == "100"
    code, available = request(service, {**base, "vintage_mode": "available"})
    assert code == 200 and not available["complete"]
    assert all(p["index_value"] is None for p in available["points"])
    code, available = request(
        service, {**base, "as_of": clock.at.isoformat(), "vintage_mode": "available"}
    )
    assert code == 200 and available["complete"]
    code, result = request(service, {**base, "vintage_mode": "official"})
    assert (
        code == 400
        and result["classification"] == "cpi_timeseries_official_vintage_unsupported"
    )
    assert len(source.calls) == 1


def test_cli_real_core_ratio_and_revision_replay(actual, capsys):
    service, _, source, clock = actual
    execute(service, "init", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    before = clock.at.isoformat()
    clock.at += timedelta(minutes=1)
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    execute(service, "update", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    source.fail = True
    argv = [
        "query",
        "--start",
        "2026-09",
        "--end",
        "2026-09",
        "--measure",
        "change_12m_ratio",
        "--local-only",
    ]
    assert main(argv, service_factory=lambda **kwargs: service) == 0
    latest = json.loads(capsys.readouterr().out)
    assert latest["points"][0]["value"] == "0.0312"
    assert latest["points"][0]["revision"] == 2
    assert (
        main(
            argv + ["--vintage-mode", "available", "--as-of", before],
            service_factory=lambda **kwargs: service,
        )
        == 0
    )
    old = json.loads(capsys.readouterr().out)
    assert old["points"][0]["value"] == "0.0000"
    assert old["points"][0]["revision"] == 1


CALENDAR = b"""<table><tr><th>Reference Month</th><th>Release Date</th>
<th>Release Time</th></tr><tr><td>October 2026</td><td>Nov. 10, 2026</td>
<td>08:30 AM</td></tr><tr><td>November 2026</td><td>Dec. 10, 2026</td>
<td>08:30 AM</td></tr></table>"""
ERRATA = b'<h1>Errata</h1><table id="errata_datatable"><tbody></tbody></table>'
NOTICE_URL = "https://www.bls.gov/errata/test-cpi-interface.htm"


def review_notice_html():
    return f'''<h1>Errata</h1><table id="errata_datatable"><tbody><tr>
    <td>11/08/2026</td><td>CPI</td><td>Database</td>
    <td><a href="{NOTICE_URL}">CPI correction</a></td>
    <td>CUSR0000SA0 September 2026 correction.</td><td>Corrected</td>
    </tr></tbody></table>'''.encode()


def actual_scheduler(actual, tmp_path, *, signal_fetcher=lambda: ERRATA, **options):
    from market_data_foundation.historical.cpi_release_schedule import (
        CpiReleaseScheduler,
    )

    service, _, _, clock = actual
    return CpiReleaseScheduler(
        service,
        tmp_path / "schedule.json",
        clock=clock,
        calendar_fetcher=lambda: CALENDAR,
        reference_fetcher=None,
        signal_fetcher=signal_fetcher,
        history_start="2025-01",
        **options,
    )


def notice(clock, *, kind="correction", expected=True):
    result = {
        "evidence_url": NOTICE_URL,
        "kind": kind,
        "verified": True,
        "scope": "national",
        "series_ids": [SERIES],
        "start_month": "2026-09",
        "end_month": "2026-09",
        "effective_at": clock.at.isoformat(),
        "evidence_text": f"Offline test fixture: {kind} CUSR0000SA0 2026-09.",
    }
    if expected and kind == "correction":
        result["evidence_text"] += " Corrected from 100 to 103.12."
        result["expected_points"] = [
            {
                "series_id": SERIES,
                "month": "2026-09",
                "previous_value": "100",
                "index_value": "103.12",
            }
        ]
    return result


def test_real_scheduler_two_stages_fixed_history_and_idle_signal_poll(actual, tmp_path):
    from market_data_foundation.historical.cpi_timeseries_cli import run_worker

    service, _, source, clock = actual
    polls = []

    def signals():
        polls.append(clock.at)
        return ERRATA

    scheduler = actual_scheduler(
        actual, tmp_path, signal_fetcher=signals, signal_poll_seconds=60
    )
    target = execute(service, "update", scheduler=scheduler)
    assert target["targets"][0]["target_available"]
    assert target["targets"][0]["history"]["state"] == "pending"
    assert source.calls == [(SERIES_IDS, "2026-10", "2026-10")]
    history = execute(service, "schedule/due", scheduler=scheduler)
    assert history["targets"][0]["state"] == "complete"
    assert source.calls[-1] == (SERIES_IDS, "2025-01", "2026-10")
    assert history["history_start"] == "2025-01"
    source.fail = True
    ticks, emissions = [], []

    def stop_after_14(seconds):
        ticks.append(seconds)
        clock.at += timedelta(seconds=seconds)
        if len(ticks) == 14:
            raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        run_worker(
            scheduler, poll_seconds=5, sleep=stop_after_14, emit_result=emissions.append
        )
    assert len(emissions) == 14
    assert all(item["action"] == "not_due" for item in emissions)
    assert len(polls) == 2  # signal cadence, independent of worker tick cadence
    assert len(source.calls) == 2


def test_cli_review_import_real_scheduler_confirms_only_actual_new_values(
    actual, tmp_path, capsys
):
    service, _, source, clock = actual
    # The next calendar release is in the future; signal work stands alone.
    clock.at -= timedelta(days=1)
    discovered = review_notice_html()
    scheduler = actual_scheduler(actual, tmp_path, signal_fetcher=lambda: discovered)
    execute(service, "init", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    refreshed = execute(None, "signals/refresh", scheduler=scheduler)
    event = next(iter(refreshed["signals"]["events"].values()))
    assert event["state"] == "review_required"
    before = len(source.calls)
    execute(service, "schedule/due", scheduler=scheduler)
    assert len(source.calls) == before  # unreviewed discovery never triggers data
    path = tmp_path / "verified.json"
    path.write_text(
        json.dumps(
            {"notices": [{**notice(clock), "discovered_signal_id": event["signal_id"]}]}
        )
    )

    def forbidden(**kwargs):
        pytest.fail("signal CLI opened database")

    assert (
        main(
            ["signals", "import", str(path)],
            service_factory=forbidden,
            scheduler_factory=lambda *args, **kwargs: scheduler,
        )
        == 0
    )
    imported = json.loads(capsys.readouterr().out)
    assert next(iter(imported["signals"]["events"].values()))["state"] == "pending"
    pending = execute(service, "schedule/due", scheduler=scheduler)
    event = next(iter(pending["signals"]["events"].values()))
    assert event["state"] == "awaiting_provider"
    assert event["classification"] == "signal_revision_unconfirmed"
    clock.at += timedelta(seconds=300)
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    confirmed = execute(service, "schedule/due", scheduler=scheduler)
    assert confirmed["action"] == "signal_completed"
    event = next(iter(confirmed["signals"]["events"].values()))
    assert event["state"] == "complete" and event["attempts"] == 2
    count = len(source.calls)
    # Reimport is idempotent, and does not recreate completed work.
    execute(
        None,
        "signals/import",
        {"notices": [json.loads(path.read_text())["notices"][0]]},
        scheduler=scheduler,
    )
    execute(service, "schedule/due", scheduler=scheduler)
    assert len(source.calls) == count
    old = execute(
        service,
        "query",
        {
            "start": "2026-09",
            "end": "2026-09",
            "as_of": (clock.at - timedelta(seconds=300)).isoformat(),
        },
    )
    assert old["points"][0]["index_value"] == "100"


def test_http_real_signal_import_and_worker_withdrawal_proxy(actual, tmp_path):
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService

    service, store, source, clock = actual
    clock.at -= timedelta(days=1)
    execute(service, "init", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    before = clock.at.isoformat()
    clock.at += timedelta(seconds=30)
    scheduler = actual_scheduler(actual, tmp_path)
    opened = []

    def factory():
        opened.append(True)
        return CpiTimeSeriesService(store, source)

    scheduler.service = WorkerService(factory)
    status, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [notice(clock, kind="withdrawal")]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert status == 200 and not opened
    source.fail = True
    result = execute(service, "schedule/due", scheduler=scheduler)
    assert result["action"] == "signal_completed"
    assert len(opened) == 3  # withdrawal, observed tombstone, strict available readback
    status, result = request(service, {"start": "2026-09", "end": "2026-09"})
    assert status == 200 and len(result["withdrawn"]) == 1
    assert result["points"] == []
    assert not result["complete"] and not result["measure_complete"]
    status, result = request(
        service, {"start": "2026-09", "end": "2026-09", "as_of": before}
    )
    assert status == 200 and result["points"][0]["index_value"] == "100"


def test_real_scheduler_unconfirmed_notice_exhausts_without_claiming_completion(
    actual, tmp_path
):
    service, _, source, clock = actual
    clock.at -= timedelta(days=1)
    scheduler = actual_scheduler(actual, tmp_path, max_attempts=1)
    execute(
        None,
        "signals/import",
        {"notices": [notice(clock, expected=False)]},
        scheduler=scheduler,
    )
    result = execute(service, "schedule/due", scheduler=scheduler)
    event = next(iter(result["signals"]["events"].values()))
    assert event["state"] == "pending_exhausted"
    assert event["classification"] == "signal_revision_evidence_incomplete"
    assert result["revision_status"] == "pending"
    source.fail = True
    clock.at += timedelta(hours=8)
    execute(service, "schedule/due", scheduler=scheduler)
    assert len(source.calls) == 1


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("evidence_url", "https://example.com/cpi", "signal_evidence_url_invalid"),
        ("verified", False, "signal_scope_unverified"),
        ("evidence_text", "CPI correction", "signal_evidence_insufficient"),
        ("effective_at", "2026-11-09", "signal_effective_at_invalid"),
        ("series_ids", ["regional"], "signal_series_invalid"),
    ],
)
def test_http_real_signal_validation_never_calls_provider(
    actual, tmp_path, field, value, classification
):
    service, _, source, clock = actual
    scheduler = actual_scheduler(actual, tmp_path)
    bad = {**notice(clock), field: value}
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [bad]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert code == 400
    assert result["classification"] == classification
    assert source.calls == []
    assert scheduler.status()["signals"]["events"] == {}


@pytest.mark.parametrize(
    "field,value",
    [
        ("kind", []),
        ("scope", {}),
        ("verified", "true"),
        ("series_ids", [[SERIES]]),
        (
            "expected_points",
            [
                {
                    "series_id": [],
                    "month": "2026-09",
                    "previous_value": "100",
                    "index_value": "103.12",
                }
            ],
        ),
        ("expected_points", "wrong"),
        ("unsupported", "field"),
    ],
)
def test_http_signal_shape_errors_are_client_errors(actual, tmp_path, field, value):
    service, _, source, clock = actual
    scheduler = actual_scheduler(actual, tmp_path)
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [{**notice(clock), field: value}]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert code == 400 and result["status"] == "ERROR"
    assert source.calls == []
    assert scheduler.status()["signals"]["events"] == {}


def test_cli_actual_confirmation_only_selected_latest_unknown_at_now(actual, capsys):
    service, store, source, clock = actual
    execute(service, "init", {"start": "2026-09", "end": "2026-09"})
    clock.at += timedelta(minutes=1)
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    execute(service, "update", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    # Model migrated rows with genuinely unknown availability, including a
    # superseded version. The real confirmation method must leave history alone.
    for history in store._history.values():
        for row in history:
            row["available_at"] = None
    known = store._history["CUUR0000SA0", "2026-09"][-1]
    known["available_at"] = clock.at.isoformat()
    known_at = known["available_at"]
    before = service.status()
    past = clock.at.isoformat()
    clock.at += timedelta(minutes=2)
    source.fail = True
    args = [
        "confirm-availability",
        "--start",
        "2026-09",
        "--end",
        "2026-09",
        "--basket",
        "headline",
        "--adjustment",
        "all",
    ]
    assert main(args, service_factory=lambda **kwargs: service) == 0
    result = json.loads(capsys.readouterr().out)
    assert result == {"confirmed_points": 1, "confirmed_at": clock.at.isoformat()}
    assert service.status() == before
    assert store._history[SERIES, "2026-09"][0]["available_at"] is None
    assert store._history[SERIES, "2026-08"][-1]["available_at"] is None
    assert store._history["CUSR0000SA0L1E", "2026-09"][-1]["available_at"] is None
    assert known["available_at"] == known_at
    code, available = request(
        service, {"start": "2026-09", "end": "2026-09", "vintage_mode": "available"}
    )
    assert code == 200 and available["points"][0]["index_value"] == "103.12"
    assert (
        available["points"][0]["change_1m_ratio"] is None
    )  # no implicit buffer confirmation
    code, older = request(
        service,
        {
            "start": "2026-09",
            "end": "2026-09",
            "vintage_mode": "available",
            "as_of": past,
        },
    )
    assert code == 200 and not older["complete"]
    assert main(args, service_factory=lambda **kwargs: service) == 0
    assert json.loads(capsys.readouterr().out) == {
        "confirmed_points": 0,
        "confirmed_at": None,
    }
    assert service.status() == before


def test_http_actual_confirmation_makes_baseline_ratios_available_only_now(actual):
    from copy import deepcopy

    service, store, source, clock = actual
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    execute(service, "init", {"start": "2026-09", "end": "2026-09"})
    for history in store._history.values():
        history[-1]["available_at"] = None
    before = deepcopy(store._history)
    cutoff = clock.at.isoformat()
    clock.at += timedelta(minutes=1)
    source.fail = True
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/confirm-availability",
        {**HEADERS, "Content-Type": "application/json"},
        b'{"start":"2025-09","end":"2026-09"}',
        port=18766,
    )
    assert code == 200
    assert result == {"confirmed_points": 52, "confirmed_at": clock.at.isoformat()}
    for key, history in store._history.items():
        assert len(history) == len(before[key]) == 1
        assert {k: v for k, v in history[0].items() if k != "available_at"} == {
            k: v for k, v in before[key][0].items() if k != "available_at"
        }
    code, available = request(
        service,
        {
            "start": "2026-09",
            "end": "2026-09",
            "measure": "change_12m_ratio",
            "vintage_mode": "available",
        },
    )
    assert code == 200 and available["points"][0]["value"] == "0.0312"
    assert available["measure_complete"]
    code, past = request(
        service,
        {
            "start": "2026-09",
            "end": "2026-09",
            "vintage_mode": "available",
            "as_of": cutoff,
        },
    )
    assert code == 200 and not past["complete"]
    assert len(source.calls) == 1


def test_actual_worker_recovery_resolves_existing_baseline_via_status(actual, tmp_path):
    from market_data_foundation.historical.cpi_revision_signals import CpiSignalError
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService

    service, store, source, clock = actual
    clock.at -= timedelta(days=1)  # calendar contains future targets only
    execute(service, "init", {"start": "2026-09", "end": "2026-09"})
    fetches = []

    def signals():
        fetches.append(True)
        if len(fetches) == 1:
            raise CpiSignalError("signal_http_403")
        return ERRATA

    scheduler = actual_scheduler(actual, tmp_path, signal_fetcher=signals)
    opened = []

    class RecoveryService(CpiTimeSeriesService):
        def status(self):
            opened.append("status")
            return super().status()

        def update_range(self, *args, **kwargs):
            opened.append("update")
            return super().update_range(*args, **kwargs)

        def query_range(self, *args, **kwargs):
            assert kwargs["vintage_mode"] == "available"
            assert kwargs["allow_upstream"] is False
            opened.append("query_available")
            return super().query_range(*args, **kwargs)

        def close(self):
            opened.append("close")
            return super().close()

    scheduler.service = WorkerService(lambda: RecoveryService(store, source))
    assert (
        execute(None, "signals/refresh", scheduler=scheduler)["signals"]["state"]
        == "degraded"
    )
    clock.at += timedelta(minutes=1)
    assert (
        execute(None, "signals/refresh", scheduler=scheduler)["signals"]["state"]
        == "healthy"
    )
    result = execute(None, "schedule/due", scheduler=scheduler)
    assert result["action"] == "recovery_history_updated"
    assert result["signals"]["recovery"]["end_month"] == "2026-09"
    assert result["signals"]["recovery"]["state"] == "complete"
    assert source.calls[-1] == (SERIES_IDS, "2025-01", "2026-09")
    assert opened == ["status", "close", "update", "close", "query_available", "close"]
    completed_calls = opened.copy()
    source.fail = True
    execute(None, "schedule/due", scheduler=scheduler)
    assert opened == completed_calls


def mapped_notice(clock, *, kind="annual_revision"):
    return {
        "evidence_url": NOTICE_URL,
        "kind": kind,
        "verified": True,
        "scope": "national",
        "series_ids": [SERIES],
        "start_month": "2026-08",
        "end_month": "2026-09",
        "effective_at": clock.at.isoformat(),
        "evidence_text": (
            "Offline fixture: U.S. city average seasonal revisions for "
            "August 2026 and September 2026. See companion table."
        ),
        "scope_mapping": {
            "verified": True,
            "series_ids": [SERIES],
            "start_month": "2026-08",
            "end_month": "2026-09",
        },
        "expected_points": [
            {
                "series_id": SERIES,
                "month": "2026-08",
                "previous_value": "100",
                "index_value": "100",
            },
            {
                "series_id": SERIES,
                "month": "2026-09",
                "previous_value": "100",
                "index_value": "103.12",
            },
        ],
    }


@pytest.mark.parametrize("interface", ["cli", "http"])
def test_actual_natural_language_annual_mapping_keeps_unchanged_cells(
    actual, tmp_path, capsys, interface
):
    service, store, source, clock = actual
    clock.at -= timedelta(days=1)
    execute(service, "init", {"start": "2026-08", "end": "2026-09", "series": SERIES})
    clock.at += timedelta(minutes=1)
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    scheduler = actual_scheduler(actual, tmp_path)
    payload = {"notices": [mapped_notice(clock)]}
    if interface == "cli":
        path = tmp_path / "annual.json"
        path.write_text(json.dumps(payload))
        assert (
            main(
                ["signals", "import", str(path)],
                scheduler_factory=lambda *args, **kwargs: scheduler,
            )
            == 0
        )
        result = json.loads(capsys.readouterr().out)
    else:
        code, result = handle_request(
            service,
            "POST",
            "/api/cpi/signals/import",
            {**HEADERS, "Content-Type": "application/json"},
            json.dumps(payload).encode(),
            port=18766,
            scheduler=scheduler,
        )
        assert code == 200
    event = next(iter(result["signals"]["events"].values()))
    assert event["evidence_text"] == payload["notices"][0]["evidence_text"]
    assert event["scope_mapping"] == payload["notices"][0]["scope_mapping"]
    result = execute(service, "schedule/due", scheduler=scheduler)
    assert result["action"] == "signal_completed"
    assert (
        len(store._history[SERIES, "2026-08"]) == 1
    )  # unchanged is valid evidence, not a new revision
    assert len(store._history[SERIES, "2026-09"]) == 2


def test_http_actual_natural_month_withdrawal_and_safe_expected_restoration(
    actual, tmp_path
):
    service, store, source, clock = actual
    clock.at -= timedelta(days=1)
    execute(service, "init", {"start": "2026-09", "end": "2026-09", "series": SERIES})
    clock.at += timedelta(minutes=1)
    scheduler = actual_scheduler(actual, tmp_path)
    withdrawn = notice(clock, kind="withdrawal")
    withdrawn["evidence_text"] = (
        "Offline fixture: CUSR0000SA0 for September 2026 was removed."
    )
    code, _ = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [withdrawn]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert code == 200
    assert (
        execute(service, "schedule/due", scheduler=scheduler)["action"]
        == "signal_completed"
    )
    clock.at += timedelta(minutes=1)
    correction = notice(clock)
    correction["evidence_url"] = "https://www.bls.gov/errata/test-cpi-restoration.htm"
    correction["evidence_text"] = (
        "Offline fixture: CUSR0000SA0 for September 2026 "
        "was corrected from 100 to 103.12."
    )
    code, _ = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [correction]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert code == 200
    assert (
        execute(service, "schedule/due", scheduler=scheduler)["action"]
        == "signal_pending"
    )
    assert store._history[SERIES, "2026-09"][-1]["version_status"] == "withdrawn"
    clock.at += timedelta(seconds=300)
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    assert (
        execute(service, "schedule/due", scheduler=scheduler)["action"]
        == "signal_completed"
    )
    assert store._history[SERIES, "2026-09"][-1]["version_status"] == "active"
    assert len(store._history[SERIES, "2026-09"]) == 3


def test_http_actual_discovered_row_identity_is_forwarded_and_only_upgrades_target(
    actual, tmp_path
):
    service, _, _, clock = actual
    clock.at -= timedelta(days=1)
    html = b"""<h1>Errata</h1><table id="errata_datatable"><tbody>
    <tr><td>11/08/2026</td><td>CPI</td><td>Database</td><td>
    <a href="https://www.bls.gov/errata/test-cpi-interface.htm">CPI correction</a>
    </td><td>CUSR0000SA0 September 2026 correction.</td><td>Corrected</td></tr>
    <tr><td>11/07/2026</td><td>CPI</td><td>Database</td><td>
    <a href="https://www.bls.gov/errata/test-cpi-interface.htm">CPI correction</a>
    </td><td>CUSR0000SA0 August 2026 correction.</td><td>Corrected</td></tr>
    </tbody></table>"""
    scheduler = actual_scheduler(actual, tmp_path, signal_fetcher=lambda: html)
    snapshot = execute(None, "signals/refresh", scheduler=scheduler)
    events = snapshot["signals"]["events"]
    assert len(events) == 2
    target = next(
        key for key, row in events.items() if row["notice_date"] == "2026-11-08"
    )
    imported = {**notice(clock), "discovered_signal_id": target}
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [imported]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert code == 200
    events = result["signals"]["events"]
    assert len(events) == 2 and events[target]["state"] == "pending"
    assert (
        next(row for key, row in events.items() if key != target)["state"]
        == "review_required"
    )


@pytest.mark.parametrize(
    "mapping",
    [
        None,
        [],
        {
            "verified": "true",
            "series_ids": [SERIES],
            "start_month": "2026-08",
            "end_month": "2026-09",
        },
        {
            "verified": True,
            "series_ids": [SERIES],
            "start_month": "2026-07",
            "end_month": "2026-09",
        },
    ],
)
def test_http_scope_mapping_malformed_or_mismatched_is_400(actual, tmp_path, mapping):
    service, _, source, clock = actual
    scheduler = actual_scheduler(actual, tmp_path)
    payload = {**mapped_notice(clock), "scope_mapping": mapping}
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps({"notices": [payload]}).encode(),
        port=18766,
        scheduler=scheduler,
    )
    assert code == 400 and result["status"] == "ERROR"
    assert source.calls == []
    assert scheduler.status()["signals"]["events"] == {}


def dismiss_request(service, scheduler, payload, *, extra_headers=None):
    return handle_request(
        service,
        "POST",
        "/api/cpi/signals/dismiss",
        {**HEADERS, "Content-Type": "application/json", **(extra_headers or {})},
        json.dumps(payload).encode(),
        port=18766,
        scheduler=scheduler,
    )


def test_cli_http_dismiss_actual_scheduler_idempotent_restart_and_no_reactivation(
    actual, tmp_path, capsys
):
    service, _, source, clock = actual
    clock.at -= timedelta(days=1)
    source.fail = True
    scheduler = actual_scheduler(actual, tmp_path, signal_fetcher=review_notice_html)
    snapshot = execute(None, "signals/refresh", scheduler=scheduler)
    signal_id, original = next(iter(snapshot["signals"]["events"].items()))

    def forbidden(**kwargs):
        pytest.fail("dismiss opened data service")

    reason = "Reviewed image-only correction; no acquisition needed"
    assert (
        main(
            [
                "signals",
                "dismiss",
                "--signal-id",
                signal_id,
                "--reason",
                "  " + reason + "  ",
            ],
            service_factory=forbidden,
            scheduler_factory=lambda *args, **kwargs: scheduler,
        )
        == 0
    )
    first = json.loads(capsys.readouterr().out)
    dismissed = first["signals"]["events"][signal_id]
    assert dismissed["state"] == "dismissed"
    assert dismissed["dismiss_reason"] == reason
    assert dismissed["dismissed_at"] == clock.at.isoformat()
    assert dismissed["next_attempt_at"] is None
    assert all(
        dismissed[key] == value for key, value in original.items() if key != "state"
    )
    assert first["revision_status"] == "no_pending_known_notices"
    clock.at += timedelta(minutes=1)
    code, repeat = dismiss_request(
        service, scheduler, {"signal_id": signal_id, "reason": reason}
    )
    assert code == 200 and repeat["signals"]["events"][signal_id] == dismissed
    restarted = actual_scheduler(actual, tmp_path, signal_fetcher=review_notice_html)
    assert restarted.status()["signals"]["events"][signal_id] == dismissed
    assert (
        execute(None, "signals/refresh", scheduler=restarted)["signals"]["events"][
            signal_id
        ]
        == dismissed
    )
    execute(service, "schedule/due", scheduler=restarted)
    assert source.calls == []
    code, result = dismiss_request(
        service, restarted, {"signal_id": signal_id, "reason": "Different reason"}
    )
    assert code == 400 and result["classification"] == "signal_dismissal_conflict"
    code, result = handle_request(
        service,
        "POST",
        "/api/cpi/signals/import",
        {**HEADERS, "Content-Type": "application/json"},
        json.dumps(
            {"notices": [{**notice(clock), "discovered_signal_id": signal_id}]}
        ).encode(),
        port=18766,
        scheduler=restarted,
    )
    assert code == 400 and result["classification"] == "signal_dismissal_not_allowed"
    assert restarted.status()["signals"]["events"][signal_id] == dismissed


@pytest.mark.parametrize("execute_first", [False, True])
def test_http_dismiss_actual_verified_or_executed_task_is_rejected(
    actual, tmp_path, execute_first
):
    service, _, source, clock = actual
    clock.at -= timedelta(days=1)
    scheduler = actual_scheduler(actual, tmp_path)
    source.values[SERIES, "2026-09"] = Decimal("103.12")
    state = execute(
        None, "signals/import", {"notices": [notice(clock)]}, scheduler=scheduler
    )
    signal_id = next(iter(state["signals"]["events"]))
    if execute_first:
        assert (
            execute(service, "schedule/due", scheduler=scheduler)["action"]
            == "signal_completed"
        )
    before = scheduler.status()["signals"]["events"][signal_id]
    source.fail = True
    code, result = dismiss_request(
        service, scheduler, {"signal_id": signal_id, "reason": "Reviewed unrelated"}
    )
    assert code == 400 and result["classification"] == "signal_dismissal_not_allowed"
    assert scheduler.status()["signals"]["events"][signal_id] == before


@pytest.mark.parametrize(
    "payload,classification",
    [
        ({}, "invalid_signal_dismiss_parameters"),
        ({"signal_id": [], "reason": "reviewed"}, "invalid_signal_dismiss_parameters"),
        ({"signal_id": "a" * 64, "reason": "  "}, "invalid_signal_dismiss_parameters"),
        ({"signal_id": "A" * 64, "reason": "reviewed"}, "signal_id_invalid"),
        (
            {"signal_id": "a" * 64, "reason": "x" * 2001},
            "signal_dismiss_reason_invalid",
        ),
        (
            {"signal_id": "a" * 64, "reason": "bad\nreason"},
            "signal_dismiss_reason_invalid",
        ),
        ({"signal_id": "a" * 64, "reason": "reviewed"}, "signal_not_found"),
        (
            {"signal_id": "a" * 64, "reason": "reviewed", "force": True},
            "unsupported_parameters",
        ),
    ],
)
def test_http_dismiss_actual_parameter_errors_are_400(
    actual, tmp_path, payload, classification
):
    service, _, source, _ = actual
    source.fail = True
    scheduler = actual_scheduler(actual, tmp_path)
    code, result = dismiss_request(service, scheduler, payload)
    assert code == 400 and result["classification"] == classification
    assert scheduler.status()["signals"]["events"] == {}
    assert source.calls == []


def test_http_dismiss_retains_origin_boundary_and_post_only(actual, tmp_path):
    service, _, source, _ = actual
    scheduler = actual_scheduler(actual, tmp_path)
    code, _ = dismiss_request(
        service,
        scheduler,
        {"signal_id": "a" * 64, "reason": "reviewed"},
        extra_headers={"Origin": "http://localhost"},
    )
    assert code == 403
    code, _ = handle_request(
        service,
        "GET",
        "/api/cpi/signals/dismiss",
        HEADERS,
        port=18766,
        scheduler=scheduler,
    )
    assert code == 404
    assert scheduler.status()["signals"]["events"] == {} and source.calls == []
