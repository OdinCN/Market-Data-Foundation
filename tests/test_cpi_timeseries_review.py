"""Independent offline counterexamples for the CPI continuous-series delivery."""

import json
from datetime import UTC, datetime

import pytest

from market_data_foundation.historical import cpi_timeseries_source as source
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult

OBSERVED = datetime(2026, 10, 28, 12, tzinfo=UTC)
TEST_KEY = "ab" * 16


def test_unicode_escaped_credential_echo_cannot_be_retained(monkeypatch):
    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    document = {
        "status": "REQUEST_SUCCEEDED",
        "message": ["echo=" + TEST_KEY],
        "Results": {
            "series": [
                {
                    "seriesID": sid,
                    "data": [{"year": "2026", "period": "M09", "value": "100.0"}],
                }
                for sid in source.SERIES_IDS
            ]
        },
    }
    escaped = "".join("\\u" + format(ord(c), "04x") for c in TEST_KEY)
    raw = json.dumps(document).replace(TEST_KEY, escaped).encode()
    assert TEST_KEY.encode() not in raw
    assert TEST_KEY in json.loads(raw)["message"][0]

    class EchoTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(200, raw, {})

    transport = EchoTransport()
    with pytest.raises(source.CpiSourceError, match="credential_echo_forbidden"):
        source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED).fetch(
            source.SERIES_IDS, "2026-09", "2026-09"
        )
    assert transport.calls == 1


class SyntheticYearsTransport:
    """Return exact year windows, with hand-calculated comparison edge values."""

    def __init__(self):
        self.requests = []

    def post(self, endpoint, body, headers, *, timeout, max_response_bytes):
        request = json.loads(body)
        self.requests.append(request)
        special = {
            "1999-01": "80",
            "1999-12": "125",
            "2000-01": "100",
            "2018-01": "200",
            "2018-12": "250",
            "2019-01": "240",
        }
        series = []
        for sid in request["seriesid"]:
            rows = []
            for year in range(int(request["startyear"]), int(request["endyear"]) + 1):
                for month in range(1, 13):
                    key = f"{year:04d}-{month:02d}"
                    rows.append(
                        {
                            "year": str(year),
                            "period": f"M{month:02d}",
                            "value": special.get(key, "150"),
                            "footnotes": [],
                        }
                    )
            series.append({"seriesID": sid, "data": rows})
        raw = json.dumps(
            {
                "status": "REQUEST_SUCCEEDED",
                "message": [],
                "Results": {"series": series},
            }
        ).encode()
        return HttpResult(200, raw, {})


def test_service_uses_twelve_month_buffer_across_twenty_year_boundary(monkeypatch):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = SyntheticYearsTransport()
    store = InMemoryTimeSeriesStore()
    service = CpiTimeSeriesService(
        store,
        source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED),
    )
    result = service.init_range("2000-01", "2026-09")
    assert result["complete"] is True
    assert len(result["points"]) == (26 * 12 + 9) * 4
    assert [(r["startyear"], r["endyear"]) for r in transport.requests] == [
        ("1999", "2018"),
        ("2019", "2026"),
    ]
    for sid in source.SERIES_IDS:
        points = {p["month"]: p for p in result["points"] if p["series_id"] == sid}
        assert points["2000-01"]["index_value"] == "100"
        assert points["2000-01"]["mom_pct"] == "-20.00"
        assert points["2000-01"]["yoy_pct"] == "25.00"
        assert points["2019-01"]["mom_pct"] == "-4.00"
        assert points["2019-01"]["yoy_pct"] == "20.00"
    # Idempotent initialization can be observed without a second BLS request.
    again = service.init_range("2000-01", "2026-09")
    assert again["saved_points"] == again["saved_revisions"] == 0
    assert len(transport.requests) == 2


def test_as_of_before_first_observation_never_calls_today_source(monkeypatch):
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = SyntheticYearsTransport()
    store = InMemoryTimeSeriesStore()
    service = CpiTimeSeriesService(
        store,
        source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED),
    )
    service.init_range("2026-09", "2026-09")
    before = store.status()
    calls = len(transport.requests)
    result = service.query_range(
        "2026-09",
        "2026-09",
        allow_upstream=True,
        as_of=datetime(2026, 10, 27, 12, tzinfo=UTC),
    )
    assert result["complete"] is False
    assert result["points"] == []
    assert len(result["missing"]) == 4
    assert result["provider_requests"] == 0
    assert len(transport.requests) == calls
    assert store.status() == before


def test_internal_single_series_gap_is_filled_without_persisting(monkeypatch):
    from dataclasses import replace

    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = SyntheticYearsTransport()
    upstream = source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED)
    missing_sid = source.SERIES_IDS[2]
    batches = upstream.fetch(source.SERIES_IDS, "2017-12", "2019-02")
    store = InMemoryTimeSeriesStore()
    store.save_batches(
        tuple(
            replace(
                batch,
                points=tuple(
                    p
                    for p in batch.points
                    if (p.series_id, p.month) != (missing_sid, "2019-01")
                ),
            )
            for batch in batches
        )
    )
    before = store.status()
    transport.requests.clear()
    result = CpiTimeSeriesService(store, upstream).query_range("2018-12", "2019-02")
    assert result["complete"] is True
    assert len(result["points"]) == 12
    assert result["persisted"] is False
    assert len(transport.requests) == 1
    assert transport.requests[0]["seriesid"] == [missing_sid]
    assert store.status() == before
    repaired = next(
        p
        for p in result["points"]
        if (p["series_id"], p["month"]) == (missing_sid, "2019-01")
    )
    assert repaired["index_value"] == "240"
    assert repaired["mom_pct"] == "-4.00"
    assert repaired["yoy_pct"] == "20.00"
    assert repaired["origin"] == "upstream"


def test_restart_after_lost_commit_ack_does_not_duplicate_revisions(monkeypatch):
    from market_data_foundation.historical.cpi_timeseries import (
        CpiTimeSeriesError,
        CpiTimeSeriesService,
    )
    from market_data_foundation.historical.cpi_timeseries_store import (
        CpiStorageError,
        InMemoryTimeSeriesStore,
    )

    class LostAcknowledgementStore(InMemoryTimeSeriesStore):
        def __init__(self):
            super().__init__()
            self.lose_acknowledgement = True

        def save_batches(self, batches):
            result = super().save_batches(batches)
            if self.lose_acknowledgement:
                self.lose_acknowledgement = False
                raise CpiStorageError("cpi_timeseries_storage_outcome_unknown")
            return result

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = SyntheticYearsTransport()
    upstream = source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED)
    store = LostAcknowledgementStore()
    with pytest.raises(CpiTimeSeriesError, match="storage_outcome_unknown"):
        CpiTimeSeriesService(store, upstream).init_range("2026-09", "2026-09")
    after_commit = store.status()
    calls = len(transport.requests)
    restarted = CpiTimeSeriesService(store, upstream)
    result = restarted.init_range("2026-09", "2026-09")
    assert result["complete"] is True
    assert result["saved_points"] == result["saved_revisions"] == 0
    assert store.status() == after_commit
    assert after_commit["revision_count"] == 0
    assert len(transport.requests) == calls


def test_schedule_cannot_complete_refresh_using_only_preexisting_rows(
    monkeypatch, tmp_path
):
    from market_data_foundation.historical.cpi_release_schedule import (
        CpiReleaseScheduler,
    )
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    class IntermittentlyEmptyTransport(SyntheticYearsTransport):
        empty = False

        def post(self, endpoint, body, headers, **kwargs):
            if not self.empty:
                return super().post(endpoint, body, headers, **kwargs)
            request = json.loads(body)
            self.requests.append(request)
            return HttpResult(
                200,
                json.dumps(
                    {
                        "status": "REQUEST_SUCCEEDED",
                        "message": [],
                        "Results": {
                            "series": [
                                {"seriesID": sid, "data": []}
                                for sid in request["seriesid"]
                            ]
                        },
                    }
                ).encode(),
                {},
            )

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = IntermittentlyEmptyTransport()
    service = CpiTimeSeriesService(
        InMemoryTimeSeriesStore(),
        source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED),
    )
    service.init_range("2026-07", "2026-09")
    transport.empty = True
    official_html = b"""<table><tr><th>Reference Month</th><th>Release Date</th>
    <th>Release Time</th></tr><tr><td>September 2026</td><td>Oct. 14, 2026</td>
    <td>08:30 AM</td></tr></table>"""
    scheduler = CpiReleaseScheduler(
        service,
        tmp_path / "state.json",
        clock=lambda: OBSERVED,
        calendar_fetcher=lambda: official_html,
    )
    result = scheduler.due_check()
    assert result["action"] == "awaiting_provider"
    assert result["targets"][0]["state"] != "complete"


@pytest.mark.parametrize("explicit_ref", [False, True])
def test_cli_and_http_use_real_runtime_construction_with_only_external_io_faked(
    monkeypatch, tmp_path, capsys, explicit_ref
):
    import psycopg

    from market_data_foundation.historical import cpi_request_control, cpi_timeseries
    from market_data_foundation.historical.cpi_timeseries import build_service
    from market_data_foundation.historical.cpi_timeseries_cli import (
        main,
        package_credential_loader,
    )
    from market_data_foundation.historical.cpi_timeseries_http import handle_request
    from market_data_foundation.storage.postgres_runtime_config import (
        _CONTROLLED_ENVIRONMENT,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog, SaveCommand
    from market_data_foundation.web.package_store import LocalPackageStore

    # Exercise actual config parsing, connect_dev, PG store, BLS source,
    # service and interface composition. Only wire boundaries are substitutes.
    for name in _CONTROLLED_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    runtime = tmp_path / "runtime.toml"
    runtime.write_text("""[storage]
backend = "s3"
[s3]
endpoint_url = "http://192.168.10.8:3900"
bucket = "mdf-artifacts"
region = "garage"
key_prefix = ""
addressing_style = "path"
[postgres]
host = "192.168.10.4"
port = 5432
database = "market_data_foundation_dev"
user = "market_data_foundation_dev"
sslmode = "require"
""")
    secret = tmp_path / "secret.toml"
    secret.write_text(
        '''[postgres]
password = "offline-test-password"
[s3]
access_key_id = "offline-test-access"
secret_access_key = "offline-test-secret"
[bls]
api_key = "'''
        + TEST_KEY
        + '"\n'
    )
    connections = []

    class OfflineCursor:
        def fetchone(self):
            return (True,)

        def fetchall(self):
            return []

    class OfflineConnection:
        autocommit = True
        closed = False

        def __init__(self):
            self.statements = []

        def execute(self, sql, params=()):
            self.statements.append(sql)
            assert sql.startswith("SELECT ")
            assert "pg_" in sql or ("r.index_value" in sql and ".batches b" in sql)
            return OfflineCursor()

        def close(self):
            self.closed = True

    def connect(**kwargs):
        assert kwargs["host"] == kwargs["hostaddr"] == "192.168.10.4"
        assert kwargs["autocommit"] is True
        connection = OfflineConnection()
        connections.append(connection)
        return connection

    transport = SyntheticYearsTransport()
    monkeypatch.setattr(psycopg, "connect", connect)
    monkeypatch.setattr(source, "ExactOneHttpsTransport", lambda: transport)
    packages_path = tmp_path / "packages"
    packages = LocalPackageStore.initialize(packages_path, PackageCatalog())
    packages.save(
        "offline",
        "macro",
        SaveCommand.model_validate(
            {
                "expected_revision": None,
                "enabled_capability_ids": ["cpi"],
                "source_changes": [
                    {
                        "source_id": "BLS",
                        "credential_action": "replace",
                        "credential_value": TEST_KEY,
                    }
                ]
                if explicit_ref
                else [],
            }
        ),
        allow_replace=True,
    )
    control_class = cpi_request_control.CpiRequestControl
    monkeypatch.setattr(
        cpi_timeseries, "DEFAULT_REQUEST_CONTROL_PATH", tmp_path / "governor.json"
    )
    monkeypatch.setattr(
        cpi_request_control,
        "CpiRequestControl",
        lambda path: control_class(path, min_interval_seconds=0),
    )
    code = main(
        [
            "--runtime",
            str(runtime),
            "--secret",
            str(secret),
            "--schedule-state",
            str(tmp_path / "schedule.json"),
            "--package-store-directory",
            str(packages_path),
            "--environment-id",
            "offline",
            "query",
            "--start",
            "2019-01",
            "--end",
            "2019-01",
            "--measure",
            "yoy",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0, captured.err
    assert json.loads(captured.out)["points"][0]["value"] == "20.00"
    assert connections[0].closed
    service = build_service(
        runtime_path=runtime,
        secret_path=secret,
        credential_loader=package_credential_loader(
            packages_path, "offline", secret_path=secret
        ),
    )
    try:
        status, result = handle_request(
            service,
            "GET",
            "/api/cpi/query?start=2019-01&end=2019-01&measure=mom",
            {"Host": "127.0.0.1:18765"},
        )
    finally:
        service.close()
    assert status == 200
    assert result["points"][0]["value"] == "-4.00"
    assert result["persisted"] is False
    assert len(transport.requests) == 2
    assert len(connections) == 2 and all(c.closed for c in connections)
    assert "offline-test" not in captured.out + captured.err
    assert TEST_KEY not in captured.out + captured.err


def test_sparse_missing_years_do_not_refetch_complete_intervening_years(monkeypatch):
    from dataclasses import replace

    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = SyntheticYearsTransport()
    upstream = source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED)
    series = (source.SERIES_IDS[0],)
    missing = {"2000-01", "2026-01"}
    seed = upstream.fetch(series, "1999-01", "2026-09")
    store = InMemoryTimeSeriesStore()
    store.save_batches(
        tuple(
            replace(
                batch, points=tuple(p for p in batch.points if p.month not in missing)
            )
            for batch in seed
        )
    )
    transport.requests.clear()
    result = CpiTimeSeriesService(store, upstream).query_range(
        "2000-01", "2026-09", series_ids=series
    )
    assert result["complete"] is True
    assert [(r["startyear"], r["endyear"]) for r in transport.requests] == [
        ("2000", "2000"),
        ("2026", "2026"),
    ]


def test_local_numerator_and_fetched_denominator_have_mixed_calculation_origin(
    monkeypatch,
):
    from dataclasses import replace

    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService
    from market_data_foundation.historical.cpi_timeseries_store import (
        InMemoryTimeSeriesStore,
    )

    monkeypatch.setattr(source, "load_registration_key", lambda _: TEST_KEY)
    transport = SyntheticYearsTransport()
    upstream = source.BlsRangeSource(transport=transport, clock=lambda: OBSERVED)
    series = (source.SERIES_IDS[0],)
    seed = upstream.fetch(series, "2018-01", "2019-01")
    store = InMemoryTimeSeriesStore()
    store.save_batches(
        tuple(
            replace(
                batch, points=tuple(p for p in batch.points if p.month != "2018-01")
            )
            for batch in seed
        )
    )
    before = store.status()
    result = CpiTimeSeriesService(store, upstream).query_range(
        "2019-01", "2019-01", series_ids=series
    )
    point = result["points"][0]
    inputs = point["calculation_inputs"]["yoy_pct"]
    assert point["origin"] == "local"
    assert point["yoy_pct"] == "20.00"
    assert inputs["numerator_origin"] == "local"
    assert inputs["denominator_origin"] == "upstream"
    assert inputs["derived_origin"] == "mixed"
    assert inputs["denominator_month"] == "2018-01"
    assert inputs["denominator_revision"] is None
    assert store.status() == before
