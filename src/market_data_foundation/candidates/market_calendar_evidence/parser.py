"""Strict explicit-material JSON parser, not an exchange announcement scraper."""

import json
import math
import re
from datetime import UTC, date, datetime
from hashlib import sha256
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.domain_contracts.market_universe import DateWindow

from .model import (
    MAX_BYTES,
    MAX_DAYS,
    MAX_ROWS,
    SCHEMA,
    Claim,
    EvidenceKind,
    MaterialReceipt,
    ParsedMaterial,
    Session,
    State,
)


def fields(value: object, required: set[str], optional: set[str] = frozenset()) -> dict:
    require(type(value) is dict, "object_required")
    require(required <= value.keys() <= required | optional, "invalid_fields")
    return value


def label(value: object) -> str:
    require(type(value) is str and bool(value.strip()), "text_required")
    return value


def local_date(value: object) -> date:
    require(
        type(value) is str and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value)),
        "invalid_local_date",
    )
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ContractError("invalid_local_date") from None


def zone(value: object) -> ZoneInfo:
    label(value)
    try:
        return ZoneInfo(value)
    except ValueError, ZoneInfoNotFoundError:
        raise ContractError("invalid_exchange_timezone") from None


def local_instant(value: object, fold: object, timezone: ZoneInfo) -> datetime:
    require(
        type(value) is str
        and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", value)),
        "local_clock_required",
    )
    require(fold is None or type(fold) is int and fold in (0, 1), "invalid_fold")
    try:
        wall = datetime.fromisoformat(value)
    except ValueError:
        raise ContractError("invalid_local_clock") from None
    candidates = {}
    for choice in (0, 1):
        try:
            candidate = wall.replace(tzinfo=timezone, fold=choice).astimezone(UTC)
            back = candidate.astimezone(timezone)
        except OverflowError:
            raise ContractError("local_clock_out_of_range") from None
        if back.replace(tzinfo=None) == wall and back.fold == choice:
            candidates[choice] = candidate
    require(bool(candidates), "nonexistent_local_clock")
    require(
        len(candidates) < 2 or fold is not None, "ambiguous_local_clock_requires_fold"
    )
    if fold is not None:
        require(fold in candidates, "invalid_fold_for_clock")
        return candidates[fold]
    return next(iter(candidates.values()))


def verified_json(raw: bytes, receipt: MaterialReceipt) -> dict:
    require(isinstance(receipt, MaterialReceipt), "receipt_required")
    require(type(raw) is bytes and 0 < len(raw) <= MAX_BYTES, "material_byte_limit")
    require(len(raw) == receipt.raw_length, "raw_length_mismatch")
    require(sha256(raw).hexdigest() == receipt.raw_sha256, "raw_digest_mismatch")

    def pairs(items: list[tuple]) -> dict:
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        raise ContractError("nonfinite_json")

    def number(value: str) -> float:
        parsed = float(value)
        require(math.isfinite(parsed), "nonfinite_json")
        return parsed

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=number,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        RecursionError,
        ValueError,
    ) as exc:
        if isinstance(exc, ContractError):
            raise
        raise ContractError("invalid_material_json") from None
    require(type(value) is dict, "object_required")
    # JSON permits escaped lone surrogates; canonical UTF-8 does not. Validate
    # keys and retained native fields too, before hashing or clock processing.
    pending = [value]
    while pending:
        item = pending.pop()
        if type(item) is str:
            require(
                re.search(r"[\ud800-\udfff]", item) is None, "invalid_material_unicode"
            )
        elif type(item) is dict:
            pending.extend(item.keys())
            pending.extend(item.values())
        elif type(item) is list:
            pending.extend(item)
    return value


def parse_material(raw: bytes, receipt: MaterialReceipt) -> ParsedMaterial:
    """Retain exact bytes, require explicit dates and never synthesize absent rows."""
    doc = fields(
        verified_json(raw, receipt),
        {"schema", "market", "timezone", "effective", "days"},
    )
    require(doc["schema"] == SCHEMA, "unsupported_calendar_schema")
    market = label(doc["market"])
    timezone = zone(doc["timezone"])
    validity = fields(doc["effective"], {"start", "end_exclusive"})
    window = DateWindow(
        local_date(validity["start"]), local_date(validity["end_exclusive"])
    )
    require(
        (window.end_exclusive - window.start).days <= MAX_DAYS, "calendar_window_limit"
    )
    require(
        type(doc["days"]) is list and len(doc["days"]) <= MAX_ROWS, "calendar_row_limit"
    )
    claims = tuple(
        _claim(item, i, window, timezone) for i, item in enumerate(doc["days"])
    )
    require(
        len({claim.claim_id for claim in claims}) == len(claims), "duplicate_claim_id"
    )
    return ParsedMaterial(receipt, market, doc["timezone"], window, claims, raw)


def _claim(value: object, index: int, window: DateWindow, timezone: ZoneInfo) -> Claim:
    row = fields(
        value,
        {
            "claim_id",
            "day",
            "state",
            "evidence_kind",
            "arrangement",
            "source_ref",
            "sessions",
            "complete_after",
            "complete_after_fold",
            "supersedes",
        },
        {"native_fields"},
    )
    claim_id = label(row["claim_id"])
    require(bool(re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", claim_id)), "invalid_claim_id")
    day = local_date(row["day"])
    require(window.contains(day), "claim_outside_effective_window")
    require(
        type(row["state"]) is str
        and row["state"] in {item.value for item in State if item != State.CONFLICT},
        "invalid_calendar_state",
    )
    require(
        type(row["evidence_kind"]) is str
        and row["evidence_kind"] in {item.value for item in EvidenceKind},
        "invalid_evidence_kind",
    )
    state, kind = State(row["state"]), EvidenceKind(row["evidence_kind"])
    label(row["arrangement"])
    label(row["source_ref"])
    require(
        type(row["sessions"]) is list and len(row["sessions"]) <= 32, "session_limit"
    )
    if "native_fields" in row:
        require(type(row["native_fields"]) is dict, "native_object_required")
    sessions = []
    for value in row["sessions"]:
        session = fields(value, {"id", "start", "end", "start_fold", "end_fold"})
        start = local_instant(session["start"], session["start_fold"], timezone)
        end = local_instant(session["end"], session["end_fold"], timezone)
        require(start < end, "empty_or_reversed_session")
        sessions.append(
            Session(
                label(session["id"]),
                session["start"],
                session["end"],
                session["start_fold"],
                session["end_fold"],
                start,
                end,
            )
        )
    sessions = tuple(sorted(sessions, key=lambda item: item.start_utc))
    require(
        len({item.session_id for item in sessions}) == len(sessions),
        "duplicate_session_id",
    )
    require(
        all(
            left.end_utc <= right.start_utc
            for left, right in zip(sessions, sessions[1:])
        ),
        "overlapping_sessions",
    )
    cutoff = None
    if row["complete_after"] is not None:
        cutoff = local_instant(
            row["complete_after"], row["complete_after_fold"], timezone
        )
        require(
            cutoff.astimezone(timezone).date() >= day, "completion_before_trading_date"
        )
        require(
            not sessions or cutoff >= sessions[-1].end_utc,
            "completion_before_session_end",
        )
    else:
        require(row["complete_after_fold"] is None, "fold_without_clock")
    if state in (State.OPEN, State.HALF_DAY):
        require(bool(sessions), "open_day_sessions_required")
    else:
        require(not sessions, "nonopen_day_has_sessions")
    if state == State.UNKNOWN:
        require(cutoff is None, "unknown_day_has_completion")
    require(
        type(row["supersedes"]) is list
        and all(type(item) is str and bool(item) for item in row["supersedes"]),
        "invalid_supersession_refs",
    )
    require(
        len(row["supersedes"]) <= 64
        and len(set(row["supersedes"])) == len(row["supersedes"]),
        "invalid_supersession_refs",
    )
    return Claim(
        claim_id,
        f"/days/{index}",
        day,
        state,
        kind,
        row["arrangement"],
        row["source_ref"],
        sessions,
        row["complete_after"],
        row["complete_after_fold"],
        cutoff,
        tuple(sorted(row["supersedes"])),
    )
