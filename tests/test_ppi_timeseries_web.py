"""Synthetic dual-asset Web contracts; no external transport or database."""

import json
from datetime import timedelta
from decimal import Decimal
from threading import Event

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesService,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.web.cpi_timeseries_api import (
    TimeSeriesHost,
    timeseries_router,
)
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.package_catalog import (
    CredentialState,
    PackageCatalog,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from tests.test_cpi_timeseries_web import NOW, command, no_network  # noqa: F401


class Source:
    def __init__(self, profile):
        self.profile = profile
        self.calls = 0
        self.offset = 0

    def fetch(self, series, start, end):
        assert set(series) <= set(self.profile.series_ids)
        self.calls += 1
        points = tuple(
            SourcePoint(s, month, Decimal(100 + int(month[-2:]) + self.offset), {}, ())
            for s in series
            for month in month_range(start, end)
        )
        return (
            SourceBatch(
                points,
                f"synthetic-{self.profile.key}-{self.offset}".encode(),
                {"seriesid": list(series)},
                NOW + timedelta(seconds=self.calls),
                1,
            ),
        )


@pytest.fixture
def dual(tmp_path):
    catalog = PackageCatalog()
    store = LocalPackageStore.initialize(tmp_path / "packages", catalog)
    store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=["cpi", "ppi"]),
    )
    packages = PackageHost(
        store,
        "fixture",
        credentials=lambda *_: [
            CredentialState(source_id="BLS", credential_state="configured")
        ],
        credential_loader=lambda *_: "synthetic-key-never-public",
    )
    hosts, sources, stores = {}, {}, {}
    for profile in (CPI_PROFILE, PPI_PROFILE):
        source = Source(profile)
        data = InMemoryTimeSeriesStore(profile=profile, clock=lambda: NOW)

        def service(data=data, source=source, profile=profile):
            return CpiTimeSeriesService(data, source, profile=profile)

        def bound(*, credential_loader, service=service):
            credential_loader()
            return service()

        hosts[profile.key] = TimeSeriesHost(
            profile=profile,
            packages=packages,
            read_service_factory=service,
            write_service_factory=bound,
            query_service_factory=bound,
            allow_collect=True,
            scheduler=CpiReleaseScheduler(
                None,
                tmp_path / profile.key / "schedule.json",
                profile=profile,
                history_start=profile.earliest_month or "2020-01",
                clock=lambda: NOW,
            ),
            job_directory=tmp_path / profile.key / "jobs",
            clock=lambda: NOW,
        )
        sources[profile.key], stores[profile.key] = source, data
    app = FastAPI()
    for host in hosts.values():
        app.include_router(timeseries_router(host))
    return app, hosts, sources, stores


def client(app):
    return TestClient(app, base_url=CANDIDATE_ORIGIN, client=("192.168.10.25", 1234))


def test_profile_jobs_and_query_are_independent(dual):
    _, hosts, sources, stores = dual
    ppi, cpi = hosts["ppi"], hosts["cpi"]
    assert ppi.submit(command("initialize", start="2026-08"))["state"] == "complete"
    assert stores["cpi"].status()["observation_count"] == 0
    assert sources["cpi"].calls == 0 and sources["ppi"].calls == 1
    local = ppi.query(
        {"start": "2026-09", "end": "2026-09", "measure": "change_1m_ratio"}
    )
    assert local["points"][0]["value"] == "0.0093"
    assert local["points"][0]["publication_event"] is None
    assert sources["ppi"].calls == 1
    assert set(local["series_ids"]) <= set(PPI_PROFILE.series_ids)
    assert cpi.query({"start": "2026-09", "end": "2026-09"})["points"] == []
    sources["ppi"].offset = 1
    updated = ppi.submit(command("update"))
    assert updated["state"] == "complete"
    assert updated["result"]["readback"]["expected_points"] == len(
        PPI_PROFILE.series_ids
    )
    assert len(ppi.executions()["executions"]) == 2
    assert cpi.executions()["executions"] == []


def test_history_is_available_local_and_default_gap_query_never_persists(dual):
    _, hosts, sources, stores = dual
    ppi = hosts["ppi"]
    result = ppi.query({"start": "2026-09", "end": "2026-09"})
    assert result["points"] and sources["ppi"].calls == 1
    assert stores["ppi"].status()["observation_count"] == 0
    history = ppi.query(
        {
            "start": "2026-09",
            "end": "2026-09",
            "as_of": (NOW + timedelta(days=1)).isoformat(),
        }
    )
    assert history["points"] == [] and history["as_of_basis"] == "available"
    assert history["query_mode"] == "history" and sources["ppi"].calls == 1


def test_independent_strategy_and_catalog_identity(dual):
    _, hosts, _, _ = dual
    ppi, cpi = hosts["ppi"], hosts["cpi"]
    original = ppi.strategy()["history_start"]
    assert ppi.update_strategy({"enabled": True})["enabled"] is True
    assert ppi.update_strategy({"enabled": False})["enabled"] is False
    assert ppi.strategy()["history_start"] == original
    assert not cpi.auto_strategy.strategy_path.exists()
    assert ppi.auto_strategy.strategy_path != cpi.auto_strategy.strategy_path
    capability = next(
        c
        for c in ppi.packages.catalog.package("macro").capabilities
        if c.capability_id == "ppi"
    )
    assert ppi.api_help()["definition_ref"] == capability.definition_ref.model_dump()
    assert ppi.api_help()["asset_id"] == capability.definition_ref.identity


def test_disabled_capability_and_cross_profile_factory_are_rejected(dual):
    _, hosts, sources, _ = dual
    ppi = hosts["ppi"]
    record = ppi.packages.store.read("fixture", "macro")
    ppi.packages.store.save(
        "fixture",
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=["cpi"]),
    )
    with pytest.raises(WebError, match="ppi_capability_disabled"):
        ppi.submit(command())
    with pytest.raises(WebError, match="ppi_capability_disabled"):
        ppi.query({"start": "2026-09", "end": "2026-09"})
    assert sources["ppi"].calls == 0
    with pytest.raises(ValueError, match="unsupported_parameters"):
        ppi.query(
            {"start": "2026-09", "end": "2026-09", "series": CPI_PROFILE.series_ids[0]}
        )
    ppi.read_factory = hosts["cpi"].read_factory
    with pytest.raises(WebError, match="bls_timeseries_profile_mismatch"):
        ppi.query({"start": "2026-09", "end": "2026-09", "local_only": "true"})


def test_asgi_dual_status_query_and_help(dual):
    app, hosts, _, _ = dual
    for host in hosts.values():
        assert host.submit(command())["state"] == "complete"
    with client(app) as http:
        for profile in (CPI_PROFILE, PPI_PROFILE):
            prefix = hosts[profile.key].prefix
            status = http.get(prefix + "/status")
            assert status.status_code == 200
            assert status.json()["schema"] == profile.schema
            assert status.json()["initialization"]["months"] == 240
            result = http.get(
                prefix + "/query?start=2026-09&end=2026-09&basket=all&adjustment=all"
            )
            assert result.status_code == 200
            assert set(result.json()["series_ids"]) == set(profile.series_ids)
            assert len(result.json()["points"]) == len(profile.series_ids)
            assert http.get(prefix + "/api-help").json()["series_ids"] == list(
                profile.series_ids
            )


def test_asgi_ppi_job_execution_and_strategy(dual, monkeypatch):
    app, hosts, _, _ = dual
    ppi = hosts["ppi"]
    finished = Event()
    submit = ppi.submit

    def tracked(*args, **kwargs):
        try:
            return submit(*args, **kwargs)
        finally:
            finished.set()

    monkeypatch.setattr(ppi, "submit", tracked)
    with client(app) as http:
        for action in ("initialize", "update"):
            finished.clear()
            response = http.post(
                ppi.prefix + "/jobs",
                json=command(action),
                headers={"Origin": CANDIDATE_ORIGIN},
            )
            assert response.status_code in {200, 202}
            assert finished.wait(5)
        executions = http.get(ppi.prefix + "/executions").json()["executions"]
        assert len(executions) == 2 and all(
            e["state"] == "complete" for e in executions
        )
        for enabled in (True, False):
            response = http.put(
                ppi.prefix + "/strategy",
                json={"enabled": enabled},
                headers={"Origin": CANDIDATE_ORIGIN},
            )
            assert response.status_code == 200 and response.json()["enabled"] is enabled
        assert http.get(hosts["cpi"].prefix + "/jobs").json() == {"jobs": []}


def test_asgi_ppi_boundary_errors_do_not_expose_credentials(dual):
    app, hosts, _, _ = dual
    prefix = hosts["ppi"].prefix
    with client(app) as http:
        assert http.post(prefix + "/jobs", json=command()).status_code == 403
        assert (
            http.post(
                prefix + "/jobs",
                json=command(),
                headers={"Origin": "http://evil.example"},
            ).status_code
            == 403
        )
        response = http.get(
            prefix + "/query?start=2026-09&end=2026-09&series=CUSR0000SA0"
        )
        assert response.status_code == 400
        assert http.request("GET", prefix + "/status", content=b"{}").status_code == 400
        hosts["ppi"].read_factory = lambda: (_ for _ in ()).throw(
            RuntimeError("synthetic-key-never-public")
        )
        response = http.get(prefix + "/status")
        assert response.status_code == 500
        assert response.json() == {
            "status": "ERROR",
            "classification": "internal_error",
            "provider_attempts": 0,
        }
        assert "synthetic-key" not in response.text
        assert "RuntimeError" not in response.text
        hosts["ppi"].read_factory = lambda: (_ for _ in ()).throw(
            WebError("ppi_storage_unavailable", 503)
        )
        response = http.get(prefix + "/status")
        assert response.status_code == 503
        assert response.json() == {
            "status": "ERROR",
            "classification": "ppi_storage_unavailable",
        }
        assert "synthetic-key" not in response.text
        assert "WebError" not in response.text


def test_asgi_unconfigured_ppi_is_unavailable_without_factories():
    app = FastAPI()
    app.include_router(timeseries_router(None, profile=PPI_PROFILE))
    with client(app) as http:
        response = http.get("/api/r5/ppi/timeseries/status")
        assert response.status_code == 503
        assert response.json()["classification"] == "ppi_timeseries_not_enabled"


def test_preset_reports_profile_boundary_without_claiming_twenty_years(
    dual, monkeypatch
):
    _, hosts, sources, _ = dual
    ppi = hosts["ppi"]
    monkeypatch.setattr(
        ppi.scheduler,
        "status",
        lambda: {
            "targets": [
                {
                    "reference_month": "2026-09",
                    "release_at": NOW.isoformat(),
                    "reference_month_evidence": {
                        "reference_month": "2026-09",
                        "source": "https://www.bls.gov/schedule/news_release/ppi.htm",
                    },
                }
            ]
        },
    )
    preset = ppi.status()["initialization"]
    assert preset["months"] == 240 and preset["editable"] is True
    assert preset["earliest_month"] == PPI_PROFILE.earliest_month
    if PPI_PROFILE.earliest_month is None:
        assert preset["available"] is True
        assert preset["start"] == "2006-10"
        assert preset["end"] == "2026-09"
        assert preset["reason"] is None
    else:
        assert preset["end"] == "2026-09"
        assert preset["start"] == max("2006-10", PPI_PROFILE.earliest_month)
        assert preset["coverage_truncated"] is (preset["actual_months"] < 240)
    assert (
        preset["coverage_basis"] == "profile_boundary_and_anchor_not_complete_holdings"
    )
    assert sources["ppi"].calls == 0


def test_real_ppi_source_transport_planner_and_readback_are_offline(dual):
    from market_data_foundation.historical.cpi_timeseries_source import BlsRangeSource
    from market_data_foundation.web.cpi_timeseries_host import LocalOnlySource
    from tests.test_cpi_timeseries_web_collection import ContinuousTransport

    _, hosts, _, stores = dual
    ppi = hosts["ppi"]

    class PpiTransport(ContinuousTransport):
        def index(self, series, year, month):
            return (
                Decimal(1000 + 100 * PPI_PROFILE.series_ids.index(series) + self.offset)
                + Decimal((year - 1999) * 12 + month - 1) / 10
            )

        def post(self, endpoint, body, headers, **options):
            assert set(json.loads(body)["seriesid"]) == set(PPI_PROFILE.series_ids)
            return super().post(endpoint, body, headers, **options)

    transport = PpiTransport()
    ppi.packages.credential_loader = lambda *_: "a" * 32
    data = stores["ppi"]
    data._clock = lambda: transport.now
    ppi.read_factory = lambda: CpiTimeSeriesService(
        data, LocalOnlySource(), profile=PPI_PROFILE
    )

    def source_service(*, credential_loader):
        return CpiTimeSeriesService(
            data,
            BlsRangeSource(
                credential_loader=credential_loader,
                transport=transport,
                clock=lambda: transport.now,
                profile=PPI_PROFILE,
            ),
            profile=PPI_PROFILE,
        )

    ppi.write_factory = ppi.query_factory = source_service
    receipt = ppi.submit(command("initialize", start="2025-01"))
    assert receipt["state"] == "complete"
    assert receipt["result"]["readback"]["verified"] is True
    assert transport.requests
    before = data.status()["observation_count"]
    result = ppi.query(
        {"start": "2026-09", "end": "2026-09", "measure": "change_12m_ratio"}
    )
    assert result["points"][0]["value"] is not None
    assert data.status()["observation_count"] == before
    assert stores["cpi"].status()["observation_count"] == 0


def test_journal_rejects_other_asset_even_if_directory_is_misbound(dual):
    from market_data_foundation.web.cpi_timeseries_jobs import JobJournal

    _, hosts, _, _ = dual
    request = command()
    cpi = hosts["cpi"]
    assert cpi.submit(request)["state"] == "complete"
    wrong = JobJournal(cpi.journal.directory, profile=PPI_PROFILE)
    with pytest.raises(WebError, match="job_journal_asset_mismatch"):
        wrong.read(request["request_id"])
    ppi = hosts["ppi"]
    submitted = ppi.submit(command())
    assert submitted["asset"] == "ppi"
    assert ppi.journal.read(submitted["request_id"]) == submitted


def test_same_request_id_is_independent_for_each_asset(dual):
    _, hosts, sources, stores = dual
    request = command()
    cpi = hosts["cpi"].submit(request)
    ppi = hosts["ppi"].submit(request)
    assert cpi["state"] == ppi["state"] == "complete"
    assert cpi["job_id"] == ppi["job_id"]
    assert hosts["ppi"].submit(request) == ppi
    assert hosts["cpi"].submit(request) == cpi
    assert sources["cpi"].calls == sources["ppi"].calls == 1
    assert stores["cpi"].status()["latest_months"].keys() == set(CPI_PROFILE.series_ids)
    assert stores["ppi"].status()["latest_months"].keys() == set(PPI_PROFILE.series_ids)


def test_shared_http_bridge_admits_only_both_timeseries_write_paths():
    from fastapi import Request

    from market_data_foundation.web.cpi_timeseries_api import (
        admitted_job,
        admitted_strategy,
    )

    for key in ("cpi", "ppi", "pce"):
        scope = {
            "type": "http",
            "scheme": "http",
            "query_string": b"",
            "client": ("192.168.10.25", 1234),
            "headers": [
                (b"host", CANDIDATE_ORIGIN.removeprefix("http://").encode()),
                (b"origin", CANDIDATE_ORIGIN.encode()),
                (b"content-type", b"application/json"),
            ],
        }
        job = Request(dict(scope, method="POST", path=f"/api/r5/{key}/timeseries/jobs"))
        strategy = Request(
            dict(scope, method="PUT", path=f"/api/r5/{key}/timeseries/strategy")
        )
        assert admitted_job(job) is (key in {"cpi", "ppi"})
        assert admitted_strategy(strategy) is (key in {"cpi", "ppi"})


def test_ppi_empty_unknown_history_has_calendar_request_preset(dual):
    _, hosts, sources, _ = dual
    preset = hosts["ppi"].status()["initialization"]
    assert preset["available"] is True
    assert preset["actual_months"] == 240
    assert preset["start"] == "2006-10" and preset["end"] == "2026-09"
    assert preset["anchor_basis"] == "request_calendar_month"
    assert sources["ppi"].calls == 0


def test_journal_copied_cpi_and_explicit_wrong_assets(dual):
    from market_data_foundation.web.cpi_timeseries_jobs import JobJournal

    _, hosts, _, _ = dual
    receipt = hosts["cpi"].submit(command())
    cpi, ppi = hosts["cpi"].journal, hosts["ppi"].journal
    assert (
        json.loads((cpi.directory / (receipt["request_id"] + ".json")).read_text())[
            "asset"
        ]
        == "cpi"
    )
    ppi.directory.mkdir(parents=True)
    path = ppi.directory / (receipt["request_id"] + ".json")
    for record in (receipt, {k: v for k, v in receipt.items() if k != "asset"}):
        path.write_text(json.dumps(record))
        with pytest.raises(WebError, match="ppi_job_journal_asset_mismatch"):
            ppi.read(receipt["request_id"])
    for profile, wrong in ((CPI_PROFILE, "ppi"), (PPI_PROFILE, "cpi")):
        journal = JobJournal(cpi.directory, profile=profile)
        with pytest.raises(WebError, match=profile.key + "_job_journal_asset_mismatch"):
            journal.save(dict(receipt, asset=wrong))
    legacy = dict(receipt)
    legacy.pop("asset")
    (cpi.directory / (receipt["request_id"] + ".json")).write_text(json.dumps(legacy))
    assert cpi.read(receipt["request_id"]) == legacy
    (cpi.directory / (receipt["request_id"] + ".json")).write_text(
        json.dumps(dict(legacy, asset="ppi"))
    )
    with pytest.raises(WebError, match="cpi_job_journal_asset_mismatch"):
        cpi.read(receipt["request_id"])


@pytest.mark.parametrize("omit_old_null", [True, False])
def test_ppi_refresh_preserves_source_gap_for_saved_null(dual, omit_old_null):
    from dataclasses import replace

    _, hosts, _, _ = dual
    host = hosts["ppi"]
    service = host.read_factory()
    null_key = (PPI_PROFILE.series_ids[0], "2026-08")

    class NullSource(Source):
        omit = False

        def fetch(self, series, start, end):
            batch = super().fetch(series, start, end)[0]
            return (
                replace(
                    batch,
                    points=tuple(
                        replace(p, index_value=None)
                        if (p.series_id, p.month) == null_key
                        else p
                        for p in batch.points
                        if not (self.omit and (p.series_id, p.month) == null_key)
                    ),
                ),
            )

    source = NullSource(PPI_PROFILE)
    host.write_factory = lambda **_: CpiTimeSeriesService(
        service.store, source, profile=PPI_PROFILE
    )
    initial = host.submit(command("initialize", start="2026-08"))
    assert initial["state"] == "complete", initial
    assert initial["result"]["coverage_complete"] is True
    local = host.query({"start": "2026-08", "end": "2026-09", "local_only": "true"})
    saved_null = next(
        p
        for p in local["source_unavailable"]
        if (p["series_id"], p["month"]) == null_key
    )
    assert saved_null["available_at"] is not None
    assert saved_null["availability"] == "source_unavailable"

    source.omit = omit_old_null
    receipt = host.submit(command("update", start="2026-08"))
    assert receipt["state"] == "complete", receipt
    result = receipt["result"]
    assert result["coverage_complete"] is (not omit_old_null)
    gaps = [{"series_id": null_key[0], "month": null_key[1]}] if omit_old_null else []
    assert result["source_missing"] == gaps
    assert result["readback"]["source_missing"] == gaps
    assert result["readback"]["verified"] is True
    assert result["readback"]["local_coverage_complete"] is True
    if omit_old_null:
        assert result["completion"] == "source_coverage_partial"


@pytest.mark.parametrize(
    "lost",
    [False, "value", "null", "buffer", "empty", "revision", "confirmation", "upstream"],
)
@pytest.mark.parametrize("action", ["init", "initialize"])
def test_ppi_source_coverage_partial_and_returned_points_required(dual, lost, action):
    _, hosts, _, stores = dual
    host = hosts["ppi"]

    class SparseSource(Source):
        def fetch(self, series, start, end):
            batches = super().fetch(series, start, end)
            batch = batches[0]
            from dataclasses import replace

            return (
                replace(
                    batch,
                    points=tuple(
                        replace(p, index_value=None) if p.month == "2010-01" else p
                        for p in batch.points
                        if p.month >= "2010-01" and lost != "empty"
                    ),
                ),
            )

    source = SparseSource(PPI_PROFILE)
    store = stores["ppi"]
    host.write_factory = lambda **_: CpiTimeSeriesService(
        store, source, profile=PPI_PROFILE
    )
    original = host.read_factory
    if lost in {"value", "null", "buffer", "revision", "confirmation", "upstream"}:
        month = {"value": "2010-02", "null": "2010-01", "buffer": "2025-01"}.get(lost)

        def read():
            service = original()
            query = service.query

            def dropped(*args, **kwargs):
                result = query(*args, **kwargs)
                for key in ("points", "source_unavailable"):
                    result[key] = [p for p in result[key] if p["month"] != month]
                if lost == "revision":
                    result["points"][0]["revision"] += 1
                if lost == "confirmation":
                    result["points"][0]["available_at"] = None
                if lost == "upstream":
                    result["provider_attempts"] = 1
                return result

            service.query = dropped
            return service

        host.read_factory = read
    start = "2026-01" if lost == "buffer" else "2006-01"
    receipt = host.submit(command(action, start=start))
    if lost:
        assert receipt["state"] != "complete", receipt
    else:
        assert receipt["state"] == "complete", receipt
        assert receipt["result"]["coverage_complete"] is False
        assert receipt["result"]["completion"] == "source_coverage_partial"
        assert receipt["result"]["readback"]["missing"]
        assert receipt["result"]["source_missing"]
