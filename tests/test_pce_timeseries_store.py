"""PCE namespace and SQL whitelist contracts without a real database."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from market_data_foundation.historical import cpi_timeseries as shared
from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
    validate_profile,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    _latest_view_sql,
    _rates_view_sql,
    _revision_ddl,
    validate_evidence_url,
)
from market_data_foundation.historical.pce_timeseries_source import BeaRangeSource
from market_data_foundation.historical.pce_timeseries_store import (
    InMemoryTimeSeriesStore,
    PceStorageError,
    PostgresTimeSeriesStore,
    raw_locator,
)


def test_three_allowlisted_profiles_and_distinct_namespaces():
    profiles = (CPI_PROFILE, PPI_PROFILE, PCE_PROFILE)
    for attr in ("schema", "raw_prefix", "lock_id", "rates_view"):
        assert len({getattr(p, attr) for p in profiles}) == 3
    with pytest.raises(ValueError):
        validate_profile(replace(PCE_PROFILE, schema="unsafe"))
    sql = _revision_ddl(profile=PCE_PROFILE)
    assert "'DPCERG','DPCCRG'" in sql and "CUSR" not in sql and "WPSFD" not in sql
    assert "mdf_pce_timeseries_dev_r1.latest" in _latest_view_sql(profile=PCE_PROFILE)
    assert "pce_timeseries_rates" in _rates_view_sql(profile=PCE_PROFILE)
    assert "pce-timeseries/dev-r1/sha256/" in raw_locator("a" * 64)


def test_raw_port_profile_mismatch_fails_before_io():
    raw = SimpleNamespace(profile=CPI_PROFILE)
    with pytest.raises(PceStorageError, match="profile_mismatch"):
        InMemoryTimeSeriesStore(raw)
    with pytest.raises(PceStorageError, match="profile_mismatch"):
        PostgresTimeSeriesStore(SimpleNamespace(), raw)


def test_bea_evidence_hosts_are_profile_specific():
    bea = "https://www.bea.gov/news/2026/synthetic-notice"
    validate_evidence_url(bea, profile=PCE_PROFILE)
    with pytest.raises(PceStorageError):
        validate_evidence_url(bea)
    with pytest.raises(PceStorageError):
        validate_evidence_url("https://www.bls.gov/notice/test", profile=PCE_PROFILE)


def test_shared_builder_assembles_bea_and_has_no_ddl(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime.toml"
    runtime.write_text(
        f'[storage]\nbackend="s3"\n[s3]\nendpoint_url="{shared.GARAGE_ENDPOINT}"\nbucket="{shared.BUCKET}"\nregion="garage"\nkey_prefix=""\naddressing_style="path"\n'
    )
    secret = tmp_path / "synthetic.toml"
    secret.write_text(
        '[postgres]\npassword="synthetic"\n[s3]\naccess_key_id="synthetic"\nsecret_access_key="synthetic"\n'
    )
    connection = SimpleNamespace(close=lambda: None, autocommit=True)
    monkeypatch.setattr(shared, "connect_dev", lambda *a, **kw: connection)
    service = shared.build_service(
        profile=PCE_PROFILE,
        runtime_path=runtime,
        secret_path=secret,
        credential_loader=lambda: pytest.fail("build loaded BEA key"),
    )
    assert isinstance(service.source, BeaRangeSource)
    assert all(
        c.profile is PCE_PROFILE
        for c in (service, service.source, service.store, service.store.raw_store)
    )
    assert service.store.connection is connection
