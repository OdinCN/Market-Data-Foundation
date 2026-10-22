"""Strict bounded network-frame replay, separate from semantic payload decoders."""

import struct
import zlib

from .model import CATALOG, MAX_PAYLOAD, Request, require


def payload_from_wire(raw: bytes, *, message_id: int, operation: str) -> bytes:
    require(type(raw) is bytes and 16 <= len(raw) <= MAX_PAYLOAD + 16, "wire_bound")
    require(
        raw[:4] == b"\xb1\xcb\x74\x00" and raw[4] & 16 != 0, "wire_prefix_or_failure"
    )
    observed_id = struct.unpack_from("<I", raw, 5)[0]
    observed_type, compressed, decoded = struct.unpack_from("<HHH", raw, 10)
    types = {str(key): value[1] for key, value in CATALOG.items()}
    types.update(
        ExMarkets=0x23F4,
        ExCount=0x23F0,
        ExInstruments=0x23F5,
        ExSetup=0x2454,
        Setup=0x000D,
    )
    require(
        operation in types
        and observed_id == message_id
        and observed_type == types[operation],
        "wire_response_identity",
    )
    require(compressed == len(raw) - 16, "wire_length")
    body = raw[16:]
    if compressed == decoded:
        return body
    try:
        decompressor = zlib.decompressobj()
        body = decompressor.decompress(body, decoded + 1)
    except zlib.error:
        from .model import DecodeError

        raise DecodeError("invalid_zlib") from None
    require(
        len(body) == decoded
        and decompressor.eof
        and not decompressor.unused_data
        and not decompressor.unconsumed_tail,
        "wire_decompression_bound_or_tail",
    )
    return body


def request_wire(req: Request, message_id: int) -> bytes:
    """Independent mirror of the pinned SDK constructors, useful for exact replay."""
    ex = req.operation.value.startswith("Ex")
    op = req.operation.value
    if ex:
        code = req.code.encode().ljust(9, b"\0")
        identity = bytes([req.market]) + code
        if op in ("ExQuote", "ExMinute"):
            data = identity
        elif op == "ExHistMinute":
            data = struct.pack("<I", int(req.date)) + identity
        elif op == "ExBars":
            data = identity + struct.pack("<HHIH", req.period, 1, req.start, req.count)
        else:
            data = identity + struct.pack("<IH", req.start, req.count)
            if op == "ExHistTrade":
                data = struct.pack("<I", int(req.date)) + data
    else:
        code = req.code[2:].encode()
        if op == "GetHistoryMinute":
            data = struct.pack("<IB", int(req.date), req.market) + code
        else:
            data = bytes([req.market, 0]) + code
            if op == "GetKline":
                data += struct.pack(
                    "<HHHH", req.period, 1, req.start, req.count
                ) + bytes(10)
            else:
                data += struct.pack("<HH", req.start, req.count)
                if op == "GetHistoryTrade":
                    data = struct.pack("<I", int(req.date)) + data
    length = len(data) + 2
    return (
        struct.pack(
            "<BIBHHH",
            1 if ex else 12,
            message_id,
            1,
            length,
            length,
            CATALOG[req.operation][1],
        )
        + data
    )


DIRECTORY_TYPES = {"ExMarkets": 0x23F4, "ExCount": 0x23F0, "ExInstruments": 0x23F5}


def planned_request_wire(request: dict, message_id: int) -> bytes:
    op = request["operation"]
    if op in DIRECTORY_TYPES:
        data = (
            struct.pack("<IH", request["start"], request["count"])
            if op == "ExInstruments"
            else b""
        )
        length = len(data) + 2
        return (
            struct.pack(
                "<BIBHHH", 1, message_id, 1, length, length, DIRECTORY_TYPES[op]
            )
            + data
        )
    from .model import Operation

    values = dict(request)
    values["operation"] = Operation(op)
    return request_wire(Request(**values), message_id)


def setup_wire(family: str) -> bytes:
    # Exact protocol/model_connect.go and protocol/model_ex.go FrameSetup bytes.
    data = b"\x01"
    prefix, typ = 12, 0x000D
    if family == "exhq":
        prefix, typ = 1, 0x2454
        data = bytes.fromhex("1f32c6e5d53dfb41") * 8 + bytes.fromhex(
            "cce16dffd5ba3fb8cbc57a054f7748ea"
        )
    length = len(data) + 2
    return struct.pack("<BIBHHH", prefix, 0, 1, length, length, typ) + data
