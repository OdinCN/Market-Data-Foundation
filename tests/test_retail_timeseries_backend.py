"""Bounded retail backend contracts; synthetic transport and offline retained
raw only."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import httpx
import pytest
from test_census_retail_timeseries import (
    AT,
    Wire,
    encode,
    no_network,  # noqa: F401 -- apply the existing deterministic network guard
    payload,
    reader,
    response,
    snapshot,
    target,
)

from market_data_foundation.historical.census_retail_live import HttpResult
from market_data_foundation.historical.census_retail_timeseries_source import (
    plan_range,
)
from market_data_foundation.historical.retail_timeseries import (
    RetailServiceError,
    RetailTimeSeriesService,
)
from market_data_foundation.historical.retail_timeseries_cli import main
from market_data_foundation.historical.retail_timeseries_store import (
    FileRetailBatchArchive,
    InMemoryRetailStore,
    PostgresRetailStore,
    RetailStorageError,
    batch_id,
    schema_sql,
)
from market_data_foundation.web.retail_timeseries import (
    PREFIX,
    RetailTimeSeriesHost,
    create_app,
)


def batch(table=None, start="2025-09", end="2026-09", observed=AT):
    return replace(
        snapshot(table or payload(start, end), start, end).batches[0],
        observed_at=observed,
    )


def service(store=None, wire=None):
    return RetailTimeSeriesService(
        store or InMemoryRetailStore(clock=lambda: AT + timedelta(days=10)),
        reader(wire or Wire(), sleep=lambda _: None),
        allow_source=True,
        clock=lambda: AT + timedelta(days=20),
    )


def test_revisions_content_only_and_raw_lineage():
    store = InMemoryRetailStore(clock=lambda: AT + timedelta(days=10))
    first = batch()
    assert store.save_batch("one", first) == 78
    assert store.save_batch("one", first) == 0
    # Different whitespace/hash/observation alone does not invent revisions.
    raw = first.raw + b"\n"
    from market_data_foundation.historical.census_retail_timeseries_source import (
        parse_range_response,
    )

    second = replace(
        first,
        raw=raw,
        cells=parse_range_response(raw, "2025-09", "2026-09"),
        observed_at=AT + timedelta(days=1),
    )
    assert store.save_batch("two", second) == 0
    table = payload("2025-09", "2026-09")
    target(table, "2026-09", "441")[table[0].index("cell_value")] = "11"
    assert store.save_batch("three", batch(table, observed=AT + timedelta(days=2))) == 1
    assert store.status() == dict(
        batch_count=3, observation_count=79, revision_count=1, latest_month="2026-09"
    )
    version = store.read_range("2026-09", "2026-09")["2026-09", "441", "SM"]
    assert version.cell.source_pointer and version.cell.raw_sha256
    assert version.revision == 2 and version.available_at > version.observed_at


def test_historical_selects_every_operand_and_comparison_first():
    store = InMemoryRetailStore(clock=lambda: AT + timedelta(days=1))
    original = batch()
    store.save_batch("one", original)
    before = service(store).query_range(
        "2026-09", "2026-09", as_of=AT + timedelta(days=1)
    )
    table = payload("2025-09", "2026-09")
    for month in ("2025-09", "2026-03", "2026-06", "2026-08", "2026-09"):
        for category in ("44X72", "441", "444", "447", "722"):
            target(table, month, category)[table[0].index("cell_value")] = (
                "40000" if category == "44X72" else "20"
            )
    store.clock = lambda: AT + timedelta(days=4)
    store.save_batch("two", batch(table, observed=AT + timedelta(days=3)))
    svc = service(store)
    after = svc.query_range(
        "2026-09", "2026-09", as_of=(AT + timedelta(days=1)).isoformat()
    )
    assert (
        before["points"] == after["points"]
        or before["points"][0]["control_rates"] == after["points"][0]["control_rates"]
    )
    point = after["points"][0]
    assert {v["local_revision"] for v in point["input_versions"].values()} == {1}
    assert {v["batch_id"] for v in point["input_versions"].values()} == {
        batch_id("one", original.request)
    }
    assert point["official_headline_mpcsm"]["value"] == Decimal("9.7")
    current = svc.query_range("2026-09", "2026-09", local_only=True)["points"][0]
    assert current["control_rates"][12]["value"] == 0
    assert point["control_rates"][12]["value"] != 0
    # Available history cannot see the second observation before confirmation.
    hidden = svc.query_range(
        "2026-09",
        "2026-09",
        as_of=AT + timedelta(days=3, hours=1),
        as_of_basis="available",
    )
    assert hidden["points"][0]["control_level"] == point["control_level"]
    observed = svc.query_range(
        "2026-09",
        "2026-09",
        as_of=AT + timedelta(days=3, hours=1),
        as_of_basis="observed",
    )
    assert observed["points"][0]["control_level"]["value"] == Decimal("39920")


@pytest.mark.parametrize(
    "marker,status",
    [(None, "unavailable"), ("(S)", "suppressed"), ("absent", "missing")],
)
def test_null_suppressed_missing_and_zero(marker, status):
    table = payload("2025-09", "2026-09")
    row = target(table, "2026-09", "441")
    if marker == "absent":
        table.remove(row)
    else:
        row[table[0].index("cell_value")] = marker
    target(table, "2026-08")[table[0].index("cell_value")] = "0"
    svc = service()
    svc.store.save_batch("one", batch(table))
    point = svc.query_range("2026-09", "2026-09", local_only=True)["points"][0]
    operand = next(c for c in point["source_cells"] if c["category_code"] == "441")
    assert operand["status"] == status
    assert point["control_level"]["value"] is None
    assert point["headline_rates"][1]["reason"] == "comparison_level_zero"
    assert point["official_headline_mpcsm"]["value"] == Decimal("9.7")
    assert point["official_headline_mpcsm"]["data_type_code"] == "MPCSM"


def test_query_gap_fill_never_writes_and_local_history_never_requests():
    wire = Wire()
    svc = service(wire=wire)
    before = svc.store.status()
    result = svc.query_range("2026-09", "2026-09")
    assert result["points"][0]["control_rates"][12]["value"] is not None
    assert len(wire.calls) == 1 and svc.store.status() == before
    for options in (
        {"local_only": True},
        {"query_mode": "history"},
        {"as_of": AT},
        {"query_mode": "current", "as_of": AT},
    ):
        result = svc.query_range("2026-09", "2026-09", **options)
        assert result["provider_attempts"] == 0
        assert result["points"][0]["source_cells"][0]["status"] == "not_provided"
    assert len(wire.calls) == 1


def test_local_failure_never_falls_back_to_source():
    wire = Wire()
    svc = service(wire=wire)

    def fail(*args, **kwargs):
        raise RetailStorageError("retail_storage_read_failed")

    svc.store.read_range = fail
    with pytest.raises(RetailStorageError):
        svc.query_range("2026-09", "2026-09")
    assert wire.calls == []


def test_partial_long_collection_resume_skips_success_and_cross_boundary_calculates():
    wire = Wire(
        [response(encode(payload("2005-10", "2026-09")))]
        + [HttpResult(503, b"", {})] * 3
    )
    svc = service(wire=wire)
    first = svc.initialize("2006-10", "2026-10", collection_id="resume")
    assert first["status"] == "PARTIAL" and first["completed_batches"] == 1
    assert first["provider_attempts"] == 4
    assert svc.store.status()["batch_count"] == 1
    wire.outcomes = None
    second = svc.initialize("2006-10", "2026-10", collection_id="resume")
    assert second["status"] == "COMPLETED" and second["provider_attempts"] == 1
    assert second["retained_provider_attempts"] == 2
    assert len(wire.calls) == 5
    assert wire.calls[-1][1]["time"] == "from 2026-10 to 2026-10"
    point = svc.query_range("2026-10", "2026-10", local_only=True)["points"][0]
    assert all(r["value"] is not None for r in point["control_rates"].values())
    third = svc.initialize("2006-10", "2026-10", collection_id="resume")
    assert third["provider_attempts"] == 0 and third["saved_points"] == 0
    assert len(wire.calls) == 5
    conflict = svc.initialize("2006-10", "2026-11", collection_id="resume")
    assert (
        conflict["status"] == "ERROR"
        and conflict["classification"] == "retail_replay_conflict"
    )
    assert len(wire.calls) == 5


def test_staged_batch_survives_uncertain_save_without_refetch():
    svc = service()
    save = svc.store.save_batch

    def uncertain(*args):
        save(*args)
        raise RetailStorageError("retail_storage_outcome_unknown")

    svc.store.save_batch = uncertain
    first = svc.initialize("2026-09", "2026-09", collection_id="uncertain")
    assert first["status"] == "ERROR" and first["provider_attempts"] == 1
    svc.store.save_batch = save
    result = svc.initialize("2026-09", "2026-09", collection_id="uncertain")
    assert result["status"] == "COMPLETED" and result["provider_attempts"] == 0
    assert svc.store.status()["revision_count"] == 0


def test_durable_archive_reopens_pending_inputs(tmp_path):
    directory = tmp_path / "artifacts" / "retail"
    first = FileRetailBatchArchive(directory)
    plan = plan_range("2026-09", "2026-09")
    first.register("recover", plan)
    value = batch()
    first.stage("recover", value)
    second = FileRetailBatchArchive(directory)
    assert second.load("recover", value.request) == value
    with pytest.raises(RetailStorageError, match="conflict"):
        second.stage("recover", replace(value, observed_at=AT + timedelta(seconds=1)))
    with pytest.raises(RetailStorageError):
        FileRetailBatchArchive(tmp_path / "untracked")


class Result:
    def __init__(self, rows=()):
        self.rows = rows

    def fetchall(self):
        return list(self.rows)

    def fetchone(self):
        return self.rows[0] if self.rows else None


class Connection:
    autocommit = True

    def __init__(self):
        self.calls, self.rows = [], ()

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return Result(self.rows if sql.startswith("SELECT DISTINCT") else ())

    @contextmanager
    def transaction(self):
        yield


def test_postgres_identifier_safety_read_only_and_cutoff_parameters(tmp_path):
    c = Connection()
    archive = FileRetailBatchArchive(tmp_path / "artifacts")
    for invalid in ("public;DROP SCHEMA public", "a.b", "UPPER", 'x"', "x" * 64):
        with pytest.raises(RetailStorageError, match="schema_invalid"):
            PostgresRetailStore(c, archive, schema=invalid)
    store = PostgresRetailStore(c, archive, schema="root_retail_isolated")
    assert c.calls == []
    store.read_range("2025-09", "2026-09", as_of=AT, basis="available")
    sql, params = c.calls[-1]
    assert sql.startswith("SELECT") and "available_at <= %s" in sql
    assert '"root_retail_isolated".revisions' in sql and params[-1] == AT
    assert all(s.startswith("SELECT") for s, _ in c.calls)
    ddl = schema_sql("root_retail_isolated")
    assert ddl.count("CREATE TABLE") == 2
    assert "NUMERIC(27,9)" in ddl and "extract(day FROM month)=1" in ddl
    assert "official_headline_mpcsm_percent" in ddl
    assert "nullif(p12.control,0)" in ddl
    assert "index_value" not in ddl and "cpi" not in ddl.lower()
    with pytest.raises(RetailStorageError, match="basis_invalid"):
        store.read_range("2025-09", "2026-09", basis="available;DROP")


def test_postgres_save_explicit_transaction_and_post_commit_confirmation(tmp_path):
    c = Connection()
    store = PostgresRetailStore(
        c,
        FileRetailBatchArchive(tmp_path / "artifacts"),
        clock=lambda: AT + timedelta(days=1),
    )
    value = batch(start="2026-09", end="2026-09")
    assert store.save_batch("save", value) == 6
    inserts = [(s, p) for s, p in c.calls if s.startswith("INSERT")]
    assert len(inserts) == 7
    for sql, params in inserts:
        assert sql.count("%s") == len(params)
    updates = [(s, p) for s, p in c.calls if s.startswith("UPDATE")]
    assert len(updates) == 2 and all("available_at IS NULL" in s for s, _ in updates)
    assert updates[0][1][0] == AT + timedelta(days=1)


def client(svc=None, *, source=False):
    host = RetailTimeSeriesHost(
        svc or service(), query_allowed=True, allow_source=source
    )
    return AsgiClient(create_app(host, authorize=lambda request: True))


def test_http_actual_requests_units_decimal_months_offsets_and_help():
    svc = service()
    svc.store.save_batch("http", batch())
    http = client(svc)
    result = http.get(
        PREFIX + "/query",
        params={"start": "2026-08", "end": "2026-09", "local_only": "true"},
    )
    assert result.status_code == 200
    document = result.json()
    assert [p["month"] for p in document["points"]] == ["2026-09", "2026-08"]
    point = document["points"][0]
    assert isinstance(point["headline_level"]["value"], str)
    assert point["control_level"]["unit"] == "million_current_usd"
    assert point["headline_rates"]["1"]["unit"] == "ratio"
    assert document["units"]["official_headline_mpcsm"] == "percent"
    params = dict(
        start="2026-09",
        end="2026-09",
        as_of="2026-11-01T08:00:00+08:00",
        as_of_basis="observed",
    )
    offset = http.get(PREFIX + "/query", params=params)
    params["as_of"] = "2026-11-01T00:00:00Z"
    utc = http.get(PREFIX + "/query", params=params)
    assert offset.status_code == utc.status_code == 200
    assert (
        offset.json()["points"][0]["control_level"]
        == utc.json()["points"][0]["control_level"]
    )
    assert offset.json()["as_of"] == utc.json()["as_of"] == "2026-11-01T00:00:00+00:00"
    assert http.get(PREFIX + "/status").status_code == 200
    help_doc = http.get(PREFIX + "/api-help").json()
    assert set(help_doc["parameters"]) == {
        "start/end",
        "local_only",
        "query_mode",
        "as_of",
        "as_of_basis/vintage_mode",
    }
    assert len(help_doc["examples"]) == 3


@pytest.mark.parametrize(
    "suffix,params,classification",
    [
        ("query", {}, "retail_range_required"),
        (
            "query",
            {"start": "2026-13", "end": "2026-09"},
            "census_retail_month_invalid",
        ),
        (
            "query",
            {"start": "2026-08", "end": "2026-09", "as_of": "2026-11-01"},
            "retail_aware_cutoff_required",
        ),
        (
            "query",
            {"start": "2026-08", "end": "2026-09", "local_only": "yes"},
            "retail_local_only_invalid",
        ),
        (
            "query",
            {
                "start": "2026-08",
                "end": "2026-09",
                "as_of_basis": "observed",
                "vintage_mode": "available",
            },
            "retail_cutoff_basis_conflict",
        ),
        (
            "query",
            {"start": "2026-08", "end": "2026-09", "unknown": "x"},
            "unsupported_parameters",
        ),
        ("status", {"start": "2026-08"}, "unsupported_parameters"),
    ],
)
def test_http_parameter_errors(suffix, params, classification):
    result = client().get(PREFIX + "/" + suffix, params=params)
    assert (
        result.status_code == 400 and result.json()["classification"] == classification
    )


def test_http_unbound_denied_duplicates_body_and_safe_failures():
    assert AsgiClient(create_app()).get(PREFIX + "/status").status_code == 503
    assert (
        AsgiClient(create_app(RetailTimeSeriesHost()))
        .get(PREFIX + "/status")
        .status_code
        == 403
    )
    assert (
        AsgiClient(create_app(RetailTimeSeriesHost(), authorize=lambda r: True))
        .get(PREFIX + "/status")
        .status_code
        == 503
    )
    http = client()
    assert (
        http.get(PREFIX + "/query?start=2026-08&start=2026-09").json()["classification"]
        == "duplicate_parameter"
    )
    assert http.request("GET", PREFIX + "/status", content=b"x").status_code == 400
    svc = service()

    def fail(*args, **kwargs):
        raise RetailStorageError("retail_storage_read_failed")

    svc.store.read_range = fail
    failure = client(svc).get(PREFIX + "/query?start=2026-08&end=2026-09")
    assert (
        failure.status_code == 503
        and failure.json()["classification"] == "retail_storage_read_failed"
    )
    wire = Wire([HttpResult(503, b"", {})] * 3)
    upstream = client(service(wire=wire), source=True).get(
        PREFIX + "/query?start=2026-08&end=2026-09"
    )
    assert upstream.status_code == 502 and len(wire.calls) == 3


def test_explicit_source_permissions_and_cli(capsys):
    svc = RetailTimeSeriesService(InMemoryRetailStore())
    with pytest.raises(RetailServiceError, match="source_not_bound"):
        svc.initialize()
    with pytest.raises(RetailServiceError, match="source_not_bound"):
        svc.query_range("2026-09", "2026-09")
    assert main(["status"]) == 2
    assert "retail_service_not_bound" in capsys.readouterr().err
    assert (
        main(
            ["query", "--start", "2026-09", "--end", "2026-09", "--local-only"],
            service_factory=lambda: svc,
        )
        == 0
    )
    assert "not_provided" in capsys.readouterr().out
    assert service().default_range() == ("2006-11", "2026-10")


class AsgiClient:
    def __init__(self, app):
        self.app = app

    def request(self, method, path, **options):
        async def request():
            async def heartbeat():
                while True:
                    await asyncio.sleep(0.005)

            tick = asyncio.create_task(heartbeat())
            try:
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=self.app),
                    base_url="http://testserver",
                ) as http:
                    return await http.request(method, path, **options)
            finally:
                tick.cancel()

        return asyncio.run(request())

    def get(self, path, **options):
        return self.request("GET", path, **options)


def test_postgres_row_roundtrip_preserves_decimal_null_and_raw(tmp_path):
    from datetime import date

    c = Connection()
    c.rows = [
        (
            date(2026, 9, 1),
            "441",
            "SM",
            None,
            "suppressed",
            "source_suppression_marker",
            "(S)",
            "/1/3",
            "a" * 64,
            2,
            "batch",
            AT,
            None,
        )
    ]
    store = PostgresRetailStore(c, FileRetailBatchArchive(tmp_path / "artifacts"))
    version = store.read_range("2026-09", "2026-09")["2026-09", "441", "SM"]
    assert version.cell.source_value == "(S)" and version.cell.value is None
    assert version.revision == 2 and version.available_at is None
    assert version.cell.source_pointer == "/1/3"


def test_postgres_lost_commit_reconciles_without_reinserting(tmp_path):
    import psycopg

    value = batch(start="2026-09", end="2026-09")
    archive = FileRetailBatchArchive(tmp_path / "artifacts")

    class Uncertain(Connection):
        fail = True
        committed = False

        @contextmanager
        def transaction(self):
            yield
            self.committed = True
            if self.fail:
                self.fail = False
                raise psycopg.OperationalError("synthetic commit acknowledgement lost")

        def execute(self, sql, params=None):
            result = super().execute(sql, params)
            if sql.startswith("SELECT raw_sha256") and self.committed:
                import hashlib

                return Result(
                    [(hashlib.sha256(value.raw).hexdigest(), value.observed_at)]
                )
            return result

    c = Uncertain()
    store = PostgresRetailStore(c, archive, clock=lambda: AT + timedelta(days=2))
    with pytest.raises(RetailStorageError, match="outcome_unknown"):
        store.save_batch("unknown", value)
    assert archive.load("unknown", value.request) == value
    count = len([sql for sql, _ in c.calls if sql.startswith("INSERT")])
    assert count == 7
    assert store.save_batch("unknown", archive.load("unknown", value.request)) == 0
    assert len([sql for sql, _ in c.calls if sql.startswith("INSERT")]) == count
    updates = [(s, p) for s, p in c.calls if s.startswith("UPDATE")]
    assert len(updates) == 2 and updates[0][1][0] == AT + timedelta(days=2)


def test_retained_success_survives_precommit_failure_and_retry_time_is_visible():
    wire = Wire([HttpResult(429, b"", {})])
    svc = service(wire=wire)
    failed = svc.initialize("2026-09", "2026-09", collection_id="deferred")
    assert failed["retry_at"] == AT + timedelta(seconds=300)
    assert failed["provider_attempts"] == 1 and failed["status"] == "ERROR"
    svc = service()
    save = svc.store.save_batch

    def fail(*args):
        raise RetailStorageError("retail_storage_write_failed")

    svc.store.save_batch = fail
    failed = svc.initialize("2026-09", "2026-09", collection_id="staged")
    assert failed["classification"] == "retail_storage_write_failed"
    assert failed["staged_batches"] == 1 and failed["completed_batches"] == 0
    assert svc.store.status()["batch_count"] == 0
    svc.store.save_batch = save
    resumed = svc.initialize("2026-09", "2026-09", collection_id="staged")
    assert resumed["provider_attempts"] == 0 and resumed["saved_points"] == 78


def test_http_source_fill_is_unpersisted_and_history_ignores_permission():
    wire = Wire()
    svc = service(wire=wire)
    http = client(svc, source=True)
    before = svc.store.status()
    current = http.get(PREFIX + "/query?start=2026-09&end=2026-09")
    assert current.status_code == 200 and current.json()["provider_attempts"] == 1
    point = current.json()["points"][0]
    assert {v["origin"] for v in point["input_versions"].values()} == {"upstream"}
    assert all(
        v["observed_at"] == AT.isoformat() for v in point["input_versions"].values()
    )
    assert all(v["available_at"] is None for v in point["input_versions"].values())
    assert point["official_headline_mpcsm"]["unit"] == "percent"
    assert svc.store.status() == before
    for suffix in (
        "&local_only=true",
        "&query_mode=history",
        "&as_of=2026-11-01T00%3A00%3A00Z",
        "&query_mode=current&as_of_basis=available",
    ):
        response = http.get(PREFIX + "/query?start=2026-09&end=2026-09" + suffix)
        assert response.status_code == 200 and response.json()["provider_attempts"] == 0
    assert len(wire.calls) == 1


def test_postgres_rejects_outer_transaction_before_availability_claim(tmp_path):
    from types import SimpleNamespace

    import psycopg

    connection = Connection()
    connection.info = SimpleNamespace(
        transaction_status=psycopg.pq.TransactionStatus.INTRANS
    )
    store = PostgresRetailStore(
        connection, FileRetailBatchArchive(tmp_path / "artifacts")
    )
    with pytest.raises(RetailStorageError, match="top_level_transaction_required"):
        store.save_batch("outer", batch())
    assert connection.calls == []


def test_non_census_source_is_rejected_before_io():
    class OtherProvider:
        def plan(self, *args):
            pytest.fail("wrong provider called")

    svc = RetailTimeSeriesService(
        InMemoryRetailStore(), OtherProvider(), allow_source=True
    )
    with pytest.raises(RetailServiceError, match="source_binding_mismatch"):
        svc.initialize("2026-09", "2026-09")


def test_projection_identity_tracks_inputs_and_cli_explicit_binder(monkeypatch, capsys):
    import sys
    from types import ModuleType

    svc = service()
    svc.store.save_batch("one", batch())
    first = svc.query_range("2026-09", "2026-09", local_only=True)["points"][0]
    table = payload("2025-09", "2026-09")
    target(table, "2026-09", "441")[table[0].index("cell_value")] = "12"
    svc.store.save_batch("two", batch(table, observed=AT + timedelta(days=1)))
    second = svc.query_range("2026-09", "2026-09", local_only=True)["points"][0]
    assert first["snapshot_identity"] != second["snapshot_identity"]
    module = ModuleType("retail_test_binding")
    module.build = lambda: svc
    monkeypatch.setitem(sys.modules, module.__name__, module)
    assert main(["status", "--service-factory", "retail_test_binding:build"]) == 0
    assert "Census" in capsys.readouterr().out


def test_get_query_has_no_schema_or_write_hooks():
    svc = service()

    def forbidden(*args, **kwargs):
        pytest.fail("GET attempted persistence or schema setup")

    svc.store.ensure_schema = forbidden
    svc.store.stage_batch = forbidden
    svc.store.save_batch = forbidden
    svc.store.register_collection = forbidden
    http = client(svc, source=True)
    assert http.get(PREFIX + "/query?start=2026-08&end=2026-09").status_code == 200
    assert http.get(PREFIX + "/status").status_code == 200
    assert http.get(PREFIX + "/api-help").status_code == 200


def test_store_cutoff_requires_aware_time_and_status_excludes_missing_headline(
    tmp_path,
):
    from datetime import datetime

    for store in (
        InMemoryRetailStore(),
        PostgresRetailStore(
            Connection(), FileRetailBatchArchive(tmp_path / "artifacts")
        ),
    ):
        with pytest.raises(RetailStorageError, match="aware_cutoff_required"):
            store.read_range("2026-09", "2026-09", as_of=datetime(2026, 11, 1))
    table = payload("2026-08", "2026-09")
    table.remove(target(table, "2026-09"))
    store = InMemoryRetailStore(clock=lambda: AT + timedelta(days=1))
    store.save_batch("missing", batch(table, start="2026-08"))
    assert store.status()["latest_month"] == "2026-08"
