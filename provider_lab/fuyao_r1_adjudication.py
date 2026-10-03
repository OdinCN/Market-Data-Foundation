"""离线响应检查；没有网络、凭据发现、重试或生产绑定入口。"""

import math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

IMPLEMENTATION = "fuyao-single-attempt-https-r1"
PRICES = ("open_price", "high_price", "low_price", "close_price")
SHANGHAI = ZoneInfo("Asia/Shanghai")


def local_date(milliseconds, zone=SHANGHAI):
    return datetime.fromtimestamp(milliseconds / 1000, zone).date().isoformat()


def universe_check(query, data):
    """记录过滤与身份不符，不以 offset 小页宣称市场完整。"""
    items = data["item"]
    return {
        "requested_exchange": query["exchange"],
        "offset": int(query["offset"]),
        "limit": int(query["limit"]),
        "returned_thscodes": [item["thscode"] for item in items],
        "returned_exchanges": sorted({item["exchange"] for item in items}),
        "exchange_filter_matches": all(
            item["exchange"] == query["exchange"] for item in items
        ),
        "row_count": len(items),
        "complete": None,
        "exhausted": None,
    }


def bars_check(query, data):
    items = data["item"]
    stamps = [item["date_ms"] for item in items]
    numeric = all(
        type(item.get(field)) in (int, float)
        and math.isfinite(item[field])
        and item[field] >= 0
        for item in items
        for field in (*PRICES, "volume", "turnover")
    )
    ohlc = numeric and all(
        item["low_price"]
        <= min(item["open_price"], item["close_price"])
        <= max(item["open_price"], item["close_price"])
        <= item["high_price"]
        for item in items
    )
    return {
        "thscode": data.get("thscode"),
        "identity_matches": data.get("thscode") == query["thscode"],
        "interval_matches": data.get("interval") == query["interval"] == "1d",
        "adjust_label_matches": data.get("adjust") == query["adjust"],
        "row_count": len(items),
        "dates_asia_shanghai": [local_date(stamp) for stamp in stamps],
        "strictly_ascending_unique": all(
            left < right for left, right in zip(stamps, stamps[1:])
        ),
        "within_requested_window": all(
            int(query["start"]) <= stamp <= int(query["end"]) for stamp in stamps
        ),
        "finite_nonnegative_values": numeric,
        "ohlc_valid": ohlc,
        "complete": None,
        "units": {},
    }


def compare_modes(none, forward, backward):
    """只比较实际行；仿射关系不是复权算法或事件证明。"""
    base = none["item"]
    result = {}
    for mode, data in (("forward", forward), ("backward", backward)):
        changed = data["item"]
        aligned = data["thscode"] == none["thscode"] and [
            row["date_ms"] for row in base
        ] == [row["date_ms"] for row in changed]
        cell = {"dates_and_identity_align": aligned}
        if aligned and base:
            pairs = [
                (raw[field], adj[field])
                for raw, adj in zip(base, changed, strict=True)
                for field in PRICES
            ]
            cell.update(
                ohlc_all_different=all(a != b for a, b in pairs),
                volume_unchanged=all(
                    a["volume"] == b["volume"]
                    for a, b in zip(base, changed, strict=True)
                ),
                turnover_unchanged=all(
                    a["turnover"] == b["turnover"]
                    for a, b in zip(base, changed, strict=True)
                ),
                ohlc_pair_count=len(pairs),
            )
            x1, y1 = pairs[0]
            other = next(((x, y) for x, y in pairs if x != x1), None)
            if other:
                x2, y2 = other
                slope = (y2 - y1) / (x2 - x1)
                intercept = y1 - slope * x1
                residual = max(abs(y - (slope * x + intercept)) for x, y in pairs)
                cell["sample_affine_fit"] = {
                    "slope": slope,
                    "intercept": intercept,
                    "max_absolute_residual": residual,
                }
        result[mode] = cell
    result["qualified_qfq_hfq_mapping"] = False
    result["anchor_established"] = False
    result["events_established"] = False
    return result


def calendar_check(data):
    items = data["item"]
    dates = [datetime.strptime(item["date"], "%Y%m%d").date() for item in items]
    return {
        "row_count": len(items),
        "observed_start": min(dates).isoformat() if dates else None,
        "observed_end": max(dates).isoformat() if dates else None,
        "strictly_ascending_unique": all(
            left < right for left, right in zip(dates, dates[1:])
        ),
        "date_ms_matches_utc_date": all(
            local_date(item["date_ms"], timezone.utc) == day.isoformat()
            for item, day in zip(items, dates, strict=True)
        ),
        "date_ms_utc_midnight": all(item["date_ms"] % 86400000 == 0 for item in items),
        "date_ms_matches_asia_shanghai_date": all(
            local_date(item["date_ms"]) == day.isoformat()
            for item, day in zip(items, dates, strict=True)
        ),
        "date_ms_asia_shanghai_midnight": all(
            datetime.fromtimestamp(item["date_ms"] / 1000, SHANGHAI).time().isoformat()
            == "00:00:00"
            for item in items
        ),
        "complete": None,
        "historical_calendar_established": False,
    }


STATEMENT_VALUES = {
    "balance-sheet": (
        "assets_total",
        "total_current_assets",
        "non_current_nets_total",
        "cash",
        "accounts_receivable",
        "total_debt",
        "holder_equity_total",
    ),
    "income": (
        "operating_income",
        "operating_costs",
        "operating_expenses",
        "sales_fee",
        "manage_fee",
        "research_and_development_expenses",
        "operating_profit",
        "interest_expenses",
        "profit_total",
        "income_tax_expense",
        "net_profit",
        "parent_holder_net_profit",
        "basic_eps",
    ),
    "cash-flow": (
        "act_cash_flow_net",
        "invest_cash_flow_net",
        "financing_cash_flow_net",
        "pay_fixed_assets_etc_cash",
        "pay_dividends_profits_interest_cash",
        "cash_equivalents_net_addition",
    ),
}
STATEMENT_IDENTITY = (
    "thscode",
    "ticker",
    "period",
    "fiscal_year",
    "fiscal_period",
    "report_date_ms",
    "period_end_ms",
    "currency",
)


def statements_check(kind, query, data):
    """三表独立结构检查；日期字段保留原名，不认证公告/更新或单位语义。"""
    items = data["item"]
    values = STATEMENT_VALUES[kind]
    fields = (*STATEMENT_IDENTITY, *values)

    def numeric(value):
        return type(value) in (int, float) and math.isfinite(value)

    dates_valid = bool(items) and all(
        type(row.get(field)) is int and 0 <= row[field] <= 253402214400000
        for row in items
        for field in ("period_end_ms", "report_date_ms")
    )
    identity = bool(items) and all(
        row.get("thscode") == query["thscode"]
        and row.get("ticker") == query["thscode"].split(".")[0]
        and row.get("period") == query["period"]
        and type(row.get("fiscal_year")) is int
        and row.get("fiscal_period") == "FY"
        and row.get("currency") == "CNY"
        for row in items
    )
    periods = [(row.get("fiscal_year"), row.get("fiscal_period")) for row in items]
    values_valid = bool(items) and all(
        all(
            field in row and (row[field] is None or numeric(row[field]))
            for field in values
        )
        and any(numeric(row.get(field)) for field in values)
        for row in items
    )
    unique_periods = len(set(periods)) == len(periods)
    period_dates_match = (
        dates_valid
        and identity
        and all(
            local_date(row["period_end_ms"]) == f"{row['fiscal_year']}-12-31"
            and row["report_date_ms"] >= row["period_end_ms"]
            for row in items
        )
    )
    return {
        "statement": kind,
        "row_count": len(items),
        "returned_fields": sorted(set().union(*(row.keys() for row in items))),
        "null_counts": {
            field: sum(row.get(field) is None for row in items) for field in fields
        },
        "rows": [
            {field: row[field] for field in fields if field in row} for row in items
        ],
        "identity_period_currency_match": identity,
        "finite_values_or_explicit_null": values_valid,
        "unique_report_periods": unique_periods,
        "date_encoding_valid": dates_valid,
        "period_dates_match": period_dates_match,
        "dates_asia_shanghai": [
            {
                field: local_date(row[field])
                for field in ("period_end_ms", "report_date_ms")
            }
            for row in items
        ]
        if dates_valid
        else [],
        "timestamp": data.get("timestamp"),
        "timestamp_asia_shanghai": local_date(data["timestamp"])
        if type(data.get("timestamp")) is int
        else None,
        "structural_sample_valid": identity
        and values_valid
        and dates_valid
        and unique_periods
        and period_dates_match,
        "publication_date_established": False,
        "update_date_established": False,
        "units": {},
        "complete": None,
    }


def taxonomy_check(tag, data, selected_group, constituents):
    """仅证明请求 tag 下的 THS catalog 与一个选中组的身份结构。"""
    import re

    catalog = data["item"]
    members = constituents["item"]
    catalog_ids = [row.get("thscode") for row in catalog]
    member_ids = [row.get("thscode") for row in members]
    catalog_valid = (
        bool(catalog)
        and all(
            re.fullmatch(r"\d{6}\.TI", row.get("thscode", ""))
            and isinstance(row.get("name"), str)
            and row["name"]
            for row in catalog
        )
        and len(set(catalog_ids)) == len(catalog_ids)
    )
    members_valid = (
        bool(members)
        and all(
            re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", row.get("thscode", ""))
            and row.get("ticker") == row["thscode"].split(".")[0]
            and isinstance(row.get("name"), str)
            and row["name"]
            for row in members
        )
        and len(set(member_ids)) == len(member_ids)
    )
    selected = [row for row in catalog if row.get("thscode") == selected_group]
    return {
        "taxonomy": "THS",
        "requested_tag": tag,
        "response_tag_present": "tag" in data,
        "catalog_item_count": len(catalog),
        "catalog_identity_fields": ["thscode", "name"],
        "catalog_identity_valid": bool(catalog_valid),
        "selected_group_id": selected_group,
        "selected_group_name": selected[0].get("name") if len(selected) == 1 else None,
        "selected_group_in_catalog": len(selected) == 1,
        "constituent_item_count": len(members),
        "constituent_identity_fields": ["thscode", "ticker", "name"],
        "constituent_identity_valid": bool(members_valid),
        "constituent_exchange_suffix_counts": {
            suffix: sum(
                isinstance(code, str) and code.endswith("." + suffix)
                for code in member_ids
            )
            for suffix in ("SH", "SZ", "BJ")
        },
        "constituents": [
            {
                field: row[field]
                for field in ("thscode", "ticker", "name")
                if field in row
            }
            for row in members
        ],
        "response_group_identity_present": "thscode" in constituents,
        "structural_sample_valid": bool(catalog_valid and members_valid)
        and len(selected) == 1
        and tag in ("industry", "cn_concept"),
        "complete": None,
        "historical_membership_established": False,
        "hierarchy_established": False,
        "tdx_mapping_established": False,
    }
