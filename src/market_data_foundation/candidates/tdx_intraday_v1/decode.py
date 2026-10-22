"""Bounded payload decoding. Never derive a source date from the host clock."""

import math
import struct
from datetime import UTC, datetime, timedelta, timezone

from .model import (
    MAX_ROWS,
    Material,
    Operation,
    Page,
    Row,
    canonical,
    require,
    sha,
)


class Cursor:
    def __init__(self, raw: bytes, offset: int = 0):
        self.raw = raw
        self.pos = offset

    def take(self, size: int) -> bytes:
        require(self.pos + size <= len(self.raw), "truncated_row")
        value = self.raw[self.pos : self.pos + size]
        self.pos += size
        return value

    def unpack(self, fmt: str) -> tuple:
        values = struct.unpack(fmt, self.take(struct.calcsize(fmt)))
        require(
            all(not isinstance(v, float) or math.isfinite(v) for v in values),
            "nonfinite_native_float",
        )
        return values

    def varint(self) -> int:
        # SDK getData accumulates int32. Reject encodings that would overflow it.
        first = self.take(1)[0]
        value, shift, item = first & 63, 6, first
        while item & 128:
            require(shift <= 27, "varint_width")
            item = self.take(1)[0]
            value += (item & 127) << shift
            shift += 7
        require(value <= 2147483647, "varint_int32_overflow")
        return -value if first & 64 else value

    def finish(self) -> None:
        require(self.pos == len(self.raw), "undeclared_trailing_bytes")


def row(
    material: Material,
    page_id: str,
    index: int,
    start: int,
    end: int,
    native: dict,
    local: str | None = None,
    utc: str | None = None,
    clock_status: str = "UNKNOWN",
) -> Row:
    return Row(
        index,
        start,
        end - start,
        sha(material.raw[start:end]),
        page_id,
        canonical(native),
        local,
        utc,
        clock_status,
    )


def page_identity(material: Material) -> str:
    return sha(
        canonical(
            {"receipt": material.receipt(), "schema": "mdf.tdx.i4.source-native.v1"}
        )
    )


def _wall_time(encoded: bytes) -> str:
    day, minutes = struct.unpack("<HH", encoded)
    require(minutes < 1440, "invalid_minute_of_day")
    try:
        return datetime(
            2004 + (day >> 11),
            (day % 2048) // 100,
            (day % 2048) % 100,
            minutes // 60,
            minutes % 60,
        ).isoformat(timespec="minutes")
    except ValueError:
        from .model import DecodeError

        raise DecodeError("invalid_wire_date") from None


def _trade_clock(day: str, minutes: int) -> tuple[str, str]:
    require(minutes < 1440, "invalid_minute_of_day")
    local = datetime(
        int(day[:4]),
        int(day[4:6]),
        int(day[6:]),
        minutes // 60,
        minutes % 60,
        tzinfo=timezone(timedelta(hours=8)),
    )
    return local.isoformat(), local.astimezone(UTC).isoformat()


def decode(material: Material) -> Page:
    require(type(material) is Material, "material_required")
    if material.request.operation.value.startswith("Ex"):
        from market_data_foundation.candidates.tdx_exhq_v1.decode import decode_ex

        return decode_ex(material)
    req = material.request
    historical_trade = req.operation == Operation.HISTORY_TRADE
    header = 6 if historical_trade or req.operation == Operation.HISTORY_MINUTE else 2
    require(len(material.raw) >= header, "short_header")
    declared = struct.unpack_from("<H", material.raw)[0]
    require(declared <= MAX_ROWS, "row_budget")
    if req.count:
        require(declared <= req.count, "declared_exceeds_request")
    cursor = Cursor(material.raw, header)
    pid = page_identity(material)
    rows = []
    price = 0
    findings = ["SOURCE_RIGHTS_UNKNOWN", "CALENDAR_UNKNOWN", "PAGE_EXHAUSTION_UNKNOWN"]
    if req.operation == Operation.HISTORY_MINUTE:
        findings += [
            "SDK_TIME_SYNTHESIZED_FROM_ROW_INDEX",
            "MINUTE_PRICE_SCALE_UPSTREAM_X10",
            "MINUTE_NUMBER_UNIT_UNKNOWN",
        ]
    if req.operation == Operation.BARS:
        findings += [
            "HOST_TIMEZONE_DEPENDENT_SDK_TIME_NOT_USED",
            "SDK_FIX_KLINE_TIME_NOT_APPLIED",
            "VOLUME_SCOPE_REQUIRES_KIND_CODE_PERIOD",
        ]
    if historical_trade:
        findings += ["NUMBER_ABSENT_IN_HISTORY"]
    for index in range(declared):
        start = cursor.pos
        local = utc = None
        status = "UNKNOWN"
        if req.operation == Operation.HISTORY_MINUTE:
            delta, opaque, number = cursor.varint(), cursor.varint(), cursor.varint()
            price += delta
            # Exact upstream model_history_minute algorithm; generated
            # label is not source time.
            minute = 570 + index + 1 + (90 if index >= 120 else 0)
            native = {
                "Price": price * 10,
                "Number": number,
                "Time": f"{minute // 60:02d}:{minute % 60:02d}",
                "encoded_price_delta": delta,
                "opaque_price": opaque,
            }
            status = "SDK_SYNTHETIC_ROW_LABEL_NOT_SOURCE_EVENT"
        elif req.operation in (Operation.TRADE, Operation.HISTORY_TRADE):
            minutes = cursor.unpack("<H")[0]
            delta, volume = cursor.varint(), cursor.varint()
            number = None if historical_trade else cursor.varint()
            trade_status, opaque = cursor.varint(), cursor.varint()
            price += delta * 10
            is_etf = (
                req.code[:2] == "sh"
                and req.code[2:4] in ("50", "51", "52", "53", "56", "58")
            ) or (req.code[:2] == "sz" and req.code[2:4] in ("15", "16"))
            scaled = abs(price) // (10 if is_etf else 1) * (-1 if price < 0 else 1)
            local, utc = _trade_clock(req.date, minutes)
            # Date is request/cache context, minute comes from payload.
            # No seconds are observed.
            status = "WIRE_MINUTE_REQUEST_DATE_FIXED_UTC8_NOT_INDEPENDENT_DATE"
            native = {
                "Time": local,
                "Price": scaled,
                "Volume": volume,
                "Number": number,
                "Status": trade_status,
                "opaque": opaque,
                "encoded_price_delta": delta,
                "encoded_minute": minutes,
            }
        else:
            encoded_date = cursor.take(4)
            local = _wall_time(encoded_date)
            opening, closing, high, low = [cursor.varint() for _ in range(4)]
            volume_bits = cursor.take(4)
            amount_bits = cursor.take(4)
            volume = struct.unpack("<f", volume_bits)[0]
            amount = struct.unpack("<f", amount_bits)[0]
            require(
                math.isfinite(volume) and math.isfinite(amount),
                "nonfinite_native_float",
            )
            previous = price
            price += opening + closing
            # Raw float interpretation and scoped SDK normalization are both retained.
            convertible = (
                req.code[:2] == "sh" and req.code[2:5] in ("110", "111", "113", "118")
            ) or (
                req.code[:2] == "sz"
                and req.code[2:5] in ("123", "125", "126", "127", "128")
            )
            require(
                -(2**63) <= volume < 2**63 and -(2**63) <= amount * 1000 < 2**63,
                "sdk_integer_projection_overflow",
            )
            unscaled = int(volume)
            sdk_volume = (
                unscaled
                if convertible
                else abs(unscaled) // 100 * (-1 if unscaled < 0 else 1)
            )
            if req.kind == "index":
                sdk_volume *= 100
            require(-(2**63) <= sdk_volume < 2**63, "sdk_integer_projection_overflow")
            native = {
                "Last": previous,
                "Open": previous + opening,
                "Close": price,
                "High": previous + opening + high,
                "Low": previous + opening + low,
                "Volume": sdk_volume,
                "Amount": int(amount * 1000),
                "Time": local,
                "encoded_volume": volume,
                "encoded_volume_bits": volume_bits.hex(),
                "encoded_amount_bits": amount_bits.hex(),
                "UpCount": None,
                "DownCount": None,
            }
            if req.kind == "index":
                native["UpCount"], native["DownCount"] = cursor.unpack("<HH")
            status = "WIRE_LOCAL_DATE_MINUTE_ZONE_UNKNOWN"
        rows.append(
            row(material, pid, index, start, cursor.pos, native, local, utc, status)
        )
    cursor.finish()
    if not rows:
        findings.append("EMPTY_PAGE_NOT_PROOF_OF_NO_DATA")
    return Page(material, tuple(rows), declared, tuple(findings))
