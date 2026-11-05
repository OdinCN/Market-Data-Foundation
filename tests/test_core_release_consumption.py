"""Consumer semantics over retained official projections; no provider or private PG."""

from datetime import timedelta

from market_data_foundation.historical.core_release_consumption import (
    enrich_core_release,
)
from tests.test_macro_release_chain import AT, material, repository


def test_employment_comparison_keeps_each_prior_role_and_all_revision_months():
    repo = repository("employment")
    repo.import_material(*material("empsit_10022026.html"))
    event = repo.read()["events"][0]
    row = next(
        c for c in event["value_comparisons"] if c["series_id"] == "CES0000000001"
    )
    assert row["actual"]["value"] == "29"
    assert row["actual"]["continuous_version"] is None
    assert row["official_previous"]["value"] == "162"
    assert row["previous_as_reported_in_this_release"]["value"] == "133"
    assert row["revision_difference"] == "-29"
    assert {r["reference_month"] for r in row["revision_history"]} == {
        "2026-07",
        "2026-08",
    }
    assert row["mdf_pre_event_last_consumable_previous"] is None
    assert row["actual"]["surprise"] is None
    assert row["actual"]["locator"]["excerpt"]


def test_late_prior_material_never_leaks_into_earlier_cutoff():
    repo = repository("pce")
    repo.import_material(*material("bea-pio-202609.html"))
    before = repo.read(as_of=AT.isoformat())
    assert all(
        r["official_previous"] is None for r in before["events"][0]["value_comparisons"]
    )
    repo.store._clock = lambda: AT + timedelta(minutes=1)
    repo.import_material(*material("bea-pio-202608.html"))
    event = next(e for e in repo.read()["events"] if e["reference_month"] == "2026-09")
    row = next(
        c
        for c in event["value_comparisons"]
        if c["series_id"] == "DPCCRG" and c["statistic"] == "mom_pct"
    )
    assert row["official_previous"]["event_id"] != event["event_id"]
    assert row["official_previous"]["source_version"]["precision"] == "date"
    assert repo.read(as_of=AT.isoformat()) == before
    assert event["shared_publication"]["member_assets"] == ["pce", "consumption"]
    assert (
        event["shared_publication"]["other_asset_status"] == "not_read_in_this_response"
    )
    assert "not_atomic" in event["shared_publication"]["numeric_snapshot"]


def test_conflicted_prior_does_not_erase_material_actual_or_compute_revision():
    repo = repository("employment")
    repo.import_material(*material("empsit_10022026.html"))
    event = repo.read()["events"][0]
    original = next(
        c
        for c in event["claims"]
        if c["role"] == "official_previous" and c["reference_month"] == "2026-08"
    )
    original["confirmation_state"] = "conflicted"
    enrich_core_release(event, "employment")
    row = next(
        c for c in event["value_comparisons"] if c["series_id"] == "CES0000000001"
    )
    assert row["actual"]["value"] == "29"
    assert row["official_previous"] is None and row["revision_difference"] is None
    assert "official_previous_unconfirmed" in row["missing"]


def test_incompatible_prior_unit_is_not_substituted_and_zero_revision_is_valid():
    repo = repository("employment")
    repo.import_material(*material("empsit_10022026.html"))
    event = repo.read()["events"][0]
    for c in event["claims"]:
        if c["role"] == "official_previous":
            c["unit"] = "people"
    enrich_core_release(event, "employment")
    row = next(
        c for c in event["value_comparisons"] if c["series_id"] == "CES0000000001"
    )
    assert row["official_previous"] is None
    prior = row["previous_as_reported_in_this_release"]
    event["claims"].append(dict(prior, role="official_previous"))
    enrich_core_release(event, "employment")
    row = next(
        c for c in event["value_comparisons"] if c["series_id"] == "CES0000000001"
    )
    assert row["revision_difference"] == "0"
