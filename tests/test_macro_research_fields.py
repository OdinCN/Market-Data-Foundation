"""Research expansion through the existing monthly service, jobs and HTTP paths.

All generated source points are synthetic. Retained official qualification is
checked separately via an explicit artifact directory, never copied into Git.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path

import pytest
from fastapi import FastAPI

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_release_schedule import CpiReleaseScheduler
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesService,
    month_number,
    month_range,
)
from market_data_foundation.historical.cpi_timeseries_cli import (
    InterfaceError,
    execute,
    selected_series,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    BlsRangeSource,
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.macro_research_migration import migration_sql
from market_data_foundation.web.cpi_timeseries_api import (
    TimeSeriesHost,
    timeseries_router,
)
from market_data_foundation.web.package_catalog import (
    CredentialState,
    PackageCatalog,
    SaveCommand,
)
from market_data_foundation.web.package_store import LocalPackageStore
from market_data_foundation.web.packages import PackageHost
from tests.test_cpi_timeseries_web import no_network  # noqa: F401

AT = datetime(2026, 10, 31, tzinfo=UTC)
PROFILES = (CPI_PROFILE, PPI_PROFILE, PCE_PROFILE, EMPLOYMENT_PROFILE)


class ResearchSource:
    def __init__(self, profile):
        self.profile, self.at, self.calls, self.overrides = profile, AT, [], {}

    def fetch(self, series, start, end):
        self.calls.append((series, start, end))
        points = []
        for sid in series:
            for month in month_range(start, end):
                n = Decimal(month_number(month) - month_number("2025-09"))
                unit = self.profile.series_metadata[sid].get("value_unit")
                value = (
                    Decimal("100000") + n * 17
                    if unit == "thousand_jobs"
                    else Decimal("60") + n / 10
                    if unit == "percent"
                    else Decimal("100.000123456789") + n / 3
                )
                points.append(
                    SourcePoint(
                        sid,
                        month,
                        self.overrides.get((sid, month), value),
                        {},
                        ({"code": "P", "text": "Preliminary"},)
                        if self.profile is PPI_PROFILE
                        else (),
                    )
                )
        raw = json.dumps(
            [(p.series_id, p.month, str(p.index_value)) for p in points]
        ).encode()
        return (
            SourceBatch(tuple(points), raw, {"seriesid": list(series)}, self.at, 1),
        )


def core(profile):
    source = ResearchSource(profile)
    store = InMemoryTimeSeriesStore(profile=profile, clock=lambda: source.at)
    return CpiTimeSeriesService(store, source, profile=profile), source, store


def make_host(profile, directory):
    directory.mkdir(parents=True, exist_ok=True)
    packages_store = LocalPackageStore.initialize(
        directory / "packages", PackageCatalog()
    )
    packages_store.save(
        "test",
        "macro",
        SaveCommand(expected_revision=None, enabled_capability_ids=[profile.key]),
    )
    packages = PackageHost(
        packages_store,
        "test",
        credentials=lambda *_: [
            CredentialState(source_id=profile.provider, credential_state="configured")
        ],
        credential_loader=lambda *_: "synthetic",
    )
    service, source, store = core(profile)
    host = TimeSeriesHost(
        profile=profile,
        packages=packages,
        read_service_factory=lambda: service,
        write_service_factory=lambda **_: service,
        query_service_factory=lambda **_: service,
        query_allowed=True,
        allow_collect=True,
        scheduler=CpiReleaseScheduler(
            None,
            directory / "schedule.json",
            profile=profile,
            history_start="2025-09",
            clock=lambda: AT,
        ),
        job_directory=directory / "jobs",
        clock=lambda: AT,
    )
    return host, source, store


@pytest.mark.parametrize(
    "profile,count", [(CPI_PROFILE, 14), (PPI_PROFILE, 6), (EMPLOYMENT_PROFILE, 18)]
)
def test_all_profile_inputs_flow_through_manual_job_readback_and_execution(
    tmp_path, profile, count
):
    host, source, store = make_host(profile, tmp_path)
    result = host.submit(
        {
            "request_id": "12345678-1234-4234-8234-123456789abc",
            "action": "update",
            "start": "2026-09",
            "end": "2026-09",
        }
    )
    assert result["state"] == "complete"
    assert source.calls[0] == (profile.series_ids, "2025-09", "2026-09")
    assert store.status()["observation_count"] == count * 13
    assert len(host.executions()["executions"]) == 1
    assert host.scheduler.series_ids == profile.series_ids
    # Backfill the newly admitted history without reinitializing old series.
    new_id = {
        "cpi": "CUSR0000SAH1",
        "ppi": "WPSFD49116",
        "employment": "CES0500000001",
    }[profile.key]
    host.submit(
        {
            "request_id": "12345678-1234-4234-8234-123456789abd",
            "action": "init",
            "start": "2025-08",
            "end": "2025-08",
            "series": new_id,
        }
    )
    assert source.calls[-1][0] == (new_id,)


def test_cpi_explicit_metadata_multi_baskets_and_old_query_defaults():
    metadata = CPI_PROFILE.series_metadata
    assert metadata["CUUR0000SAH1"]["indicator"] == "shelter"
    assert metadata["CUUR0000SAH1"]["seasonal_adjustment"] == "NSA"
    assert metadata["CUSR0000SACL1E"]["indicator"] == "core_goods"
    assert metadata["CUSR0000SASLE"]["start_month"] is None
    assert metadata["CUSR0000SASLE"]["index_base"] is None
    assert metadata["CUSR0000SASLE"]["evidence_start_month"] == "2026-01"
    assert selected_series({}, query=True) == ("CUSR0000SA0",)
    assert selected_series(
        {"basket": "shelter,energy", "adjustment": "nsa"}, query=True
    ) == ("CUUR0000SAH1", "CUUR0000SA0E")
    service, source, _ = core(CPI_PROFILE)
    result = execute(
        service,
        "query",
        {
            "start": "2026-09",
            "end": "2026-09",
            "basket": "shelter,core_goods",
            "adjustment": "all",
        },
    )
    assert len(result["points"]) == 4 and len(source.calls) == 1
    assert {p["seasonal_adjustment"] for p in result["points"]} == {"SA", "NSA"}
    assert len({p["series_id"] for p in result["points"]}) == 4


def test_ppi_third_basket_status_comes_only_from_provider_footnotes():
    service, source, _ = core(PPI_PROFILE)
    result = execute(
        service,
        "update",
        {"start": "2026-09", "end": "2026-09", "basket": "core_ex_trade"},
    )
    assert {p["series_id"] for p in result["points"]} == {"WPSFD49116", "WPUFD49116"}
    assert all(
        p["revision"] == 1 and p["provider_status"]["preliminary"] is True
        for p in result["points"]
    )
    source.at += timedelta(days=1)
    source.overrides["WPSFD49116", "2026-09"] = Decimal("105")
    revised = execute(
        service,
        "update",
        {"start": "2026-09", "end": "2026-09", "basket": "core_ex_trade"},
    )["points"][0]
    assert revised["revision"] == 2 and revised["provider_status"]["revised"] is None
    from market_data_foundation.historical.cpi_timeseries import _provider_status

    assert _provider_status([{}])["preliminary"] is None
    assert _provider_status([{"code": "R", "text": "Revised"}])["revised"] is True
    assert selected_series({"basket": "core"}, query=True, profile=PPI_PROFILE) == (
        "WPSFD49104",
    )


def test_pce_annualized_unrounded_endpoints_same_cutoff_and_null_bases():
    service, source, _ = core(PCE_PROFILE)
    initial = service.init_range("2026-09", "2026-09", series_ids=("DPCERG",))
    point = initial["points"][0]
    with localcontext() as context:
        context.prec = 60
        for lag, exponent in ((3, 4), (6, 2)):
            field = f"annualized_{lag}m_ratio"
            base = Decimal(
                point["calculation_inputs"][field]["denominator_index_value"]
            )
            expected = (
                (Decimal(point["index_value"]) / base) ** exponent - 1
            ).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
            assert Decimal(point[field]) == expected
            assert point[field] != point[f"change_{lag}m_ratio"]
    source.at += timedelta(days=1)
    source.overrides["DPCERG", "2026-06"] = Decimal("90")
    service.update_range("2026-09", "2026-09", series_ids=("DPCERG",))
    before = service.query_range(
        "2026-09", "2026-09", series_ids=("DPCERG",), as_of=AT, as_of_basis="available"
    )["points"][0]
    assert before["annualized_3m_ratio"] == point["annualized_3m_ratio"]
    assert (
        before["calculation_inputs"]["annualized_3m_ratio"]["denominator_revision"] == 1
    )
    source.at += timedelta(days=1)
    source.overrides["DPCERG", "2026-06"] = None
    gap = service.update_range("2026-09", "2026-09", series_ids=("DPCERG",))
    assert gap["points"][0]["annualized_3m_ratio"] is None
    assert any(
        g["metric"] == "annualized_3m_ratio" and g["required_month"] == "2026-06"
        for g in gap["calculation_missing"]
    )
    assert (
        execute(
            service,
            "query",
            {
                "start": "2026-09",
                "end": "2026-09",
                "local_only": True,
                "measure": "annualized_6m_ratio",
            },
        )["unit"]
        == "ratio"
    )
    empty, _, _ = core(PCE_PROFILE)
    missing = empty.query_range("2026-09", "2026-09", allow_upstream=False)
    assert not missing["points"] and len(missing["missing"]) == 2


def test_employment_new_series_units_changes_and_legacy_default():
    service, _, _ = core(EMPLOYMENT_PROFILE)
    result = service.init_range("2026-09", "2026-09")
    assert len(result["points"]) == 18
    for p in result["points"]:
        if p["value_unit"] == "thousand_jobs":
            assert p["net_change_1m_thousand_jobs"] == "17"
            assert "mom_pct" not in p
        if p["series_id"] == "LNS12300000":
            assert p["change_1m_percentage_points"] == "0.1" and p["survey"] == "CPS"
    sectors = [
        m
        for m in EMPLOYMENT_PROFILE.series_metadata.values()
        if m.get("aggregation_role") == "major_sector"
    ]
    assert len(sectors) == 11  # excludes private aggregate and nested industries
    old = execute(
        service, "query", {"start": "2026-09", "end": "2026-09", "local_only": True}
    )
    assert tuple(old["series_ids"]) == EMPLOYMENT_PROFILE.default_query_series_ids
    selected = execute(
        service,
        "query",
        {
            "start": "2026-09",
            "end": "2026-09",
            "series": "CES0500000001,LNS12300000",
            "columns": "value,net_change_1m_thousand_jobs",
            "local_only": True,
        },
    )
    assert len(selected["points"]) == 2
    assert "change_1m_percentage_points" not in selected["points"][1]
    with pytest.raises(InterfaceError):
        execute(
            service,
            "query",
            {"start": "2026-09", "end": "2026-09", "columns": "unknown"},
        )


@pytest.mark.parametrize("profile", [CPI_PROFILE, PPI_PROFILE, EMPLOYMENT_PROFILE])
def test_expanded_bls_inputs_stay_in_bounded_source_windows(profile):
    from tests.test_cpi_timeseries_review import SyntheticYearsTransport

    wire = SyntheticYearsTransport()
    src = BlsRangeSource(
        profile=profile, transport=wire, credential_loader=lambda: "ab" * 16
    )
    batches = src.fetch(profile.series_ids, "2005-09", "2026-09")
    assert len(wire.requests) == len(batches) == 2
    assert all(r["seriesid"] == list(profile.series_ids) for r in wire.requests)
    assert all(int(r["endyear"]) - int(r["startyear"]) < 20 for r in wire.requests)


def test_additive_migration_matches_reviewable_sql_and_preserves_columns():
    sql = migration_sql()
    assert Path("scripts/sql/macro-research-fields-r1.sql").read_text() == sql
    assert all(
        token not in sql
        for token in ("DROP TABLE", "DROP VIEW", "TRUNCATE", "DELETE FROM", "COMMIT")
    )
    assert "annualized_3m_ratio" in sql and "ex_auto_change_12m_ratio" in sql
    for profile in (CPI_PROFILE, PPI_PROFILE, EMPLOYMENT_PROFILE):
        assert all(sid in sql for sid in profile.series_ids)
    assert sql.index("AS change_1m_hours") < sql.index("AS total_private_thousand_jobs")


def test_retail_ex_auto_same_cutoff_derivation_input_versions_and_no_extra_fetch():
    from market_data_foundation.historical.retail_timeseries_store import (
        InMemoryRetailStore,
    )
    from tests.test_retail_timeseries_backend import (
        AT as RETAIL_AT,
    )
    from tests.test_retail_timeseries_backend import (
        batch,
        payload,
        service,
        target,
    )

    store = InMemoryRetailStore(clock=lambda: RETAIL_AT + timedelta(days=1))
    store.save_batch("one", batch())
    svc = service(store)
    old = svc.query_range("2026-09", "2026-09", as_of=RETAIL_AT + timedelta(days=1))
    point = old["points"][0]
    assert point["ex_auto_level"]["value"] == point["headline_level"]["value"] - 10
    assert len(point["ex_auto_rates"][12]["inputs"]) == 4
    derivation = point["derivations"]["ex_auto"]
    assert derivation["formula"] == "SM44X72-SM441:v1"
    assert derivation["provider_official"] is False
    assert {v["local_revision"] for v in derivation["input_versions"].values()} == {1}
    assert {key.split("/")[1] for key in derivation["input_versions"]} == {
        "44X72",
        "441",
    }
    table = payload("2025-09", "2026-09")
    target(table, "2026-08", "441")[table[0].index("cell_value")] = "500"
    store.clock = lambda: RETAIL_AT + timedelta(days=4)
    store.save_batch("two", batch(table, observed=RETAIL_AT + timedelta(days=3)))
    same = svc.query_range("2026-09", "2026-09", as_of=RETAIL_AT + timedelta(days=1))[
        "points"
    ][0]
    assert same["ex_auto_rates"] == point["ex_auto_rates"]
    assert (
        svc.query_range("2026-09", "2026-09", local_only=True)["points"][0][
            "ex_auto_rates"
        ][1]
        != point["ex_auto_rates"][1]
    )
    live = service()
    projected = live.query_range("2026-09", "2026-09")
    assert (
        projected["provider_requests"] == 1 and len(live.source._transport.calls) == 1
    )


@pytest.mark.parametrize("profile", PROFILES)
def test_wire_research_selection_columns_and_help(tmp_path, profile):
    from tests.test_ppi_timeseries_web import client

    host, _, _ = make_host(profile, tmp_path)
    host.submit(
        {
            "request_id": "12345678-1234-4234-8234-123456789abc",
            "action": "update",
            "start": "2026-09",
            "end": "2026-09",
        }
    )
    app = FastAPI()
    app.include_router(timeseries_router(host))
    with client(app) as http:
        help_doc = http.get(host.prefix + "/api-help").json()
        assert set(help_doc["series_ids"]) == set(profile.series_ids)
        params = {"start": "2026-09", "end": "2026-09", "local_only": "true"}
        if profile is EMPLOYMENT_PROFILE:
            params.update(series="CES0500000001,LNS12300000", columns="value")
        else:
            params.update(
                basket="shelter,energy"
                if profile is CPI_PROFILE
                else "core_ex_trade"
                if profile is PPI_PROFILE
                else "all",
                columns="index_value",
            )
        response = http.get(host.prefix + "/query", params=params)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["selected_columns"] == params["columns"].split(",")
        expected_count = (
            2 if profile in (CPI_PROFILE, EMPLOYMENT_PROFILE, PCE_PROFILE) else 1
        )
        assert len(result["points"]) == expected_count
        assert all("change_3m_ratio" not in p for p in result["points"])
        assert result["provider_requests"] == 0
        if profile is not PCE_PROFILE:
            job = http.post(
                host.prefix + "/jobs",
                headers={"Origin": "http://192.168.10.20:18761"},
                json={
                    "request_id": "12345678-1234-4234-8234-123456789abe",
                    "action": "init",
                    "start": "2025-08",
                    "end": "2025-08",
                    "series": params.get("series", result["series_ids"][0]),
                },
            )
            assert job.status_code in {200, 202}, job.text
            assert job.json()["series"] == params.get("series", result["series_ids"][0])


def test_annualization_does_not_use_rounded_cumulative_change():
    service, source, _ = core(PCE_PROFILE)
    source.overrides.update(
        {
            ("DPCERG", "2026-09"): Decimal("100.9975"),
            ("DPCERG", "2026-06"): Decimal("100"),
        }
    )
    point = service.query_range("2026-09", "2026-09", series_ids=("DPCERG",))["points"][
        0
    ]
    assert point["change_3m_ratio"] == "0.0100"
    assert point["annualized_3m_ratio"] == "0.0405"
    assert ((1 + Decimal(point["change_3m_ratio"])) ** 4 - 1).quantize(
        Decimal("0.0001")
    ) == Decimal("0.0406")


@pytest.mark.parametrize("marker", [None, "(S)", "absent"])
def test_ex_auto_missing_operands_and_comparison_gap_are_null(marker):
    from tests.test_retail_timeseries_backend import batch, payload, service, target

    table = payload("2025-09", "2026-09")
    row = target(table, "2026-08", "441")
    if marker == "absent":
        table.remove(row)
    else:
        row[table[0].index("cell_value")] = marker
    svc = service()
    svc.store.save_batch("synthetic", batch(table))
    point = svc.query_range("2026-09", "2026-09", local_only=True)["points"][0]
    assert point["ex_auto_level"]["value"] is not None
    assert point["ex_auto_rates"][1]["value"] is None
    assert point["ex_auto_rates"][1]["reason"] == "comparison_level_unavailable"
    assert point["headline_rates"][1]["value"] is not None


def test_query_openapi_parameters_match_actual_admitted_filters():
    from market_data_foundation.web.macro_query_contract import contract_app
    from market_data_foundation.web.retail_timeseries import create_app as retail_app

    for app in (contract_app(), retail_app()):
        for path, operations in app.openapi()["paths"].items():
            if path.endswith("/query"):
                fields = {p["name"] for p in operations["get"]["parameters"]}
                assert {"start", "end", "columns", "local_only", "as_of"} <= fields
                assert ("series" in fields) == ("employment" in path)
                assert ("basket" in fields) == ("employment" not in path)
    assert (
        json.loads(Path("web/src/macro-timeseries.openapi.json").read_text())
        == contract_app().openapi()
    )


def test_retained_bls_research_qualification_without_provider_access():
    import os

    directory = os.environ.get("MDF_RESEARCH_RETAINED_DIR")
    if not directory:
        pytest.skip("explicit retained artifact directory required")
    from market_data_foundation.historical.cpi_timeseries_source import parse_response

    document = json.loads(
        (Path(directory) / "bls-research-series-2026.json").read_bytes()
    )
    qualification = json.loads(
        (Path(directory) / "bls-research-series-qualification.json").read_bytes()
    )
    assert qualification["attempts"] == 1 and len(qualification["series_ids"]) == 25
    for profile in (CPI_PROFILE, PPI_PROFILE, EMPLOYMENT_PROFILE):
        rows = [
            s
            for s in document["Results"]["series"]
            if s["seriesID"] in profile.series_ids
        ]
        ids = tuple(s["seriesID"] for s in rows)
        projection = {**document, "Results": {"series": rows}}
        parsed = parse_response(
            json.dumps(projection).encode(), ids, "2026-01", "2026-09", profile=profile
        )
        assert len(parsed) == 9 * len(ids)
        for row in rows:
            metadata = profile.series_metadata[row["seriesID"]]
            assert row["catalog"]["seasonality"] == (
                "Seasonally Adjusted"
                if metadata["seasonal_adjustment"] == "SA"
                else "Not Seasonally Adjusted"
            )
        if profile is PPI_PROFILE:
            assert all(p.footnotes for p in parsed)


def test_wire_retail_basket_columns_default_and_derived_provenance():
    from market_data_foundation.web.retail_timeseries import (
        PREFIX,
        RetailTimeSeriesHost,
        create_app,
    )
    from tests.test_ppi_timeseries_web import client
    from tests.test_retail_timeseries_backend import batch, service

    svc = service()
    svc.store.save_batch("synthetic", batch())
    app = create_app(
        RetailTimeSeriesHost(svc, query_allowed=True), authorize=lambda _: True
    )
    with client(app) as http:
        path = PREFIX + "/query?start=2026-09&end=2026-09&local_only=true"
        default = http.get(path).json()
        assert (
            "headline_level" in default["points"][0]
            and "control_level" in default["points"][0]
        )
        assert "ex_auto_level" not in default["points"][0]
        result = http.get(path + "&basket=ex_auto&columns=index_value,change_1m_ratio")
        assert result.status_code == 200, result.text
        point = result.json()["points"][0]
        assert "headline_level" not in point and "control_level" not in point
        assert set(point["ex_auto_rates"]) == {"1"}
        assert point["derivations"]["ex_auto"]["identity"] == "mdf_retail_ex_auto_v1"
        assert result.json()["provider_requests"] == 0
        help_doc = http.get(PREFIX + "/api-help").json()
        assert "ex_auto" in help_doc["baskets"] and "columns" in help_doc["parameters"]


def test_cpi_sa_and_nsa_never_share_comparison_inputs():
    service, source, _ = core(CPI_PROFILE)
    source.overrides.update(
        {
            ("CUSR0000SAH1", "2026-09"): Decimal("120"),
            ("CUSR0000SAH1", "2025-09"): Decimal("100"),
            ("CUUR0000SAH1", "2026-09"): Decimal("140"),
            ("CUUR0000SAH1", "2025-09"): Decimal("50"),
        }
    )
    result = execute(
        service,
        "query",
        {
            "start": "2026-09",
            "end": "2026-09",
            "basket": "shelter",
            "adjustment": "all",
        },
    )
    assert [p["change_12m_ratio"] for p in result["points"]] == ["0.2000", "1.8000"]


def test_research_migration_targets_real_retail_schema_explicitly():
    from market_data_foundation.historical.macro_research_migration import (
        migration_sql,
    )

    sql = migration_sql()
    assert '"mdf_retail_timeseries_dev_r1".retail_timeseries_rates' in sql
    assert "retail_timeseries_candidate" not in sql
    custom = migration_sql(retail_schema="research_test_retail")
    assert '"research_test_retail".retail_timeseries_rates' in custom
    assert '"mdf_retail_timeseries_dev_r1".retail_timeseries_rates' not in custom
