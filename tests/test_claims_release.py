"""Synthetic DOL report structure; original provider bytes stay in artifacts."""

import copy
import json
from contextlib import nullcontext
from io import BytesIO
from types import SimpleNamespace

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from market_data_foundation.historical.claims_contract import SERIES_IDS, ClaimsError
from market_data_foundation.historical.claims_release import (
    PARSER,
    ClaimsReleaseRecords,
    merge_evidence,
    parse_release,
    parse_release_pages,
)

URL = "https://www.dol.gov/sites/dolgov/files/OPA/newsreleases/ui-claims/synthetic.pdf"


def pages():
    return [
        """UNEMPLOYMENT INSURANCE WEEKLY CLAIMS
        8:30 A.M. (Eastern) Thursday, October 29, 2026
        In the week ending October 24, seasonally adjusted initial claims was 100.
        The previous week's level was revised up by 10 from 80 to 90.
        The 4-week moving average was 105.
        The previous week's average was revised down by 5 from 105 to 100.
        The advance seasonally adjusted insured unemployment rate was 1.0 percent.
        insured unemployment during the week ending October 17 was 200.
        The previous week's level was revised down by 5 from 215 to 210.
        The 4-week moving average was 205.
        The previous week's average was revised up by 5 from 195 to 200.
        """,
        """UNADJUSTED DATA synthetic narrative""",
        """UNEMPLOYMENT INSURANCE DATA FOR REGULAR STATE PROGRAMS
        WEEK ENDING October 24 October 17 Change October 10 Prior Year
        Initial Claims (SA) 100 90 +10 80 70
        Initial Claims (NSA) 110 100 +10 90 80
        4-Wk Moving Average (SA) 105 100 +5 95 90
        WEEK ENDING October 17 October 10 Change October 3 Prior Year
        Insured Unemployment (SA) 200 210 -10 220 230
        Insured Unemployment (NSA) 220 215 +5 210 205
        4-Wk Moving Average (SA) 205 200 +5 195 190
        INITIAL CLAIMS FILED IN FEDERAL PROGRAMS
        WEEK ENDING October 17 October 10 Change
        Fake 900 800 +100
        """,
    ]


def test_six_actuals_prior_roles_and_reference_headers_not_date_offsets():
    result = parse_release_pages(pages(), URL)
    actual = [c for c in result["claims"] if c["role"] == "actual"]
    assert [c["series_id"] for c in actual] == list(SERIES_IDS)
    assert [c["value"] for c in actual] == ["100", "110", "200", "220", "105", "205"]
    assert actual[2]["week_ending"] == "2026-10-17"
    assert actual[0]["evidence"]["page"] == 3
    assert "October 24 October 17" in actual[0]["evidence"]["reference_header"]
    original = {
        c["series_id"]: c["value"]
        for c in result["claims"]
        if c["role"] == "official_previously_reported_prior"
    }
    assert original == dict(
        ICSA="80", ICNSA=None, CCSA="215", CCNSA=None, IC4WSA="105", CC4WSA="195"
    )
    assert result["scheduled_publication_at"] == "2026-10-29T08:30:00-04:00"
    assert result["official_actual_publication_at"] is None
    assert result["mdf_pre_event_available_prior"] is None
    assert result["actual_available"] and result["surprise"] is None
    assert not result["continuous_version_bound"]


def test_text_adapter_and_unknown_layouts_fail_closed():
    valid = parse_release("\f".join(pages()).encode(), URL)
    assert valid["event_id"] == "dol-claims-2026-10-29"
    with pytest.raises(ClaimsError, match="parse_failed"):
        parse_release(b"%PDF-1.7 corrupted synthetic body", URL)
    broken = pages()
    broken[2] = broken[2].replace("Initial Claims (SA) 100", "Initial Claims (SA) 101")
    with pytest.raises(ClaimsError, match="table_change_mismatch"):
        parse_release_pages(broken, URL)
    with pytest.raises(ClaimsError, match="named_official_url_required"):
        parse_release_pages(pages(), "https://example.com/synthetic.pdf")
    broken = pages()
    broken[0] = broken[0].replace("from 80 to 90", "from 70 to 90")
    with pytest.raises(ClaimsError, match="narrative_revision_mismatch"):
        parse_release_pages(broken, URL)


def _pdf_with_detached_zero(sample):
    """Synthetic coordinates reproduce a zero emitted before its own row."""
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Courier"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    for text in sample:
        page = writer.add_blank_page(width=1000, height=800)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
        )
        commands = []

        def emit(line, x, y):
            escaped = line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            commands.append(f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({escaped}) Tj ET")

        for i, line in enumerate(text.strip().splitlines()):
            line = line.strip()
            y = 750 - i * 14
            if line.startswith("4-Wk Moving Average (SA) 105 105 0"):
                zero = line.index(" 0 ") + 1
                emit("0", 30 + zero * 6, y)
                emit(line[:zero], 30, y)
                emit(line[zero + 1 :], 30 + (zero + 1) * 6, y)
                continue
            emit(line, 30, y)
        stream = DecodedStreamObject()
        stream.set_data("\n".join(commands).encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_pdf_layout_preserves_detached_zero_and_extraction_provenance():
    sample = pages()
    sample[0] = (
        sample[0]
        .replace(
            "The previous week's average was revised down by 5 from 105 to 100.",
            "The previous week's average was revised down by 5 from 110 to 105.",
        )
        .replace("average was 105.", "average was 105, unchanged.")
    )
    sample[2] = sample[2].replace(
        "4-Wk Moving Average (SA) 105 100 +5", "4-Wk Moving Average (SA) 105 105 0"
    )
    raw = _pdf_with_detached_zero(sample)
    reader = PdfReader(BytesIO(raw), strict=True)
    with pytest.raises(ClaimsError, match="table_change_mismatch"):
        parse_release_pages([p.extract_text() for p in reader.pages], URL)
    result = parse_release(raw, URL)
    assert result["parser_revision"] == PARSER == "dol-regular-state-claims-v4"
    assert result["text_extraction"]["engine"] == "pypdf"
    assert result["text_extraction"]["mode"] == "layout"
    assert result["text_extraction"]["version"]
    average = [c for c in result["claims"] if c["series_id"] == "IC4WSA"]
    assert [c["value"] for c in average] == ["105", "105", "110"]
    assert "105 105 0" in average[0]["evidence"]["fragment"]


def test_layout_still_rejects_real_change_and_narrative_mismatch():
    for old, new, reason in (
        ("100 90 +10", "100 90 +11", "table_change_mismatch"),
        ("105 100 +5", "105 105 +5", "table_change_mismatch"),
        (
            "initial claims was 100",
            "initial claims was 101",
            "narrative_actual_mismatch",
        ),
    ):
        sample = [page.replace(old, new) for page in pages()]
        with pytest.raises(ClaimsError, match=reason):
            parse_release(_pdf_with_detached_zero(sample), URL)


def test_conflicting_actual_evidence_does_not_hide_unknown_prior_or_invent_surprise():
    result = parse_release_pages(pages(), URL)
    first = dict(result, evidence_id="synthetic-a")
    second = copy.deepcopy(first)
    second["evidence_id"] = "synthetic-b"
    second["claims"][0]["value"] = "101"
    merged = merge_evidence([first, second])
    assert not merged["actual_available"]
    assert merged["claims"][0]["confirmation_state"] == "conflict"
    assert merged["claims"][0]["value"] is None
    assert merged["conflicts"]
    assert merged["surprise_missing"] == [
        "named_consensus_unavailable",
        "event_actual_unconfirmed",
    ]
    without_conflict = merge_evidence([first])
    assert without_conflict["actual_available"]
    assert without_conflict["surprise_missing"] == ["named_consensus_unavailable"]


def test_scope_linking_never_binds_equal_continuous_value():
    event = dict(parse_release_pages(pages(), URL), evidence_id="synthetic")
    records = ClaimsReleaseRecords(None)
    records.read = lambda **options: dict(events=[event])
    result = records.link_result(
        dict(
            rows=[
                dict(
                    series_id="ICSA",
                    week_ending="2026-10-24",
                    value="100",
                    event_binding_status="unconfirmed",
                )
            ]
        )
    )
    assert result["rows"][0]["event_binding_status"] == "unconfirmed"
    assert result["rows"][0]["related_releases"][0]["association"] == "scope_only"
    assert not result["rows"][0]["related_releases"][0]["continuous_version_bound"]


def test_read_rejects_mode_without_connection_or_write():
    records = ClaimsReleaseRecords(SimpleNamespace(read_snapshot=nullcontext))
    with pytest.raises(ClaimsError, match="read_mode_invalid"):
        records.read(read_mode="bad")


def test_cli_requires_explicit_live_and_write_opt_in_before_factory(
    monkeypatch, capsys
):
    from scripts import claims_timeseries as cli

    monkeypatch.setattr(
        cli, "build_service", lambda **kwargs: pytest.fail("unexpected connection")
    )
    for args in (
        ["init"],
        ["init", "--confirm-dev-write"],
        ["query", "--live-provider"],
    ):
        with pytest.raises(SystemExit) as error:
            cli.main(args)
        assert error.value.code == 2
    capsys.readouterr()


def test_cli_releases_exposes_real_record_reader(monkeypatch, capsys):
    from market_data_foundation.historical import claims_release
    from scripts import claims_timeseries as cli

    monkeypatch.setattr(
        cli,
        "build_service",
        lambda **kwargs: SimpleNamespace(store="bound", close=lambda: None),
    )
    monkeypatch.setattr(
        claims_release,
        "ClaimsReleaseRecords",
        lambda store: SimpleNamespace(
            read=lambda **kwargs: dict(events=[dict(event_id="synthetic")])
        ),
    )
    assert cli.main(["releases"]) == 0
    assert json.loads(capsys.readouterr().out)["events"][0]["event_id"] == "synthetic"


def test_unicode_minus_and_true_current_pdf_snapshot_evidence():
    sample = pages()
    sample[2] = sample[2].replace("-10", "−10")
    for url in (
        "https://www.dol.gov/ui/data.pdf",
        "https://www.dol.gov/ui/data.pdf?os=v0",
    ):
        parsed = parse_release_pages(sample, url)
        assert parsed["evidence_kind"] == "official_current_report_snapshot"
        assert parsed["source_url"] == url
        assert parsed["event_id"] == "dol-claims-2026-10-29"
        assert parsed["claims"][0]["evidence"]["source_url"] == url
    assert parse_release_pages(sample, URL)["evidence_kind"] == "official_archive"
    with pytest.raises(ClaimsError, match="named_official_url_required"):
        parse_release_pages(sample, "https://www.dol.gov/ui/data.pdf?invented=archive")


def test_merge_retains_original_proofs_and_only_true_nonnull_supporters():
    first = dict(
        parse_release_pages(pages(), URL), evidence_id="first", raw_sha256="0" * 64
    )
    second = copy.deepcopy(first)
    second.update(evidence_id="second", raw_sha256="1" * 64)
    first["claims"][0].update(value=None, confirmation_state="unknown")
    second["claims"][0]["evidence"]["page"] = 9
    originals = copy.deepcopy([first, second])
    merged = merge_evidence([first, second])
    claim = merged["claims"][0]
    assert claim["value"] == "100"
    assert claim["evidence_id"] == "second"
    assert claim["evidence_ids"] == ["second"]
    assert claim["evidence"]["page"] == 9
    assert merged["evidence"] == originals
    assert [first, second] == originals
    merged["evidence"][0]["claims"].clear()
    assert first["claims"]
