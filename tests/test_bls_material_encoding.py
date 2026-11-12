"""Synthetic pages with the exact reviewed CPI notice; no live/host fixtures."""

import copy
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.historical.bls_material_encoding import NOTICE_SHA256
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.macro_release_acquisition import MaterialAcquirer
from market_data_foundation.historical.macro_release_material import parse_material
from tests.test_m3_jolts_release_material import JOLTS_URL, jolts_body
from tests.test_macro_release_chain import material, repository

URL = "https://www.bls.gov/news.release/archives/cpi_11102026.htm"
NOTICE = (
    b"With the publication of January 2027 CPI data on February 11, 2027, "
    b"several index titles will change: \n"
    b'"wireless telephone services"\x9d will change to '
    b'"wireless telephone service"\x9d; "physicians\' services"\x9d will change \n'
    b'to "physician services"\x9d; and "music instruments and accessories"\x9d '
    b'will change to "music instruments and \n'
    b'accessories including instrument rentals."\x9d '
    b"More information is available online at \n"
    b"www.bls.gov/cpi/additional-resources/series-title-changes.htm"
)


def body():
    base, _ = material("cpi_10142026.html")
    # This is an integration fixture, not an original HTTP document. Numeric
    # tables remain the existing explicit government-source fixture projection.
    return base + (
        b"<pre>Upcoming Consumer Price Index Publication Changes\n"
        + NOTICE
        + b"\n------------------------\nTechnical Note\n</pre>"
    )


def test_only_reviewed_prose_bytes_get_visible_audited_parser_markers():
    raw = body()
    assert hashlib.sha256(NOTICE).hexdigest() == NOTICE_SHA256
    before = bytes(raw)
    actual = parse_material(raw, URL, "cpi")
    strict_reference = parse_material(
        raw.replace(b"\x9d", "\ufffd".encode()), URL, "cpi"
    )
    assert actual["claims"] == strict_reference["claims"]
    assert actual["source_version"] == strict_reference["source_version"]
    assert actual["missing_scopes"] == strict_reference["missing_scopes"]
    audit = actual["parser_input"]
    assert audit["retained_raw_unchanged"] is True
    assert audit["retained_raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert len(audit["transformations"]) == 6
    for change in audit["transformations"]:
        assert raw[change["byte_start"] : change["byte_end"]] == b"\x9d"
        assert change["replacement"] == "U+FFFD"
    assert raw == before


@pytest.mark.parametrize(
    "old,new",
    [
        (b"336.940", b"336.\x9d940"),
        (b"SEPTEMBER 2026", b"SEPTEM\x9dBER 2026"),
        (b"Sep.<br>2026", b"Sep.<br>20\x9d26"),
        (b"Unadjusted indexes", b"Unadjusted in\x9ddexes"),
        (b'"physician services"\x9d', b'"physician services"\x9c'),
    ],
)
def test_damage_in_value_date_header_or_unreviewed_prose_rejects(old, new):
    raw = body()
    assert old in raw
    with pytest.raises(CpiTimeSeriesError, match="release_material_encoding_invalid"):
        parse_material(raw.replace(old, new), URL, "cpi")


@pytest.mark.parametrize(
    "tag", ["table", "td", "th", "caption", "h1", "h2", "script", "style"]
)
def test_known_notice_inside_protected_structure_is_not_repaired(tag):
    raw = (
        body()
        .replace(b"<pre>", ("<" + tag + "><pre>").encode())
        .replace(b"</pre>", ("</pre></" + tag + ">").encode())
    )
    with pytest.raises(CpiTimeSeriesError, match="release_material_encoding_invalid"):
        parse_material(raw, URL, "cpi")


def test_same_invalid_notice_in_different_report_or_family_is_not_a_general_codec():
    for url, family in [
        (URL.replace("11102026", "12102026"), "cpi"),
        (URL.replace("cpi_", "ppi_"), "ppi"),
    ]:
        with pytest.raises(
            CpiTimeSeriesError, match="release_material_encoding_invalid"
        ):
            parse_material(body(), url, family)


@pytest.mark.parametrize(
    "family,name",
    [
        ("cpi", "cpi_10142026.html"),
        ("ppi", "ppi_10152026.html"),
        ("employment", "empsit_10022026.html"),
    ],
)
def test_normal_utf8_and_bom_preserve_existing_parsed_contract(family, name):
    raw, url = material(name)
    original = parse_material(raw, url, family)
    assert parse_material(b"\xef\xbb\xbf" + raw, url, family) == original
    assert "parser_input" not in original


def test_jolts_utf8_bom_and_existing_legacy_punctuation_compatibility():
    raw = jolts_body()
    original = parse_material(raw, JOLTS_URL, "jolts")
    assert parse_material(b"\xef\xbb\xbf" + raw, JOLTS_URL, "jolts") == original
    # Existing JOLTS Windows punctuation fallback remains scoped to that parser.
    assert (
        parse_material(raw + b"<p>\x93legacy prose\x94</p>", JOLTS_URL, "jolts")
        == original
    )


def test_http200_denial_or_error_page_is_not_a_material_event():
    with pytest.raises(CpiTimeSeriesError, match="release_reference_month_missing"):
        parse_material(b"<html>Access Denied: error</html>", URL, "cpi")


def test_normal_acquirer_retains_exact_original_audit_dedup_and_first_availability(
    tmp_path,
):
    repo = repository("cpi")
    at = datetime(2026, 11, 10, 16, tzinfo=UTC)
    clock = [at]
    repo.store._clock = lambda: clock[0]
    calls = []
    raw = body()

    def fetch(*args):
        calls.append(args)
        return raw

    acquirer = MaterialAcquirer(tmp_path / "material.json", fetcher=fetch)
    selection = dict(
        source_url=URL, source_date="2026-11-10", reference_month="2026-09"
    )
    numeric_before = copy.deepcopy(repo.store._history)
    result = acquirer.capture(
        repo.store, selection, batch_ids=["already-confirmed-numeric-batch"]
    )
    assert result["state"] == "retained" and result["numeric_batch_ids"] == [
        "already-confirmed-numeric-batch"
    ]
    event = repo.read()["events"][0]
    proof = event["evidence"][0]
    assert proof["retention"]["exact_original_retained"]
    assert repo.store.raw_store.objects[proof["raw_sha256"]] == raw
    assert proof["parser_input"]["retained_raw_sha256"] == proof["raw_sha256"]
    assert repo.read(as_of=(at - timedelta(microseconds=1)).isoformat())["events"] == []
    clock[0] += timedelta(hours=1)
    assert (
        acquirer.capture(repo.store, selection)["transport_calls"] == 0
        and len(calls) == 1
    )
    duplicate = repo.import_material(
        raw,
        URL,
        material_format="original_http_body",
        acquisition_method="bounded_official_transport",
    )
    assert (
        duplicate["available_at"] == proof["available_at"]
        and duplicate["obtained_at"] == proof["obtained_at"]
    )
    assert repo.store._history == numeric_before


def test_temporary_material_failure_retries_material_only_with_existing_cooldown(
    tmp_path,
):
    repo = repository("cpi")
    clock = [datetime(2026, 11, 10, 16, tzinfo=UTC)]
    repo.store._clock = lambda: clock[0]
    calls = []

    def fetch(*args):
        calls.append(args)
        if len(calls) == 1:
            raise CpiTimeSeriesError("http_503")
        return body()

    acquirer = MaterialAcquirer(tmp_path / "material.json", fetcher=fetch)
    selection = dict(
        source_url=URL, source_date="2026-11-10", reference_month="2026-09"
    )
    assert acquirer.capture(repo.store, selection)["reasons"] == ["http_503"]
    assert (
        acquirer.capture(repo.store, selection)["transport_calls"] == 0
        and len(calls) == 1
    )
    clock[0] += timedelta(hours=1)
    assert (
        acquirer.capture(repo.store, selection)["state"] == "retained"
        and len(calls) == 2
    )
    assert not repo.store._history


def test_employment_missing_cells_preserve_official_dashes_and_original_evidence():
    url = "https://www.bls.gov/news.release/archives/empsit_01092026.htm"
    base, _ = material("empsit_10022026.html")
    base = base.replace(b"SEPTEMBER 2026", b"DECEMBER 2025").replace(
        b"October 2, 2026", b"January 9, 2026"
    )
    extra = (
        b"<pre>Table A. Seasonally adjusted unemployment rates in 2025"
        b" and changes due to revision\n"
        b"October.............\t  \x97\t  \x97\t  \x97\n"
        b"NOTE: Data for October 2025 were not collected"
        b" due to the federal government shutdown.</pre>"
    )
    raw = base + extra
    expected = parse_material(raw.replace(b"\x97", "—".encode()), url, "employment")
    doc = parse_material(raw, url, "employment")
    audit = doc.pop("parser_input")
    assert doc.pop("parser_revision") == "employment-annual-table-missing-dashes-v1"
    expected.pop("parser_revision")
    assert doc == expected
    assert audit["retained_raw_sha256"] == hashlib.sha256(raw).hexdigest()
    assert all(change["replacement"] == "U+2014" for change in audit["transformations"])
    assert [change["byte_start"] for change in audit["transformations"]] == [
        i for i, byte in enumerate(raw) if byte == 0x97
    ]
    for damaged in (
        raw + b"12\x973",
        raw.replace(b"October......", b"July......"),
        raw.replace(b"\x97\t", b"\x971\t", 1),
    ):
        with pytest.raises(
            CpiTimeSeriesError, match="release_material_encoding_invalid"
        ):
            parse_material(damaged, url, "employment")
