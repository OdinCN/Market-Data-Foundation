"""Retained admission/quality discovery; no SourceBinding or publication adoption."""

import base64
import struct
from dataclasses import asdict
from datetime import datetime
from hashlib import sha256

from market_data_foundation.candidates.tdx_security_directory.adapter import (
    DirectoryRequest,
    OfflineMaterial,
    decode,
)
from market_data_foundation.candidates.tdx_security_directory.schema import (
    Exchange,
    Operation,
    Route,
)
from market_data_foundation.canonical.model import Key, Kind, require
from market_data_foundation.domain_contracts.market_universe import EvidenceMode
from market_data_foundation.historical.market_daily_source import (
    NativeField,
    NativeRow,
    Page,
    PageRequest,
)
from market_data_foundation.qualification.harness import (
    Binding,
    Endpoint,
    Evidence,
    observe,
)
from market_data_foundation.runtime.tdx_watchlist import decode_json

from .tdx_market_plan import SEMANTICS, SOURCE, UPSTREAM, Plan, Request, encoded


def source_binding(request: Request):
    suffix = request.capability.value
    return Key(Kind.SOURCE, SOURCE, UPSTREAM), Key(
        Kind.BINDING, f"i2.{suffix}", SEMANTICS
    )


def request_wire(request: Request | None, index: int) -> bytes:
    """Exact pinned protocol frames; this is validation/encoding, never transport."""
    if request is None:
        kind, payload = 0x000D, b"\x01"
    elif request.operation == "GetCount":
        kind, payload = 0x044E, bytes((request.market, 0, 0x75, 0xC7, 0x33, 1))
    elif request.operation == "GetCode":
        kind, payload = 0x0450, struct.pack("<HH", request.market, request.start)
    else:
        kind = 0x052D
        payload = (
            struct.pack("<H", request.market)
            + request.code.encode("ascii")
            + struct.pack("<HHHH", 9, 1, request.start, request.count)
            + bytes(10)
        )
    return (
        b"\x0c"
        + struct.pack("<IBHHH", index + 2, 1, len(payload) + 2, len(payload) + 2, kind)
        + payload
    )


def admit_line(
    line: bytes, plan: Plan, index: int, *, known_at: datetime | None = None
):
    """Validate same frozen request and wire identity before decoding projections."""
    from .tdx_market_plan import utc_now

    known_at = known_at or utc_now()
    require(known_at.tzinfo is not None, "aware_known_clock_required")
    require(len(line) <= 2 * 1024 * 1024, "retained_line_budget")
    row = decode_json(line)
    row["inspection_known_at"] = known_at.isoformat()
    require(
        row["index"] == index and row["upstream_revision"] == UPSTREAM, "identity_drift"
    )
    expected = (
        asdict(plan.requests[index])
        if index >= 0
        else {"operation": "MConnect", "market": 0, "code": "", "start": 0, "count": 0}
    )
    # A failed dial retains only its classification.
    if index == -1 and not row["request_wire"]:
        require(row["classification"] != "retained_raw", "missing_raw")
        return row, b"", b""
    require(row["request"] == expected, "request_shape_drift")
    sent = base64.b64decode(row["request_wire"] or "", validate=True)
    raw = base64.b64decode(row["response_wire"] or "", validate=True)
    requested, received = (
        datetime.fromisoformat(row[key]) for key in ("requested_at", "received_at")
    )
    require(
        requested.tzinfo is not None
        and received.tzinfo is not None
        and requested <= received <= known_at,
        "source_clock_order",
    )
    if row["classification"] == "retained_raw":
        require(len(sent) >= 12 and len(raw) >= 16, "frame_truncated")
        require(
            sent == request_wire(plan.requests[index] if index >= 0 else None, index),
            "frozen_request_wire_mismatch",
        )
        require(
            sent[0] == 12
            and struct.unpack_from("<I", sent, 1)[0] == index + 2
            and struct.unpack_from("<I", raw, 5)[0] == index + 2
            and sent[10:12] == raw[10:12]
            and struct.unpack_from(">I", raw)[0] == 0xB1CB7400
            and len(raw) == 16 + struct.unpack_from("<H", raw, 12)[0],
            "wire_identity_invalid",
        )
        payload = encoded(row["source_projection"])
        if index >= 0 and plan.requests[index].operation != "GetKline":
            request = plan.requests[index]
            source, binding = source_binding(request)
            material = OfflineMaterial(
                DirectoryRequest(
                    Operation(request.operation),
                    (Exchange.SH if request.market else Exchange.SZ,),
                    Route.STANDARD,
                    plan.run_id,
                    start=request.start if request.operation == "GetCode" else None,
                ),
                payload,
                sha256(payload).hexdigest(),
                source,
                binding,
                f"i2-{plan.run_id}-directory",
                EvidenceMode.RETAINED,
                requested,
                received,
                known_at,
                plan.collector_sha256,
                UPSTREAM,
            )
            decoded = decode(material)
            row["directory_assessment"] = {
                "candidate_id": material.material_id,
                "issues": [item.classification for item in decoded.issues],
                "termination": decoded.termination,
                "row_count": len(decoded.records),
                "declared_count": decoded.declared_count,
                "identity_mapping": "UNKNOWN",
                "historical_membership": "UNKNOWN",
            }
        elif index >= 0:
            row["daily_assessment"] = inspect_daily(
                row["source_projection"], plan.requests[index]
            )
    return row, sent, raw


def inspect_daily(value: dict, request: Request):
    require(set(value) == {"Count", "List"}, "daily_projection_shape")
    require(type(value["Count"]) is int, "daily_count_invalid")
    rows = value["List"] or []
    require(type(rows) is list and len(rows) <= request.count, "daily_row_budget")
    issues, days = [], []
    keys = {
        "Last",
        "Open",
        "High",
        "Low",
        "Close",
        "Order",
        "Volume",
        "Amount",
        "Time",
        "UpCount",
        "DownCount",
    }
    for row in rows:
        require(type(row) is dict and set(row) == keys, "daily_native_fields")
        require(
            all(type(v) is int for k, v in row.items() if k != "Time"),
            "daily_int_fields",
        )
        clock = datetime.fromisoformat(row["Time"])
        require(clock.tzinfo is not None, "daily_clock_invalid")
        days.append(clock.date().isoformat())
        if (
            not row["Low"]
            <= min(row["Open"], row["Close"])
            <= max(row["Open"], row["Close"])
            <= row["High"]
        ):
            issues.append("ohlc_order")
        if row["Volume"] < 0 or row["Amount"] < 0:
            issues.append("negative_volume_or_amount")
    if value["Count"] != len(rows):
        issues.append("declared_count_mismatch")
    if days != sorted(set(days)):
        issues.append("duplicate_or_unordered_dates")
    return {
        "row_count": len(rows),
        "issues": sorted(set(issues)),
        "days": days,
        "terminal": "UNKNOWN",
        "units": "UNKNOWN",
        "source_event_time": "UNKNOWN",
        "date_semantics": (
            "SDK GetTime YYYYMMDD projected at 15:00 fixed UTC+8; event UNKNOWN"
        ),
        "adjustment": "UNKNOWN",
    }


def qualification_observation(row: dict, request: Request, plan: Plan, index: int):
    host, port = plan.endpoint.split(":")
    endpoint = Endpoint(host=host, port=int(port), unobservable_reason=None)
    binding = Binding(
        SOURCE, SEMANTICS, UPSTREAM, request.capability, "offline", endpoint
    )
    assessment = row.get("directory_assessment", row.get("daily_assessment", {}))
    success = row["classification"] == "retained_raw" and not assessment.get("issues")
    evidence = Evidence(
        success=success,
        row_count=assessment.get("row_count", 0),
        pages=1,
        complete=None,
        exhausted=None,
        endpoint=endpoint,
        units={"all": "UNKNOWN"},
        semantics={
            "material_kind": "source_projection",
            "source_event_time": "UNKNOWN",
        },
    )
    # Reinspection is offline. Live transport is separately retained in effect ledger.
    return observe(binding, request.fixture(plan.run_id, index), lambda _: evidence)


def daily_page(
    request: PageRequest,
    *,
    plan: Plan,
    source_index: int,
    line: bytes,
    retained_reference: str,
    expected_line_sha256: str,
    known_at: datetime,
    day_assertions: tuple[tuple[int, str, str], ...],
) -> Page:
    """Explicit retained D2 port. Caller supplies local-date proof for every row.

    A source projection is derived, not wire raw. No implicit fetch or mapping.
    Unknown event clocks/units and unknown pagination exhaustion remain unknown.
    """
    require(0 <= source_index < len(plan.requests), "source_index_invalid")
    require(
        sha256(line).hexdigest() == expected_line_sha256, "retained_digest_mismatch"
    )
    row, _, _ = admit_line(line, plan, source_index, known_at=known_at)
    original = plan.requests[source_index]
    source, binding = source_binding(original)
    require(
        original.operation == "GetKline"
        and row["classification"] == "retained_raw"
        and not row["daily_assessment"]["issues"],
        "daily_not_admissible",
    )
    prefix = "sh" if original.market else "sz"
    require(
        request.shard.route.source == source
        and request.shard.route.binding == binding
        and request.shard.mapping.request_code == original.code
        and request.shard.mapping.request_namespace == f"tdx.standard.{prefix.upper()}"
        and request.shard.route.implementation_identity == plan.collector_sha256
        and request.shard.route.endpoint_contract == plan.endpoint
        and request.offset == original.start
        and request.count == original.count,
        "d2_request_mismatch",
    )
    require(bool(retained_reference), "retained_reference_required")
    values = row["source_projection"]["List"] or []
    require(
        type(day_assertions) is tuple
        and len(day_assertions) == len(values)
        and tuple(item[0] for item in day_assertions) == tuple(range(len(values)))
        and all(type(day) is str and bool(proof) for _, day, proof in day_assertions),
        "explicit_row_day_proofs_required",
    )
    natives = []
    for (index, day, proof), value in zip(day_assertions, values, strict=True):
        require(day == value["Time"][:10], "local_date_evidence_mismatch")
        require(
            request.shard.window.contains(datetime.fromisoformat(day).date()),
            "row_outside_shard",
        )
        natives.append(
            NativeRow(
                day,
                f"/List/{index}",
                tuple(
                    NativeField(
                        name,
                        (
                            "time.Time"
                            if name == "Time"
                            else "Price"
                            if name
                            in {"Last", "Open", "High", "Low", "Close", "Amount"}
                            else "int64"
                            if name == "Volume"
                            else "int"
                        ),
                        field,
                        "protocol/model_kline.go:Kline",
                        proof,
                    )
                    for name, field in sorted(value.items())
                ),
            )
        )
    received = datetime.fromisoformat(row["received_at"])
    return Page(
        request.identity,
        encoded(row["source_projection"]),
        tuple(natives),
        plan.revision,
        None,
        None,
        "GetKline",
        UPSTREAM,
        SEMANTICS,
        datetime.fromisoformat(row["requested_at"]),
        received,
        known_at,
        None,
        "derived",
        retained_reference,
    )


def quote_changes(batches: tuple[bytes, ...], plan: Plan):
    """Compare economic fields for identical symbols, excluding SDK local Time."""
    previous, changes, digests = None, [], []
    for index, wire in enumerate(batches):
        body = decode_json(wire)
        require(
            body["sdk_release"] == plan.quote_config().driver_sdk_release
            and body["endpoint"] == plan.endpoint
            and body["session_id"] == plan.run_id
            and tuple(body["securities"]) == plan.securities
            and body["failure"] is None
            and body["round"] == index + 1,
            "quote_identity_or_failure",
        )
        rows = {}
        for row in body["rows"]:
            market = row["Exchange"]
            # Go Exchange marshals to its underlying integer in the pinned SDK.
            symbol = ("sh" if market == 1 else "sz") + row["Code"]
            require(market in (0, 1) and symbol not in rows, "quote_symbol_ambiguous")
            kline = row["Kline"]
            rows[symbol] = {
                **{
                    f"Kline.{k}": kline[k]
                    for k in ("Open", "High", "Low", "Close", "Volume", "Amount")
                },
                "BuyLevel": row["BuyLevel"],
                "SellLevel": row["SellLevel"],
            }
        require(set(rows) == set(plan.securities), "quote_members_mismatch")
        if previous is not None:
            changes.append(
                {
                    "round": index + 1,
                    "symbols": {
                        symbol: [
                            key
                            for key in rows[symbol]
                            if rows[symbol][key] != previous[symbol][key]
                        ]
                        for symbol in plan.securities
                    },
                }
            )
        previous = rows
        digests.append(
            {
                "round": index + 1,
                "raw_ipc_sha256": sha256(wire).hexdigest(),
                "content_sha256": sha256(encoded(rows)).hexdigest(),
                "observed_at": body["clocks"]["received"],
                "source_event_time": "UNKNOWN",
            }
        )
    changed = sum(
        any(fields for fields in change["symbols"].values()) for change in changes
    )
    return {
        "rounds": digests,
        "changes": changes,
        "content_refresh": "OBSERVED_MULTIPLE_TRANSITIONS"
        if changed >= 2
        else "NOT_PROVEN",
        "source_event_freshness": "UNKNOWN",
        "stream_live_qualification": "UNKNOWN",
        "snapshot_context": "AFTER_CLOSE_READ_ONLY"
        if plan.mode == "after_close"
        else "SESSION_OBSERVATION",
    }


def directory_inputs(
    lines: tuple[bytes, ...],
    plan: Plan,
    *,
    market: int,
    known_at: datetime,
):
    """Retained A-STD/D1 port with count bracketing and explicit partial closure.

    lines include the handshake at position zero. Return existing typed responses
    or PageAssembly. Integrators must supply MemberSelection/MaterialLink before
    calling existing snapshot_universe; no stable identity/category is invented.
    """
    from market_data_foundation.candidates.tdx_security_directory.adapter import (
        assemble_pages,
    )

    require(
        type(lines) is tuple and len(lines) <= len(plan.requests) + 1,
        "directory_intake_budget",
    )
    require(market in (0, 1), "standard_market_only")
    responses = []
    for index, request in enumerate(plan.requests):
        if index + 1 >= len(lines):
            break
        if request.market != market or request.operation == "GetKline":
            continue
        row, _, _ = admit_line(lines[index + 1], plan, index, known_at=known_at)
        if row["classification"] != "retained_raw":
            break
        source, binding = source_binding(request)
        payload = encoded(row["source_projection"])
        material = OfflineMaterial(
            DirectoryRequest(
                Operation(request.operation),
                (Exchange.SH if market else Exchange.SZ,),
                Route.STANDARD,
                plan.run_id,
                start=request.start if request.operation == "GetCode" else None,
            ),
            payload,
            sha256(payload).hexdigest(),
            source,
            binding,
            f"i2-{plan.run_id}-directory",
            EvidenceMode.RETAINED,
            datetime.fromisoformat(row["requested_at"]),
            datetime.fromisoformat(row["received_at"]),
            known_at,
            plan.collector_sha256,
            UPSTREAM,
        )
        responses.append(decode(material))
    counts = [
        row
        for row in responses
        if row.material.request.operation == Operation.GET_COUNT
    ]
    if len(counts) != 2 or responses[0] != counts[0] or responses[-1] != counts[-1]:
        return tuple(responses), {
            "schema": "mdf.i2.universe-discovery.v1",
            "market": market,
            "closure": "PARTIAL_NO_BRACKET",
            "qualification": "UNKNOWN",
            "revision": sha256(
                encoded([row.material.material_id for row in responses])
            ).hexdigest(),
        }
    assembly = assemble_pages(responses[0], tuple(responses[1:-1]), responses[-1])
    return (assembly,), {
        "schema": "mdf.i2.universe-discovery.v1",
        "market": market,
        "closure": assembly.termination,
        "issues": [issue.classification for issue in assembly.issues],
        "qualification": "UNKNOWN",
        "historical_membership": "UNKNOWN",
        "identity_mapping": "UNKNOWN",
        "same_count_constituent_drift": "UNKNOWN",
        "revision": sha256(
            encoded([row.material.material_id for row in responses])
        ).hexdigest(),
        "row_count": len(assembly.records),
        "member_digest": sha256(
            encoded([[row.exchange.value, row.code] for row in assembly.records])
        ).hexdigest(),
    }
