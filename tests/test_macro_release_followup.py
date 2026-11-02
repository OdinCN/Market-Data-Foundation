"""Round03b regressions: retained evidence and synthetic storage, no live IO."""

import copy
import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.bls_timeseries_profile import (
    EMPLOYMENT_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.cpi_timeseries import (
    CpiTimeSeriesError,
)
from market_data_foundation.historical.cpi_timeseries_source import (
    SourceBatch,
    SourcePoint,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    _row_document,
    batch_identity,
    digest,
)
from market_data_foundation.historical.macro_release_acquisition import select_material
from market_data_foundation.historical.macro_release_material import parse_material
from market_data_foundation.historical.macro_release_records import (
    ReleaseRecords,
    official_event_time,
    release_surprise,
    temporal_bounds,
)
from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_macro_release_chain import AT, material, repository, synthetic_consensus
from tests.test_macro_research_fields import make_host


def test_consensus_timezone_and_actual_confirmation_fail_closed_zero_valid():
    doc = parse_material(*material("empsit_10022026.html"), "employment")
    actual = dict(doc["claims"][0], value="0", confirmation_state="confirmed")
    consensus = dict(synthetic_consensus(doc, actual), value="0")
    args = dict(event_id=doc["event_id"], event_time_evidence=official_event_time(doc))
    assert release_surprise(actual, consensus, **args) == ("0", [])
    for state in (None, "unknown", "unconfirmed", "conflicted"):
        assert release_surprise(
            dict(actual, confirmation_state=state), consensus, **args
        ) == (None, ["event_actual_unconfirmed"])
    unknown = dict(consensus, frozen_at={"value": "2026-10-01", "precision": "date"})
    assert release_surprise(actual, unknown, **args) == (
        None,
        ["release_date_timezone_missing"],
    )
    with pytest.raises(CpiTimeSeriesError, match="release_date_timezone_missing"):
        temporal_bounds(unknown["frozen_at"])
    with pytest.raises(CpiTimeSeriesError, match="release_date_timezone_missing"):
        repository("employment").freeze_consensus(unknown)
    invalid = dict(
        consensus,
        frozen_at={
            "value": "2026-10-01",
            "precision": "date",
            "timezone": "unconfirmed",
        },
    )
    assert release_surprise(actual, invalid, **args) == (
        None,
        ["release_date_timezone_invalid"],
    )


def test_legacy_two_employment_source_dates_read_without_changing_proof():
    repo = repository("employment")
    for name in ("empsit_09042026.html", "empsit_10022026.html"):
        saved = repo.import_material(*material(name))
        # Real old-format rows lack both a timezone and event_time_evidence.
        proof = repo.store._release_evidence[saved["evidence_id"]]
        proof.pop("event_time_evidence", None)
        proof["source_version"].pop("timezone", None)
    before = copy.deepcopy(repo.store._release_evidence)
    events = repo.read(as_of=AT.isoformat())["events"]
    assert len(events) == 2
    assert all(e["claims"][0]["confirmation_state"] == "confirmed" for e in events)
    assert before == repo.store._release_evidence


def test_merged_coverage_drops_latest_false_absence_keeps_per_material_proof():
    repo = repository("employment")
    raw, url = material("empsit_10022026.html")
    first = repo.import_material(raw, url)
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    latest = repo.import_material(
        b"THE EMPLOYMENT SITUATION - SEPTEMBER 2026. No numerical claims.", url
    )
    event = repo.read()["events"][0]
    assert event["state"] == "confirmed"
    assert not any(
        s["series_id"] == "CES0000000001" and s["statistic"] == "net_change_1m"
        for s in event["missing_scopes"]
    )
    assert "nfp_current_net_change_not_found_in_material" not in event["missing"]
    deficiencies = {p["evidence_id"]: p for p in event["material_deficiencies"]}
    assert (
        "nfp_current_net_change_not_found_in_material"
        in deficiencies[latest["evidence_id"]]["missing"]
    )
    assert any(c["evidence_id"] == first["evidence_id"] for c in event["claims"])
    assert event["missing_scopes"]  # Unsupported industry levels remain missing.
    assert event["material_state"] == "partial"


@pytest.mark.parametrize("change", ["basket", "statistic", "seasonal"])
def test_ppi_same_width_wrong_header_is_not_confirmed(change):
    raw, url = material("ppi_10152026.html")
    if change == "basket":
        raw = raw.replace(b"Total<br>final<br>demand", b"Final<br>demand<br>goods", 1)
    elif change == "statistic":
        raw = raw.replace(b"from 12<br>months", b"from 3<br>months", 1)
    else:
        raw = raw.replace(
            b"seasonally adjusted</span>", b"not seasonally adjusted</span>", 1
        )
    doc = parse_material(raw, url, "ppi")
    assert doc["claims"] == []
    assert "ppi_tablea_header_not_supported" in doc["missing"]


def test_ppi_reordered_header_same_width_fails_closed():
    raw, url = material("ppi_10152026.html")
    a, b = (
        b"Total<br>final<br>demand",
        b"Final<br>demand<br>less<br>foods,<br>energy,<br>and<br>trade",
    )
    raw = raw.replace(a, b"TEMP", 1).replace(b, a, 1).replace(b"TEMP", b, 1)
    assert parse_material(raw, url, "ppi")["claims"] == []


def test_retail_normalization_retains_reported_precision_and_no_api_equality_binding():
    doc = parse_material(*material("census-retail.html"), "retail")
    amount = next(c for c in doc["claims"] if c["statistic"] == "level")
    assert (
        amount["reported_value"] == "745.0" and amount["reported_unit"] == "billion_USD"
    )
    assert amount["normalization"]["display_decimal_places"] == 1
    assert amount["normalization"]["multiplier"] == "1000"
    assert Decimal(amount["value"]) == Decimal("745000.0") != Decimal("745021")
    assert amount["continuous_version"] is None


def test_pce_official_zero_does_not_fall_back_to_other_calculation():
    repo = repository("pce")
    v = dict(
        index_value="100",
        month="2026-09",
        bea_pct_1m=0,
        provider_calculations={"bea_pct_1m": "9"},
    )
    result = repo._statistic(v, {"statistic": "mom_pct"}, AT)
    assert result["value"] == "0"


def historical_snapshot(family):
    """Qualified durable schedule shapes, with synthetic public calendar IDs."""
    host = (
        "www.bea.gov"
        if family == "pce"
        else "www.census.gov"
        if family == "retail"
        else "www.bls.gov"
    )
    source = (
        f"https://{host}/news/schedule/ics/online-calendar-subscription.ics"
        if family == "pce"
        else f"https://{host}/retail/release_schedule.html"
        if family == "retail"
        else f"https://{host}/schedule/news_release/{family}.htm"
    )
    day = {"cpi": "14", "ppi": "15", "employment": "02", "pce": "29", "retail": "15"}[
        family
    ]
    event = {
        "uid": "synthetic-published",
        "release_at": f"2026-10-{day}T12:30:00+00:00",
        "status": "CONFIRMED",
    }
    target = dict(
        reference_month="2026-10",
        release_at="2026-11-25T13:30:00+00:00",
        uid="synthetic-future",
        reference_month_evidence={"source": source, "reference_month": "2026-10"},
    )
    cal = {"source": source, "events": {"synthetic-published": event}}
    if family == "pce":
        event.update(
            source=source,
            reference_months=["2026-09"],
            summary="Personal Income and Outlays, September 2026",
            sequence=0,
        )
    elif family == "retail":
        event.update(reference_month="2026-09")
    else:
        cal["display_history"] = [
            dict(
                reference_month="2026-09",
                release_at=event["release_at"],
                uid=event["uid"],
                reference_month_evidence={
                    "source": source,
                    "reference_month": "2026-09",
                    "release_at": event["release_at"],
                },
            )
        ]
    return {"targets": [target], "calendar": cal}


@pytest.mark.parametrize("family", ["cpi", "ppi", "employment", "pce", "retail"])
def test_future_only_targets_select_authoritative_published_history(family):
    snapshot = historical_snapshot(family)
    selected = select_material(family, snapshot, "2006-10", "2026-09", AT)
    assert selected and selected["reference_month"] == "2026-09"
    assert selected["source_date"].startswith("2026-10")


@pytest.mark.parametrize(
    "mutation",
    [
        "cancel_target",
        "retime_target",
        "cancel_event",
        "retime_event",
        "mapping_mismatch",
    ],
)
def test_old_display_history_cannot_revive_cancelled_or_retimed_event(mutation):
    snapshot = historical_snapshot("ppi")
    hist = snapshot["calendar"]["display_history"][0]
    event = snapshot["calendar"]["events"]["synthetic-published"]
    if mutation.endswith("target"):
        snapshot["targets"].append(
            dict(
                hist,
                state="calendar_removed" if mutation == "cancel_target" else "pending",
                release_at="2026-11-15T13:30:00Z",
            )
        )
    elif mutation == "cancel_event":
        event["status"] = "CANCELLED"
    elif mutation == "retime_event":
        event["release_at"] = "2026-11-15T13:30:00Z"
    else:
        hist["reference_month_evidence"]["release_at"] = "2026-10-16T12:30:00Z"
    assert select_material("ppi", snapshot, "2026-09", "2026-09", AT) is None


def test_bea_multi_month_title_uid_is_single_selection_for_either_reference():
    snapshot = historical_snapshot("pce")
    event = snapshot["calendar"]["events"]["synthetic-published"]
    event.update(
        reference_months=["2026-08", "2026-09"],
        summary="Personal Income and Outlays, August and September 2026",
    )
    selections = [
        select_material("pce", snapshot, ref, ref, AT)
        for ref in event["reference_months"]
    ]
    assert selections[0]["source_url"] == selections[1]["source_url"]
    assert selections[0]["calendar_uid"] == selections[1]["calendar_uid"]
    assert selections[0]["reference_months"] == ["2026-08", "2026-09"]


def test_pce_manual_worker_select_published_calendar_event_with_future_targets(
    tmp_path, monkeypatch
):
    from market_data_foundation.historical import (
        macro_release_acquisition as acquisition,
    )
    from market_data_foundation.historical.cpi_timeseries_cli import WorkerService

    host, _, store = make_host(PCE_PROFILE, tmp_path)
    store.raw_store = repository("pce").store.raw_store
    ReleaseRecords(store).prepare()
    monkeypatch.setattr(host.scheduler, "status", lambda: historical_snapshot("pce"))
    raw, url = material("bea-pio-202609.html")
    calls = []

    def retained_fetch(selected, family):
        calls.append((selected, family))
        assert selected == url and family == "pce"
        return raw

    monkeypatch.setattr(acquisition, "fetch_official_material", retained_fetch)
    job = host.submit(
        dict(
            request_id="12345678-1234-4234-8234-123456789abc",
            action="update",
            start="2026-09",
            end="2026-09",
        )
    )
    assert job["state"] == "complete"
    assert job["result"]["release_material"]["state"] == "retained"
    worker = WorkerService(lambda: host.read_factory(), profile=PCE_PROFILE)
    worker.material_capture = host.capture_material
    result = worker.update_range(
        "2026-09", "2026-09", series_ids=PCE_PROFILE.series_ids, include_buffer=False
    )
    assert result["release_material"]["event_ids"] == [
        "bea:personal-income-and-outlays:2026-10-29"
    ]
    assert result["release_material"]["transport_calls"] == 0
    assert len(calls) == 1


def test_init_already_complete_has_zero_source_requests_and_is_verified(tmp_path):
    host, source, _ = make_host(PPI_PROFILE, tmp_path)
    command = dict(
        request_id="12345678-1234-4234-8234-123456789abc",
        action="init",
        start="2026-09",
        end="2026-09",
    )
    assert host.submit(command)["state"] == "complete"
    calls = len(source.calls)
    again = host.submit(
        dict(command, request_id="22345678-1234-4234-8234-123456789abc")
    )
    assert again["state"] == "complete" and again["result"]["readback"]["verified"]
    assert again["result"]["provider_requests"] == 0 and len(source.calls) == calls


def test_partial_init_existing_rows_and_attempted_source_absence(tmp_path):
    host, source, store = make_host(PPI_PROFILE, tmp_path)
    series = PPI_PROFILE.series_ids[-2:]
    store.save_batches(source.fetch(series, "2026-08", "2026-09"))
    before = copy.deepcopy(store._history)
    original = source.fetch

    def sparse(selected, start, end):
        batch = original(selected, start, end)[0]
        return (
            replace(
                batch, points=tuple(p for p in batch.points if p.month >= "2026-08")
            ),
        )

    source.fetch = sparse
    command = dict(
        request_id="12345678-1234-4234-8234-123456789abc",
        action="init",
        start="2026-06",
        end="2026-09",
        series=",".join(series),
    )
    result = host.submit(command)
    assert result["state"] == "complete", result
    assert result["result"]["completion"] == "source_coverage_partial"
    assert result["result"]["readback"]["verified"]
    assert all(p["month"] < "2026-08" for p in result["result"]["source_missing"])
    assert all(store._history[key] == history for key, history in before.items())


def test_relational_serializer_and_memory_versions_keep_real_batch_identity():
    row = (
        "WPSFD4",
        AT.date().replace(day=1),
        1,
        AT,
        "real-batch",
        Decimal("100"),
        None,
        None,
        None,
        None,
        {},
        [],
        "a" * 64,
        AT,
        "active",
        None,
    )
    assert _row_document(row, profile=PPI_PROFILE)["batch_id"] == "real-batch"


def bound_employment_host(tmp_path):
    host, source, store = make_host(EMPLOYMENT_PROFILE, tmp_path)
    source.at = AT
    store.raw_store = repository("employment").store.raw_store
    raw, url = material("empsit_10022026.html")
    numeric = json.dumps({"release_url": url, "synthetic": True}).encode()
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
        numeric,
        {},
        AT,
        1,
    )
    store.save_batches((batch,))
    repo = ReleaseRecords(store)
    repo.prepare()
    saved = repo.import_material(raw, url)
    actual = saved["claims"][0]
    repo.bind_version(
        event_id=saved["event_id"],
        evidence_id=saved["evidence_id"],
        value_identity=actual["value_identity"],
        version={
            "revision": 1,
            "batch_id": batch_identity(batch),
            "raw_sha256": digest(numeric),
        },
        proof={
            "kind": "official_version_reference",
            "official_version_token": url,
            "version_metadata_pointer": "/release_url",
            "numeric_response": numeric,
        },
    )
    return host, batch


def test_memory_current_history_and_projection_keep_batch_identity(tmp_path):
    host, batch = bound_employment_host(tmp_path)
    for historical in (False, True):
        result = host.query(
            dict(
                start="2026-09",
                end="2026-09",
                series="CES0000000001",
                local_only="true",
                columns="value",
                **(
                    {"query_mode": "history", "as_of": AT.isoformat()}
                    if historical
                    else {}
                ),
            )
        )
        point = result["points"][0]
        assert point["batch_id"] == batch_identity(batch)
        assert point["mdf_first_captured"]["versions"][0]["batch_id"] == batch_identity(
            batch
        )
        assert (
            point["related_release_events"][0]["association"]
            == "explicit_version_proof"
        )
        link = point["related_release_events"][0]
        assert link["source_version"]["precision"] == "date"
        assert link["source_version"]["value"] == "2026-10-02"
        assert link["roles"] == ["actual"]
        assert result["provider_requests"] == 0


def test_http_first_capture_and_explicit_link_keep_batch_id_after_column_projection(
    tmp_path,
):
    """Root runs in normal ASGI environment; no external service required."""
    from fastapi import FastAPI

    from market_data_foundation.web.cpi_timeseries_api import timeseries_router
    from tests.test_ppi_timeseries_web import client

    host, batch = bound_employment_host(tmp_path)
    app = FastAPI()
    app.include_router(timeseries_router(host))
    with client(app) as http:
        for suffix in (
            "",
            "&query_mode=history&as_of=" + AT.isoformat().replace("+", "%2B"),
        ):
            response = http.get(
                "/api/r5/employment/timeseries/query?start=2026-09&end=2026-09&series=CES0000000001&local_only=true&columns=value"
                + suffix
            )
            assert response.status_code == 200, response.text
            point = response.json()["points"][0]
            assert point["batch_id"] == batch_identity(batch)
            assert point["mdf_first_captured"]["versions"][0][
                "batch_id"
            ] == batch_identity(batch)
            assert (
                point["related_release_events"][0]["association"]
                == "explicit_version_proof"
            )
            assert response.json()["provider_requests"] == 0


def test_retained_ppi_real_init_action_offline_replays_316_points(tmp_path):
    import os
    from pathlib import Path

    from market_data_foundation.historical.cpi_timeseries_source import (
        BlsRangeSource,
        HttpResult,
    )

    retained = os.environ.get("MDF_RESEARCH_RETAINED_DIR")
    if not retained:
        pytest.skip("explicit retained-material directory required; never fetch")
    paths = {
        "2024": "d8c6e5858b7d95191dfe403d30fbe55f29f68c122dee45eda30db3a4bed65921",
        "2026": "4866ad83faeb529ee0d2d37cb7325deca3ec8667376c1697d48200320534ca10",
    }
    replayed = []

    class RetainedOnlyTransport:
        def post(self, endpoint, body, headers, **kwargs):
            request = json.loads(body)
            sha = paths[request["endyear"]]
            raw = (Path(retained) / f"ppi-new-series-retained-{sha}.json").read_bytes()
            assert digest(raw) == sha
            replayed.append(sha)
            return HttpResult(200, raw, {})

    host, _, store = make_host(PPI_PROFILE, tmp_path)
    store._clock = lambda: AT
    source = BlsRangeSource(
        credential_loader=lambda: "a" * 32,
        transport=RetainedOnlyTransport(),
        clock=lambda: AT,
        profile=PPI_PROFILE,
    )
    from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesService

    host.write_factory = lambda **_: CpiTimeSeriesService(
        store, source, profile=PPI_PROFILE
    )
    job = host.submit(
        dict(
            request_id="12345678-1234-4234-8234-123456789abc",
            action="init",
            start="2006-10",
            end="2026-09",
            series="WPSFD49116,WPUFD49116",
        )
    )
    assert job["state"] == "complete", job
    result = job["result"]
    assert result["saved_points"] == 316
    assert result["readback"]["expected_points"] == 480
    assert len(result["readback"]["missing"]) == 164
    assert len(result["readback"]["source_missing"]) >= 164
    assert result["readback"]["source_unavailable_count"] == 0
    assert result["readback"]["verified"] is True
    assert result["completion"] == "source_coverage_partial"
    assert len(replayed) == 2  # Local file replay only; no provider transport.


def test_unknown_official_date_basis_preserves_claims_but_reports_order_missing():
    repo = repository("employment")
    saved = repo.import_material(*material("empsit_10022026.html"))
    proof = repo.store._release_evidence[saved["evidence_id"]]
    proof.pop("event_time_evidence", None)
    proof["source_version"].pop("timezone", None)
    proof["source_version"]["basis"] = "unknown"
    event = repo.read()["events"][0]
    actual = next(c for c in event["claims"] if c["role"] == "actual")
    assert actual["mdf_pre_event_last_consumable_previous"] is None
    assert actual["mdf_pre_event_previous_missing"] == "release_date_timezone_missing"
    assert event["source_version"]["basis"] == "unknown"
    assert "release_date_timezone_missing" in event["missing"]


def test_cpi_nsa_base_has_specific_table_proof_without_native_start_claim():
    from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
    from market_data_foundation.historical.macro_release_material import Material

    raw, url = material("cpi_10142026.html")
    parsed = Material(raw)
    assert "[1982-84=100, unless otherwise noted]" in parsed.text
    labels = {
        "SAH1": "Shelter",
        "SAF1": "Food",
        "SA0E": "Energy",
        "SACL1E": "Commodities less food and energy commodities",
        "SASLE": "Services less energy services",
    }
    for suffix, label in labels.items():
        row = next(
            row
            for table, row in parsed.rows
            if table == "cpipress1" and row[0] == label
        )
        assert row[0] == label and not any(
            note in row[0] for note in ("(2)", "(3)", "(4)")
        )
        metadata = CPI_PROFILE.series_metadata["CUUR0000" + suffix]
        assert metadata["index_base"] == "1982-84=100"
        assert metadata["index_base_source_url"] == url
        assert metadata["index_base_table"] == "cpipress1"
        assert metadata["start_month"] is None
        assert CPI_PROFILE.series_metadata["CUSR0000" + suffix]["index_base"] is None


def test_same_value_explicit_parser_correction_can_select_new_metadata():
    repo = repository("retail")
    raw, url = material("census-retail.html")
    first = repo.import_material(raw, url)
    old = repo.store._release_evidence[first["evidence_id"]]
    old["claims"][0].pop("normalization", None)
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    corrected = repo.import_material(raw, url, supersedes_evidence=first["evidence_id"])
    event = repo.read()["events"][0]
    actual = next(c for c in event["claims"] if c["statistic"] == "level")
    assert actual["evidence_id"] == corrected["evidence_id"]
    assert actual["normalization"]["display_decimal_places"] == 1
    assert event["conflicts"][0]["selection_basis"] == "explicit_parser_correction"
    assert len(event["evidence"]) == 2


def test_history_without_official_source_or_uid_with_cancelled_time_is_rejected():
    snapshot = historical_snapshot("ppi")
    history = snapshot["calendar"]["display_history"][0]
    history.pop("uid")
    snapshot["calendar"]["events"]["synthetic-published"]["status"] = "CANCELLED"
    assert select_material("ppi", snapshot, "2026-09", "2026-09", AT) is None
    snapshot = historical_snapshot("pce")
    snapshot["calendar"].pop("source")
    snapshot["calendar"]["events"]["synthetic-published"].pop("source")
    assert select_material("pce", snapshot, "2026-09", "2026-09", AT) is None


def test_source_gap_cannot_excuse_a_scope_not_actually_attempted(tmp_path):
    from market_data_foundation.web.cpi_timeseries_jobs import verify_readback

    host, _, _ = make_host(PPI_PROFILE, tmp_path)
    service = host.read_factory()
    series = PPI_PROFILE.series_ids[-2:]
    written = service.init_range("2026-09", "2026-09", series_ids=series)
    readback = service.query_range(
        "2025-09",
        "2026-09",
        series_ids=series,
        allow_upstream=False,
        vintage_mode="available",
    )
    lost = readback["points"].pop()
    key = dict(series_id=lost["series_id"], month=lost["month"])
    written["source_missing"] = [key]
    written["source_returned_series_months"] = [
        p for p in written["source_returned_series_months"] if p != key
    ]
    written["source_attempted_series_months"] = []
    # Even a claimed source gap cannot hide a lost produced point or invent an
    # attempt. This also exercises compatibility with absent old accounting.
    command = dict(
        action="init", start="2026-09", end="2026-09", series=",".join(series)
    )
    verified, result = verify_readback(written, readback, command, profile=PPI_PROFILE)
    assert verified is False
    assert result["source_missing"] == []


def test_twenty_year_fourteen_series_links_after_event_import_use_bounded_selects():
    from datetime import date

    from market_data_foundation.historical.bls_timeseries_profile import CPI_PROFILE
    from market_data_foundation.historical.cpi_timeseries import month_range
    from market_data_foundation.historical.cpi_timeseries_store import (
        PostgresTimeSeriesStore,
    )

    memory = repository("cpi")
    proof = memory.import_material(*material("cpi_10142026.html"))
    refs = month_range("2006-10", "2026-09")
    points = [
        dict(
            series_id=s,
            month=m,
            index_value="100",
            revision=1,
            batch_id="numeric-batch",
        )
        for s in CPI_PROFILE.series_ids
        for m in refs
    ]

    from contextlib import nullcontext

    class Connection:
        autocommit = True

        def __init__(self):
            self.queries = []
            self.rows = []

        def transaction(self):
            return nullcontext()

        def execute(self, sql, params=None):
            self.queries.append(sql)
            if ".revisions r " in sql:
                self.rows = [
                    (
                        p["series_id"],
                        date.fromisoformat(p["month"] + "-01"),
                        1,
                        AT,
                        "numeric-batch",
                        Decimal("100"),
                        None,
                        None,
                        None,
                        None,
                        {},
                        [],
                        "a" * 64,
                        AT,
                        "active",
                        None,
                    )
                    for p in points
                ]
            elif ".release_evidence " in sql:
                self.rows = [
                    (
                        proof["evidence_id"],
                        proof["raw_sha256"],
                        proof["raw_locator"],
                        AT,
                        AT,
                        proof,
                    )
                ]
            else:
                self.rows = []
            return self

        def fetchall(self):
            return self.rows

    connection = Connection()
    repo = ReleaseRecords(
        PostgresTimeSeriesStore(connection, None, profile=CPI_PROFILE, clock=lambda: AT)
    )
    result = repo.link_result(dict(points=points, as_of=AT.isoformat()))
    assert len(result["points"]) == 3360
    assert len(connection.queries) <= 6
    assert sum(".revisions r " in sql for sql in connection.queries) == 1
    assert connection.queries[0] == (
        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    )
    assert all(sql.lstrip().startswith("SELECT") for sql in connection.queries[1:])
    point = next(
        p
        for p in points
        if p["series_id"] == "CUUR0000SAH1" and p["month"] == "2026-09"
    )
    assert point["mdf_first_captured"]["versions"][0]["batch_id"] == "numeric-batch"
    assert point["related_release_events"][0]["association"] == "related_scope_only"


def test_noop_exemption_is_init_only_refresh_still_requires_returned_points(tmp_path):
    from market_data_foundation.web.cpi_timeseries_jobs import verify_readback

    host, _, _ = make_host(PPI_PROFILE, tmp_path)
    service = host.read_factory()
    series = PPI_PROFILE.series_ids[-2:]
    service.init_range("2026-09", "2026-09", series_ids=series)
    written = service.init_range("2026-09", "2026-09", series_ids=series)
    assert written["source_returned_series_months"] == []
    assert written["provider_requests"] == 0
    readback = service.query_range(
        "2026-09",
        "2026-09",
        series_ids=series,
        allow_upstream=False,
        vintage_mode="available",
    )
    for action in ("init", "update", "initialize"):
        command = dict(
            action=action, start="2026-09", end="2026-09", series=",".join(series)
        )
        verified, _ = verify_readback(written, readback, command, profile=PPI_PROFILE)
        assert verified is (action == "init")


def test_empty_inert_adapter_cannot_claim_source_absence_for_partial_init(tmp_path):
    host, source, store = make_host(PPI_PROFILE, tmp_path)
    series = PPI_PROFILE.series_ids[-2:]
    store.save_batches(source.fetch(series, "2026-08", "2026-09"))
    source.fetch = lambda *_: ()
    job = host.submit(
        dict(
            request_id="12345678-1234-4234-8234-123456789abc",
            action="init",
            start="2026-06",
            end="2026-09",
            series=",".join(series),
        )
    )
    assert job["state"] == "failed"
    assert job["classification"] == "local_readback_incomplete"
    assert job["result"]["provider_requests"] == 0
    assert job["result"]["source_missing"] == []
