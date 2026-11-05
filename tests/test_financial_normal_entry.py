"""Normal catalog/manual entry boundaries; no network or private storage."""

from types import SimpleNamespace

import pytest

from market_data_foundation.historical.financial_daily import DEFINITION
from market_data_foundation.web.cpi_timeseries_host import TimeSeriesRuntime
from market_data_foundation.web.financial_daily_api import FinancialOptions
from market_data_foundation.web.package_catalog import (
    HistoryEvidence,
    PackageHistory,
    ref,
)
from market_data_foundation.web.packages import PackageAccess
from tests.d01_package_fixture import fixture_host
from tests.test_financial_daily import AT


@pytest.mark.parametrize("holding", ["present", "absent", "unknown"])
def test_manual_registration_preserves_holding_and_disabled_strategy(tmp_path, holding):
    host = fixture_host(tmp_path / "configuration")
    host.history = lambda *args: PackageHistory(
        history=[
            HistoryEvidence(
                definition_ref=ref(DEFINITION),
                manual_registered=True,
                holding=holding,
                metadata_view_permission="allowed",
                read_permission="allowed",
                publication_readable=True if holding == "present" else None,
            )
        ]
    )
    before = host.store.path.read_bytes()
    view = host.project(
        "macro", None, PackageAccess(metadata_view_permission="allowed")
    )
    assert view.enabled_capability_ids == []
    asset = next(a for a in view.assets if a.identity == DEFINITION.identity)
    revision = asset.revisions[0]
    assert not revision.capability_enabled
    assert "REGISTERED_MANUAL" in revision.visibility_basis
    assert revision.local_holding == holding.upper()
    assert revision.local_read.allowed is (
        True if holding == "present" else False if holding == "absent" else None
    )
    assert len(revision.source_entries) == 6
    assert {e.source_name for e in revision.source_entries} == {
        "Federal Reserve Board",
        "New York Fed",
        "US Treasury",
        "MDF",
        "Credit archive",
        "FRED",
    }
    assert host.store.path.read_bytes() == before


def test_financial_catalog_history_is_readonly_and_storage_failure_stays_unknown(
    tmp_path,
):
    runtime = TimeSeriesRuntime.__new__(TimeSeriesRuntime)
    runtime.financial_options = FinancialOptions(tmp_path, True, True)
    runtime.profile = SimpleNamespace(connection_kwargs=lambda: {})
    calls = []

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            assert sql.startswith("SELECT EXISTS(")
            return SimpleNamespace(fetchone=lambda: (True,))

        def close(self):
            calls.append("close")

    runtime.connect = lambda **kwargs: Connection()
    evidence = runtime._history("financial_daily", ref(DEFINITION), AT)
    assert evidence.manual_registered and evidence.holding == "present"
    assert evidence.read_permission == "allowed"
    assert calls[-1] == "close" and len(calls) == 2

    def fail(**kwargs):
        raise RuntimeError("controlled_storage_unavailable")

    runtime.connect = fail
    evidence = runtime._history("financial_daily", ref(DEFINITION), AT)
    assert evidence.manual_registered and evidence.holding == "unknown"
    runtime.financial_options = None
    assert not runtime._history(
        "financial_daily", ref(DEFINITION), AT
    ).manual_registered


@pytest.mark.parametrize(
    "periodic,paths,collect,expected",
    [
        (False, True, True, False),
        (True, False, True, False),
        (True, True, False, False),
        (True, True, True, True),
    ],
)
def test_directory_reports_configured_periodic_capability_without_changing_intent(
    tmp_path, monkeypatch, periodic, paths, collect, expected
):
    from market_data_foundation.web.cpi_timeseries_host import TimeSeriesPackages
    from market_data_foundation.web.packages import PackageHost

    host = fixture_host(tmp_path / "config", scenario="financial_manual")
    access = PackageAccess(metadata_view_permission="allowed")
    view = host.project("macro", None, access)
    raw_before = host.store.path.read_bytes()
    monkeypatch.setattr(PackageHost, "project", lambda *a: view)
    runtime = TimeSeriesPackages.__new__(TimeSeriesPackages)
    runtime.timeseries_hosts = {}
    runtime.financial_options = FinancialOptions(
        tmp_path / "jobs",
        True,
        collect,
        tmp_path / "state" if paths else None,
        tmp_path / "window" if paths else None,
        periodic,
    )
    result = runtime.project("macro", None, access)
    revision = next(
        a for a in result.assets if a.identity == DEFINITION.identity
    ).revisions[0]
    assert revision.plan.allowed is expected
    assert not revision.capability_enabled  # package intent is independent
    assert revision.visibility_basis == [
        "REGISTERED_MANUAL",
        "RETAINED_HISTORY",
    ] or set(revision.visibility_basis) == {"REGISTERED_MANUAL", "RETAINED_HISTORY"}
    assert host.store.path.read_bytes() == raw_before
    assert not (tmp_path / "jobs").exists() and not (tmp_path / "state").exists()
