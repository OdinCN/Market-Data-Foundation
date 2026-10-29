"""Offline PPI reuse, asset isolation and availability regression contracts."""

import json
from contextlib import nullcontext
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from market_data_foundation.historical import cpi_timeseries as core
from market_data_foundation.historical import ppi_timeseries as ppi
from market_data_foundation.historical import ppi_timeseries_source as ppi_source
from market_data_foundation.historical import ppi_timeseries_store as ppi_store
from market_data_foundation.historical.bls_cpi_direct_live import HttpResult
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_request_control import CpiRequestControl
from market_data_foundation.historical.cpi_timeseries_source import (
    BlsRangeSource,
    CpiSourceError,
    SourceBatch,
    SourcePoint,
    parse_response,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    CpiStorageError,
    GarageCpiRawStore,
    InMemoryTimeSeriesStore,
    PostgresTimeSeriesStore,
    _latest_view_sql,
    _rates_view_sql,
    _relational_content,
    _revision_ddl,
    digest,
    raw_locator,
)

AT = datetime(2026, 10, 29, tzinfo=UTC)
NOTICE = "https://www.bls.gov/errata/2026/test-notice.htm"
SID = PPI_PROFILE.series_ids[0]


def raw_response(series_ids, months, values=None):
    values = values or {}
    return json.dumps(
        {
            "status": "REQUEST_SUCCEEDED",
            "Results": {
                "series": [
                    {
                        "seriesID": series,
                        "data": [
                            {
                                "year": month[:4],
                                "period": "M" + month[-2:],
                                "value": values.get(month, "100"),
                                "calculations": {"pct_changes": {"1": "9.9"}},
                                "footnotes": [{"code": "P", "text": "Preliminary"}],
                            }
                            for month in months
                        ],
                    }
                    for series in series_ids
                ]
            },
        }
    ).encode()


def batch(values, *, series=SID, at=AT):
    raw = raw_response((series,), values, values)
    points = parse_response(
        raw, (series,), min(values), max(values), profile=PPI_PROFILE
    )
    return SourceBatch(points, raw, {"seriesid": [series]}, at, 1)


class OfflineTransport:
    def __init__(self):
        self.calls = []
        self.increment = 0
        self.absent = set()

    def post(self, endpoint, body, headers, **kwargs):
        request = json.loads(body)
        self.calls.append(request)
        months = core.month_range(
            request["startyear"] + "-01", request["endyear"] + "-12"
        )
        values = {
            month: "-"
            if month in self.absent
            else str(
                100
                + core.month_number(month)
                - core.month_number("2000-01")
                + self.increment
            )
            for month in months
        }
        return HttpResult(200, raw_response(request["seriesid"], months, values), {})


def fixture_service(profile=PPI_PROFILE, *, now=None):
    now = now or [AT]
    transport = OfflineTransport()
    source = BlsRangeSource(
        profile=profile, registered=False, transport=transport, clock=lambda: now[0]
    )
    store = InMemoryTimeSeriesStore(profile=profile, clock=lambda: now[0])
    return core.CpiTimeSeriesService(store, source, profile=profile), transport, store


def test_profiles_are_frozen_and_core_is_food_energy_exclusion():
    with pytest.raises(FrozenInstanceError):
        PPI_PROFILE.schema = "other"
    assert PPI_PROFILE.series_ids == ("WPSFD4", "WPUFD4", "WPSFD49104", "WPUFD49104")
    assert PPI_PROFILE.earliest_month is None
    assert "foods and energy" in PPI_PROFILE.label
    assert PPI_PROFILE.series_metadata["WPSFD49104"] == {
        "indicator": "core",
        "seasonal_adjustment": "SA",
    }


def test_ppi_four_series_parser_retains_null_footnotes_and_provider_values():
    raw = raw_response(PPI_PROFILE.series_ids, ("2026-09",), {"2026-09": "-"})
    points = parse_response(
        raw, PPI_PROFILE.series_ids, "2026-09", "2026-09", profile=PPI_PROFILE
    )
    assert len(points) == 4
    assert all(p.index_value is None for p in points)
    assert points[0].footnotes == ({"code": "P", "text": "Preliminary"},)
    assert points[0].provider_calculations == {"pct_changes": {"1": "9.9"}}
    assert (
        ppi_source.parse_response(raw, ppi.SERIES_IDS, "2026-09", "2026-09") == points
    )
    assert (
        ppi_source.SourcePoint is SourcePoint and ppi_source.SourceBatch is SourceBatch
    )


@pytest.mark.parametrize(
    "profile,other", [(PPI_PROFILE, CPI_PROFILE), (CPI_PROFILE, PPI_PROFILE)]
)
def test_parser_and_client_reject_cross_asset_requests_and_responses(profile, other):
    raw = raw_response(other.series_ids, ("2026-09",))
    with pytest.raises(CpiSourceError, match="series_invalid"):
        parse_response(raw, other.series_ids, "2026-09", "2026-09", profile=profile)
    with pytest.raises(CpiSourceError, match="series_response_invalid"):
        parse_response(raw, profile.series_ids, "2026-09", "2026-09", profile=profile)
    transport = OfflineTransport()
    client = BlsRangeSource(profile=profile, registered=False, transport=transport)
    with pytest.raises(CpiSourceError, match="series_invalid"):
        client.fetch(other.series_ids, "2026-09", "2026-09")
    assert not transport.calls


@pytest.mark.parametrize("profile", [CPI_PROFILE, PPI_PROFILE])
def test_profile_validation_blocks_custom_sql_identifiers_before_io(profile):
    unsafe = replace(profile, schema='bad"; DROP SCHEMA public; --')
    for operation in (
        lambda: _revision_ddl(profile=unsafe),
        lambda: _latest_view_sql(profile=unsafe),
        lambda: _rates_view_sql(profile=unsafe),
        lambda: raw_locator("sha", profile=unsafe),
        lambda: BlsRangeSource(profile=unsafe),
        lambda: InMemoryTimeSeriesStore(profile=unsafe),
        lambda: PostgresTimeSeriesStore(None, None, profile=unsafe),
        lambda: GarageCpiRawStore(None, profile=unsafe),
        lambda: core.CpiTimeSeriesService(None, None, profile=unsafe),
        lambda: core.build_service(profile=unsafe),
        lambda: core.build_source(credential_loader=lambda: "a" * 32, profile=unsafe),
    ):
        with pytest.raises(ValueError, match="bls_timeseries_profile_invalid"):
            operation()


def test_default_cpi_and_ppi_in_same_process_do_not_change_each_other():
    cpi_service, cpi_wire, cpi_memory = fixture_service(CPI_PROFILE)
    now = [AT]
    ppi_service, ppi_wire, ppi_memory = fixture_service(now=now)
    assert (
        core.CpiTimeSeriesService(cpi_memory, cpi_service.source).profile is CPI_PROFILE
    )
    for service, profile in ((cpi_service, CPI_PROFILE), (ppi_service, PPI_PROFILE)):
        result = service.init_range("2026-09", "2026-09")
        assert result["series_ids"] == list(profile.series_ids)
        assert len(result["points"]) == 4
        assert all(profile.raw_prefix in p["raw_locator"] for p in result["points"])
        assert service.status()["schema"] == profile.schema
        repeated = service.init_range("2026-09", "2026-09")
        assert repeated["provider_requests"] == 0 and repeated["complete"]
        assert repeated["source_returned_series_months"] == []
    now[0] += timedelta(days=1)
    ppi_service.withdraw_range("2026-09", "2026-09", evidence_url=NOTICE)
    assert len(ppi_service.query_range("2026-09", "2026-09")["withdrawn"]) == 4
    assert cpi_service.query_range("2026-09", "2026-09")["complete"]
    assert len(cpi_wire.calls) == len(ppi_wire.calls) == 1
    assert cpi_memory.status()["revision_count"] == 0
    assert ppi_memory.status()["revision_count"] == 4
    for store, other in ((cpi_memory, PPI_PROFILE), (ppi_memory, CPI_PROFILE)):
        with pytest.raises(CpiStorageError, match="series_invalid"):
            store.read_range(other.series_ids, "2026-09", "2026-09")
        with pytest.raises(CpiStorageError, match="series_invalid"):
            store.withdraw_range(
                other.series_ids, "2026-09", "2026-09", evidence_url=NOTICE
            )
        with pytest.raises(CpiStorageError, match="series_invalid"):
            store.confirm_current_availability(other.series_ids, "2026-09", "2026-09")
    with pytest.raises(core.CpiTimeSeriesError, match="series_invalid"):
        ppi_service.query_range("2026-09", "2026-09", series_ids=CPI_PROFILE.series_ids)
    with pytest.raises(core.CpiTimeSeriesError, match="profile_mismatch"):
        core.CpiTimeSeriesService(cpi_memory, ppi_service.source, profile=PPI_PROFILE)


def test_ppi_multibatch_ratios_and_shared_twelve_month_buffer():
    service, wire, store = fixture_service()
    result = service.init_range("2015-01", "2026-01")
    assert len(wire.calls) == 2  # unregistered 2014..2023 and 2024..2026
    assert wire.calls[0]["startyear"] == "2014"
    assert not result["calculation_missing"]
    assert store.status()["batch_count"] == 2
    point = next(
        p for p in result["points"] if p["series_id"] == SID and p["month"] == "2024-01"
    )
    assert [point[f"change_{k}m_ratio"] for k in (1, 3, 6, 12)] == [
        "0.0026",
        "0.0078",
        "0.0157",
        "0.0319",
    ]
    assert point["provider_calculations"] == {"pct_changes": {"1": "9.9"}}
    assert (
        point["calculation_inputs"]["change_12m_ratio"]["denominator_month"]
        == "2023-01"
    )
    local = service.query_range("2024-01", "2024-01")
    assert local["provider_requests"] == 0
    assert local["source_returned_series_months"] == []


@pytest.mark.parametrize("operation", ["init_range", "update_range", "query_range"])
def test_ppi_source_returned_keys_include_null_targets_and_calculation_buffer(
    operation,
):
    service, wire, _ = fixture_service()
    wire.absent = {"2025-09", "2026-09"}
    result = getattr(service, operation)("2026-09", "2026-09")
    returned = result["source_returned_series_months"]
    assert len(returned) == 4 * 13
    for series in PPI_PROFILE.series_ids:
        assert {"series_id": series, "month": "2025-09"} in returned
        assert {"series_id": series, "month": "2026-09"} in returned
    assert not result["complete"] and result["coverage_complete"]
    if operation == "update_range":
        assert not result["refresh_complete"]
        assert {"series_id": SID, "month": "2026-09"} not in result[
            "refreshed_series_months"
        ]
    if operation == "init_range":
        repeated = service.init_range("2026-09", "2026-09")
        assert repeated["source_returned_series_months"] == []
        assert repeated["provider_requests"] == 0 and len(wire.calls) == 1
        assert not repeated["complete"] and repeated["coverage_complete"]


@pytest.mark.parametrize("operation", ["init_range", "update_range"])
def test_ppi_source_returned_keys_survive_store_readback_row_loss(
    operation, monkeypatch
):
    service, _, store = fixture_service()
    read_range = store.read_range
    monkeypatch.setattr(
        store,
        "read_range",
        lambda *args, **kwargs: [
            p for p in read_range(*args, **kwargs) if p["month"] != "2026-09"
        ],
    )
    result = getattr(service, operation)("2026-09", "2026-09")
    assert not result["complete"] and not result["coverage_complete"]
    assert not result["points"]
    assert len(result["source_returned_series_months"]) == 4 * 13
    for series in PPI_PROFILE.series_ids:
        assert {"series_id": series, "month": "2026-09"} in result[
            "source_returned_series_months"
        ]


def test_ppi_source_returned_keys_are_sorted_deduplicated_and_fetch_scoped():
    service, _, _ = fixture_service()
    original = batch({"2026-09": "100"})
    point = original.points[0]
    other = PPI_PROFILE.series_ids[1]
    first = replace(
        original,
        request={"seriesid": [other, SID]},
        points=(
            replace(point, series_id=other),
            point,
            replace(point, month="2025-09", index_value=None),
            replace(point, month="2026-10"),
        ),
    )
    service.source = SimpleNamespace(fetch=lambda *args: (first, original))
    result = service.query_range("2026-09", "2026-09", series_ids=(other, SID))
    assert result["source_returned_series_months"] == [
        {"series_id": SID, "month": "2025-09"},
        {"series_id": SID, "month": "2026-09"},
        {"series_id": other, "month": "2026-09"},
    ]


@pytest.mark.parametrize("operation", ["init_range", "update_range", "query_range"])
def test_ppi_empty_source_response_does_not_complete_missing_coverage(operation):
    service, _, _ = fixture_service()
    empty = replace(batch({"2026-09": "100"}), points=())
    service.source = SimpleNamespace(fetch=lambda *args: (empty,))
    result = getattr(service, operation)("2026-09", "2026-09")
    assert result["source_returned_series_months"] == []
    assert not result["complete"] and not result["coverage_complete"]
    assert result["provider_requests"] == 1


def test_ppi_gap_fill_is_ephemeral_and_null_metadata_survives_revision():
    now = [AT]
    service, wire, store = fixture_service(now=now)
    wire.absent = {"2026-08"}
    result = service.init_range("2026-09", "2026-09")
    assert result["coverage_complete"] and result["complete"]
    assert all(p["change_1m_ratio"] is None for p in result["points"])
    saved_count = store.status()["observation_count"]
    wire.absent.clear()
    now[0] += timedelta(days=1)
    result = service.update_range("2026-09", "2026-09")
    assert result["saved_revisions"] == 4
    old = store.read_range(PPI_PROFILE.series_ids, "2026-08", "2026-08", as_of=AT)
    assert all(
        p["index_value"] is None and p["footnotes"][0]["code"] == "P" for p in old
    )
    assert all(p["change_1m_ratio"] is not None for p in result["points"])
    temporary = service.query_range("2026-10", "2026-10")
    assert all(p["raw_locator"] is None for p in temporary["points"])
    assert store.status()["observation_count"] == saved_count + 4
    assert not service.query_range("2026-10", "2026-10", allow_upstream=False)[
        "complete"
    ]


def test_ppi_strict_available_asof_uses_confirmed_revision_at_both_endpoints():
    now = [AT + timedelta(hours=1)]
    store = InMemoryTimeSeriesStore(profile=PPI_PROFILE, clock=lambda: now[0])
    store.save_batches((batch({"2026-08": "100", "2026-09": "110"}),))
    source = SimpleNamespace(fetch=lambda *a: pytest.fail("history fetched upstream"))
    service = ppi.PpiTimeSeriesService(store, source)

    def query(at):
        return service.query_range("2026-09", "2026-09", series_ids=(SID,), as_of=at)

    assert not query(AT)["points"]
    assert query(now[0])["points"][0]["change_1m_ratio"] == "0.1000"
    now[0] += timedelta(hours=2)
    store.save_batches(
        (batch({"2026-08": "105", "2026-09": "126"}, at=AT + timedelta(hours=2)),)
    )
    earlier = query(AT + timedelta(hours=2, minutes=30))
    assert earlier["points"][0]["change_1m_ratio"] == "0.1000"
    latest = query(now[0])["points"][0]
    assert latest["change_1m_ratio"] == "0.2000" and latest["revision"] == 2
    assert latest["calculation_inputs"]["change_1m_ratio"]["denominator_revision"] == 2
    assert (
        latest["calculation_inputs"]["change_1m_ratio"]["denominator_available_at"]
        == now[0].isoformat()
    )


def test_ppi_withdraw_and_evidence_restoration_preserve_history():
    now = [AT]
    service, wire, store = fixture_service(now=now)
    service.init_range("2026-09", "2026-09", series_ids=(SID,))
    now[0] += timedelta(days=1)
    service.withdraw_range("2026-09", "2026-09", series_ids=(SID,), evidence_url=NOTICE)
    assert service.query_range("2026-09", "2026-09", series_ids=(SID,), as_of=AT)[
        "complete"
    ]
    now[0] += timedelta(days=1)
    stale = service.update_range(
        "2026-09", "2026-09", series_ids=(SID,), include_buffer=False
    )
    assert stale["withdrawn"] and stale["saved_points"] == 0
    restored = service.update_range(
        "2026-09",
        "2026-09",
        series_ids=(SID,),
        include_buffer=False,
        restoration_evidence_url=NOTICE,
        restoration_expected_points=[
            {"series_id": SID, "month": "2026-09", "index_value": "420"}
        ],
    )
    assert restored["complete"] and restored["points"][0]["revision"] == 3
    assert store.status()["revision_count"] == 2


class RawPort:
    def __init__(self, profile):
        self.profile, self.materials = profile, []

    def put_verified(self, raw):
        self.materials.append(raw)
        return raw_locator(digest(raw), profile=self.profile)


class RecordingConnection:
    autocommit = True

    def __init__(self):
        self.calls, self.rows = [], []

    def transaction(self):
        return nullcontext()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        one = (True,) if "advisory_" in sql else None
        rows = self.rows if "JOIN" in sql and ".batches b" in sql else []
        return SimpleNamespace(fetchone=lambda: one, fetchall=lambda: rows)


@pytest.mark.parametrize(
    "profile,other", [(PPI_PROFILE, CPI_PROFILE), (CPI_PROFILE, PPI_PROFILE)]
)
def test_postgres_sql_raw_read_commit_withdraw_and_lock_are_isolated(profile, other):
    connection = RecordingConnection()
    raw_store = RawPort(profile)
    store = PostgresTimeSeriesStore(
        connection, raw_store, profile=profile, clock=lambda: AT
    )
    point = SourcePoint(profile.series_ids[0], "2026-09", Decimal("110"), {}, ())
    original = SourceBatch(
        (point,), b"offline-original", {"seriesid": [point.series_id]}, AT, 1
    )
    assert store.save_batches((original,))["saved_points"] == 1
    connection.rows = [
        (
            point.series_id,
            date(2026, 9, 1),
            1,
            AT,
            "batch",
            *_relational_content(point),
            digest(original.raw),
            AT,
            "active",
            None,
        )
    ]
    result = store.read_range(
        profile.series_ids, "2026-09", "2026-09", as_of=AT, vintage_mode="available"
    )
    assert profile.raw_prefix in result[0]["raw_locator"]
    with store.operation():
        store.withdraw_range(
            (point.series_id,), "2026-09", "2026-09", evidence_url=NOTICE
        )
    assert len(raw_store.materials) == 2
    assert all(other.schema not in sql for sql, _ in connection.calls)
    writes = [
        sql for sql, _ in connection.calls if sql.startswith(("INSERT", "UPDATE"))
    ]
    assert writes and all(profile.schema in sql for sql in writes)
    assert [params for sql, params in connection.calls if "advisory_" in sql] == [
        (profile.lock_id,),
        (profile.lock_id,),
    ]
    for operation in (
        lambda: store.read_range(other.series_ids, "2026-09", "2026-09"),
        lambda: store.confirm_current_availability(
            other.series_ids, "2026-09", "2026-09"
        ),
        lambda: store.withdraw_range(
            other.series_ids, "2026-09", "2026-09", evidence_url=NOTICE
        ),
        lambda: store.save_batches(
            (
                replace(
                    original, points=(replace(point, series_id=other.series_ids[0]),)
                ),
            )
        ),
    ):
        count, material_count = len(connection.calls), len(raw_store.materials)
        with pytest.raises(CpiStorageError, match="series_invalid"):
            operation()
        assert (
            len(connection.calls) == count
            and len(raw_store.materials) == material_count
        )
    sql = (
        _revision_ddl(profile=profile)
        + _latest_view_sql(profile=profile)
        + _rates_view_sql(profile=profile)
    )
    assert (
        profile.schema in sql and profile.rates_view in sql and other.schema not in sql
    )
    assert all(series in sql for series in profile.series_ids)
    assert all(series not in sql for series in other.series_ids)


def test_garage_identical_bytes_have_separate_verified_namespaces():
    objects, requests = {}, []

    def transport(method, url, headers, raw, maximum, timeout):
        requests.append((method, url))
        if method == "PUT":
            objects[url] = raw
            return 200, b""
        return (200, objects[url]) if url in objects else (404, b"")

    credentials = GarageCredentials("offline", "offline")
    for profile in (CPI_PROFILE, PPI_PROFILE):
        raw_store = GarageCpiRawStore(
            credentials, transport=transport, clock=lambda: AT, profile=profile
        )
        assert raw_store.put_verified(b"identical") == raw_locator(
            digest(b"identical"), profile=profile
        )
    assert len(objects) == 2
    assert sum(method == "PUT" for method, _ in requests) == 2
    assert any(PPI_PROFILE.raw_prefix in url for url in objects)
    assert any(CPI_PROFILE.raw_prefix in url for url in objects)


def test_ppi_bad_batch_is_rejected_before_query_filtering_or_raw_writes():
    service, _, store = fixture_service()
    original = batch({"2026-09": "100"})
    foreign = replace(
        original,
        points=(
            *original.points,
            SourcePoint(CPI_PROFILE.series_ids[0], "2026-09", Decimal(100), {}, ()),
        ),
    )
    service.source = SimpleNamespace(fetch=lambda *a: (foreign,))
    with pytest.raises(core.CpiTimeSeriesError, match="series_invalid"):
        service.query_range("2026-09", "2026-09")
    with pytest.raises(CpiStorageError, match="series_invalid"):
        store.save_batches((foreign,))
    assert store.status()["observation_count"] == 0


def test_factories_share_bls_control_and_propagate_profile_to_every_component(
    tmp_path, monkeypatch
):
    control_path = tmp_path / "shared-bls.json"
    monkeypatch.setattr(core, "DEFAULT_REQUEST_CONTROL_PATH", control_path)
    cpi_client = core.build_source(credential_loader=lambda: "a" * 32)
    ppi_client = ppi.build_source(credential_loader=lambda: "a" * 32)
    assert (
        cpi_client._request_control.state_path
        == ppi_client._request_control.state_path
        == control_path
    )
    runtime = tmp_path / "runtime.toml"
    runtime.write_text(
        '[storage]\nbackend="s3"\n[s3]\nendpoint_url="http://192.168.10.8:3900"\nbucket="mdf-data"\nregion="garage"\nkey_prefix=""\naddressing_style="path"\n'
    )
    # Use the existing constants instead of asserting any production binding.
    runtime.write_text(
        runtime.read_text().replace('bucket="mdf-data"', f'bucket="{core.BUCKET}"')
    )
    secret = tmp_path / "offline.toml"
    secret.write_text(
        '[postgres]\npassword="offline"\n[s3]\naccess_key_id="offline"\nsecret_access_key="offline"\n'
    )
    monkeypatch.setattr(core, "connect_dev", lambda *a, **kw: RecordingConnection())
    service = ppi.build_service(
        runtime_path=runtime, secret_path=secret, credential_loader=lambda: "a" * 32
    )
    assert all(
        component.profile is PPI_PROFILE
        for component in (
            service,
            service.source,
            service.store,
            service.store.raw_store,
        )
    )
    assert not service.store.connection.calls  # build does not run DDL/requests
    assert ppi_store.InMemoryTimeSeriesStore().profile is PPI_PROFILE
    assert ppi_source.BlsRangeSource(registered=False).profile is PPI_PROFILE


@pytest.mark.parametrize(
    "first,second", [(CPI_PROFILE, PPI_PROFILE), (PPI_PROFILE, CPI_PROFILE)]
)
def test_ppi_and_cpi_share_provider_daily_limit_across_control_instances(
    tmp_path, first, second
):
    path = tmp_path / "bls-budget.json"

    class LimitedTransport:
        calls = 0

        def post(self, *args, **kwargs):
            self.calls += 1
            return HttpResult(
                200,
                b'{"status":"REQUEST_FAILED",'
                b'"message":["daily API request limit exceeded"]}',
                {},
            )

    transport = LimitedTransport()
    for profile in (first, second):
        client = BlsRangeSource(
            profile=profile,
            registered=False,
            transport=transport,
            clock=lambda: AT,
            request_control=CpiRequestControl(
                path, clock=lambda: AT, min_interval_seconds=0
            ),
        )
        with pytest.raises(CpiSourceError, match="provider_daily_limit") as caught:
            client.fetch(profile.series_ids, "2026-09", "2026-09")
        assert caught.value.attempts == (1 if profile is first else 0)
    assert transport.calls == 1
    assert len(json.loads(path.read_text())["requests"]) == 1


def test_ppi_unknown_earliest_month_keeps_existing_query_input_contract():
    service, wire, _ = fixture_service()
    result = service.query_range("1901-01", "1901-01", allow_upstream=False)
    assert len(result["missing"]) == 4
    for start, end, classification in (
        ("1900-12", "1901-01", "range_invalid"),
        ("2026-13", "2026-13", "month_invalid"),
        ("2026-02", "2026-01", "range_invalid"),
    ):
        with pytest.raises(core.CpiTimeSeriesError, match=classification):
            service.query_range(start, end, allow_upstream=False)
    with pytest.raises(core.CpiTimeSeriesError, match="series_invalid"):
        service.query_range("2026-09", "2026-09", series_ids=None)
    assert not wire.calls


def test_ppi_local_numerator_with_temporary_buffer_keeps_raw_evidence_and_local_value():
    service, wire, store = fixture_service()
    original = batch({"2026-09": "110"})
    store.save_batches((original,))
    result = service.query_range("2026-09", "2026-09", series_ids=(SID,))
    point = result["points"][0]
    assert point["index_value"] == "110" and point["origin"] == "local"
    assert point["raw_sha256"] == digest(original.raw)
    assert point["raw_locator"] == raw_locator(
        digest(original.raw), profile=PPI_PROFILE
    )
    inputs = point["calculation_inputs"]["change_12m_ratio"]
    assert inputs["numerator_origin"] == "local"
    assert (
        inputs["denominator_origin"] == "upstream"
        and inputs["derived_origin"] == "mixed"
    )
    assert inputs["denominator_month"] == "2025-09"
    assert store.status()["observation_count"] == 1
    assert store._batches[next(iter(store._batches))].raw == original.raw
    assert len(wire.calls) == 1


def test_ppi_raw_port_mismatch_fails_before_any_io():
    raw = RawPort(CPI_PROFILE)
    for create in (
        lambda: InMemoryTimeSeriesStore(raw, profile=PPI_PROFILE),
        lambda: PostgresTimeSeriesStore(
            RecordingConnection(), raw, profile=PPI_PROFILE
        ),
    ):
        with pytest.raises(CpiStorageError, match="profile_mismatch"):
            create()
    assert not raw.materials
    assert ppi.PpiTimeSeriesError is core.CpiTimeSeriesError
    assert ppi_source.PpiSourceError is CpiSourceError
    assert ppi_store.PpiStorageError is CpiStorageError
