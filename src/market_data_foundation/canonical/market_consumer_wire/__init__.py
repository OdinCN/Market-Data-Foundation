"""Strict independent JSON body contract; no changes to legacy consumer v1."""

from datetime import datetime

from market_data_foundation.canonical.market_consumer_read import (
    WIRE,
    Item,
    Query,
    ReadError,
)
from market_data_foundation.canonical.market_consumer_read.contracts import (
    Limits,
    check,
    encode,
    strict_json,
)
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_universe import (
    InstrumentIdentity,
)


def shape(value, required, optional=()):
    check(
        type(value) is dict
        and set(required) <= set(value)
        and set(value) <= set(required) | set(optional),
        "INVALID_QUERY",
        400,
    )


def text(value):
    check(type(value) is str and 0 < len(value) <= 512, "INVALID_QUERY", 400)
    return value


def key(value, kind):
    shape(value, ("identity", "revision") if kind == Kind.DEFINITION else ("identity",))
    return Key(
        kind,
        text(value["identity"]),
        text(value["revision"]) if kind == Kind.DEFINITION else "",
    )


def local_date(value):
    from datetime import date

    text(value)
    result = date.fromisoformat(value)
    check(result.isoformat() == value, "INVALID_QUERY", 400)
    return result


def parse_body(raw: bytes, limits: Limits = Limits()) -> tuple[Query, str | None]:
    check(type(raw) is bytes, "INVALID_JSON", 400)
    check(len(raw) <= limits.request, "REQUEST_TOO_LARGE", 413)
    body = strict_json(raw)
    shape(body, ("query",), ("cursor",))
    cursor = body.get("cursor")
    if "cursor" in body:
        check(type(cursor) is str and bool(cursor), "INVALID_CURSOR", 400)
        check(len(cursor) <= limits.cursor, "CURSOR_TOO_LARGE", 413)
    q = body["query"]
    check(len(encode(q)) <= limits.query, "REQUEST_TOO_LARGE", 413)
    shape(
        q,
        ("wire_revision", "definition", "items", "selection", "page_size"),
        ("universe_revision", "semantics"),
    )
    check(q["wire_revision"] == WIRE, "INVALID_QUERY", 400)
    selection = q["selection"]
    shape(selection, ("mode",), ("at",))
    check(selection["mode"] in ("as_of", "exact_versions"), "INVALID_QUERY", 400)
    at = selection.get("at")
    check("at" not in selection or type(at) is str, "INVALID_QUERY", 400)
    check(selection["mode"] == "as_of" or "at" not in selection, "INVALID_QUERY", 400)
    try:
        if at is not None:
            at = datetime.fromisoformat(at)
            check(
                at.utcoffset() is not None and at.utcoffset().total_seconds() == 0,
                "INVALID_AS_OF",
                400,
            )
            check(
                selection["at"]
                in (at.isoformat(), at.isoformat().replace("+00:00", "Z")),
                "INVALID_AS_OF",
                400,
            )
        check(type(q["items"]) is list, "INVALID_QUERY", 400)
        items = []
        for i in q["items"]:
            shape(
                i,
                ("item_id", "instrument", "start_date", "end_date_exclusive"),
                ("exact_versions",),
            )
            shape(i["instrument"], ("namespace", "identity"))
            check(type(i.get("exact_versions", [])) is list, "INVALID_QUERY", 400)
            check(
                ("exact_versions" in i) == (selection["mode"] == "exact_versions"),
                "INVALID_EXACT_LIST",
                400,
            )
            items.append(
                Item(
                    text(i["item_id"]),
                    InstrumentIdentity(
                        text(i["instrument"]["namespace"]),
                        text(i["instrument"]["identity"]),
                    ),
                    local_date(i["start_date"]),
                    local_date(i["end_date_exclusive"]),
                    tuple(key(k, Kind.VERSION) for k in i.get("exact_versions", [])),
                )
            )
        semantics = q.get("semantics", {})
        check(type(semantics) is dict, "INVALID_QUERY", 400)
        semantics = tuple(sorted((text(k), text(v)) for k, v in semantics.items()))
        result = Query(
            key(q["definition"], Kind.DEFINITION),
            tuple(items),
            selection["mode"],
            at,
            q["page_size"],
            text(q["universe_revision"]) if "universe_revision" in q else None,
            semantics,
        )
        from market_data_foundation.canonical.market_consumer_read.reader import (
            validate_query,
        )

        validate_query(result, limits)
        return result, cursor
    except ValueError, TypeError, ContractError:
        raise ReadError("INVALID_QUERY", 400) from None


def error_body(error: ReadError) -> dict:
    return {
        "wire_revision": WIRE,
        "status": "ERROR",
        "error": error.code,
        **({"dimension": error.dimension} if error.dimension else {}),
    }


def read_body(reader, raw: bytes) -> tuple[int, bytes]:
    try:
        query, cursor = parse_body(raw, reader.limits)
        return 200, encode(reader.read(query, cursor))
    except ReadError as exc:
        return exc.status, encode(error_body(exc))
