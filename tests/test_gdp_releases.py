"""Synthetic GDP release fixtures. No real archive content or provider calls in CI."""

from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.historical.gdp_release_material import parse_release
from market_data_foundation.historical.gdp_release_records import GdpReleaseRecords
from market_data_foundation.historical.gdp_store import GdpStorageError
from market_data_foundation.historical.gdp_timeseries import GdpTimeSeriesError
from tests.test_gdp_chain import service

AT = datetime(2026, 11, 2, tzinfo=UTC)
URL = "https://www.bea.gov/news/2026/gdp-{}-estimate-2nd-quarter-2026"


def material(kind="third", actual="2.2", *, prior=True):
    kinds = ["Advance", "Second", "Third"][
        : ["advance", "second", "third"].index(kind) + 1
    ]
    values = ["1.5"] * (len(kinds) - 1) + [actual]
    headings = "".join(f"<th>{k} Estimate</th>" for k in kinds)
    numbers = "".join(f"<td>{v}</td>" for v in values)
    earlier = (
        """
    <table><caption>First Quarter 2026</caption>
    <tr><th></th><th>Previous Estimate</th><th>Revised Estimate</th></tr>
    <tr><td>(Percent change (SAAR) from preceding quarter)</td></tr>
    <tr><td>Real GDP</td><td>2.1</td><td>2.5</td></tr></table>
    """
        if prior
        else ""
    )
    return f"""
    <h1>News Release</h1><h1>GDP ({kind.title()} Estimate), 2nd Quarter 2026</h1>
    <p>EMBARGOED UNTIL RELEASE AT 8:30 a.m. EDT, Wednesday,
    September 30, 2026 BEA 26—00</p>
    <table><caption>Real GDP and Related Measures
    [Percent change (SAAR) from 2026:Q1 to 2026:Q2]</caption>
    <tr><th></th>{headings}</tr><tr><td>Real GDP</td>{numbers}</tr></table>
    {earlier}
    """.encode()


def records():
    s, wire, clock, archive = service()
    clock[0] = AT
    evidence = GdpReleaseRecords(s.store)
    evidence.prepare()
    return s, evidence, clock, archive


@pytest.mark.parametrize(
    ("kind", "count", "delta"),
    [("advance", 1, None), ("second", 2, "0.7"), ("third", 3, "0.7")],
)
def test_estimate_columns_are_explicit_and_not_local_revision(kind, count, delta):
    doc = parse_release(material(kind, prior=False), URL.format(kind))
    assert len(doc["claims"]) == count
    assert doc["estimate_kind"] == kind and doc["reference_quarter"] == "2026-Q2"
    assert doc["claims"][-1]["value"] == "2.2"
    assert doc["claims"][-1]["confirmation_state"] == "confirmed"
    assert all(c["continuous_version"] is None for c in doc["claims"])
    assert doc["previous_estimate_revision_pp"] == delta
    assert doc["actual_at"] is None and doc["source_version"]["precision"] == "date"
    assert doc["surprise"] is None
    assert len(doc["missing_scopes"]) == 7


def test_prior_quarter_old_new_values_are_separate_from_same_quarter_estimates():
    doc = parse_release(material(), URL.format("third"))
    assert [
        (c["reference_quarter"], c["role"], c["value"]) for c in doc["claims"][-2:]
    ] == [
        ("2026-Q1", "prior_original", "2.1"),
        ("2026-Q1", "prior_revised", "2.5"),
    ]
    assert doc["previous_estimate_revision_pp"] == "0.7"


@pytest.mark.parametrize("value", ["0", "-0.5", "3.40"])
def test_zero_negative_and_source_precision_are_preserved(value):
    doc = parse_release(material(actual=value), URL.format("third"))
    assert next(c for c in doc["claims"] if c["role"] == "actual")["value"] == value


def test_official_null_is_not_zero():
    doc = parse_release(material(actual="…"), URL.format("third"))
    assert next(c for c in doc["claims"] if c["role"] == "actual")["value"] is None
    assert doc["previous_estimate_revision_pp"] is None


@pytest.mark.parametrize(
    "url",
    [
        URL.format("third").replace("https:", "http:"),
        URL.format("third").replace("www.bea.gov", "bea.gov.evil.test"),
        URL.format("third") + "?x=1",
        URL.format("third") + "#other",
        URL.format("second"),
        URL.format("third").replace("2nd-quarter", "3rd-quarter"),
    ],
)
def test_official_identity_guard(url):
    with pytest.raises(GdpTimeSeriesError):
        parse_release(material(), url)


@pytest.mark.parametrize(
    ("old", "new"),
    [
        (b"Second Estimate</th>", b"Other Estimate</th>"),
        (b"2026:Q1 to 2026:Q2", b"2025:Q4 to 2026:Q2"),
        (b"Real GDP</td>", b"Nominal GDP</td>"),
        (b"September 30, 2026", b"June 30, 2026"),
        (b"2nd Quarter 2026", b"5th Quarter 2026"),
        (b"<td>2.2</td>", b"<td>NaN</td>"),
        (b"<th>Third Estimate</th>", b""),
    ],
)
def test_bad_period_columns_units_and_identity_fail_closed(old, new):
    with pytest.raises(GdpTimeSeriesError):
        parse_release(material().replace(old, new), URL.format("third"))


def test_multiple_candidate_tables_or_titles_are_not_silently_selected():
    raw = material()
    with pytest.raises(GdpTimeSeriesError):
        parse_release(raw + raw, URL.format("third"))


def test_evidence_import_is_idempotent_cutoff_safe_and_does_not_change_values():
    s, r, clock, archive = records()
    s.init_range("2026-Q2", "2026-Q2")
    status = s.status()
    raw = material()
    first = r.import_material(
        raw, URL.format("third"), material_format="original_http_body"
    )
    assert s.status() == status
    before = datetime.fromisoformat(first["available_at"]) - timedelta(microseconds=1)
    assert r.read(as_of=before)["events"] == []
    assert (
        r.read(as_of=first["available_at"])["events"][0]["claims"][2]["value"] == "2.2"
    )
    clock[0] += timedelta(days=1)
    assert (
        r.import_material(
            raw, URL.format("third"), material_format="original_http_body"
        )
        == first
    )
    assert len(r.read()["events"]) == 1
    assert r.read(source_version="2026-09-30")["events"]
    assert r.read(reference_quarter="2026-Q1")["events"]
    assert not r.read(reference_quarter="2026-Q3")["events"]
    assert (
        r.read(read_mode="source_history")["events"][0]["obtained_at"]
        == first["obtained_at"]
    )
    continuous = s.query_range("2026-Q2", "2026-Q2")
    linked = r.link_result(continuous)
    growth = next(p for p in linked["points"] if p["series_id"] == "A191RL")
    level = next(p for p in linked["points"] if p["series_id"] == "A191RX")
    assert growth["related_releases"][0]["continuous_version_bound"] is False
    assert growth["estimate_kind"] == "unknown"
    assert level["related_releases"] == []
    assert "related_releases" not in continuous["points"][0]


def test_conflicting_material_blocks_event_actual_without_destroying_history():
    _, r, _, _ = records()
    r.import_material(material(actual="2.2"), URL.format("third"))
    r.import_material(material(actual="2.3"), URL.format("third"))
    event = r.read()["events"][0]
    actual = next(c for c in event["claims"] if c["role"] == "actual")
    assert actual["confirmation_state"] == "conflict" and actual["value"] is None
    assert event["previous_estimate_revision_pp"] is None and event["surprise"] is None
    assert "event_actual_unconfirmed" in event["surprise_missing"]
    assert len(r.read(read_mode="source_history")["events"]) == 2


def test_raw_failure_leaves_no_release_record():
    _, r, _, archive = records()

    def failure(raw):
        raise GdpStorageError("gdp_test_raw_unavailable")

    archive.put_verified = failure
    with pytest.raises(GdpTimeSeriesError, match="gdp_test_raw_unavailable"):
        r.import_material(material(), URL.format("third"))
    assert r.read()["events"] == []


def test_pending_availability_is_hidden_and_replay_uses_current_confirmation_time():
    s, r, clock, archive = records()
    original = archive.put_verified

    def backwards(raw):
        locator = original(raw)
        clock[0] -= timedelta(days=1)
        return locator

    archive.put_verified = backwards
    with pytest.raises(GdpTimeSeriesError, match="availability_confirmation_unknown"):
        r.import_material(material(), URL.format("third"))
    assert r.read()["events"] == []
    archive.put_verified = original
    clock[0] = AT + timedelta(days=2)
    imported = r.import_material(material(), URL.format("third"))
    assert datetime.fromisoformat(imported["obtained_at"]) == AT + timedelta(seconds=1)
    assert datetime.fromisoformat(imported["available_at"]) == clock[0] + timedelta(
        seconds=1
    )
    assert not r.read(as_of=AT + timedelta(days=1))["events"]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"read_mode": "latest"},
        {"reference_quarter": "2026-07"},
        {"source_version": "2026-09-30T00:00:00Z"},
        {"as_of": "2026-11-02"},
    ],
)
def test_invalid_reads_do_not_guess(kwargs):
    _, r, _, _ = records()
    with pytest.raises(GdpTimeSeriesError):
        r.read(**kwargs)


def test_unprepared_reads_are_explicit_and_import_does_not_migrate():
    s, _, _, _ = service()
    r = GdpReleaseRecords(s.store)
    assert r.read()["missing"] == ["gdp_release_schema_not_prepared"]
    with pytest.raises(GdpTimeSeriesError, match="schema_not_prepared"):
        r.import_material(material(), URL.format("third"))


@pytest.mark.postgres_integration
def test_isolated_postgres_release_import_and_strict_cutoff():
    import os
    from uuid import uuid4

    from market_data_foundation.historical.gdp_store import PostgresGdpStore
    from market_data_foundation.historical.gdp_timeseries import build_service

    if os.getenv("MDF_GDP_PG_TEST") != "1":
        pytest.skip("Explicit isolated PostgreSQL opt-in required")
    runtime = build_service()
    _, _, _, archive = service()
    schema = "test_gdp_" + uuid4().hex
    pg = PostgresGdpStore(runtime.store.connection, archive, schema=schema)
    r = GdpReleaseRecords(pg)
    try:
        pg.ensure_schema()
        r.prepare()
        r.prepare()
        first = r.import_material(material(), URL.format("third"))
        assert r.import_material(material(), URL.format("third")) == first
        assert pg.status()["observation_count"] == 0
        cutoff = datetime.fromisoformat(first["available_at"]) - timedelta(
            microseconds=1
        )
        assert not r.read(as_of=cutoff)["events"]
        assert len(r.read(as_of=first["available_at"])["events"]) == 1
        assert len(r.read(read_mode="source_history")["events"]) == 1
        r.import_material(material(actual="2.3"), URL.format("third"))
        assert r.read()["events"][0]["conflicts"]
        assert len(r.read(read_mode="source_history")["events"]) == 2
    finally:
        runtime.store.connection.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        runtime.close()


@pytest.mark.parametrize("command", ["prepare", "import"])
def test_release_cli_requires_write_opt_in_before_opening_ports(monkeypatch, command):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts/gdp_release_records.py"
    spec = importlib.util.spec_from_file_location("gdp_release_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden():
        raise AssertionError("Port opened before explicit write opt-in")

    monkeypatch.setattr(module, "build_service", forbidden)
    with pytest.raises(SystemExit) as error:
        module.main([command])
    assert error.value.code == 2


def test_release_cli_read_does_not_prepare_schema(monkeypatch, capsys):
    import importlib.util
    import json
    from pathlib import Path
    from types import SimpleNamespace

    path = Path(__file__).resolve().parents[1] / "scripts/gdp_release_records.py"
    spec = importlib.util.spec_from_file_location("gdp_release_read_cli_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    s, _, _, _ = service()
    monkeypatch.setattr(
        module,
        "build_service",
        lambda: SimpleNamespace(store=s.store, close=lambda: None),
    )
    assert module.main(["read"]) == 0
    assert json.loads(capsys.readouterr().out)["missing"] == [
        "gdp_release_schema_not_prepared"
    ]
    assert not hasattr(s.store, "_gdp_release_evidence")


def test_release_reads_and_query_links_do_not_acquire_writer_lock(monkeypatch):
    from contextlib import contextmanager

    candidate, evidence, clock, archive = records()
    candidate.init_range("2026-Q2", "2026-Q2")
    imported = evidence.import_material(material(), URL.format("third"))
    continuous = candidate.query_range("2026-Q2", "2026-Q2")

    @contextmanager
    def writer_busy():
        raise GdpStorageError("gdp_timeseries_busy")
        yield

    monkeypatch.setattr(candidate.store, "operation", writer_busy)
    assert evidence.read()["events"][0]["event_id"] == imported["event_id"]
    assert evidence.read(read_mode="source_history")["events"]
    assert evidence.read(as_of=AT - timedelta(seconds=1))["events"] == []
    linked = evidence.link_result(continuous)
    growth = next(p for p in linked["points"] if p["series_id"] == "A191RL")
    assert growth["related_releases"][0]["continuous_version_bound"] is False
    with pytest.raises(GdpStorageError, match="gdp_timeseries_busy"):
        evidence.import_material(material(actual="2.3"), URL.format("third"))
