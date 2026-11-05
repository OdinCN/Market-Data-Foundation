"""Original-publisher semantics and scoped reads, without source/LAN access."""

import json
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from market_data_foundation.historical.claims_source import ClaimsResponse
from market_data_foundation.historical.financial_daily import (
    FinancialError,
    day,
    registry,
)
from market_data_foundation.historical.financial_daily_origin import (
    APPROVED,
    BINDING_ID,
    BOARD,
    OriginSource,
    binding,
    derive,
    derived_points,
    metadata,
    points,
    request_for,
    validate,
)
from market_data_foundation.historical.financial_daily_service import project
from tests.test_financial_daily import AT, Control, payload, snapshot

POLICY = b"""<h4>2026</h4><table>
<tr><td>September 17</td>
<td>25</td>
<td>0</td>
<td>3.75-4.00</td></tr></table>
<h4>2025</h4><table>
<tr><td>December 11</td>
<td>0</td>
<td>25</td>
<td>3.50-3.75</td></tr></table>
<h4>2008</h4><table>
<tr><td>December 16</td>
<td>...</td>
<td>75-100</td>
<td>0-0.25</td></tr>
<tr><td>October 29</td>
<td>...</td>
<td>50</td>
<td>1.00</td></tr></table>"""


def test_bindings_are_named_and_no_fred_source_permission_is_implied():
    rows = registry()
    assert len(rows) == 10
    assert len(APPROVED) == 8
    assert all(
        not r["source_use"]["storage_permitted"]
        for r in rows
        if r["series_id"] in APPROVED
    )
    assert all(binding(s)["storage_permitted"] for s in APPROVED)
    assert binding("BAMLC0A0CM")["authorized_scope"].startswith(
        "personal noncommercial"
    )
    assert binding("T10YIE")["identity"] == "MDF_derived_not_FRED_T10YIE"
    assert binding("DGS10")["original_agency"] == "U.S. Department of the Treasury"
    for sid in ("EFFR", "SOFR"):
        assert "Market-Data-Foundation" in binding(sid)["required_notice"]
        assert "not affiliated" in binding(sid)["affiliation_notice"]


def test_policy_effective_start_and_single_target_boundary():
    parsed = points(POLICY, "DFEDTARL", "2026-09-16", "2026-09-20")
    assert [p.value for p in parsed] == [Decimal("3.50")] + [Decimal("3.75")] * 4
    assert parsed[0].provider_extra["policy_effective_date"] == "2025-12-11"
    assert parsed[-1].provider_extra["policy_effective_date"] == "2026-09-17"
    assert parsed[0].provider_extra["policy_effective_until_exclusive"] == "2026-09-17"
    assert len(points(POLICY, "DFEDTARL", "2008-10-29", "2008-10-31")) == 0
    assert points(POLICY, "DFEDTARU", "2026-09-18", "2026-09-18")[0].value == Decimal(
        "4.00"
    )


def test_nyfed_identity_date_and_revision_are_source_specific():
    raw = json.dumps(
        {
            "refRates": [
                dict(
                    effectiveDate="2026-10-30",
                    type="EFFR",
                    percentRate=3.9,
                    revisionIndicator="*",
                )
            ]
        }
    ).encode()
    p = points(raw, "EFFR", "2026-10-30", "2026-10-30")[0]
    assert p.value == Decimal("3.9") and p.provider_extra["revision_indicator"] == "*"
    assert p.provider_extra["source_available_at"] is None
    with pytest.raises(FinancialError):
        points(raw, "SOFR", "2026-10-30", "2026-10-30")
    with pytest.raises(FinancialError):
        points(raw, "EFFR", "2026-10-29", "2026-10-29")


def treasury_raw(date="2026-10-30", value="4.20", null=False):
    value_text = "" if null else value
    return f'''<feed xmlns="http://www.w3.org/2005/Atom"
xmlns:m="http://schemas.microsoft.com/ado/2007/08/dataservices/metadata"
xmlns:d="http://schemas.microsoft.com/ado/2007/08/dataservices"><entry><content><m:properties>
    <d:NEW_DATE>{date}T00:00:00</d:NEW_DATE>
<d:BC_10YEAR m:null="{"true" if null else "false"}">{value_text}</d:BC_10YEAR>
    <d:BC_2YEAR>0.00</d:BC_2YEAR><d:TC_10YEAR>-0.12</d:TC_10YEAR>
    </m:properties></content></entry></feed>'''.encode()


def test_treasury_native_fields_null_zero_negative_and_scope_are_not_fred():
    raw = treasury_raw()
    assert points(raw, "DGS10", "2026-10-30", "2026-10-30")[0].value == Decimal("4.20")
    assert points(raw, "DGS2", "2026-10-30", "2026-10-30")[0].value == 0
    assert points(raw, "DFII10", "2026-10-30", "2026-10-30")[0].value == Decimal(
        "-0.12"
    )
    assert (
        points(treasury_raw(null=True), "DGS10", "2026-10-30", "2026-10-30")[0].value
        is None
    )
    assert points(raw, "DGS10", "2026-10-29", "2026-10-29") == ()
    with pytest.raises(FinancialError):
        points(raw, "DGS10", "2026-11-01", "2026-11-02")
    m = metadata("DGS10", points(raw, "DGS10", "2026-10-30", "2026-10-30"))
    assert (
        m["declared_history_start"] == "1990-01-01"
        and m["declared_history_end"] is None
    )
    assert m["response_last_observation"] == "2026-10-30"
    assert "observation_end" not in m


@pytest.mark.parametrize("sid", ["DGS2", "DGS10"])
def test_annual_treasury_retains_reliable_dates_when_one_date_has_no_maturity(sid):
    import xml.etree.ElementTree as ET

    root = ET.fromstring(treasury_raw(date="2010-10-08"))
    absent = ET.fromstring(treasury_raw(date="2010-10-11")).find("{*}entry")
    properties = absent.find(".//{*}properties")
    for cell in list(properties):
        if cell.tag.split("}")[-1] != "NEW_DATE":
            properties.remove(cell)
    root.append(absent)
    root.append(ET.fromstring(treasury_raw(date="2010-10-12")).find("{*}entry"))
    raw = ET.tostring(root)
    source = OriginSource(
        controls={"home.treasury.gov": Control()},
        transport=lambda r: ClaimsResponse(200, raw),
        clock=lambda: AT,
    )
    batch = source.fetch(sid, "2010-01-01", "2010-12-31", period="year")
    validate(batch)
    assert [p.observation_date for p in batch.points] == ["2010-10-08", "2010-10-12"]
    assert batch.metadata["native_field_absent_dates"] == ["2010-10-11"]
    assert batch.request["parameters"]["field_tdr_date_value"] == "2010"
    snap = dict(
        values=[],
        batches=[
            (
                sid,
                batch.batch_id,
                batch.metadata,
                AT,
                AT,
                day(batch.start),
                day(batch.end),
                batch.origin,
                "raw",
                "metadata",
            )
        ],
        attempts=[],
        ranges=[(sid, day(batch.start), day(batch.end))],
        pending=[],
        cutoff=AT,
        basis="available",
    )
    row = project(snap, (sid,), "2010-10-11", "2010-10-11")["rows"][0]
    assert row["state"] == "provider_field_absent" and row["value"] is None
    assert row["coverage_evidence"]["raw_locator"] == "raw"


def test_origin_caches_shared_raw_and_retains_html_before_parse_with_strict_optin(
    tmp_path,
):
    seen = []
    source = OriginSource(
        controls={"www.federalreserve.gov": Control()},
        transport=lambda r: ClaimsResponse(200, POLICY),
        clock=lambda: AT,
        raw_sink=lambda *args: seen.append(args[1].body),
    )
    lower = source.fetch("DFEDTARL", "2026-10-23", "2026-10-30")
    upper = source.fetch("DFEDTARU", "2026-10-23", "2026-10-30")
    assert source.request_count == 1 and seen == [POLICY]
    assert lower.raw == upper.raw and lower.origin == "federal_reserve_board"
    assert lower.request["url"] == BOARD
    validate(lower)
    with pytest.raises(FinancialError):
        validate(replace(lower, origin="fred"))
    with pytest.raises(FinancialError):
        source.fetch("BAMLC0A0CM", "2026-10-23", "2026-10-30")
    assert source.request_count == 1
    with pytest.raises(FinancialError, match="live_opt_in_required"):
        OriginSource.live(control_directory=tmp_path)
    assert not list(tmp_path.iterdir())


def leg(sid, value, revision=1):
    return dict(
        series_id=sid,
        observation_date="2026-10-30",
        state="observed",
        value=value,
        origin="us_treasury",
        revision=revision,
        batch_id=sid + str(revision),
        available_at=AT.isoformat(),
        raw_locator="test://leg",
    )


def test_derived_same_date_cutoff_and_changed_legs_with_equal_difference():
    read = dict(
        rows=[leg("DGS10", "4.2"), leg("DFII10", "2.1")],
        as_of=(AT + timedelta(seconds=1)).isoformat(),
    )
    b = derive(read, observed_at=AT + timedelta(seconds=2))
    validate(b)
    assert b.points[0].value == Decimal("2.1") and b.origin == "mdf_derived"
    assert b.points[0].provider_extra["inputs"][0]["revision"] == 1
    later = dict(read, rows=[leg("DGS10", "4.3", 2), leg("DFII10", "2.2", 2)])
    changed = derive(later, observed_at=AT + timedelta(seconds=3))
    assert changed.points[0].value == b.points[0].value
    assert (
        changed.points[0].provider_extra["semantic_identity"]
        != b.points[0].provider_extra["semantic_identity"]
    )
    read["rows"][1]["observation_date"] = "2026-10-29"
    assert not derive(read).points
    doc = json.loads(b.raw)
    doc["cutoff"] = (AT - timedelta(seconds=1)).isoformat()
    with pytest.raises(FinancialError):
        derived_points(json.dumps(doc).encode(), b.start, b.end)
    doc = json.loads(b.raw)
    doc["observations"][0]["inputs"][1]["observation_date"] = "2026-10-29"
    with pytest.raises(FinancialError):
        derived_points(json.dumps(doc).encode(), b.start, b.end)


def test_narrow_failure_does_not_poison_other_days_and_later_batch_not_global_range():
    data = snapshot(payload())
    data["values"] = []
    data["attempts"] = [
        (
            "DGS2",
            "failed",
            "financial_http_failure",
            AT,
            day("2026-10-29"),
            day("2026-10-29"),
        )
    ]
    result = project(data, ("DGS2",), "2026-10-28", "2026-10-30")["rows"]
    assert [r["state"] for r in result] == [
        "not_collected",
        "request_failed",
        "not_returned_calendar_unconfirmed",
    ]
    assert (
        result[0]["latest_request_state"] is None
        and result[-1]["latest_request_state"] is None
    )
    old = data["batches"][0]
    data["batches"].insert(
        0,
        tuple(
            [
                *old[:2],
                dict(old[2], observation_start="2026-11-02"),
                *old[3:5],
                day("2026-11-02"),
                day("2026-11-03"),
                old[7],
            ]
        ),
    )
    assert (
        project(data, ("DGS2",), "2026-10-30", "2026-10-30")["rows"][0]["state"]
        == "not_returned_calendar_unconfirmed"
    )


def test_origin_requests_are_exact_and_unsupported_months_are_explicit():
    r = request_for("DGS10", "2026-10-23", "2026-10-30")
    assert r["parameters"]["field_tdr_date_value_month"] == "202610"
    with pytest.raises(FinancialError, match="one_month_window_required"):
        request_for("DGS10", "2026-10-30", "2026-11-02")
    assert binding("EFFR")["fred_code_role"] == "metric_mapping_only"
    assert binding("DFEDTARL")["binding_id"] == BINDING_ID


def test_original_http_failure_is_bounded_and_does_not_poison_another_origin():
    source = OriginSource(
        controls={"www.federalreserve.gov": Control(), "home.treasury.gov": Control()},
        transport=lambda r: (
            ClaimsResponse(503, b"")
            if r.url == BOARD
            else ClaimsResponse(200, treasury_raw())
        ),
        clock=lambda: AT,
        sleep=lambda _: None,
    )
    with pytest.raises(FinancialError) as error:
        source.fetch("DFEDTARL", "2026-10-30", "2026-10-30")
    assert error.value.http_status == 503 and source.request_count == 3
    assert source.fetch("DGS10", "2026-10-30", "2026-10-30").points[0].value == Decimal(
        "4.20"
    )
    assert source.request_count == 4
    with pytest.raises(FinancialError, match="bounded_window_exceeded"):
        source.fetch("DFEDTARL", "2026-01-01", "2026-03-01")
    assert source.request_count == 4


def test_future_policy_days_and_future_derivation_cutoff_are_not_captured():
    source = OriginSource(
        controls={},
        transport=lambda _r: pytest.fail("source request"),
        clock=lambda: AT,
    )
    with pytest.raises(FinancialError, match="future_window_not_acquired"):
        source.fetch("DFEDTARL", "2026-11-06", "2026-11-06")
    b = derive(
        dict(
            rows=[leg("DGS10", "4.2"), leg("DFII10", "2.1")],
            as_of=(AT + timedelta(seconds=1)).isoformat(),
        ),
        observed_at=AT,
    )
    with pytest.raises(FinancialError, match="future_cutoff_rejected"):
        validate(b)
