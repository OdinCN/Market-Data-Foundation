"""Retained government material plus synthetic version/consensus contracts.

No provider requests or business DB writes. Synthetic forecasts never represent
an actual source delivery. Focused fixtures carry their projection provenance.
"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
    batch_identity,
    digest,
    raw_locator,
)
from market_data_foundation.historical.macro_release_acquisition import (
    MaterialAcquirer,
    select_material,
)
from market_data_foundation.historical.macro_release_material import parse_material
from market_data_foundation.historical.macro_release_records import (
    ReleaseRecords,
    migration_sql,
)
from market_data_foundation.historical.retail_timeseries_store import (
    InMemoryRetailStore,
)

FIXTURES = Path(__file__).parent / "fixtures" / "macro_release"
MANIFEST = json.loads((FIXTURES / "provenance.json").read_text())
AT = datetime(2026, 11, 1, 11, tzinfo=UTC)
PROFILES = {
    p.key: p for p in (CPI_PROFILE, PPI_PROFILE, PCE_PROFILE, EMPLOYMENT_PROFILE)
}


def material(name):
    row = next(x for x in MANIFEST if x["file"] == name)
    raw = (FIXTURES / name).read_bytes()
    assert digest(raw) == row["sha256"]
    assert row["fixture_retention"] == "focused_official_material_projection"
    return raw, row["source_url"]


class Raw:
    def __init__(self, profile):
        self.profile, self.objects = profile, {}

    def put_verified(self, raw):
        self.objects[digest(raw)] = raw
        return raw_locator(digest(raw), profile=self.profile)


def repository(family):
    if family == "retail":
        store = InMemoryRetailStore(clock=lambda: AT)
        store.raw_store = Raw(CPI_PROFILE)
    else:
        store = InMemoryTimeSeriesStore(
            Raw(PROFILES[family]), profile=PROFILES[family], clock=lambda: AT
        )
    repo = ReleaseRecords(store)
    repo.prepare()
    return repo


@pytest.mark.parametrize(
    "family,name,series,statistic,value",
    [
        ("employment", "empsit_10022026.html", "CES0000000001", "net_change_1m", "29"),
        ("employment", "empsit_09042026.html", "CES0000000001", "net_change_1m", "162"),
        ("cpi", "cpi_10142026.html", "CUSR0000SAH1", "mom_pct", "0.4"),
        ("ppi", "ppi_10152026.html", "WPSFD49116", "mom_pct", "0.5"),
        ("pce", "bea-pio-202609.html", "DPCCRG", "mom_pct", "0.3"),
        ("retail", "census-retail.html", "44X72:SM", "level", "745000.0"),
    ],
)
def test_retained_official_scope_and_import_read_chain(
    family, name, series, statistic, value
):
    raw, url = material(name)
    repo = repository(family)
    saved = repo.import_material(
        raw, url, material_format="official_selected_projection"
    )
    assert saved["retention"]["exact_original_retained"] is False
    event = repo.read()["events"][0]
    assert event["source_version"]["precision"] == "date"
    assert event["actual_at"] is None
    matches = [
        c
        for c in event["claims"]
        if c["series_id"] == series
        and c["statistic"] == statistic
        and c["role"] == "actual"
    ]
    assert len(matches) == 1 and matches[0]["value"] == value
    assert matches[0]["evidence_id"] == saved["evidence_id"]
    assert matches[0]["continuous_version"] is None
    assert repo.read(as_of=(AT - timedelta(microseconds=1)).isoformat())["events"] == []
    assert repo.read(as_of=AT.isoformat())["events"]
    assert (
        repo.import_material(raw, url, material_format="official_selected_projection")[
            "evidence_id"
        ]
        == saved["evidence_id"]
    )
    assert not getattr(repo.store, "_history", {}) and not getattr(
        repo.store, "revisions", {}
    )


def test_real_employment_revisions_rates_earnings_and_sector_changes_not_levels():
    doc = parse_material(*material("empsit_10022026.html"), "employment")
    values = {
        (c["series_id"], c["statistic"], c["role"], c["reference_month"]): c["value"]
        for c in doc["claims"]
    }
    assert (
        values["CES0000000001", "net_change_1m", "official_previous", "2026-08"]
        == "162"
    )
    assert values["CES0000000001", "net_change_1m", "prior_revised", "2026-08"] == "133"
    assert values["CES0000000001", "net_change_1m", "prior_revised", "2026-07"] == "-10"
    assert values["LNS14000000", "level", "actual", "2026-09"] == "4.2"
    assert values["LNS11300000", "level", "actual", "2026-09"] == "61.8"
    assert values["LNS12300000", "level", "actual", "2026-09"] == "59.2"
    assert values["CES0500000002", "level", "actual", "2026-09"] == "34.4"
    assert values["CES0500000003", "level", "actual", "2026-09"] == "37.81"
    assert values["CES6000000001", "net_change_1m", "actual", "2026-09"] == "-9"
    assert not any(
        c["series_id"] == "CES6000000001" and c["statistic"] == "level"
        for c in doc["claims"]
    )


def test_cpi_sa_month_nsa_year_index_and_ppi_goods_core_are_not_confused():
    cpi = parse_material(*material("cpi_10142026.html"), "cpi")
    headline = [c for c in cpi["claims"] if c["series_id"].endswith("SA0")]
    assert {(c["statistic"], c["adjustment"], c["value"]) for c in headline} == {
        ("index", "nsa", "336.940"),
        ("yoy_pct", "nsa", "3.7"),
        ("mom_pct", "sa", "0.6"),
    }
    ppi = parse_material(*material("ppi_10152026.html"), "ppi")
    assert len(ppi["claims"]) == 4  # September 2025 row must not masquerade as 2026.
    assert not any(c["series_id"].endswith("49104") for c in ppi["claims"])
    assert "ppi_core_ex_food_energy_not_in_selected_material" in ppi["missing"]
    assert ppi["material_footnotes"]  # Genuine revised-month note; no invented P flag.


def test_bea_separate_old_event_and_current_revised_prior_are_both_retained():
    repo = repository("pce")
    older = repo.import_material(*material("bea-pio-202608.html"))
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    newer = repo.import_material(*material("bea-pio-202609.html"))
    event = repo.read(event_id=newer["event_id"])["events"][0]
    core = next(
        c
        for c in event["claims"]
        if c["series_id"] == "DPCCRG" and c["role"] == "prior_revised"
    )
    assert core["reference_month"] == "2026-08" and core["value"] == "0.3"
    original = next(
        c
        for c in event["prior_event_values"]
        if c["series_id"] == "DPCCRG" and c["statistic"] == "mom_pct"
    )
    assert (
        original["value"] == "0.2" and original["evidence_id"] == older["evidence_id"]
    )
    assert original["available_at"] == AT.isoformat()


def test_delayed_bea_multi_month_coverage_one_canonical_event_and_filter():
    raw, url = material("bea-pio-202609.html")
    multi = raw.replace(
        b"Personal Income and Outlays, September 2026",
        b"Personal Income and Outlays, August and September 2026",
    )
    repo = repository("pce")
    saved = repo.import_material(
        multi, url.replace("september-2026", "august-and-september-2026")
    )
    assert saved["reference_months"] == ["2026-08", "2026-09"]
    assert {c["reference_month"] for c in saved["claims"] if c["role"] == "actual"} == {
        "2026-08",
        "2026-09",
    }
    assert saved["event_id"] == "bea:personal-income-and-outlays:2026-10-29"
    assert len(repo.read(reference_month="2026-08")["events"]) == 1
    assert repo.read(reference_month="2026-07")["events"] == []


def test_partial_evidence_preserves_prior_proven_scope_and_conflict_correction_trace():
    repo = repository("employment")
    raw, url = material("empsit_10022026.html")
    full = repo.import_material(raw, url)
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    summary = b"THE EMPLOYMENT SITUATION - SEPTEMBER 2026. No numerical claims."
    repo.import_material(summary, url)
    current = repo.read()["events"][0]
    assert (
        len(current["claims"]) == len(full["claims"])
        and current["state"] == "confirmed"
    )
    assert (
        repo.read(as_of=AT.isoformat())["events"][0]["available_at"] == AT.isoformat()
    )
    repo.store._clock = lambda: AT + timedelta(minutes=2)
    conflict = repo.import_material(raw.replace(b"29,000", b"30,000"), url)
    current = repo.read()["events"][0]
    actual = next(
        c
        for c in current["claims"]
        if c["series_id"] == "CES0000000001" and c["role"] == "actual"
    )
    assert actual["value"] == "29" and actual["confirmation_state"] == "conflicted"
    assert (
        current["conflicts"][-1]["selection_basis"]
        == "first_proven_claim_pending_resolution"
    )
    repo.store._clock = lambda: AT + timedelta(minutes=3)
    repo.import_material(
        raw.replace(b"29,000", b"30,000"), url, supersedes_evidence=full["evidence_id"]
    )
    current = repo.read()["events"][0]
    actual = next(
        c
        for c in current["claims"]
        if c["series_id"] == "CES0000000001" and c["role"] == "actual"
    )
    assert (
        actual["value"] == "30"
        and current["conflicts"][-1]["selection_basis"] == "explicit_parser_correction"
    )
    assert (
        len(current["evidence"]) == 4 and conflict["evidence_id"] != full["evidence_id"]
    )


def synthetic_consensus(event, actual):
    return {
        **{
            k: actual[k]
            for k in ("series_id", "reference_month", "statistic", "unit", "adjustment")
        },
        "event_id": event["event_id"],
        "value": "27",
        "source": "SYNTHETIC_TEST_SURVEY",
        "method": "median",
        "source_available_at": {
            "value": "2026-10-01",
            "precision": "date",
            "timezone": "America/New_York",
        },
        "frozen_at": {
            "value": "2026-10-01",
            "precision": "date",
            "timezone": "America/New_York",
        },
        "provenance": {
            "locator": "synthetic-fixture-only",
            "sha256": "a" * 64,
            "synthetic": True,
        },
    }


def test_persisted_consensus_before_date_event_no_official_prior_required_and_cutoff():
    repo = repository("employment")
    raw, url = material("empsit_10022026.html")
    # Remove revision prose; actual still qualifies for surprise.
    raw = raw[: raw.find(b"<table")]
    saved = repo.import_material(raw, url)
    actual = next(c for c in saved["claims"] if c["role"] == "actual")
    consensus = synthetic_consensus(saved, actual)
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    frozen = repo.freeze_consensus(consensus)
    assert frozen["obtained_at"].startswith("2026-11-01")
    assert frozen["source_available_at"]["value"] == "2026-10-01"
    latest = repo.read()["events"][0]["claims"][0]
    assert latest["surprise"] == "2"  # Date evidence proves ordering, actual_at null.
    assert latest["mdf_pre_event_last_consumable_previous"] is None
    early = repo.read(as_of=AT.isoformat())["events"][0]["claims"][0]
    assert early["surprise"] is None and early["surprise_missing"] == [
        "consensus_source_unavailable"
    ]
    assert repo.freeze_consensus(consensus)["record_id"] == frozen["record_id"]
    with pytest.raises(CpiTimeSeriesError, match="scope_mismatch"):
        repo.freeze_consensus(dict(consensus, unit="index"))
    unknown_order = dict(
        consensus,
        frozen_at={
            "precision": "date",
            "value": "2026-10-02",
            "timezone": "America/New_York",
        },
    )
    repo.store._clock = lambda: AT + timedelta(minutes=2)
    repo.freeze_consensus(unknown_order)
    assert repo.read()["events"][0]["claims"][0]["surprise"] is None


def test_proven_version_association_positive_not_equality_and_late_cutoff():
    repo = repository("employment")
    raw, url = material("empsit_10022026.html")
    numeric_raw = json.dumps({"release_url": url, "synthetic": True}).encode()
    source_at = datetime(2026, 10, 1, 12, tzinfo=UTC)
    repo.store._clock = lambda: source_at
    batch = SourceBatch(
        (
            SourcePoint(
                "CES0000000001",
                "2026-09",
                Decimal("100029"),
                {"net_changes": {"1": "29"}},
                (),
            ),
        ),
        numeric_raw,
        {},
        source_at,
        1,
    )
    repo.store.save_batches((batch,))
    repo.store._clock = lambda: AT
    saved = repo.import_material(raw, url)
    actual = saved["claims"][0]
    version = {
        "revision": 1,
        "batch_id": batch_identity(batch),
        "raw_sha256": digest(numeric_raw),
    }
    with pytest.raises(CpiTimeSeriesError, match="explicit_version_proof"):
        repo.bind_version(
            event_id=saved["event_id"],
            evidence_id=saved["evidence_id"],
            value_identity=actual["value_identity"],
            version=version,
            proof={},
        )
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    bound = repo.bind_version(
        event_id=saved["event_id"],
        evidence_id=saved["evidence_id"],
        value_identity=actual["value_identity"],
        version=version,
        proof={
            "kind": "official_version_reference",
            "official_version_token": url,
            "version_metadata_pointer": "/release_url",
            "numeric_response": numeric_raw,
        },
    )
    current = repo.read()["events"][0]["claims"][0]
    assert current["continuous_version"]["batch_id"] == batch_identity(batch)
    assert current["association"]["record_id"] == bound["record_id"]
    assert current["mdf_first_captured"]["value"] == "29"
    assert current["mdf_first_captured"]["versions"][0]["batch_id"] == batch_identity(
        batch
    )
    assert (
        repo.read(as_of=AT.isoformat())["events"][0]["claims"][0]["continuous_version"]
        is None
    )
    assert len(repo.store._history["CES0000000001", "2026-09"]) == 1


def test_mdf_first_and_pre_event_last_values_keep_one_cutoff_and_batch_refs():
    repo = repository("employment")
    early = datetime(2026, 10, 1, 12, tzinfo=UTC)
    repo.store._clock = lambda: early
    b = SourceBatch(
        tuple(
            SourcePoint("LNS14000000", month, Decimal(value), {}, ())
            for month, value in [("2026-08", "4.1"), ("2026-09", "4.2")]
        ),
        b"synthetic",
        {},
        early,
        1,
    )
    repo.store.save_batches((b,))
    repo.store._clock = lambda: AT
    repo.import_material(*material("empsit_10022026.html"))
    claim = next(
        c for c in repo.read()["events"][0]["claims"] if c["series_id"] == "LNS14000000"
    )
    assert claim["mdf_first_captured"]["value"] == "4.2"
    prior = claim["mdf_pre_event_last_consumable_previous"]
    assert prior["value"] == "4.1" and prior["versions"][0]["month"] == "2026-08"
    assert prior["as_of"] == "2026-10-02T03:59:59.999999+00:00"
    assert claim["continuous_version"] is None  # same number is insufficient.


def test_material_acquisition_bounded_durable_failure_and_success_dedup(tmp_path):
    repo = repository("employment")
    raw, url = material("empsit_10022026.html")
    selection = {
        "source_url": url,
        "source_date": "2026-10-02",
        "reference_month": "2026-09",
    }
    calls = []

    def unavailable(*args):
        calls.append(args)
        raise CpiTimeSeriesError("http_403")

    failed = MaterialAcquirer(tmp_path / "failed.json", fetcher=unavailable)
    assert failed.capture(repo.store, selection)["reasons"] == ["http_403"]
    assert (
        failed.capture(repo.store, selection)["transport_calls"] == 0
        and len(calls) == 1
    )
    assert json.loads((tmp_path / "failed.json").read_text())

    def fetch(*args):
        calls.append(args)
        return raw

    acquirer = MaterialAcquirer(tmp_path / "ok.json", fetcher=fetch)
    result = acquirer.capture(repo.store, selection, batch_ids=["numeric-batch"])
    assert result["state"] == "retained" and result["numeric_batch_ids"] == [
        "numeric-batch"
    ]
    assert result["event_ids"] == ["bls:employment-situation:2026-10-02"]
    assert (
        acquirer.capture(repo.store, selection)["transport_calls"] == 0
        and len(calls) == 2
    )
    assert (
        repo.read()["events"][0]["retention"]["acquisition_method"]
        == "bounded_official_transport"
    )


def test_selector_never_initializes_240_archives_and_refuses_unmapped_future():
    targets = [
        {
            "reference_month": "2026-08",
            "release_at": "2026-09-04T12:30:00Z",
            "reference_month_evidence": {
                "source": "https://www.bls.gov/schedule/2026/10_sched.htm"
            },
        },
        {
            "reference_month": "2026-09",
            "release_at": "2026-10-02T12:30:00Z",
            "reference_month_evidence": {
                "source": "https://www.bls.gov/schedule/2026/10_sched.htm"
            },
        },
        {
            "reference_month": "2026-10",
            "release_at": "2026-11-06T13:30:00Z",
            "reference_month_evidence": {
                "source": "https://www.bls.gov/schedule/2026/10_sched.htm"
            },
        },
    ]
    selected = select_material(
        "employment", {"targets": targets}, "2006-10", "2026-10", AT
    )
    assert selected["source_url"].endswith("empsit_10022026.htm")
    assert (
        select_material(
            "employment",
            {"targets": [dict(targets[1], reference_month_evidence=None)]},
            "2026-09",
            "2026-09",
            AT,
        )
        is None
    )
    assert "ALTER TABLE" in migration_sql(EMPLOYMENT_PROFILE)
    assert "DROP" not in migration_sql(EMPLOYMENT_PROFILE)


@pytest.mark.parametrize(
    "family,name",
    [
        ("cpi", "cpi_10142026.html"),
        ("ppi", "ppi_10152026.html"),
        ("pce", "bea-pio-202609.html"),
        ("employment", "empsit_10022026.html"),
    ],
)
def test_manual_and_worker_material_failure_preserves_numeric_success(
    tmp_path, monkeypatch, family, name
):
    from market_data_foundation.historical import (
        macro_release_acquisition as acquisition,
    )
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService
    from tests.test_macro_research_fields import make_host

    host, source, store = make_host(PROFILES[family], tmp_path / family)
    repo = repository(family)
    store.raw_store = repo.store.raw_store
    releases = ReleaseRecords(store)
    releases.prepare()
    raw, url = material(name)
    doc = parse_material(raw, url, family)
    target = {
        "reference_month": "2026-09",
        "release_at": doc["source_version"]["value"] + "T12:30:00Z",
        "reference_month_evidence": {
            "source": "https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics"
            if family == "pce"
            else "https://www.bls.gov/schedule/2026/10_sched.htm",
            "reference_month": "2026-09",
        },
    }
    monkeypatch.setattr(host.scheduler, "status", lambda: {"targets": [target]})
    calls = []

    def fetch(url, family):
        calls.append((url, family))
        raise CpiTimeSeriesError("http_403")

    monkeypatch.setattr(acquisition, "fetch_official_material", fetch)
    command = {
        "request_id": "12345678-1234-4234-8234-123456789abc",
        "action": "update",
        "start": "2026-09",
        "end": "2026-09",
    }
    job = host.submit(command)
    assert job["state"] == "complete" and job["result"]["readback"]["verified"]
    assert job["result"]["release_material"]["reasons"] == ["http_403"]
    assert job["result"]["release_material"]["numeric_batch_ids"]
    assert len(calls) == 1
    # The actual automatic service path calls the same adapter with the numeric
    # service still open. Its durable cooldown prevents a second source call.
    worker = WorkerService(lambda: host.read_factory(), profile=PROFILES[family])
    worker.material_capture = host.capture_material
    result = worker.update_range(
        "2026-09",
        "2026-09",
        series_ids=PROFILES[family].series_ids,
        include_buffer=False,
    )
    assert result["persisted"] is True
    assert result["release_material"]["transport_calls"] == 0 and len(calls) == 1
    assert host.submit(command) == job


def test_readonly_contract_and_generated_schema_cover_all_families_without_writes():
    from jsonschema import Draft202012Validator

    from market_data_foundation.web.macro_query_contract import release_openapi

    schema = release_openapi()["responses"]["200"]["content"]["application/json"][
        "schema"
    ]
    for family, name in [
        ("employment", "empsit_10022026.html"),
        ("cpi", "cpi_10142026.html"),
        ("ppi", "ppi_10152026.html"),
        ("pce", "bea-pio-202609.html"),
        ("retail", "census-retail.html"),
    ]:
        repo = repository(family)
        repo.import_material(
            *material(name), material_format="official_selected_projection"
        )
        before = json.dumps(repo.store._release_evidence, sort_keys=True)
        count = len(repo.store.raw_store.objects)
        result = repo.read(read_mode="source_history")
        Draft202012Validator(schema).validate(result)
        assert (
            result["history_basis"]
            == "named_source_archive_evidence_only_no_latest_fallback"
        )
        assert "ALFRED not implemented" in result["coverage"]
        assert result["provider_requests"] == 0 and result["persisted"] is False
        assert json.dumps(repo.store._release_evidence, sort_keys=True) == before
        assert len(repo.store.raw_store.objects) == count


def test_date_event_with_intraday_version_has_unknown_last_previous():
    repo = repository("employment")

    def save(at, value):
        repo.store._clock = lambda: at
        repo.store.save_batches(
            (
                SourceBatch(
                    (SourcePoint("LNS14000000", "2026-08", Decimal(value), {}, ()),),
                    value.encode(),
                    {},
                    at,
                    1,
                ),
            )
        )

    save(datetime(2026, 10, 1, 12, tzinfo=UTC), "4.0")
    save(datetime(2026, 10, 2, 13, tzinfo=UTC), "4.1")
    repo.store._clock = lambda: AT
    repo.import_material(*material("empsit_10022026.html"))
    item = next(
        c for c in repo.read()["events"][0]["claims"] if c["series_id"] == "LNS14000000"
    )
    assert item["mdf_pre_event_last_consumable_previous"] is None
    assert (
        item["mdf_pre_event_previous_missing"] == "pre_event_last_value_order_unknown"
    )
    assert item["mdf_pre_event_guaranteed_previous"]["value"] == "4.0"


def test_numeric_first_capture_survives_missing_event_actual_and_uses_batch_refs():
    repo = repository("cpi")
    batch = SourceBatch(
        (SourcePoint("CUSR0000SA0", "2026-09", Decimal("100"), {}, ()),),
        b"synthetic",
        {},
        AT,
        1,
    )
    repo.store.save_batches((batch,))
    result = repo.link_result(
        {
            "as_of": AT.isoformat(),
            "points": [
                {
                    "series_id": "CUSR0000SA0",
                    "month": "2026-09",
                    "revision": 1,
                    "batch_id": batch_identity(batch),
                }
            ],
        }
    )
    assert result["points"][0]["related_release_events"] == []
    assert result["points"][0]["mdf_first_captured"]["value"] == "100"
    assert result["points"][0]["mdf_first_captured"]["versions"][0][
        "batch_id"
    ] == batch_identity(batch)


def test_mutable_retail_url_cannot_silently_key_old_content_to_another_event(tmp_path):
    repo = repository("retail")
    raw, url = material("census-retail.html")
    selection = {
        "source_url": url,
        "reference_month": "2026-08",
        "source_date": "2026-09-17",
    }
    acquirer = MaterialAcquirer(tmp_path / "state.json", fetcher=lambda *_: raw)
    result = acquirer.capture(repo.store, selection)
    assert result["reasons"] == ["release_material_calendar_identity_mismatch"]
    assert repo.read()["events"] == [] and not repo.store.raw_store.objects


def test_operator_qualified_403_import_prevents_live_reprobe_after_restart(tmp_path):
    repo = repository("employment")
    _, url = material("empsit_10022026.html")
    selection = {
        "source_url": url,
        "reference_month": "2026-09",
        "source_date": "2026-10-02",
    }
    path = tmp_path / "material.json"
    MaterialAcquirer(path).record_limitation(
        repo.profile,
        selection,
        classification="http_403",
        observed_at=AT,
        cooldown_until=AT + timedelta(days=1),
        evidence_locator="retained-qualification.json",
    )

    def forbidden(*args):
        pytest.fail("qualified 403 was re-probed")

    result = MaterialAcquirer(path, fetcher=forbidden).capture(repo.store, selection)
    assert result["transport_calls"] == 0 and result["reasons"] == ["http_403"]
    assert (
        result["material_outcomes"][0]["basis"]
        == "operator_imported_prior_transport_observation"
    )


def test_official_null_statistic_keeps_scope_reason_and_other_actuals():
    raw, url = material("cpi_10142026.html")
    doc = parse_material(raw.replace(b"336.940", b"\xe2\x80\x94"), url, "cpi")
    assert not any(
        c["series_id"] == "CUUR0000SA0" and c["statistic"] == "index"
        for c in doc["claims"]
    )
    assert any(
        c["series_id"] == "CUSR0000SA0" and c["statistic"] == "mom_pct"
        for c in doc["claims"]
    )
    assert doc["missing_scopes"][0]["value"] is None
    assert (
        doc["missing_scopes"][0]["reason"]
        == "official_statistic_null_suppressed_or_unavailable"
    )


def test_failed_acquisition_can_link_prior_operator_evidence_without_claiming_delivery(
    tmp_path,
):
    repo = repository("employment")
    saved = repo.import_material(
        *material("empsit_10022026.html"),
        material_format="official_selected_projection",
    )
    selection = {
        "source_url": saved["source_url"],
        "reference_month": "2026-09",
        "source_date": "2026-10-02",
    }
    path = tmp_path / "state.json"
    acquirer = MaterialAcquirer(path, fetcher=lambda *_: pytest.fail("no re-probe"))
    acquirer.record_limitation(
        repo.profile,
        selection,
        classification="http_403",
        observed_at=AT,
        cooldown_until=AT + timedelta(days=1),
        evidence_locator="qualified-403.json",
    )
    result = acquirer.capture(repo.store, selection)
    assert result["state"] == "partial" and result["transport_calls"] == 0
    assert result["event_ids"] == [saved["event_id"]]
    assert result["evidence_ids"] == [saved["evidence_id"]]
    assert (
        result["evidence_origin"]
        == "preexisting_local_material_not_this_transport_delivery"
    )


def test_retail_first_capture_reads_versions_without_equality_binding():
    from tests.test_retail_timeseries_backend import batch

    repo = repository("retail")
    numeric = batch()
    repo.store.save_batch("synthetic-numeric-batch", numeric)
    repo.import_material(*material("census-retail.html"))
    event = repo.read()["events"][0]
    actual = next(
        c
        for c in event["claims"]
        if c["series_id"] == "44X72:SM" and c["role"] == "actual"
    )
    expected = next(
        c.value
        for c in numeric.cells
        if c.month == "2026-09"
        and c.category_code == "44X72"
        and c.data_type_code == "SM"
    )
    assert Decimal(actual["mdf_first_captured"]["value"]) == expected
    assert actual["mdf_first_captured"]["versions"][0]["batch_id"]
    assert actual["continuous_version"] is None


def test_optional_evidence_failure_preserves_numeric_result_and_reports_reason(
    monkeypatch,
):
    repo = repository("cpi")

    def unavailable(*args):
        raise CpiTimeSeriesError("release_storage_failed")

    monkeypatch.setattr(repo, "_rows", unavailable)
    numeric = {
        "points": [
            {"series_id": "CUSR0000SA0", "month": "2026-09", "index_value": "123"}
        ],
        "persisted": False,
    }
    assert repo.link_result(numeric)["points"][0]["index_value"] == "123"
    assert numeric["release_records_missing"] == ["release_storage_failed"]
    with pytest.raises(CpiTimeSeriesError, match="release_storage_failed"):
        repo.read()


def test_range_evidence_and_first_capture_use_bounded_read_queries_without_bootstrap():
    from market_data_foundation.historical.cpi_timeseries import month_at, month_number
    from market_data_foundation.historical.cpi_timeseries_store import (
        PostgresTimeSeriesStore,
    )

    class Connection:
        autocommit = True

        def __init__(self):
            self.queries = []

        def execute(self, sql, params=None):
            self.queries.append(sql)
            return self

        def fetchall(self):
            return []

    connection = Connection()
    repo = ReleaseRecords(
        PostgresTimeSeriesStore(connection, None, profile=CPI_PROFILE, clock=lambda: AT)
    )
    points = [
        {
            "series_id": "CUSR0000SA0",
            "month": month_at(month_number("2006-10") + i),
            "index_value": "100",
        }
        for i in range(240)
    ]
    result = repo.link_result({"points": points, "as_of": AT.isoformat()})
    assert len(result["points"]) == 240
    assert len(connection.queries) <= 5
    assert all(sql.lstrip().startswith("SELECT") for sql in connection.queries)
    assert all(p["mdf_first_captured"] is None for p in points)
