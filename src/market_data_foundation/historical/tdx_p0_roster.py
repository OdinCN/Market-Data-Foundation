"""Replay exchange originals into dated A-share membership, without live I/O."""

import json
import re
from datetime import date, datetime, timedelta
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from market_data_foundation.canonical.model import require

PATTERNS = {
    0: r"(000|001|002|003|300|301|302)[0-9]{3}",
    1: r"(600|601|603|605|688)[0-9]{3}",
    2: r"(4[0-9]{5}|8[0-9]{5}|92[0-9]{4})",
}


def a_share(market, code):
    return bool(re.fullmatch(PATTERNS[market], code))


def xlsx_rows(raw):
    ns = {"s": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with ZipFile(BytesIO(raw)) as archive:
        strings = (
            [
                "".join(node.itertext())
                for node in ET.fromstring(archive.read("xl/sharedStrings.xml")).findall(
                    "s:si", ns
                )
            ]
            if "xl/sharedStrings.xml" in archive.namelist()
            else []
        )
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        rows = []
        for row in sheet.findall("s:sheetData/s:row", ns):
            cells = {}
            for cell in row:
                value = cell.findtext("s:v", default="", namespaces=ns)
                if cell.get("t") == "s":
                    value = strings[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(cell.find("s:is", ns).itertext())
                cells[re.sub(r"[0-9]", "", cell.get("r"))] = value.strip()
            rows.append(cells)
    require(bool(rows), "tdx_exchange_sheet_empty")
    header = rows[0]
    return [{header[k]: v for k, v in row.items()} for row in rows[1:]]


class MappingTable(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows, self.row, self.cell = [], None, None

    def handle_starttag(self, tag, attrs):
        if tag == "tr":
            self.row = []
        if tag in {"td", "th"}:
            self.cell = ""

    def handle_data(self, value):
        if self.cell is not None:
            self.cell += value

    def handle_endtag(self, tag):
        if tag in {"td", "th"} and self.row is not None:
            self.row.append(self.cell.strip())
            self.cell = None
        if tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None


def lifecycle(member, day):
    """Delisting date is exclusive; an unknown interval never becomes active."""
    evidence = member.get("evidence", {})
    listed, delisted = evidence.get("listing_date"), evidence.get("delist_date")
    if listed and day < date.fromisoformat(listed):
        return "NOT_YET_LISTED"
    if delisted and day >= date.fromisoformat(delisted):
        return "DELISTED_OR_TERMINATED"
    if listed and (delisted or day <= date.fromisoformat(evidence["valid_through"])):
        return "ACTIVE"
    return "UNKNOWN"


def annual_calendar(raw, market, year, proof):
    """Exchange annual schedule: explicit holiday ranges plus weekend closure."""
    text = re.sub(r"\s+", "", unescape(re.sub(r"<[^>]*>", "", raw.decode())))
    require(f"{year}年" in text, "tdx_calendar_year")
    ranges = []
    for holiday in ("元旦", "春节", "清明节", "劳动节", "端午节", "中秋节", "国庆节"):
        match = re.search(
            holiday + r"[：:]([0-9]+)月([0-9]+)日[^至]{0,12}至"
            r"([0-9]+)月([0-9]+)日[^休]{0,12}休市",
            text,
        )
        require(match is not None, "tdx_calendar_holiday_missing")
        sm, sd, em, ed = map(int, match.groups())
        ranges.append((date(year, sm, sd), date(year, em, ed)))
    day = date(year, 1, 1)
    result = []
    while day.year == year:
        result.append(
            dict(
                market=market,
                day=day.isoformat(),
                is_open=day.weekday() < 5 and not any(a <= day <= b for a, b in ranges),
                available_at=proof["received_at"],
                evidence=proof,
            )
        )
        day += timedelta(days=1)
    return result


def bse_pages(sources, through):
    """Require the whole official census, including consistent dated pagination."""
    pages = {}
    total = None
    count = None
    for kind, (raw, proof) in sources.items():
        if not kind.startswith("bj_current_page_"):
            continue
        match = re.fullmatch(rb"\s*null\((.*)\)\s*;?\s*", raw, re.S)
        require(match is not None, "tdx_bj_jsonp")
        data = json.loads(match[1])[0]
        number = int(data["number"])
        require(kind == f"bj_current_page_{number}", "tdx_bj_page_identity")
        require(number not in pages, "tdx_bj_duplicate_page")
        if count is None:
            count, total = int(data["totalPages"]), int(data["totalElements"])
        require(
            (count, total) == (int(data["totalPages"]), int(data["totalElements"])),
            "tdx_bj_census_changed",
        )
        require(
            len(data["content"]) == int(data["numberOfElements"]),
            "tdx_bj_page_size",
        )
        for row in data["content"]:
            require(
                date.fromisoformat(row["xxjsrq"]) == through,
                "tdx_bj_census_day",
            )
        pages[number] = [(row, proof) for row in data["content"]]
    if count is None:
        return None
    require(set(pages) == set(range(count)), "tdx_bj_page_missing")
    rows = [item for number in sorted(pages) for item in pages[number]]
    require(len(rows) == total, "tdx_bj_census_size")
    return rows


def exchange_suspensions(sources, through):
    """Bound ongoing exchange events by retrieval, and apply explicit resumptions."""
    result = []

    def add(market, code, start, end, proof):
        require(
            datetime.fromisoformat(proof["received_at"])
            .astimezone(ZoneInfo("Asia/Shanghai"))
            .date()
            >= through,
            "tdx_suspension_observation_day",
        )
        end = min(end, through)
        if start <= end:
            result.append(
                dict(
                    market=market,
                    instrument=code,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    available_at=proof["received_at"],
                    evidence=proof,
                )
            )

    if "sh_suspension_current" in sources:
        raw, proof = sources["sh_suspension_current"]
        data = json.loads(raw)
        require(
            len(data["result"]) == int(data["pageHelp"]["total"]),
            "tdx_suspension_page_missing",
        )
        require(
            proof["parameters"]["sqlId"] == "GW_PL_JYTS_TFPXX"
            and date.fromisoformat(proof["parameters"]["endStopDate"]) == through,
            "tdx_suspension_query_scope",
        )
        for row in data["result"]:
            if (
                a_share(1, row["productCode"])
                and row["controlType"] == "TR"
                and row["type"] == "LXTP"
                and not row["stopTime"]
            ):
                add(
                    1,
                    row["productCode"],
                    date.fromisoformat(row["startStopDate"]),
                    date.fromisoformat(row["endStopDate"])
                    if row["endStopDate"]
                    else through,
                    proof,
                )
    for kind, (raw, proof) in sources.items():
        if not re.fullmatch(r"sz_suspension_[0-9]{6}", kind):
            continue
        data = json.loads(raw)[0]
        code = kind.rsplit("_", 1)[1]
        require(
            len(data["data"]) == int(data["metadata"]["recordcount"]),
            "tdx_suspension_page_missing",
        )
        parameters = proof["parameters"]
        require(
            parameters["CATALOGID"] == "1798"
            and parameters["txtDmorjc"] == code
            and date.fromisoformat(parameters["txtZzrq"]) == through,
            "tdx_suspension_query_scope",
        )
        rows = data["data"]
        require(all(r["zqdm"] == code for r in rows), "tdx_suspension_code")
        resumptions = [
            date.fromisoformat(r["fpkssj"][:10])
            for r in rows
            if r["tpsj"] == "取消停牌" and r["fpkssj"].endswith("开市")
        ]
        for row in rows:
            if not (
                a_share(0, code)
                and row["tpsj"] == "停牌"
                and row["tpkssj"].endswith("开市")
            ):
                continue
            start = date.fromisoformat(row["tpkssj"][:10])
            require(
                start >= date.fromisoformat(parameters["txtKsrq"]),
                "tdx_suspension_query_scope",
            )
            end = min(
                (r - timedelta(days=1) for r in resumptions if r >= start),
                default=through,
            )
            add(0, code, start, end, proof)
    return result


def build_roster(manifest, provider_snapshot):
    """Manifest binds URLs, receipt clocks and digests of retained originals."""
    through = date.fromisoformat(manifest["valid_through"])
    sources = {}
    for item in manifest["sources"]:
        raw = Path(item["path"]).read_bytes()
        require(sha256(raw).hexdigest() == item["sha256"], "tdx_exchange_digest")
        sources[item["kind"]] = (raw, item)
    members = {}

    def add(market, code, name, listed, delisted, proof, **extra):
        if not a_share(market, code):
            return
        listed = date.fromisoformat(listed).isoformat() if listed else None
        delisted = date.fromisoformat(delisted).isoformat() if delisted else None
        require(not (listed and delisted) or listed < delisted, "tdx_lifecycle_order")
        key = (market, code)
        require(key not in members, "tdx_exchange_duplicate_code")
        members[key] = dict(
            market=market,
            instrument=code,
            name=name,
            evidence=dict(
                **proof,
                listing_date=listed,
                delist_date=delisted,
                valid_through=through.isoformat(),
                **extra,
            ),
        )

    for kind in ("sz_current", "sz_terminated"):
        raw, proof = sources[kind]
        for row in xlsx_rows(raw):
            current = kind == "sz_current"
            code = row["A股代码" if current else "证券代码"]
            if not code:
                continue
            add(
                0,
                code,
                row["A股简称" if current else "证券简称"],
                row["A股上市日期" if current else "上市日期"],
                None if current else row["终止上市日期"],
                proof,
            )
    for kind in ("sh_main", "sh_star"):
        raw, proof = sources[kind]
        data = json.loads(raw)
        rows = data["result"]
        require(
            len(rows) == int(data["pageHelp"]["total"]), "tdx_exchange_page_missing"
        )
        for row in rows:
            if row["STOCK_TYPE"] not in {"1", "8"}:
                continue
            add(
                1,
                row["A_STOCK_CODE"],
                row["COMPANY_ABBR"],
                date.fromisoformat(row["LIST_DATE"]).isoformat(),
                None if row["DELIST_DATE"] == "-" else row["DELIST_DATE"],
                proof,
            )
    raw, mapping_proof = sources["bj_mapping"]
    parser = MappingTable()
    parser.feed(raw.decode())
    mapping = {
        r[4]: (
            max(
                date(2021, 11, 15), datetime.strptime(r[2], "%Y/%m/%d").date()
            ).isoformat(),
            r[3],
        )
        for r in parser.rows
        if len(r) == 5 and r[4].isdigit()
    }
    bj_current = bse_pages(sources, through)
    if bj_current is not None:
        for row, proof in bj_current:
            code = row["xxzqdm"]
            add(
                2,
                code,
                row["xxzqjc"],
                max(date(2021, 11, 15), date.fromisoformat(row["fxssrq"])).isoformat(),
                None,
                proof,
                old_code=mapping.get(code, (None, None))[1],
                listing_basis="BSE_LISTING_SELECTED_LAYER_FLOORED_TO_BSE_OPEN",
            )
    else:
        for m in provider_snapshot["members"]:
            if m["market"] != 2:
                continue
            listed, old = mapping.get(m["instrument"], (None, None))
            add(
                2,
                m["instrument"],
                m["name"],
                listed,
                None,
                mapping_proof if listed else m["evidence"],
                old_code=old,
                provider_current=True,
            )
    for kind, (raw, proof) in sources.items():
        if not kind.startswith("bj_terminated_"):
            continue
        code = kind.rsplit("_", 1)[1]
        text = re.sub(
            r"\s+", "", "".join(p.extract_text() for p in PdfReader(BytesIO(raw)).pages)
        )
        match = re.search(
            r"公司股票将于([0-9]{4})年([0-9]{1,2})月([0-9]{1,2})日"
            r"被北京证券交易所终止上市并摘牌",
            text,
        )
        require(code in text and match is not None, "tdx_bj_termination_notice")
        require(code in mapping, "tdx_bj_listing_mapping_required")
        listed, old = mapping[code]
        add(
            2,
            code,
            code,
            listed,
            date(*map(int, match.groups())).isoformat(),
            proof,
            old_code=old,
            listing_evidence=mapping_proof,
        )
    market_details = []
    provider_keys = {
        (m["market"], m["instrument"]) for m in provider_snapshot["members"]
    }
    for market in (0, 1, 2):
        current = {
            k
            for k, m in members.items()
            if k[0] == market
            and (
                lifecycle(m, through) == "ACTIVE"
                or m["evidence"].get("provider_current")
            )
        }
        observed = {k for k in provider_keys if k[0] == market}
        market_details.append(
            dict(
                market=market,
                state="COMPLETE",
                current_count=len(current),
                missing_in_provider=[k[1] for k in sorted(current - observed)],
                extra_in_provider=[k[1] for k in sorted(observed - current)],
                enumeration="EXCHANGE_CURRENT_AND_TERMINATED"
                if market < 2 or bj_current is not None
                else "PROVIDER_CURRENT_EXCHANGE_MAPPING_PARTIAL",
                historical_enumeration="KNOWN_INTERVALS_ONLY_CODE_LINEAGE_UNVERIFIED",
                enumeration_from=through.isoformat()
                if market < 2 or bj_current is not None
                else None,
                enumeration_through=through.isoformat(),
            )
        )
    calendar = []
    for market, exchange in enumerate(("sz", "sh", "bj")):
        kind = f"{exchange}_calendar_2026"
        if kind in sources:
            raw, proof = sources[kind]
            calendar.extend(annual_calendar(raw, market, 2026, proof))
    suspensions = exchange_suspensions(sources, through)
    return dict(
        members=list(members.values()),
        details=dict(
            roster_kind="exchange_lifecycle",
            valid_through=through.isoformat(),
            sources=manifest["sources"],
            markets=market_details,
            historical_membership="EXCHANGE_KNOWN_INTERVALS_CODE_LINEAGE_UNVERIFIED",
            code_reuse="UNKNOWN",
            calendar=calendar,
            suspensions=suspensions,
            suspension_status="EXCHANGE_DATED_EVENTS_PARTIAL"
            if suspensions
            else "NOT_BOUND",
            provider_snapshot_id=provider_snapshot["snapshot_id"],
        ),
    )
