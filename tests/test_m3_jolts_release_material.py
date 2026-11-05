"""Synthetic table/PDF ports; live original bytes remain in transaction evidence."""

from datetime import timedelta
from types import SimpleNamespace

import pytest

from market_data_foundation.historical.bls_timeseries_profile import JOLTS_PROFILE
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.cpi_timeseries_store import (
    InMemoryTimeSeriesStore,
)
from market_data_foundation.historical.m3_capital_goods_store import InMemoryM3Store
from market_data_foundation.historical.macro_release_material import parse_material
from market_data_foundation.historical.macro_release_records import ReleaseRecords
from tests.test_macro_release_chain import AT, Raw

M3_URL = "https://www.census.gov/manufacturing/m3/prel/pdf/s-i-o.pdf"
JOLTS_URL = "https://www.bls.gov/news.release/archives/jolts_09292026.htm"
PDF_TEXT = """FOR RELEASE AT 10:00 AM EST, TUESDAY, NOVEMBER 3, 2026
MONTHLY FULL REPORT ON MANUFACTURERS’ SHIPMENTS, INVENTORIES AND ORDERS
SEPTEMBER 2026 Release Number: CB 26-170 M3-2 (26)-09
Table 2.  Value of Manufacturers' New Orders for Industry Groups
Estimates are shown in millions of dollars.
Seasonally Adjusted   Not Seasonally Adjusted
Sep.   Aug.   July  Aug. - July - June - Sep. Aug. July Sep.
2026p  2026r  2026 Sep. Aug. July 2026p 2026r 2026 2025 2026p 2025 2025
""" + (
    "Durable goods industries2....  345,672  339,185  338,700  1.9 0.1 0.9 "
    "368,950 337,424 321,721 334,949 3,007,591 2,784,653 8.0\n"
)


def m3_repo(monkeypatch, text=PDF_TEXT):
    from market_data_foundation.historical import m3_release_material as module

    monkeypatch.setattr(
        module,
        "PdfReader",
        lambda _: SimpleNamespace(
            pages=[SimpleNamespace(extract_text=lambda **_: text)]
        ),
    )
    repo = ReleaseRecords(InMemoryM3Store(Raw(JOLTS_PROFILE), clock=lambda: AT))
    repo.prepare()
    return repo


def jolts_body():
    columns = [
        "Aug. 2025",
        "May 2026",
        "June 2026",
        "July 2026",
        "Aug. 2026 ( p )",
        "Change from: July 2026 - Aug. 2026 ( p )",
    ]

    def row(cells):
        return "<tr>" + "".join("<td>" + c + "</td>" for c in cells) + "</tr>"

    table = (
        '<table id="jolts_table1"><caption>Table 1. Job openings levels and rates '
        "by industry and region, seasonally adjusted</caption>"
    )
    table += row(["Industry and region", "Levels (in thousands)", "Rates ( 2 )"])
    table += row(columns + columns)
    table += row(
        [
            "Total",
            "6,919",
            "7,537",
            "7,182",
            "7,335",
            "7,079",
            "-256",
            "4.2",
            "4.5",
            "4.3",
            "4.4",
            "4.3",
            "-0.1",
        ]
    )
    return (
        "<h1>JOB OPENINGS AND LABOR TURNOVER – AUGUST 2026</h1>"
        "For release 10:00 a.m. Tuesday, September 29, 2026. " + table + "</table>"
    ).encode("cp1252")


def test_m3_identity_exact_cells_unbound_prior_duplicate_and_cutoff(monkeypatch):
    repo = m3_repo(monkeypatch)
    saved = repo.import_material(b"synthetic PDF port", M3_URL)
    assert saved["event_id"] == "census:m3:full:2026-11-03"
    assert saved["report_stage"] == "full"
    assert repo.read(as_of=(AT - timedelta(microseconds=1)).isoformat())["events"] == []
    event = repo.read(as_of=AT.isoformat())["events"][0]
    assert [
        (c["role"], c["value"], c["unit"], c["adjustment"]) for c in event["claims"]
    ] == [
        ("actual", "345672", "million_USD", "sa"),
        ("prior_as_reported_in_this_event", "339185", "million_USD", "sa"),
    ]
    assert len(event["missing_scopes"]) == 2
    assert all(c["continuous_version"] is None for c in event["claims"])
    assert (
        repo.import_material(b"synthetic PDF port", M3_URL)["evidence_id"]
        == saved["evidence_id"]
    )
    assert not repo.store.rows and not repo.store.batches
    with pytest.raises(CpiTimeSeriesError, match="stage_unproven"):
        parse_material(
            b"synthetic PDF port",
            M3_URL.replace("prel/pdf/s-i-o", "adv/pdf/durgd"),
            "m3",
        )


def test_m3_missing_first_cell_cannot_shift_prior_into_actual(monkeypatch):
    repo = m3_repo(monkeypatch, PDF_TEXT.replace("345,672", "—"))
    repo.import_material(b"synthetic null PDF port", M3_URL)
    event = repo.read()["events"][0]
    assert not any(c["role"] == "actual" for c in event["claims"])
    assert len(event["missing_scopes"]) == 3
    assert event["claims"][0]["value"] == "339185"


def test_jolts_legacy_bytes_exact_national_sa_columns_and_cutoff():
    body = jolts_body()
    repo = ReleaseRecords(
        InMemoryTimeSeriesStore(
            Raw(JOLTS_PROFILE), profile=JOLTS_PROFILE, clock=lambda: AT
        )
    )
    repo.prepare()
    saved = repo.import_material(body, JOLTS_URL)
    assert repo.read(as_of=(AT - timedelta(microseconds=1)).isoformat())["events"] == []
    event = repo.read()["events"][0]
    assert {
        (c["series_id"], c["reference_month"], c["value"], c["unit"])
        for c in event["claims"]
        if c["role"] == "actual"
    } == {
        ("JTS000000000000000JOL", "2026-08", "7079", "thousand_jobs"),
        ("JTS000000000000000JOR", "2026-08", "4.3", "percent"),
    }
    assert len(event["missing_scopes"]) == 6
    assert all(
        c["adjustment"] == "sa" and c["continuous_version"] is None
        for c in event["claims"]
    )
    assert repo.import_material(body, JOLTS_URL)["evidence_id"] == saved["evidence_id"]
    assert body in repo.store.raw_store.objects.values()


@pytest.mark.parametrize(
    "old,new",
    [
        (b", seasonally adjusted", b", not seasonally adjusted"),
        (b">Total<", b">Northeast<"),
        (b"Levels (in thousands)", b"Levels (in millions)"),
        (b"Aug. 2026 ( p )", b"Sep. 2026 ( p )"),
    ],
)
def test_jolts_scope_mismatch_is_not_substituted(old, new):
    doc = parse_material(jolts_body().replace(old, new), JOLTS_URL, "jolts")
    assert doc["claims"] == [] and len(doc["missing_scopes"]) == 8


@pytest.mark.parametrize(
    "before,after",
    [
        ("345,672", "       "),
        ("339,185", "       "),
        ("345,672", "345,67 2"),
        ("339,185", "339,18 5"),
        ("337,424", "       "),
        ("1.9 0.1", "0.1"),
        ("Seasonally Adjusted   Not Seasonally Adjusted", "Not Seasonally Adjusted"),
        (
            "Seasonally Adjusted   Not Seasonally Adjusted",
            "Not Seasonally Adjusted   Seasonally Adjusted",
        ),
        ("Sep.   Aug.   July", "Aug.   Sep.   July"),
        ("Sep.   Aug.   July", "Sep.         July"),
        ("Sep.   Aug.   July", "Sep.   Aug.   June"),
        ("2026p  2026r", "2026p  2025r"),
    ],
)
def test_m3_incomplete_or_unverified_columns_leave_whole_row_missing(
    monkeypatch, before, after
):
    repo = m3_repo(monkeypatch, PDF_TEXT.replace(before, after))
    repo.import_material(b"synthetic damaged column PDF", M3_URL)
    event = repo.read()["events"][0]
    assert event["claims"] == []
    assert len(event["missing_scopes"]) == 3
    assert event["state"] == "unknown"
