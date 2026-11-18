"""Collect trade-date evidence without changing the TDX acquisition schedule."""

import json
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from time import sleep
from xml.etree.ElementTree import ParseError
from zipfile import BadZipFile

import httpx

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.tdx_p0_roster import (
    bse_page,
    build_roster,
    exchange_suspensions,
    lifecycle,
)

SZ = "https://www.szse.cn/api/report/ShowReport"
SH = "https://query.sse.com.cn"
BJ = "https://www.bse.cn"
BSE_CENSUS_URL = BJ + "/nqxxController/nqxxCnzq.do"
MARKETS = {0: "sz", 1: "sh", 2: "bj"}
SOURCE_ERRORS = (
    ValueError,
    KeyError,
    TypeError,
    IndexError,
    AttributeError,
    OSError,
    ParseError,
    BadZipFile,
    httpx.HTTPError,
)
REFERERS = {
    "www.szse.cn": "https://www.szse.cn/",
    "query.sse.com.cn": "https://www.sse.com.cn/",
    "www.bse.cn": BJ + "/nq/listedcompany.html",
}


class ExchangeOriginals:
    def __init__(self, directory, client):
        self.directory = directory
        self.client = client

    def get(self, kind, url, parameters, *, method="GET"):
        # Public exchange endpoints are read in series, with bounded requests.
        sleep(1.5)
        referer = REFERERS[httpx.URL(url).host]
        options = {"params" if method == "GET" else "data": parameters}
        request = self.client.build_request(
            method, url, headers={"Referer": referer}, **options
        )
        response = self.client.send(request, stream=True, follow_redirects=False)
        redirected = False
        try:
            # The public BSE endpoint can issue a session cookie on a 307 to
            # its own URL. Follow that standard redirect once, preserving POST.
            retry = response.next_request
            if (
                str(request.url) == BSE_CENSUS_URL
                and request.method == "POST"
                and response.status_code == 307
                and retry is not None
                and retry.url == request.url
                and retry.method == request.method
            ):
                response.close()
                response = self.client.send(retry, stream=True, follow_redirects=False)
                redirected = True
            response.raise_for_status()
            pieces = []
            size = 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                require(size <= 8_000_000, "tdx_exchange_response_size")
                pieces.append(chunk)
        finally:
            response.close()
        raw = b"".join(pieces)
        path = self.directory / kind
        path.write_bytes(raw)
        return dict(
            kind=kind,
            url=str(response.request.url),
            parameters=parameters,
            method=method,
            **({"redirect_status": 307} if redirected else {}),
            path=str(path),
            sha256=sha256(raw).hexdigest(),
            received_at=datetime.now(UTC).isoformat(),
        )


def collect_census(get, market, day):
    if market == 0:
        return [
            get(
                "sz_current", SZ, dict(CATALOGID="1110", SHOWTYPE="xlsx", TABKEY="tab1")
            ),
            get(
                "sz_terminated",
                SZ,
                dict(CATALOGID="1793_ssgs", SHOWTYPE="xlsx", TABKEY="tab2"),
            ),
        ]
    if market == 1:
        return [
            get(
                f"sh_{board}",
                SH + "/sseQuery/commonQuery.do",
                {
                    "sqlId": "COMMON_SSE_CP_GPJCTPZ_GPLB_GP_L",
                    "STOCK_TYPE": stock_type,
                    "COMPANY_STATUS": "2,4,5,7,8",
                    "type": "inParams",
                    "isPagination": "true",
                    "pageHelp.pageSize": "5000",
                    "pageHelp.pageNo": "1",
                    "pageHelp.beginPage": "1",
                    "pageHelp.endPage": "1",
                },
            )
            for board, stock_type in (("main", "1"), ("star", "8"))
        ]

    parameters = {
        "typejb": "T",
        "zqdm": "",
        "sortfield": "xxzqdm",
        "sorttype": "asc",
        "xxfcbj[]": "2",
    }
    sources = []
    total_pages = 1
    for page in range(100):
        if page >= total_pages:
            break
        proof = get(
            f"bj_current_page_{page}",
            BSE_CENSUS_URL,
            dict(parameters, page=str(page)),
            method="POST",
        )
        data = bse_page(read_proof(proof))
        if page == 0:
            total_pages = int(data["totalPages"])
            require(0 < total_pages <= 100, "tdx_bj_page_budget")
        require(int(data["totalPages"]) == total_pages, "tdx_bj_census_changed")
        require(int(data["number"]) == page, "tdx_bj_page_identity")
        require(
            bool(data["content"])
            and all(
                date.fromisoformat(row["xxjsrq"]) == day for row in data["content"]
            ),
            "tdx_bj_census_day",
        )
        sources.append(proof)
    return sources


def read_proof(proof):

    raw = Path(proof["path"]).read_bytes()
    require(sha256(raw).hexdigest() == proof["sha256"], "tdx_exchange_digest")
    return raw


def collect_suspensions(get, market, day, candidates):
    if market == 0:
        # Only securities without a positive published bar need targeted checks.
        # Query from listing to include a halt that started before today's window.
        require(len(candidates) <= 24, "tdx_sz_suspension_request_budget")
        sources = []
        for code, listed in sorted(candidates.items()):
            parameters = dict(
                SHOWTYPE="JSON",
                CATALOGID="1798",
                TABKEY="tab1",
                txtDmorjc=code,
                txtKsrq=listed,
                txtZzrq=day.isoformat(),
            )
            page_count = 1
            for number in range(1, 17):
                if number > page_count:
                    break
                kind = f"sz_suspension_{code}" + (
                    f"_page_{number}" if number > 1 else ""
                )
                proof = get(kind, SZ + "/data", dict(parameters, PAGENO=str(number)))
                meta = json.loads(read_proof(proof))[0]["metadata"]
                if number == 1:
                    page_count = max(1, int(meta.get("pagecount", 1)))
                require(
                    page_count <= 16 and len(sources) < 60,
                    "tdx_sz_suspension_page_budget",
                )
                sources.append(proof)
        return sources
    if market == 1:
        return [
            get(
                "sh_suspension_current",
                SH + "/commonSoaQuery.do",
                {
                    "sqlId": "GW_PL_JYTS_TFPXX",
                    "startStopDate": (day - timedelta(days=1)).strftime("%Y%m%d"),
                    "endStopDate": day.strftime("%Y%m%d"),
                    "isPagination": "true",
                    "pageHelp.pageNo": "1",
                    "pageHelp.pageSize": "1000",
                    "productCode": "",
                    "keyWords": "",
                },
            )
        ]
    return []


def validate_suspensions(census, sources, *, market, day, candidates, positive):
    events = exchange_suspensions({s["kind"]: (read_proof(s), s) for s in sources}, day)
    suspended = {
        e["instrument"] for e in events if e["start"] <= day.isoformat() <= e["end"]
    }
    require(
        not any((market, code) in positive for code in suspended),
        "tdx_exchange_halt_conflicts_with_positive_bar",
    )
    if market == 2:
        require(
            all(
                row.get("xxtpbz") == "F"
                for proof in census
                for row in bse_page(read_proof(proof))["content"]
            ),
            "tdx_bj_suspension_requires_dated_notice",
        )
    return sorted(candidates.keys() - suspended)


def collect_manifest(day, reference, provider, positive, get):
    """An incomplete market retains its old census or its qualified new members."""
    day_text = day.isoformat()
    seed_kinds = {"bj_mapping", *(f"{m}_calendar_{day.year}" for m in MARKETS.values())}
    seed = [s for s in reference["sources"] if s["kind"] in seed_kinds]
    require({s["kind"] for s in seed} == seed_kinds, "tdx_exchange_reference_missing")
    base = {"valid_through": day_text}
    sources = list(seed)
    skipped = []
    pending = {}
    for market in MARKETS:
        try:
            census = collect_census(get, market, day)
            single = build_roster(
                dict(
                    base,
                    sources=[*seed, *census],
                    skipped_census_markets=sorted(MARKETS.keys() - {market}),
                ),
                provider,
            )
            members = {
                m["instrument"]: m for m in single["members"] if m["market"] == market
            }
            require(
                not any(
                    m["instrument"] not in members
                    and lifecycle(
                        m,
                        date.fromisoformat(
                            m["evidence"].get("valid_through", day_text)
                        ),
                    )
                    == "ACTIVE"
                    for m in provider.get("historical_members", [])
                    if m["market"] == market
                ),
                "tdx_exchange_member_disappeared_without_termination",
            )
        except SOURCE_ERRORS as error:
            skipped.append(market)
            pending[market] = classification(error)
            continue
        sources.extend(census)
        candidates = {
            code: m["evidence"]["listing_date"]
            for code, m in members.items()
            if lifecycle(m, day) == "ACTIVE" and (market, code) not in positive
        }
        try:
            suspensions = collect_suspensions(get, market, day, candidates)
            unresolved = validate_suspensions(
                census,
                suspensions,
                market=market,
                day=day,
                candidates=candidates,
                positive=positive,
            )
        except SOURCE_ERRORS as error:
            pending[market] = classification(error)
        else:
            sources.extend(suspensions)
            if unresolved:
                pending[market] = "tdx_exchange_trade_status_unresolved"
    return dict(
        base,
        sources=sources,
        skipped_census_markets=skipped,
        exchange_refresh=dict(
            day=day_text, pending_markets=sorted(pending), issues=pending
        ),
    )


def classification(error):
    if isinstance(error, ContractError):
        return str(error)
    if isinstance(error, httpx.HTTPStatusError):
        return f"tdx_exchange_http_{error.response.status_code}"
    return f"tdx_exchange_{type(error).__name__}"
