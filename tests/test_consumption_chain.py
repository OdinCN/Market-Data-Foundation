"""Offline native units, versions, durable jobs, strategy and evidence chain."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from market_data_foundation.historical.bea_shared_material import (
    cached_material,
    calendar_fetcher,
)
from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE as P,
)
from market_data_foundation.historical.bls_timeseries_profile import PCE_PROFILE
from market_data_foundation.historical.consumption_cli import schema_sql
from market_data_foundation.historical.consumption_schedule import (
    ConsumptionAutoStrategy,
    ConsumptionReleaseScheduler,
)
from market_data_foundation.historical.cpi_timeseries_cli import WorkerService
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from tests.consumption_fixtures import NOW, Raw, make_host
from tests.test_pce_release_schedule import ics


def test_numeric_schema_is_native_additive_and_readonly():
    sql = schema_sql()
    assert "value numeric" in sql and "index_value" not in sql and "bls_pct" not in sql
    assert "series_id='A072RC' OR value > 0" in sql
    assert "consumption_timeseries_rates" in sql and "percentage_points" in sql
    assert (
        "ALTER DEFAULT PRIVILEGES" in sql
        and "GRANT SELECT" in sql
        and "TO odin_dba" in sql
    )
    assert P.schema != PCE_PROFILE.schema and P.lock_id != PCE_PROFILE.lock_id
    assert all(m["start_month"] is None for m in P.series_metadata.values())


def test_amounts_native_millions_and_saving_rate_only_point_changes(tmp_path):
    _, host, source, store = make_host(tmp_path)
    result = host.query(dict(start="2026-09", end="2026-09", local_only="true"))
    points = {p["series_id"]: p for p in result["points"]}
    assert points["DPCERC"]["value"] == "22452780"
    assert "index_value" not in str(result) and "bls_pct" not in str(result)
    rate = points["A072RC"]
    assert rate["value"] == "3.8" and rate["value_unit"] == "percent"
    assert rate["change_1m_percentage_points"] == "3.8"
    assert rate["change_12m_percentage_points"] == "5.0"
    assert "mom_pct" not in rate and "change_1m_ratio" not in rate
    assert rate["annual_rate"] is False and points["A067RX"]["chained_base"] == "2017"
    assert points["DPCERC"]["annual_rate"] is True
    assert source.calls == []
    assert result["measure"] == "native" and result["unit"] == "per_series_value_unit"
    with pytest.raises(Exception, match="measure"):
        host.query(
            dict(start="2026-09", end="2026-09", local_only="true", measure="mom")
        )


def test_same_cutoff_comparison_versions_and_first_capture(tmp_path):
    _, host, source, store = make_host(tmp_path)
    before = NOW
    store._clock = lambda: NOW + timedelta(hours=1)
    source.offset = 1000
    source.observed = NOW + timedelta(minutes=30)
    host.read_factory().update_range(
        "2026-08", "2026-08", series_ids=["DPCERC"], include_buffer=False
    )
    old = host.query(
        dict(
            start="2026-09",
            end="2026-09",
            series="DPCERC",
            local_only="true",
            query_mode="history",
            as_of=before.isoformat(),
        )
    )["points"][0]
    current = host.query(
        dict(start="2026-09", end="2026-09", series="DPCERC", local_only="true")
    )["points"][0]
    assert old["calculation_inputs"]["mom_pct"]["comparison_value"] == "22452680"
    assert current["calculation_inputs"]["mom_pct"]["comparison_value"] == "22453680"
    assert old["mdf_first_captured"]["value"] == "22452780"
    early = host.query(
        dict(
            start="2026-09",
            end="2026-09",
            series="DPCERC",
            query_mode="history",
            as_of=(NOW - timedelta(seconds=1)).isoformat(),
        )
    )
    assert early["points"] == [] and early["provider_requests"] == 0


def test_ephemeral_gapfill_and_future_range_keep_local_rows(tmp_path):
    _, host, source, store = make_host(tmp_path)
    before = store.status()
    result = host.query(dict(start="2026-09", end="2027-02", series="DPCERC"))
    assert result["points"][0]["value"] == "22452780"
    assert len(result["missing"]) == 5 and not result["complete"]
    assert all(last <= "2026-10" for _, _, last in source.calls)
    assert store.status() == before
    source.calls.clear()
    result = host.query(dict(start="2027-01", end="2027-02", series="DPCERC"))
    assert (
        result["points"] == []
        and result["provider_requests"] == 0
        and source.calls == []
    )


def test_manual_job_durable_readback_and_partial_coverage(tmp_path):
    _, host, source, store = make_host(tmp_path, preload=False)
    source.missing.add(("DGDSRX", "2026-09"))
    command = dict(
        request_id=str(uuid4()), action="initialize", start="2026-09", end="2026-09"
    )
    job = host.submit(command)
    assert job["state"] == "complete"
    assert job["asset"] == "consumption" and job["result"]["readback"]["verified"]
    assert not job["result"]["coverage_complete"]
    assert (
        job["result"]["provider_requests"] == 2
        and job["result"]["provider_attempts"] == 2
    )
    assert host.journal.read(command["request_id"]) == job
    from market_data_foundation.web.cpi_timeseries_jobs import JobJournal

    assert (
        JobJournal(host.journal.directory, profile=P).read(command["request_id"]) == job
    )
    count = len(source.calls)
    assert host.submit(command) == job and len(source.calls) == count
    assert host.executions()["executions"][0]["id"] == command["request_id"]


def test_actual_worker_service_strategy_path_and_history_cursor(tmp_path):
    _, host, source, store = make_host(tmp_path, preload=False)
    worker = WorkerService(host.read_factory, profile=P)
    scheduler = ConsumptionReleaseScheduler(
        worker,
        tmp_path / "worker-calendar.json",
        clock=lambda: NOW,
        history_start="2026-08",
        calendar_fetcher=lambda: ics(),
    )
    strategy = ConsumptionAutoStrategy(
        scheduler, tmp_path / "consumption-strategy.json", clock=lambda: NOW
    )
    assert strategy.tick()["action"] == "paused" and not source.calls
    strategy.update(enabled=True, history_start="2026-08")
    strategy.tick()
    targets = scheduler.status()["targets"]
    assert targets[0]["target"]["state"] == "complete"
    assert targets[0]["target"]["readback_verified"]
    assert store.status()["observation_count"] == 7
    scheduler.clock = lambda: NOW + timedelta(seconds=4)
    strategy.clock = scheduler.clock
    strategy.tick()
    targets = scheduler.status()["targets"]
    assert targets[0]["history"]["state"] == "complete"
    assert targets[0]["history"]["update_start"] == "2026-08"
    assert targets[0]["history"]["actual_http"] == 2
    assert strategy.get()["worker"]["last_heartbeat_at"]
    assert strategy.get()["policy"]["provider"] == "BEA"


def material():
    return Path(
        "tests/fixtures/macro_release/bea-consumption-synthetic.html"
    ).read_bytes()


URL = "https://www.bea.gov/news/2026/personal-income-and-outlays-september-2026"


def test_shared_canonical_event_separate_parse_evidence_and_late_cutoff(tmp_path):
    _, host, _, store = make_host(tmp_path)
    repo = ReleaseRecords(store)
    consume = repo.import_material(
        material(), URL, material_format="official_selected_projection"
    )
    price_store = InMemoryTimeSeriesStore(profile=PCE_PROFILE, clock=lambda: NOW)
    price_store.raw_store = Raw()
    price_repo = ReleaseRecords(price_store)
    price_repo.prepare()
    price = price_repo.import_material(
        material(), URL, material_format="official_selected_projection"
    )
    assert (
        consume["event_id"]
        == price["event_id"]
        == "bea:personal-income-and-outlays:2026-10-29"
    )
    assert consume["raw_sha256"] == price["raw_sha256"]
    assert consume["evidence_id"] != price["evidence_id"]
    assert all(c["series_id"] in P.series_ids for c in consume["claims"])
    assert (
        repo.read(as_of=(NOW - timedelta(microseconds=1)).isoformat())["events"] == []
    )
    result = repo.read(as_of=NOW.isoformat())["events"][0]
    assert result["actual_at"] is None
    assert any(
        c["series_id"] == "A072RC" and c["statistic"] == "level"
        for c in result["claims"]
    )
    assert all(
        c["continuous_version"] is None
        for c in result["claims"]
        if c["statistic"] == "mom_pct"
    )
    assert (
        repo.import_material(
            material(), URL, material_format="official_selected_projection"
        )["evidence_id"]
        == consume["evidence_id"]
    )


def test_shared_original_material_and_calendar_cache_avoid_duplicate_calls(tmp_path):
    selection = dict(
        source_url=URL, source_date="2026-10-29", reference_month="2026-09"
    )
    calls = []

    def fetch(url, family):
        calls.append(family)
        return material()

    first, fetched = cached_material(tmp_path / "cache", selection, "pce", fetch)
    second, refetched = cached_material(
        tmp_path / "cache", selection, "consumption", fetch
    )
    assert fetched and not refetched and first == second and calls == ["pce"]
    calls.clear()
    fetch_calendar = calendar_fetcher(
        tmp_path / "calendar-cache.json",
        fetcher=lambda: calls.append("calendar") or ics(),
        clock=lambda: NOW,
    )
    assert fetch_calendar() == fetch_calendar() == ics()
    assert calls == ["calendar"]


def test_actual_acquirer_reuses_bytes_but_imports_each_asset_scope(tmp_path):
    from market_data_foundation.historical.macro_release_acquisition import (
        MaterialAcquirer,
    )

    _, _, _, consumption = make_host(tmp_path)
    price = InMemoryTimeSeriesStore(profile=PCE_PROFILE, clock=lambda: NOW)
    price.raw_store = Raw()
    ReleaseRecords(price).prepare()
    calls = []

    def fetch(url, family):
        calls.append(family)
        return material()

    acquirer = MaterialAcquirer(tmp_path / "shared-acquisition.json", fetcher=fetch)
    selection = dict(
        source_url=URL, source_date="2026-10-29", reference_month="2026-09"
    )
    pce = acquirer.capture(price, selection)
    native = acquirer.capture(consumption, selection)
    assert pce["state"] == native["state"] == "retained"
    assert pce["event_ids"] == native["event_ids"]
    assert pce["evidence_ids"] != native["evidence_ids"]
    assert pce["transport_calls"] == 1 and native["transport_calls"] == 0
    assert calls == ["pce"]
    assert pce["evidence_origin"] == "current_bounded_official_transport"
    assert native["evidence_origin"] == "current_shared_original_material_cache"
    doc = ReleaseRecords(consumption).read()["events"][0]
    assert doc["retention"]["acquisition_method"] == "shared_original_material_cache"
    assert all(c["series_id"] in P.series_ids for c in doc["claims"])
    replay = acquirer.capture(consumption, selection)
    assert replay["transport_calls"] == 0 and calls == ["pce"]
    assert replay["evidence_origin"] == "previous_shared_original_material_cache"


def test_shared_bea_asset_storage_failure_recovers_after_restart_without_refetch(
    tmp_path, monkeypatch
):
    """Synthetic transport/storage fault; independent asset success stays valid."""
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
    from market_data_foundation.historical.macro_release_acquisition import (
        MaterialAcquirer,
    )
    from tests.test_macro_release_chain import Raw as ProfileRaw

    _, _, _, consumption = make_host(tmp_path)
    numeric_before = consumption._history.copy()
    price = InMemoryTimeSeriesStore(
        ProfileRaw(PCE_PROFILE), profile=PCE_PROFILE, clock=lambda: NOW
    )
    ReleaseRecords(price).prepare()
    calls = []
    path = tmp_path / "shared-acquisition.json"
    selection = dict(
        source_url=URL, source_date="2026-10-29", reference_month="2026-09"
    )

    def fetch(*_):
        calls.append("synthetic_transport")
        return material()

    retained = MaterialAcquirer(path, fetcher=fetch).capture(price, selection)
    price_before = ReleaseRecords(price).read()
    put = consumption.raw_store.put_verified

    def fail(_):
        raise CpiTimeSeriesError("release_storage_failed")

    monkeypatch.setattr(consumption.raw_store, "put_verified", fail)
    failed = MaterialAcquirer(path, fetcher=fetch).capture(consumption, selection)
    assert retained["state"] == "retained" and failed["state"] == "partial"
    assert failed["reasons"] == ["release_storage_failed"]
    assert failed["transport_calls"] == 0
    assert ReleaseRecords(consumption).read()["events"] == []
    monkeypatch.setattr(consumption.raw_store, "put_verified", put)
    restarted = MaterialAcquirer(path, fetcher=fetch)
    cooldown = restarted.capture(consumption, selection)
    assert cooldown["state"] == "partial" and cooldown["transport_calls"] == 0
    consumption._clock = lambda: NOW + timedelta(hours=1, seconds=1)
    recovered = restarted.capture(consumption, selection)
    event = ReleaseRecords(consumption).read()["events"][0]
    assert recovered["state"] == "retained" and recovered["transport_calls"] == 0
    assert event["event_id"] == retained["event_ids"][0]
    assert event["shared_publication"]["current_asset"] == "consumption"
    assert event["value_comparisons"]
    assert recovered["evidence_origin"] == "current_shared_original_material_cache"
    assert (
        restarted.capture(consumption, selection)["evidence_ids"]
        == recovered["evidence_ids"]
    )
    assert len(consumption._release_evidence) == 1
    assert consumption._history == numeric_before
    assert ReleaseRecords(price).read() == price_before
    assert calls == ["synthetic_transport"]


def test_bea_material_commit_before_state_failure_reconciles_without_duplicate(
    tmp_path,
):
    """Simulate interruption after asset evidence commit, before state receipt."""
    from market_data_foundation.historical.macro_release_acquisition import (
        MaterialAcquirer,
    )

    _, _, _, consumption = make_host(tmp_path)
    path = tmp_path / "acquisition.json"
    selection = dict(
        source_url=URL, source_date="2026-10-29", reference_month="2026-09"
    )

    class InterruptedReceipt(MaterialAcquirer):
        saves = 0

        def _save(self, state):
            self.saves += 1
            if self.saves == 2:
                raise OSError("synthetic receipt failure")
            super()._save(state)

    acquirer = InterruptedReceipt(path, fetcher=lambda *_: material())
    uncertain = acquirer.capture(consumption, selection)
    assert uncertain["state"] == "unknown"
    assert uncertain["reasons"] == ["release_material_state_write_outcome_unknown"]
    before = ReleaseRecords(consumption).read()
    restarted = MaterialAcquirer(
        path, fetcher=lambda *_: pytest.fail("must reconcile retained evidence")
    )
    recovered = restarted.capture(consumption, selection)
    assert recovered["state"] == "retained" and recovered["transport_calls"] == 0
    assert recovered["evidence_ids"] == uncertain["evidence_ids"]
    assert len(consumption._release_evidence) == 1
    assert ReleaseRecords(consumption).read() == before


def test_native_postgres_insert_and_serializer_use_correct_column_port():
    from datetime import date

    from market_data_foundation.historical.cpi_timeseries_store import (
        PostgresTimeSeriesStore,
        _relational_content,
    )
    from tests.consumption_fixtures import Source
    from tests.test_pce_percent_persistence import PgConnection

    source = Source()
    batch = source.fetch(("A072RC",), "2026-08", "2026-08")[0]
    point = batch.points[0]
    values = _relational_content(point, profile=P)
    row = (
        "A072RC",
        date(2026, 8, 1),
        1,
        NOW,
        "batch",
        *values,
        "sha",
        NOW,
        "active",
        None,
    )
    connection = PgConnection(rows=[row])
    store = PostgresTimeSeriesStore(connection, Raw(), profile=P, clock=lambda: NOW)
    assert store.save_batches((batch,))["saved_points"] == 1
    sql, params = next(
        (sql, params)
        for sql, params in connection.statements
        if f"INSERT INTO {P.schema}.revisions" in sql
    )
    assert "value" in sql and "index_value" not in sql and "bls_pct" not in sql
    assert params[5] == 0 and params[6].obj == point.provider_calculations
    read = store.read_range(("A072RC",), "2026-08", "2026-08")[0]
    assert (
        read["index_value"] == "0"
    )  # shared internal port, projected to value by service
    assert read["provider_calculations"] == point.provider_calculations


def test_history_reservation_includes_five_year_windows_and_derivative_buffer(tmp_path):
    _, host, _, _ = make_host(tmp_path, preload=False)
    worker = WorkerService(host.read_factory, profile=P)
    scheduler = ConsumptionReleaseScheduler(
        worker,
        tmp_path / "budget.json",
        clock=lambda: NOW,
        history_start="2006-01",
        calendar_fetcher=lambda: ics(),
    )
    strategy = ConsumptionAutoStrategy(
        scheduler, tmp_path / "budget-strategy.json", clock=lambda: NOW
    )
    strategy.update(enabled=True, history_start="2006-01")
    strategy.tick()
    scheduler.clock = lambda: NOW + timedelta(seconds=4)
    strategy.clock = scheduler.clock
    strategy.tick()
    import json

    state = json.loads(scheduler.state_path.read_text())
    assert [a["reserved_http"] for a in state["data_admissions"]] == [2, 4]


def test_future_request_gate_uses_bound_clock_and_preserves_local_values(tmp_path):
    _, host, source, store = make_host(tmp_path)
    source.calls.clear()
    store._clock = lambda: NOW.replace(month=9, day=15)
    result = host.query(dict(start="2026-09", end="2026-12", series="DPCERC"))
    assert source.calls == [] and result["provider_requests"] == 0
    assert result["points"][0]["value"] == "22452780"
    assert not result["complete"]
