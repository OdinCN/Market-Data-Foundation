"""Synthetic shared-app retail admission, durable jobs and read-only composition."""

from copy import deepcopy
from datetime import timedelta
from threading import Event
from unittest.mock import Mock

import pytest

from market_data_foundation.historical.retail_contract import CENSUS_RETAIL
from market_data_foundation.historical.retail_timeseries import RetailTimeSeriesService
from market_data_foundation.historical.retail_timeseries_store import (
    InMemoryRetailStore,
)
from market_data_foundation.web.cpi_timeseries_jobs import WebError
from market_data_foundation.web.package_catalog import SaveCommand
from market_data_foundation.web.pce_dev_host import CANDIDATE_ORIGIN
from market_data_foundation.web.retail_timeseries import PREFIX
from tests.test_census_retail_timeseries import (  # noqa: F401
    AT,
    Wire,
    no_network,
    reader,
)
from tests.test_cpi_timeseries_web import command
from tests.test_cpi_timeseries_web_host import client, setup


def retail(tmp_path, monkeypatch, **options):
    data = InMemoryRetailStore(clock=lambda: AT)
    wire = Wire()
    calls = []

    def read():
        calls.append("read")
        return RetailTimeSeriesService(data, clock=lambda: AT)

    def write(*, credential_loader):
        calls.append("write")
        assert credential_loader() == "synthetic-census-key"
        return RetailTimeSeriesService(
            data,
            reader(wire, sleep=lambda _: None),
            allow_source=True,
            clock=lambda: AT,
        )

    app, runtime, *_ = setup(
        tmp_path,
        monkeypatch,
        retail_enabled=options.pop("retail_enabled", True),
        retail_access=options.pop(
            "retail_access",
            __import__("tests.test_readonly_host", fromlist=["ALLOW"]).ALLOW,
        ),
        retail_allow_collect=options.pop("retail_allow_collect", True),
        retail_job_directory=tmp_path / "retail-jobs",
        retail_artifacts_path=tmp_path / "artifacts" / "retail",
        retail_read_service_factory=read,
        retail_write_service_factory=write,
        **options,
    )
    with (runtime.settings.config_directory / "secret.toml").open("a") as stream:
        stream.write('\n[census]\napi_key="synthetic-census-key"\n')
    old = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(
            expected_revision=old.revision, enabled_capability_ids=["cpi", "retail"]
        ),
    )
    return app, runtime, data, wire, calls


def test_shared_mount_defaults_closed_and_t05_not_qualified(tmp_path, monkeypatch):
    app, runtime, data, wire, calls = retail(
        tmp_path, monkeypatch, retail_enabled=False
    )
    assert runtime.retail_timeseries is None
    assert calls == [] and not wire.calls and not CENSUS_RETAIL.source_qualified
    with client(app) as http:
        assert http.get(PREFIX + "/status").status_code == 503
    assert calls == [] and not data.batches


def test_shared_get_and_factory_never_materialize_write_ports(tmp_path, monkeypatch):
    app, runtime, data, wire, calls = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    assert (
        next(
            s
            for s in runtime.packages.credential_states("macro", record)
            if s.source_id == "Census"
        ).credential_state
        == "configured"
    )
    with client(app) as http:
        status = http.get(PREFIX + "/status")
        assert status.status_code == 200, status.text
        assert status.json()["host_binding"] == "retail-continuous-dev-r1"
        assert status.json()["initialization"]["months"] == 240
        assert status.json()["source_qualified"] is False
        assert http.get(PREFIX + "/api-help").status_code == 200
        response = http.get(PREFIX + "/query?start=2026-08&end=2026-09&local_only=true")
        assert response.status_code == 200
        assert [p["month"] for p in response.json()["points"]] == ["2026-09", "2026-08"]
        assert http.get(PREFIX + "/jobs").json() == {"jobs": []}
        assert http.get(PREFIX + "/executions").json() == {"executions": []}
    assert "write" not in calls and not wire.calls and not data.batches
    assert not host.journal.directory.exists()
    # Exercise the real trusted read factory with an injected protocol connection.
    connection = Mock(autocommit=True)
    runtime.connector = Mock(return_value=connection)
    service = runtime.retail_read_service()
    assert service.store.schema == "mdf_retail_timeseries_dev_r1"
    assert service.store.archive is None and service.source is None
    assert (
        "default_transaction_read_only=on"
        in runtime.connector.call_args.kwargs["options"]
    )
    connection.execute.assert_not_called()


def test_background_idempotency_serialization_and_list_detail(tmp_path, monkeypatch):
    app, runtime, data, wire, _ = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    entered, release = Event(), Event()
    original = host.write_factory

    def blocked(**kwargs):
        entered.set()
        assert release.wait(5)
        return original(**kwargs)

    host.write_factory = blocked
    pending = command("initialize")
    try:
        with client(app) as http:
            receipt = http.post(
                PREFIX + "/jobs", json=pending, headers={"Origin": CANDIDATE_ORIGIN}
            )
            assert receipt.status_code == 202, receipt.text
            assert entered.wait(2)
            assert (
                receipt.json()["job_id"]
                == receipt.json()["collection_id"]
                == pending["request_id"]
            )
            assert (
                http.get(PREFIX + "/executions").json()["executions"][0]["state"]
                == "running"
            )
            replay = http.post(
                PREFIX + "/jobs", json=pending, headers={"Origin": CANDIDATE_ORIGIN}
            )
            assert replay.json()["job_id"] == pending["request_id"]
            busy = http.post(
                PREFIX + "/jobs", json=command(), headers={"Origin": CANDIDATE_ORIGIN}
            )
            assert busy.status_code == 409
    finally:
        release.set()
    # The execution lock provides completion synchronization without polling IO.
    import time

    for _ in range(100):
        rows = host.journal.list()
        if rows[0]["state"] != "running":
            break
        time.sleep(0.01)
    assert rows[0]["state"] == "complete", rows
    assert rows[0]["result"]["readback"]["verified"]
    assert len(wire.calls) == 1 and len(data.batches) == 1
    before = deepcopy(rows)
    assert host.submit(pending) == before[0]
    assert len(wire.calls) == 1
    with pytest.raises(WebError, match="request_id_conflict"):
        host.submit(dict(pending, end="2026-10"))


def test_partial_and_unknown_keep_same_collection_for_explicit_resume(
    tmp_path, monkeypatch
):
    _, runtime, data, wire, _ = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    pending = command("update")
    original = host.write_factory
    host.write_factory = lambda **_: (_ for _ in ()).throw(
        RuntimeError("must never expose raw exception")
    )
    unknown = host.submit(pending)
    assert (
        unknown["state"] == "unknown"
        and unknown["classification"] == "retail_local_failure"
    )
    assert "must never" not in str(unknown)
    host.write_factory = original
    recovered = host.submit(pending, resume=True)
    assert recovered["state"] == "complete"
    assert recovered["result"]["collection_id"] == pending["request_id"]
    assert len(data.batches) == len(wire.calls) == 1


@pytest.mark.parametrize("flag", [False, True])
def test_source_access_requires_exact_retail_capability(tmp_path, monkeypatch, flag):
    app, runtime, _, wire, calls = retail(
        tmp_path, monkeypatch, retail_allow_source=flag
    )
    record = runtime.packages.store.read(runtime.settings.environment_id, "macro")
    runtime.packages.store.save(
        runtime.settings.environment_id,
        "macro",
        SaveCommand(expected_revision=record.revision, enabled_capability_ids=["cpi"]),
    )
    host = runtime.retail_timeseries
    assert host.source()["capability_enabled"] is False
    assert host.capabilities(host.source())["collect"] is False
    with pytest.raises(WebError, match="retail_capability_disabled"):
        host.submit(command())
    assert not wire.calls and calls == []


def test_absent_census_input_is_partial_not_zero_or_complete(tmp_path, monkeypatch):
    from tests.test_census_retail_timeseries import encode, payload, response

    app, runtime, _, wire, _ = retail(tmp_path, monkeypatch)
    table = payload("2025-09", "2026-09")
    month_index = table[0].index("time")
    category_index = table[0].index("category_code")
    table = [
        table[0],
        *[
            r
            for r in table[1:]
            if not (r[month_index] == "2026-09" and r[category_index] == "441")
        ],
    ]
    wire.outcomes = [response(encode(table))]
    pending = command("initialize")
    result = runtime.retail_timeseries.submit(pending)
    assert result["state"] == "complete"
    assert result["result"]["readback"]["verified"] is True
    assert result["result"]["readback"]["expected_points"] == 6
    assert result["result"]["readback"]["buffer_point_count"] == 72
    assert result["result"]["coverage_complete"] is False
    assert result["result"]["completion"] == "source_coverage_partial"
    assert result["result"]["readback"]["source_missing"]
    with client(app) as http:
        point = http.get(
            PREFIX + "/query?start=2026-09&end=2026-09&local_only=true"
        ).json()["points"][0]
    assert point["control_level"]["value"] is None
    assert point["headline_level"]["value"] is not None


def test_resume_http_and_retry_time_are_explicit(tmp_path, monkeypatch):
    app, runtime, _, wire, _ = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    pending = command("update")
    original = host.write_factory
    host.write_factory = lambda **_: (_ for _ in ()).throw(RuntimeError("private"))
    first = host.submit(pending)
    host.write_factory = original
    first["result"] = {"retry_at": "2099-01-01T00:00:00+00:00"}
    host.journal.save(first)
    with client(app) as http:
        assert (
            http.post(
                PREFIX + "/jobs/resume",
                json=pending,
                headers={"Origin": CANDIDATE_ORIGIN},
            ).status_code
            == 409
        )
        assert http.post(PREFIX + "/jobs/resume", json=pending).status_code == 405
        first["result"] = None
        host.journal.save(first)
        result = http.post(
            PREFIX + "/jobs/resume", json=pending, headers={"Origin": CANDIDATE_ORIGIN}
        )
        assert result.status_code in {200, 202}, result.text
        assert result.json()["collection_id"] == pending["request_id"]
    import time

    for _ in range(100):
        if host.journal.list()[0]["state"] != "running":
            break
        time.sleep(0.01)
    assert host.journal.list()[0]["state"] == "complete"
    assert len(wire.calls) == 1


def test_get_source_permission_and_beijing_history_never_write(tmp_path, monkeypatch):
    app, runtime, data, wire, _ = retail(
        tmp_path, monkeypatch, retail_allow_source=True
    )
    host = runtime.retail_timeseries

    def query_factory(*, credential_loader):
        assert credential_loader() == "synthetic-census-key"
        return RetailTimeSeriesService(
            data,
            reader(wire, sleep=lambda _: None),
            allow_source=True,
            clock=lambda: AT,
        )

    host.query_factory = query_factory
    assert host.status()["source_allowed"] is True
    data.ensure_schema = Mock(side_effect=AssertionError("GET must not perform DDL"))
    with client(app) as http:
        current = http.get(
            PREFIX + "/query", params={"start": "2026-09", "end": "2026-09"}
        )
        assert current.status_code == 200, current.text
        assert current.json()["persisted"] is False
        assert current.json()["provider_requests"] == 1
        point = current.json()["points"][0]
        assert isinstance(point["headline_level"]["value"], str)
        assert point["official_headline_mpcsm"]["unit"] == "percent"
        historical = http.get(
            PREFIX + "/query",
            params={
                "start": "2026-09",
                "end": "2026-09",
                "query_mode": "history",
                "as_of": "2026-11-01T08:00:00+08:00",
            },
        )
        assert historical.status_code == 200
        assert historical.json()["provider_requests"] == 0
        assert historical.json()["as_of"] == "2026-11-01T00:00:00+00:00"
    assert len(wire.calls) == 1 and not data.batches
    data.ensure_schema.assert_not_called()


def test_unknown_retail_access_keeps_shared_get_and_collect_closed(
    tmp_path, monkeypatch
):
    from market_data_foundation.web.cpi_query import CpiQueryAccess

    app, runtime, _, wire, calls = retail(
        tmp_path, monkeypatch, retail_access=CpiQueryAccess()
    )
    with client(app) as http:
        assert http.get(PREFIX + "/status").status_code == 403
        assert (
            http.post(
                PREFIX + "/jobs", json=command(), headers={"Origin": CANDIDATE_ORIGIN}
            ).status_code
            == 403
        )
    assert not calls and not wire.calls
    assert not runtime.retail_timeseries.journal.directory.exists()


def test_wrong_family_or_schema_factory_fails_closed(tmp_path, monkeypatch):
    from market_data_foundation.historical.retail_timeseries_store import (
        PostgresRetailStore,
    )

    _, runtime, _, _, _ = retail(tmp_path, monkeypatch)
    host = runtime.retail_timeseries
    host.read_factory = runtime.timeseries.read_factory
    with pytest.raises(WebError, match="retail_service_binding_mismatch"):
        with host.service():
            pytest.fail("CPI service admitted as retail")
    connection = Mock(autocommit=True)
    host.read_factory = lambda: RetailTimeSeriesService(
        PostgresRetailStore(connection, None, schema="other_family")
    )
    with pytest.raises(WebError, match="retail_service_binding_mismatch"):
        with host.service():
            pytest.fail("wrong schema admitted")
    connection.close.assert_called_once()


@pytest.mark.parametrize("partial", [False, True])
def test_query_projects_calendar_for_past_and_empty_future_months_read_only(
    tmp_path, monkeypatch, partial
):
    from market_data_foundation.historical.census_retail_live import HttpResult
    from tests.test_census_retail_release_schedule import table

    app, runtime, data, wire, calls = retail(
        tmp_path, monkeypatch, retail_allow_source=partial
    )
    host = runtime.retail_timeseries
    if partial:
        wire.outcomes = [HttpResult(204, b"", {})]
        host.query_factory = lambda **_: RetailTimeSeriesService(
            data,
            reader(wire, sleep=lambda _: None),
            allow_source=True,
            clock=lambda: AT,
        )
    host.scheduler._accept(
        host.scheduler._read(),
        table(
            [
                ("September 2026", "October 15, 2026"),
                ("October 2026", "November 17, 2026"),
                ("November 2026", "December 16, 2026"),
                ("December 2026", "January 15, 2027"),
                ("January 2027", "TBA"),
            ]
        ).encode(),
        AT,
    )
    # The already-past September release has no execution target.
    assert "2026-09" not in {
        t["reference_month"] for t in host.scheduler.status()["targets"]
    }
    retained = host.scheduler.state_path.read_bytes()
    host.scheduler.calendar_fetcher = Mock(
        side_effect=AssertionError("GET calendar IO")
    )
    host.scheduler._save = Mock(side_effect=AssertionError("GET schedule write"))
    data.save_batch = Mock(side_effect=AssertionError("GET database write"))
    data.ensure_schema = Mock(side_effect=AssertionError("GET DDL"))

    def query(params):
        with client(app) as http:
            response = http.get(PREFIX + "/query", params=params)
            assert response.status_code == 200
            return response.json()

    params = {"start": "2026-08", "end": "2027-01"}
    result = query(params)
    assert result["status"] == ("PARTIAL" if partial else "OK")
    points = {p["month"]: p for p in result["points"]}
    assert points["2026-08"]["publication_event"] is None
    expected = {
        "2026-09": "2026-10-15T12:30:00+00:00",
        "2026-10": "2026-11-17T13:30:00+00:00",
        "2026-11": "2026-12-16T13:30:00+00:00",
        "2026-12": "2027-01-15T13:30:00+00:00",
    }
    for month, at in expected.items():
        event = points[month]["publication_event"]
        assert event["time"] == at and event["status"] == "scheduled"
        assert event["evidence"]["source"].startswith("https://www.census.gov/")
    assert points["2026-12"]["headline_level"]["value"] is None
    assert points["2026-12"]["control_level"]["value"] is None
    tba = points["2027-01"]["publication_event"]
    assert tba["status"] == "unknown" and tba["time"] is None
    assert len(result["publication_events"]) == 5
    assert result["metadata"]["publication_event"]["binds_current_revision"] is False
    for basis in ("available", "observed"):
        for delta, visible in ((-1, False), (0, True)):
            history = query(
                dict(
                    params,
                    query_mode="history",
                    as_of_basis=basis,
                    as_of=(AT + timedelta(microseconds=delta)).isoformat(),
                )
            )
            assert bool(history["publication_events"]) is visible
            assert all(p["publication_event"] is None for p in history["points"]) is (
                not visible
            )
            assert history["provider_requests"] == 0
    assert host.scheduler.state_path.read_bytes() == retained
    host.scheduler.calendar_fetcher.assert_not_called()
    host.scheduler._save.assert_not_called()
    data.save_batch.assert_not_called()
    data.ensure_schema.assert_not_called()
    assert "write" not in calls
