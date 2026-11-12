"""Exchange replay, lifecycle conservation and evidence-aware completeness."""

import json
from datetime import UTC, date, datetime
from hashlib import sha256
from io import BytesIO
from xml.sax.saxutils import escape
from zipfile import ZipFile

import pytest

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.historical.tdx_p0_jobs import coverage
from market_data_foundation.historical.tdx_p0_roster import (
    annual_calendar,
    bse_pages,
    build_roster,
    exchange_suspensions,
    lifecycle,
)


def test_exchange_halts_apply_explicit_resumption_and_do_not_extend_past_receipt():
    rows = [
        dict(zqdm="002623", tpkssj="2026-11-03 开市", fpkssj="", tpsj="停牌"),
        dict(zqdm="002623", tpkssj="", fpkssj="2026-11-10 开市", tpsj="取消停牌"),
    ]
    proof = dict(
        received_at="2026-11-12T08:00:00+00:00",
        parameters=dict(
            CATALOGID="1798",
            txtDmorjc="002623",
            txtKsrq="2026-09-01",
            txtZzrq="2026-11-12",
        ),
    )
    sources = {
        "sz_suspension_002623": (
            json.dumps([dict(metadata=dict(recordcount=2), data=rows)]).encode(),
            proof,
        )
    }
    result = exchange_suspensions(sources, date(2026, 11, 12))
    assert [(r["start"], r["end"]) for r in result] == [("2026-11-03", "2026-11-09")]
    rows.pop()
    sources["sz_suspension_002623"] = (
        json.dumps([dict(metadata=dict(recordcount=1), data=rows)]).encode(),
        proof,
    )
    assert exchange_suspensions(sources, date(2026, 11, 12))[0]["end"] == "2026-11-12"
    proof["received_at"] = "2026-11-11T08:00:00+00:00"
    with pytest.raises(ContractError, match="tdx_suspension_observation_day"):
        exchange_suspensions(sources, date(2026, 11, 12))


def test_bse_census_requires_all_dated_pages_before_using_official_denominator():
    def page(number, code):
        return (
            (
                "null("
                + json.dumps(
                    [
                        dict(
                            number=number,
                            totalPages=2,
                            totalElements=2,
                            numberOfElements=1,
                            content=[dict(xxzqdm=code, xxjsrq="20261112")],
                        )
                    ]
                )
                + ")"
            ).encode(),
            dict(url="https://fixture.invalid/"),
        )

    sources = {"bj_current_page_0": page(0, "920001")}
    with pytest.raises(ContractError, match="tdx_bj_page_missing"):
        bse_pages(sources, date(2026, 11, 12))
    sources["bj_current_page_1"] = page(1, "920002")
    assert [r["xxzqdm"] for r, _ in bse_pages(sources, date(2026, 11, 12))] == [
        "920001",
        "920002",
    ]
    with pytest.raises(ContractError, match="tdx_bj_census_day"):
        bse_pages(sources, date(2026, 11, 11))


def sheet(rows):
    raw = BytesIO()
    with ZipFile(raw, "w") as z:
        body = "".join(
            "<row>"
            + "".join(
                f'<c r="{chr(65 + i)}{n}" t="inlineStr"><is><t>{escape(v)}</t></is></c>'
                for i, v in enumerate(row)
            )
            + "</row>"
            for n, row in enumerate(rows, 1)
        )
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
            + body
            + "</sheetData></worksheet>",
        )
    return raw.getvalue()


def test_originals_replay_cdr_retired_and_bj_selected_layer_boundary(tmp_path):
    sh = dict(
        result=[
            dict(
                A_STOCK_CODE="600001",
                COMPANY_ABBR="retired",
                LIST_DATE="19980122",
                DELIST_DATE="20091229",
                STOCK_TYPE="1",
            ),
            dict(
                A_STOCK_CODE="689009",
                COMPANY_ABBR="CDR",
                LIST_DATE="20201029",
                DELIST_DATE="-",
                STOCK_TYPE="8",
            ),
        ],
        pageHelp=dict(total=2),
    )
    originals = dict(
        sz_current=sheet(
            [["A股代码", "A股简称", "A股上市日期"], ["302132", "current", "2010-08-27"]]
        ),
        sz_terminated=sheet(
            [
                ["证券代码", "证券简称", "上市日期", "终止上市日期"],
                ["000003", "retired", "1991-01-14", "2002-06-14"],
            ]
        ),
        sh_main=json.dumps(sh).encode(),
        sh_star=b'{"result":[],"pageHelp":{"total":0}}',
        bj_mapping=b"<table><tr><td>1</td><td>fixture</td><td>2020/7/27</td><td>830002</td><td>920002</td></tr></table>",
    )
    sources = []
    for kind, raw in originals.items():
        path = tmp_path / kind
        path.write_bytes(raw)
        sources.append(
            dict(
                kind=kind,
                path=str(path),
                sha256=sha256(raw).hexdigest(),
                received_at="2026-11-12T00:00:00+00:00",
                url="https://fixture.invalid/",
            )
        )
    manifest = dict(valid_through="2026-11-12", sources=sources)
    provider = dict(
        snapshot_id="fixture",
        members=[dict(market=2, instrument="920002", name="fixture", evidence={})],
    )
    snapshot = build_roster(manifest, provider)
    members = {(m["market"], m["instrument"]): m for m in snapshot["members"]}
    assert (1, "689009") not in members
    assert len(members) == 4
    assert lifecycle(members[0, "000003"], date(2002, 6, 13)) == "ACTIVE"
    assert (
        lifecycle(members[0, "000003"], date(2002, 6, 14)) == "DELISTED_OR_TERMINATED"
    )
    assert lifecycle(members[2, "920002"], date(2020, 7, 27)) == "NOT_YET_LISTED"
    assert lifecycle(members[2, "920002"], date(2026, 11, 13)) == "UNKNOWN"
    assert members[2, "920002"]["evidence"]["old_code"] == "830002"
    (tmp_path / "sz_current").write_bytes(b"changed")
    with pytest.raises(ContractError, match="tdx_exchange_digest"):
        build_roster(manifest, provider)


def test_missing_calendar_holiday_is_rejected_and_weekend_never_makeup_trades():
    proof = dict(received_at="2026-11-12T00:00:00+00:00")
    holidays = [
        ("元旦", 1, 1, 3),
        ("春节", 2, 15, 23),
        ("清明节", 4, 4, 6),
        ("劳动节", 5, 1, 5),
        ("端午节", 6, 19, 21),
        ("中秋节", 9, 25, 27),
        ("国庆节", 10, 1, 7),
    ]
    text = "2026年" + "".join(
        f"{name}：{m}月{a}日（星期四）至{m}月{b}日（星期日）休市。"
        for name, m, a, b in holidays
    )
    days = {
        r["day"]: r["is_open"] for r in annual_calendar(text.encode(), 0, 2026, proof)
    }
    assert days["2026-11-12"] is True
    assert days["2026-02-23"] is False
    assert days["2026-02-28"] is False
    with pytest.raises(ContractError, match="tdx_calendar_holiday_missing"):
        annual_calendar(text.replace("国庆节", "missing").encode(), 0, 2026, proof)


def test_coverage_can_prove_complete_and_respects_evidence_time_and_job_scope():
    cutoff = datetime(2026, 11, 12, 9, tzinfo=UTC)
    day = date(2026, 11, 12)
    members = [
        dict(
            market=m,
            instrument="000001",
            name="fixture",
            evidence=dict(
                listing_date="2020-01-01", delist_date=None, valid_through="2026-11-12"
            ),
        )
        for m in range(3)
    ]
    snapshot = dict(
        snapshot_id="fixture",
        members=members,
        details=dict(
            markets=[
                dict(
                    market=m,
                    state="COMPLETE",
                    enumeration_from="2026-11-12",
                    enumeration_through="2026-11-12",
                )
                for m in range(3)
            ],
            calendar=[
                dict(
                    market=m,
                    day=day.isoformat(),
                    is_open=True,
                    available_at="2026-11-12T00:00:00+00:00",
                )
                for m in range(3)
            ],
            suspensions=[
                dict(
                    market=2,
                    instrument="000001",
                    start=day.isoformat(),
                    end=day.isoformat(),
                    available_at=cutoff.isoformat(),
                )
            ],
        ),
    )

    class Store:
        def universe(self, **kwargs):
            assert kwargs["as_of"] == cutoff
            return snapshot

        def query(self, **kwargs):
            assert kwargs["as_of"] == cutoff
            return dict(
                as_of=cutoff,
                rows=[dict(market=m, instrument="000001", volume=0) for m in range(2)],
            )

    report = coverage(Store(), day, as_of=cutoff)
    assert report["complete"] is True
    assert report["markets"][0]["confirmed_suspended"] == 0
    assert report["markets"][2]["confirmed_suspended"] == 1
    snapshot["details"]["suspensions"][0]["available_at"] = "2026-11-12T10:00:00+00:00"
    job = dict(
        command=dict(start="2025-01-01", end="2025-01-02"),
        updated_at=cutoff.isoformat(),
        steps=[
            dict(
                market=2, instrument="000001", state="FAILED", classification="timeout"
            )
        ],
    )
    report = coverage(Store(), day, as_of=cutoff, job=job)
    assert report["complete"] is False
    assert report["markets"][2]["unknown"] == 1
    assert report["markets"][2]["failed"] == 0
