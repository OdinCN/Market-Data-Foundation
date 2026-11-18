"""Import retained exchange and Tongdaxin short-name changes on DEV."""

import argparse
import json
from datetime import date
from hashlib import sha256
from pathlib import Path

from market_data_foundation.historical.tdx_p0_roster import a_share, xlsx_rows
from market_data_foundation.storage.tdx_p0 import connect, migrate

SZSE = "https://www.szse.cn/api/report/ShowReport?SHOWTYPE=xlsx&CATALOGID=SSGSGMXX&TABKEY=tab2"
TDX = "https://fk.tdx.com.cn/TQLEX?Entry=CWServ.tdxsj_bfzl_bfzl"


def szse_changes(path):
    raw = path.read_bytes()
    digest = sha256(raw).hexdigest()
    for r in xlsx_rows(raw):
        code = r["证券代码"]
        if a_share(0, code):
            yield (
                0,
                code,
                date.fromisoformat(r["变更日期"]),
                r["变更前简称"],
                r["变更后简称"],
                SZSE,
                digest,
            )


def tdx_changes(directory):
    """Read a complete paged response from 股票更名, 实施ST or 撤销ST."""
    expected = None
    seen = set()
    received = 0
    for page, path in enumerate(sorted(directory.glob("*.json")), 1):
        raw = path.read_bytes()
        digest = sha256(raw).hexdigest()
        result = json.loads(raw)
        data, cursor, total = result["ResultSets"]
        count = total["Content"][0][0]
        if (
            result["ErrorCode"] != 0
            or cursor["Content"] != [[page]]
            or expected not in (None, count)
            or data["Count"] != len(data["Content"])
        ):
            raise ValueError(f"Inconsistent Tongdaxin response on page {page}")
        expected = count
        for values in data["Content"]:
            r = dict(zip(data["ColName"], values, strict=True))
            received += 1
            key = (r["sc"], r["code"], r["rq"], r["prename"], r["lastname"])
            if key in seen:
                raise ValueError(f"Overlapping Tongdaxin pages at {key}")
            seen.add(key)
            market, code, day, old, new = key
            # Shenzhen uses its own exchange report. Listing rows can lack
            # a previous name. 'name' is today's name, 'lastname' is the
            # name that actually took effect on 'rq'.
            if (
                market in (1, 2)
                and r.get("zqlx", "A股") == "A股"
                and a_share(market, code)
                and old
                and new
                and old != new
            ):
                yield market, code, date.fromisoformat(day), old, new, TDX, digest
    if received != expected:
        raise ValueError(f"Incomplete Tongdaxin pages: {received}/{expected}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument(
        "--xlsx", type=Path, help="SZSE securities short-name change report"
    )
    source.add_argument(
        "--tdx-pages",
        type=Path,
        help="Ordered, zero-padded Tongdaxin name-change or ST event JSON pages",
    )
    parser.add_argument("--dev-opt-in", action="store_true", required=True)
    args = parser.parse_args()
    changes = list(
        szse_changes(args.xlsx) if args.xlsx else tdx_changes(args.tdx_pages)
    )
    if not changes or any(
        not old.strip() or not new.strip() for _, _, _, old, new, _, _ in changes
    ):
        raise ValueError("Incomplete security short-name change report")
    with connect() as conn:
        migrate(conn, dev_opt_in=args.dev_opt_in)
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO mdf_tdx_daily.roster_name_changes "
                "(market,instrument,effective_date,old_name,new_name,"
                "source_url,raw_sha256) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                changes,
            )
            print(f"{len(changes)} A-share change records, {cur.rowcount} inserted")


if __name__ == "__main__":
    main()
