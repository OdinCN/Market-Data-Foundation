"""Synthetic archive-shaped fixtures: never presented as retained live evidence."""

from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
    digest,
    raw_locator,
)
from market_data_foundation.historical.employment_timeseries_cli import main
from market_data_foundation.historical.macro_release_records import (
    ReleaseRecords,
    migration_sql,
    parse_employment_release,
)
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_employment_web import setup
from tests.test_ppi_timeseries_web import client

URL = "https://www.bls.gov/news.release/archives/empsit_10042024.htm"
AT = datetime(2026, 11, 1, 10, tzinfo=UTC)
# Deliberately hypothetical numbers, not official historical values.
MATERIAL = b"""<html><h1>THE EMPLOYMENT SITUATION -- SEPTEMBER 2024</h1>
<p>Total nonfarm payroll employment increased by 123,000 in September.</p>
<p>The change in total nonfarm payroll employment for July was revised up by
1,000, from +20,000 to +21,000, and the change for August was revised down by
2,000, from +40,000 to +38,000.</p></html>""".replace(b" -- ", b" - ")
UNKNOWN = b"THE EMPLOYMENT SITUATION - SEPTEMBER 2024. No numerical release evidence."


class Raw:
    profile = EMPLOYMENT_PROFILE

    def __init__(self):
        self.objects = {}

    def put_verified(self, raw):
        self.objects[digest(raw)] = raw
        return raw_locator(digest(raw), profile=self.profile)


def records():
    store = InMemoryTimeSeriesStore(Raw(), profile=EMPLOYMENT_PROFILE, clock=lambda: AT)
    result = ReleaseRecords(store)
    result.prepare()
    return result


def test_roles_statistic_month_units_and_exact_material_locator():
    doc = parse_employment_release(MATERIAL, URL)
    claims = doc["claims"]
    assert [(c["role"], c["reference_month"], c["value"]) for c in claims] == [
        ("actual", "2024-09", "123"),
        ("official_previous", "2024-07", "20"),
        ("prior_revised", "2024-07", "21"),
        ("official_previous", "2024-08", "40"),
        ("prior_revised", "2024-08", "38"),
    ]
    assert {
        (c["series_id"], c["statistic"], c["unit"], c["adjustment"]) for c in claims
    } == {("CES0000000001", "net_change_1m", "thousand_jobs", "sa")}
    assert all(
        c["locator"]["excerpt"] and c["continuous_version"] is None for c in claims
    )
    assert doc["actual_at"] is None
    assert doc["source_version"] == {
        "value": "2024-10-04",
        "precision": "date",
        "basis": "BLS_official_archive_filename",
        "source": "BLS Employment Situation archive",
    }
    assert doc["surprise"] is None and doc["consensus"] is None
    assert "nfp_previous_month_revision_not_found_in_material" not in doc["missing"]


def test_late_evidence_inclusive_cutoff_idempotency_same_value_new_event():
    repo = records()
    first = repo.import_employment(MATERIAL, URL)
    assert repo.read(as_of=(AT - timedelta(microseconds=1)).isoformat())["events"] == []
    assert len(repo.read(as_of=AT.isoformat())["events"]) == 1
    repo.store._clock = lambda: AT + timedelta(days=1)
    assert repo.import_employment(MATERIAL, URL) == first
    other = repo.import_employment(
        b"THE EMPLOYMENT SITUATION - OCTOBER 2024. "
        b"Total nonfarm payroll employment increased by 123,000 in October. "
        b"The change in total nonfarm payroll employment for September was revised "
        b"up by 0,000, from +123,000 to +123,000.",
        URL.replace("10042024", "11012024"),
    )
    # Equal numbers in different reference months have different semantic identities.
    assert other["claims"][0]["value"] == first["claims"][0]["value"]
    assert other["claims"][0]["value_identity"] != first["claims"][0]["value_identity"]
    # Same metric/reference/value reported again by a later event may link the
    # same content, without inserting an economic revision into continuous data.
    assert other["claims"][1]["value_identity"] == first["claims"][0]["value_identity"]
    assert len(repo.read()["events"]) == 2
    assert repo.store._history == {} and repo.store._batches == {}
    historical = repo.read(read_mode="source_history", source_version="2024-10-04")
    assert len(historical["events"]) == 1
    assert historical["events"][0]["source_version"]["precision"] == "date"


def test_unknown_and_later_material_do_not_leak_into_old_cutoff():
    repo = records()
    unknown = repo.import_employment(UNKNOWN, URL)
    assert repo.read()["events"][0]["state"] == "unknown"
    assert "nfp_current_net_change_not_found_in_material" in unknown["missing"]
    repo.store._clock = lambda: AT + timedelta(hours=1)
    repo.import_employment(MATERIAL, URL)
    early = repo.read(as_of=AT.isoformat())["events"][0]
    assert early["state"] == "unknown" and len(early["evidence"]) == 1
    current = repo.read()["events"][0]
    assert current["state"] == "confirmed" and len(current["evidence"]) == 2
    assert current["available_at"] != early["available_at"]


@pytest.mark.parametrize(
    "url",
    [
        "https://www.bls.gov/news.release/empsit.nr0.htm",
        "https://other.invalid/news.release/archives/empsit_10042024.htm",
        URL + "?x=y",
    ],
)
def test_no_latest_or_foreign_source_substitution(url):
    with pytest.raises(CpiTimeSeriesError, match="official_archive_url_required"):
        parse_employment_release(MATERIAL, url)


def test_get_is_local_read_only_and_http_help_matches_wire(tmp_path, monkeypatch):
    app, runtime, source, store, connections = setup(tmp_path, monkeypatch)
    repo = ReleaseRecords(store)
    prefix = runtime.employment_timeseries.prefix
    with client(app) as http:
        missing = http.get(prefix + "/releases").json()
        assert missing["events"] == []
        assert not hasattr(store, "_release_evidence")
        assert not source.calls and not connections
        store.raw_store = Raw()
        repo.prepare()
        repo.import_employment(MATERIAL, URL)
        saved = len(store.raw_store.objects)
        response = http.get(
            prefix + "/releases?as_of=" + source.at.isoformat().replace("+", "%2B")
        )
        assert response.status_code == 200
        doc = response.json()
        # Fixture source clock predates archive acquisition only in real-world
        # calendar, but import uses its current MDF clock, never archive time.
        event = doc["events"][0]
        assert event["source_version"]["precision"] == "date"
        assert event["actual_at"] is None
        assert event["claims"][0]["role"] == "actual"
        assert doc["persisted"] is False and doc["provider_requests"] == 0
        detail = http.get(
            prefix + "/releases", params={"event_id": event["event_id"]}
        ).json()
        assert detail["events"] == doc["events"]
        help_doc = http.get(prefix + "/api-help").json()
        assert "releases" in help_doc["operations"]
        assert help_doc["release_records"]["read_only"] is True
        assert http.get(prefix + "/releases?as_of=2024-10-04").status_code == 400
        assert http.get(prefix + "/releases?latest=true").status_code == 400
        assert http.get(prefix + "/releases?as_of=x&as_of=y").status_code == 400
        assert (
            not source.calls
            and not connections
            and len(store.raw_store.objects) == saved
        )
        assert store._history == {}


def test_denied_event_reads_do_not_open_service(tmp_path, monkeypatch):
    app, runtime, source, _, connections = setup(tmp_path, monkeypatch, allowed=False)
    with client(app) as http:
        assert (
            http.get(runtime.employment_timeseries.prefix + "/releases").status_code
            == 403
        )
    assert not source.calls and not connections


def test_explicit_cli_prepare_import_read_and_other_family_missing(tmp_path, capsys):
    repo = records()
    repo.store.close = lambda: None
    service = type(
        "Service",
        (),
        {
            "profile": EMPLOYMENT_PROFILE,
            "store": repo.store,
            "close": lambda self: None,
        },
    )()

    def factory(**options):
        return service

    material = tmp_path / "release.htm"
    material.write_bytes(MATERIAL)
    assert (
        main(
            ["import-release", "--file", str(material), "--source-url", URL],
            service_factory=factory,
        )
        == 0
    )
    assert main(["releases", "--as-of", AT.isoformat()], service_factory=factory) == 0
    assert '"provider_requests": 0' in capsys.readouterr().out
    other = ReleaseRecords(InMemoryTimeSeriesStore(profile=CPI_PROFILE))
    assert other.read()["missing"] == [
        "official_release_evidence_not_available_at_cutoff"
    ]
    ddl = migration_sql(EMPLOYMENT_PROFILE)
    assert "CREATE SCHEMA" not in ddl and ".revisions" not in ddl
    assert "available_at timestamptz" in ddl


def test_surprise_requires_matching_pre_release_consensus_but_no_prior_value():
    from market_data_foundation.historical.macro_release_records import release_surprise

    doc = parse_employment_release(MATERIAL, URL)
    actual = dict(doc["claims"][0], confirmation_state="confirmed")
    consensus = {
        **{
            key: actual[key]
            for key in (
                "series_id",
                "reference_month",
                "statistic",
                "unit",
                "adjustment",
            )
        },
        "event_id": doc["event_id"],
        "source": "synthetic-qualified-survey",
        "method": "median",
        "value": "120",
        "source_available_at": "2024-10-04T12:00:00Z",
        "frozen_at": "2024-10-04T12:01:00Z",
    }
    args = {"event_id": doc["event_id"], "actual_at": "2024-10-04T12:30:00Z"}
    assert release_surprise(actual, consensus, **args) == ("3", [])
    assert (
        release_surprise(actual, {**consensus, "statistic": "level"}, **args)[0] is None
    )
    assert (
        release_surprise(actual, {**consensus, "frozen_at": args["actual_at"]}, **args)[
            0
        ]
        is None
    )
    assert release_surprise(actual, None, **args) == (
        None,
        ["consensus_source_unavailable"],
    )
    assert release_surprise(actual, consensus, **{**args, "actual_at": None})[0] is None
    assert parse_employment_release(MATERIAL.replace(b" - ", b" -- "), URL)["claims"]


def test_projection_retention_is_explicit_and_raw_failure_is_classified():
    repo = records()
    doc = repo.import_employment(
        MATERIAL, URL, material_format="official_summary_projection"
    )
    assert doc["retention"]["exact_original_retained"] is False
    assert doc["retention"]["coverage"] == "summary_only_no_statistical_tables"
    assert repo.store.raw_store.objects[doc["raw_sha256"]] == MATERIAL
    from market_data_foundation.historical.cpi_timeseries_store import CpiStorageError

    def broken(raw):
        raise CpiStorageError("cpi_timeseries_raw_write_rejected")

    repo.store.raw_store.put_verified = broken
    with pytest.raises(CpiTimeSeriesError, match="raw_write_rejected"):
        repo.import_employment(MATERIAL + b" ", URL)
    assert len(repo.read()["events"][0]["evidence"]) == 1


def test_retail_release_endpoint_is_local_without_bootstrap_or_provider(
    tmp_path, monkeypatch
):
    from tests.test_retail_timeseries_web import retail

    app, runtime, data, wire, calls = retail(tmp_path, monkeypatch)
    with client(app) as http:
        response = http.get(runtime.retail_timeseries.prefix + "/releases")
        assert response.status_code == 200
        doc = response.json()
        assert doc["events"] == []
        assert doc["missing"] == ["official_release_evidence_not_available_at_cutoff"]
        assert (
            http.get(
                runtime.retail_timeseries.prefix + "/releases?as_of=2024-01-01"
            ).status_code
            == 400
        )
    assert calls == ["read", "read"] and not wire.calls and not data.batches
