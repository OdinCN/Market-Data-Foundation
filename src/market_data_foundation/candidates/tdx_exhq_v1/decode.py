"""Strict ExHq native payloads, separate from standard TDX units and clocks."""

import struct

from market_data_foundation.candidates.tdx_intraday_v1.decode import (
    Cursor,
    _wall_time,
    page_identity,
    row,
)
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    MAX_ROWS,
    Material,
    Operation,
    Page,
    require,
)


def decode_ex(material: Material) -> Page:
    from .catalog import verified_association

    req = material.request
    require(req.operation.value.startswith("Ex"), "ex_operation_required")
    pid = page_identity(material)
    raw = material.raw
    findings = [
        "SOURCE_RIGHTS_UNKNOWN",
        "CALENDAR_UNKNOWN",
        "CURRENCY_LOT_MULTIPLIER_UNKNOWN",
        "SOURCE_ZONE_AND_DATE_UNKNOWN",
        "PAGE_EXHAUSTION_UNKNOWN",
    ]
    if not verified_association(material):
        findings.append("DIRECTORY_ASSOCIATION_UNKNOWN")
    if req.operation == Operation.EX_QUOTE:
        # SDK guard and consumed quote body require 14 + 136 = 150 bytes.
        require(len(raw) == 150, "ex_quote_exact_150_bytes_required")
        cursor = Cursor(raw)
        market = cursor.take(1)[0]
        code_raw = cursor.take(9).split(b"\0", 1)[0]
        try:
            code = code_raw.decode("ascii")
        except UnicodeDecodeError:
            from market_data_foundation.candidates.tdx_intraday_v1.model import (
                DecodeError,
            )

            raise DecodeError("invalid_quote_code") from None
        require(market == req.market and code == req.code, "quote_identity_mismatch")
        cursor.take(4)
        native = {"market": market, "code": code}
        for name in ("preClose", "open", "high", "low", "price"):
            native[name] = cursor.unpack("<f")[0]
        native["kaiCang"] = cursor.unpack("<I")[0]
        cursor.take(4)
        native["zongLiang"], native["xianLiang"] = cursor.unpack("<II")
        cursor.take(4)
        native["neiPan"], native["waiPan"] = cursor.unpack("<II")
        cursor.take(4)
        native["chiCang"] = cursor.unpack("<I")[0]
        for name, fmt in (
            ("bid", "<5f"),
            ("bidVol", "<5I"),
            ("ask", "<5f"),
            ("askVol", "<5I"),
        ):
            native[name] = cursor.unpack(fmt)
        cursor.finish()
        return Page(
            material,
            (row(material, pid, 0, 0, len(raw), native),),
            1,
            tuple(findings + ["QUOTE_SOURCE_EVENT_CLOCK_UNKNOWN"]),
        )
    shapes = {
        Operation.EX_BARS: (20, 32),
        Operation.EX_MINUTE: (12, 18),
        Operation.EX_HISTORY_MINUTE: (20, 18),
        Operation.EX_TRADE: (16, 16),
        Operation.EX_HISTORY_TRADE: (16, 16),
    }
    require(req.operation in shapes, "unsupported_ex_operation")
    header, width = shapes[req.operation]
    require(len(raw) >= header, "short_ex_header")
    count = struct.unpack_from("<H", raw, header - 2)[0]
    require(count <= MAX_ROWS, "row_budget")
    require(not req.count or count <= req.count, "declared_exceeds_request")
    require(len(raw) == header + count * width, "ex_declared_shape_mismatch")
    cursor = Cursor(raw, header)
    rows = []
    for index in range(count):
        start = cursor.pos
        local = None
        status = "WIRE_TIME_OF_DAY_DATE_ZONE_UNKNOWN"
        if req.operation == Operation.EX_BARS:
            local = _wall_time(cursor.take(4))
            native = {
                name: cursor.unpack("<f")[0]
                for name in ("open", "high", "low", "close")
            }
            position_bytes = cursor.take(4)
            native.update(
                datetime=local.replace("T", " "),
                position=struct.unpack("<I", position_bytes)[0],
                amount=struct.unpack("<f", position_bytes)[0],
                position_amount_shared_bytes=position_bytes.hex(),
                trade=cursor.unpack("<I")[0],
                price=cursor.unpack("<f")[0],
            )
            # Reinterpretation may legitimately be NaN: keep bits and an explicit null.
            import math

            if not math.isfinite(native["amount"]):
                native["amount"] = None
            status = "WIRE_LOCAL_DATE_MINUTE_ZONE_UNKNOWN"
        elif req.operation in (Operation.EX_MINUTE, Operation.EX_HISTORY_MINUTE):
            minute, price, avg, volume, interest = cursor.unpack("<HffII")
            require(minute < 1440, "invalid_minute_of_day")
            native = dict(
                hour=minute // 60,
                minute=minute % 60,
                price=price,
                avgPrice=avg,
                volume=volume,
                openInterest=interest,
            )
            local = f"{minute // 60:02d}:{minute % 60:02d}"
        else:
            minute, price, volume, change, nature = cursor.unpack("<HIIiH")
            require(minute < 1440, "invalid_minute_of_day")
            # Preserve the original nature. SDK second=0 may be a
            # fallback, not observed seconds.
            second = nature % 10000
            native = dict(
                hour=minute // 60,
                minute=minute % 60,
                price=price,
                volume=volume,
                zengCang=change,
                nature=nature,
                sdk_second=second if second <= 59 else 0,
                observed_second=None,
                direction=None,
            )
            if req.market in (31, 48):
                native["direction"] = {0: 1, 256: -1}.get(nature, 0)
            else:
                # SDK nature group only, preserving market-specific
                # open/close semantics as unknown.
                native["direction"] = {0: 1, 1: -1}.get(nature // 10000, 0)
            local = f"{minute // 60:02d}:{minute % 60:02d}"
        rows.append(
            row(material, pid, index, start, cursor.pos, native, local, None, status)
        )
    cursor.finish()
    if req.operation == Operation.EX_BARS:
        findings += [
            "AMOUNT_POSITION_SAME_BYTES_NOT_INDEPENDENT_AMOUNT",
            "BAR_ADJUSTMENT_FLAG_SEMANTICS_UNKNOWN",
        ]
    if req.operation in (Operation.EX_TRADE, Operation.EX_HISTORY_TRADE):
        findings += [
            "TRADE_PRICE_UINT32_NO_QUOTE_FLOAT_SCALING",
            "SDK_SECOND_MAY_BE_FALLBACK",
        ]
    if req.operation in (Operation.EX_MINUTE, Operation.EX_HISTORY_MINUTE):
        findings += ["OPEN_INTEREST_ECONOMIC_MEANING_UNKNOWN"]
    if not rows:
        findings.append("EMPTY_PAGE_NOT_PROOF_OF_NO_DATA")
    return Page(material, tuple(rows), count, tuple(findings))
