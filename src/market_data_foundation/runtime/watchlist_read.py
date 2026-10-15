"""Bounded local latest accepted Watchlist Read. No provider or write effects."""

import argparse
import ipaddress
import json
import os
import re
import stat
from contextlib import contextmanager
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from market_data_foundation.candidates.go_stock_real_observation_ingress import (
    OFFLINE_ORIGIN,
    REAL_ORIGIN,
    _utc,
)
from market_data_foundation.candidates.stock_snapshot import _cny
from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.runtime.tdx_watchlist import (
    DRIVER_SDK_RELEASE,
    MAX_FRAME,
    SessionConfig,
    WatchlistIngress,
    decode_json,
)
from market_data_foundation.runtime.watchlist_receipt import (
    CLASSIFICATIONS,
    RECEIPT_NAME,
    RECEIPT_REVISION,
    STATES,
    _last_errors,
)

READ_REVISION = "mdf.nonproduction.watchlist.latest.v1"
MAX_RECEIPT_BYTES = 131_072
MAX_REPORT_BYTES = 2_097_152
_ENTRY_FIELDS = {
    "round",
    "sha256",
    "byte_count",
    "known",
    "read_at",
    "outcome",
    "classification",
}


class _ReadError(Exception):
    pass


@contextmanager
def _directory(path):
    # Walk from / using pinned dirfds. Never resolve/follow even ancestor links.
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise _ReadError("unsafe_session_path")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            child = os.open(
                part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd
            )
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _load(fd, name, maximum, *, expected_size=None, expected_sha=None):
    try:
        file = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
    except FileNotFoundError:
        raise _ReadError(
            "receipt_missing" if name == RECEIPT_NAME else "evidence_missing"
        ) from None
    try:
        before = os.fstat(file)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink > 1:
            raise _ReadError("unsafe_artifact")
        if not 0 < before.st_size <= maximum:
            raise _ReadError("artifact_size_bound")
        chunks = []
        remaining = maximum + 1
        while remaining:
            chunk = os.read(file, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        after = os.fstat(file)
        # Unlink of an old receipt during atomic replace is safe. Content writes,
        # truncation and in-place changes fail closed; SHA pins each batch too.
        if (
            (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns)
            or len(raw) != before.st_size
            or len(raw) > maximum
        ):
            raise _ReadError("artifact_changed")
        if (expected_size is not None and len(raw) != expected_size) or (
            expected_sha is not None and sha256(raw).hexdigest() != expected_sha
        ):
            raise _ReadError("batch_identity_mismatch")
        return raw
    finally:
        os.close(file)


def _completed_report(fd):
    """Explicit legacy final-report compatibility; never scans/selects sessions."""
    report = decode_json(_load(fd, "report.json", MAX_REPORT_BYTES))
    require(
        report["sdk_release"] == DRIVER_SDK_RELEASE
        and report["provider"] == "tdx_go_pinned"
        and report["session_id"] == report["config"]["session_id"]
        and type(report["session_complete"]) is bool
        and (report["child_exit"] is None or type(report["child_exit"]) is int)
        and report["health"] in {"readable_snapshot", "unavailable"}
        and type(report["rounds"]) is list
        and type(report["events"]) is list
        and len(report["rounds"]) <= 120,
        "invalid_report",
    )
    rounds = []
    for r in report["rounds"]:
        require(
            type(r["ack_sent"]) is bool and type(r["go_ack_received"]) is bool,
            "invalid_report_ack",
        )
        if not r["go_ack_received"]:
            break
        require(r["ack_sent"], "invalid_report_ack")
        raw = _load(fd, f"batch-{len(rounds) + 1}.json", MAX_FRAME)
        rounds.append(
            {
                k: r[k]
                for k in ("round", "sha256", "read_at", "outcome", "classification")
            }
            | {"known": r["clocks"]["known"], "byte_count": len(raw)}
        )
    complete = report["session_complete"]
    require(
        complete == (report["stop_reason"] == "budget_exhausted")
        and (
            not complete
            or (
                type(report["child_exit"]) is int
                and report["child_exit"] == 0
                and bool(rounds)
                and len(rounds) == report["config"]["rounds"]
                and len(rounds) == len(report["rounds"])
                and report["health"]
                == (
                    "readable_snapshot"
                    if rounds and rounds[-1]["outcome"] in {"sample", "duplicate"}
                    else "unavailable"
                )
                and bool(report["events"])
                and report["events"][-1]["classification"] == "complete"
                and report["events"][-1]["reason"] == "budget_exhausted"
                and report["events"][-1]["round"] == len(rounds)
                and report["events"][-1]["segment"] == report["rounds"][-1]["segment"]
            )
        ),
        "invalid_report_completion",
    )
    classification = report["stop_reason"]
    return {
        "revision": RECEIPT_REVISION,
        "config": report["config"],
        "input_origin": report["input_origin"],
        "rounds": rounds,
        "producer_state": "ended" if complete else "unavailable",
        "classification": classification
        if classification in CLASSIFICATIONS
        else "contract_rejected",
        "last_errors": _last_errors(report["events"]),
    }, report["rounds"]


def _receipt(value, at):
    require(
        value.keys()
        == {
            "revision",
            "config",
            "input_origin",
            "rounds",
            "producer_state",
            "classification",
            "last_errors",
        }
        and value["revision"] == RECEIPT_REVISION
        and value["input_origin"] in {OFFLINE_ORIGIN, REAL_ORIGIN}
        and value["producer_state"] in STATES
        and value["classification"] in CLASSIFICATIONS | {None},
        "invalid_receipt",
    )
    require(
        type(value["config"]) is dict
        and value["config"].keys() == {f.name for f in fields(SessionConfig)}
        and type(value["config"]["securities"]) is list
        and type(value["config"]["endpoints"]) is list,
        "invalid_receipt_config",
    )
    config_body = value["config"].copy()
    config_body["securities"] = tuple(config_body["securities"])
    config_body["endpoints"] = tuple(config_body["endpoints"])
    config = SessionConfig(**config_body)
    require(bool(config.session_id), "session_id_required")
    entries = value["rounds"]
    require(
        type(entries) is list and len(entries) <= config.rounds, "receipt_round_bound"
    )
    previous = None
    hashes = set()
    for n, r in enumerate(entries, 1):
        require(
            type(r) is dict
            and r.keys() == _ENTRY_FIELDS
            and type(r["round"]) is int
            and r["round"] == n
            and type(r["byte_count"]) is int
            and 0 < r["byte_count"] <= MAX_FRAME
            and type(r["sha256"]) is str
            and re.fullmatch(r"[a-f0-9]{64}", r["sha256"])
            and r["sha256"] not in hashes
            and r["outcome"] in {"sample", "duplicate", "failed"}
            and r["classification"]
            in {None, "timeout", "disconnected", "source_error"},
            "invalid_receipt_entry",
        )
        known, read = _utc(r["known"]), _utc(r["read_at"])
        require(
            known <= read <= at and (previous is None or previous < known),
            "receipt_clock_order",
        )
        previous = read
        hashes.add(r["sha256"])
    errors = value["last_errors"]
    require(
        type(errors) is dict and errors.keys() == {"connection", "sampling"},
        "invalid_errors",
    )
    for error in errors.values():
        require(
            error is None
            or (
                type(error) is dict
                and error.keys() == {"classification", "round", "segment"}
                and error["classification"]
                in {
                    "refused",
                    "access_restricted",
                    "timeout",
                    "disconnected",
                    "source_error",
                }
                and type(error["round"]) is int
                and 0 <= error["round"] <= config.rounds
                and type(error["segment"]) is int
                and 0 <= error["segment"] <= config.max_failovers
            ),
            "invalid_errors",
        )
    state, classification = value["producer_state"], value["classification"]
    require(
        (
            state == "ended"
            and classification == "budget_exhausted"
            and len(entries) == config.rounds
        )
        or (state == "accepted" and bool(entries) and classification is None)
        or (state == "starting" and not entries and classification is None)
        or (
            state == "pending_ack"
            and len(entries) < config.rounds
            and classification is None
        )
        or (
            state == "unavailable"
            and classification not in {None, "", "budget_exhausted"}
        ),
        "receipt_state_mismatch",
    )
    return config


def _error_progress(value, ingress):
    """Cross-check the retained error ledger against reconstructed acceptance.

    An ACKed failure is an unavailable observation, not recovery. A later ACKed
    success can recover it only through the ingress's new segment/baseline rules.
    Unaccepted failures are allowed only with a state that blocks old quotes.
    """
    errors = value["last_errors"]
    blocked = value["producer_state"] in {"pending_ack", "unavailable"}
    history = ingress.history
    sampling = errors["sampling"]
    last_failure = next((o for o in reversed(history) if o.source_failure), None)
    if sampling is None:
        require(last_failure is None, "missing_sampling_error")
    elif sampling["round"] <= len(history):
        require(
            last_failure is not None
            and sampling
            == {
                "classification": last_failure.source_failure,
                "round": history.index(last_failure) + 1,
                "segment": last_failure.record.segment,
            },
            "sampling_error_drift",
        )
    else:
        require(
            blocked
            and sampling["round"] == len(history) + 1
            and sampling["segment"] == ingress.segment,
            "unaccepted_sampling_error",
        )
    connection = errors["connection"]
    if connection is not None:
        # Dial failures do not advance a segment. Accepting a batch in that
        # segment proves recovery; an error for a higher segment does not.
        latest_segment = history[-1].record.segment if history else 0
        require(
            connection["round"] == 0
            and connection["segment"]
            <= latest_segment + int(blocked and bool(history)),
            "unaccepted_connection_error",
        )


def _peer(value):
    require(
        type(value) is str
        and re.fullmatch(r"[0-9A-Fa-f:.\[\]]{1,80}:[0-9]{1,5}", value),
        "invalid_peer",
    )
    host, port = value.rsplit(":", 1)
    ipaddress.ip_address(host.strip("[]"))
    require(
        port.isascii() and port.isdecimal() and 1 <= int(port) <= 65535, "invalid_peer"
    )
    return value


def _quote(quote):
    if quote is None:
        return None
    return {
        "prices_cny": dict(
            zip(
                ("last", "open", "high", "low", "close"),
                (str(_cny(p)) for p in quote.prices_li),
                strict=True,
            )
        ),
        "amount_cny": str(quote.amount_cny),
        "volume_sdk": quote.volume_sdk,
        "volume_unit": "sdk_volume_count_UNKNOWN",
        "bids_sdk": [list(level) for level in quote.bids],
        "asks_sdk": [list(level) for level in quote.asks],
        "level_units": ["decoded_price_integer_li", "sdk_level_count_UNKNOWN"],
    }


def _read(fd, value, at, result, legacy_rounds=None):
    config = _receipt(value, at)
    ingress = WatchlistIngress(
        config, offline_fixture_mode=value["input_origin"] == OFFLINE_ORIGIN
    )
    observation = body = None
    previous_read = None
    for r in value["rounds"]:
        raw = _load(
            fd,
            f"batch-{r['round']}.json",
            MAX_FRAME,
            expected_size=r["byte_count"],
            expected_sha=r["sha256"],
        )
        body = decode_json(raw)
        if (
            ingress.connection is None
            or body["segment"] != ingress.connection["segment"]
        ):
            require(
                previous_read is None
                or previous_read <= _utc(body["connection"]["requested"]),
                "connection_clock_not_forward",
            )
            ingress.bind(
                {
                    k: body[k]
                    for k in (
                        "session_id",
                        "sdk_release",
                        "endpoint",
                        "segment",
                        "connection",
                    )
                }
            )
        observation = ingress._ingest_at(raw, known_at=_utc(r["known"]))
        record = observation.record
        require(
            record.outcome == r["outcome"]
            and observation.classification == r["classification"]
            and (previous_read is None or previous_read <= record.clocks.requested),
            "acceptance_reconstruction_mismatch",
        )
        previous_read = _utc(r["read_at"])
        expected = "unavailable" if observation.source_failure else "observed"
        views = [
            ingress.read(m, at=previous_read, batch_id=observation.batch_id)
            for m in config.securities
        ]
        require(
            all(view.status == expected for view in views),
            "producer_read_mismatch",
        )
        if legacy_rounds is not None:
            original = legacy_rounds[r["round"] - 1]
            require(
                all(
                    original[k] == body[k]
                    for k in (
                        "batch_id",
                        "endpoint",
                        "sdk_release",
                        "segment",
                        "input_origin",
                        "clock_provenance",
                    )
                )
                and original["connection_peer"] == body["connection"]["peer"]
                and type(original["quote_elapsed_ns"]) is int
                and original["quote_elapsed_ns"] == observation.quote_elapsed_ns
                and original["known_provenance"] == observation.known_provenance
                and original["clocks"].keys()
                == {"requested", "received", "frozen", "known"}
                and all(
                    _utc(original["clocks"][k]) == getattr(record.clocks, k)
                    for k in original["clocks"]
                )
                and original["reads"]
                == [
                    {
                        "security": m,
                        "status": v.status,
                        "stale": v.stale,
                        "local_received_age_seconds": None
                        if v.age is None
                        else v.age.total_seconds(),
                        "close_cny": None
                        if v.state.quote is None
                        else str(v.state.quote.close_cny),
                    }
                    for m, v in zip(config.securities, views, strict=True)
                ],
                "legacy_report_drift",
            )
        _peer(body["connection"]["peer"])
    _error_progress(value, ingress)
    result.update(
        session_id=config.session_id,
        fixture_id=f"tdx-watchlist-{config.session_id}",
        securities=list(config.securities),
        ttl_seconds=config.ttl_seconds,
        producer_state=value["producer_state"],
        last_errors=value["last_errors"],
        input_origin=value["input_origin"],
    )
    if observation is None:
        result.update(
            status="unavailable"
            if value["producer_state"] == "unavailable"
            else "no_data",
            classification=value["classification"] or "no_accepted_batch",
        )
        return result
    record = observation.record
    blocked = (
        value["producer_state"] in {"pending_ack", "unavailable"}
        or observation.source_failure is not None
    )
    members = []
    for m in config.securities:
        view = ingress.read(m, at=at, batch_id=observation.batch_id)
        members.append(
            {
                "security": m,
                "status": "unavailable" if blocked else view.status,
                "stale": view.stale,
                "local_received_age_seconds": None
                if view.age is None
                else view.age.total_seconds(),
                "value_acquisition": view.state.acquisition,
                "value_received_at": None
                if view.state.observed_at is None
                else view.state.observed_at.isoformat(),
                "quote": None if blocked else _quote(view.state.quote),
            }
        )
    result.update(
        empty=all(m["quote"] is None for m in members),
        status="unavailable"
        if blocked
        else "stale"
        if any(m["stale"] for m in members)
        else "observed",
        classification=(
            value["classification"] or "awaiting_go_ack"
            if blocked and value["producer_state"] in {"pending_ack", "unavailable"}
            else observation.classification
        ),
        batch_status="failure" if record.outcome == "failed" else record.outcome,
        batch={
            "batch_id": observation.batch_id,
            "round": body["round"],
            "segment": record.segment,
            "sha256": observation.envelope_sha256,
            "byte_count": value["rounds"][-1]["byte_count"],
            "acquisition": record.acquisition,
            "sdk_release": observation.sdk_release,
            "endpoint": observation.endpoint,
            "peer": _peer(body["connection"]["peer"]),
            "parser_revision": record.parser_revision,
            "provider": "tdx_go_pinned",
            "capability": "quote.snapshot",
            "clock_provenance": observation.clock_provenance,
            "known_provenance": observation.known_provenance,
            "clocks": {
                k: getattr(record.clocks, k).isoformat()
                for k in ("requested", "received", "frozen", "known")
            }
            | {"producer_read_at": value["rounds"][-1]["read_at"]},
        },
        local_known_age_seconds=(at - record.clocks.known).total_seconds(),
        members=members,
    )
    return result


def read_latest_watchlist(session_dir, *, read_at=None, completed_report=False) -> dict:
    """Read one explicit absolute local session directory; never discover a source.

    Default evidence is the producer's atomic Go-ACK acceptance receipt, visible
    during a running session. completed_report=True explicitly selects legacy
    final-report evidence and cannot follow a running producer. Errors contain
    fixed classifications only; no paths, raw envelopes or exception messages.
    Explicit UTC read_at supports deterministic retained observation inspection.
    """
    result = {
        "read_revision": READ_REVISION,
        "scope": "bounded_nonproduction_research",
        "publication_status": "UNPUBLISHED",
        "dataset_version": None,
        "source_admission": "NOT_GRANTED",
        "revision_admission": "NOT_GRANTED",
        "producer_liveness": "UNKNOWN",
        "source_event_clock": "UNKNOWN",
        "server_time": "UNKNOWN",
        "upstream_freshness": "UNKNOWN",
        "source_freshness": "UNKNOWN",
        "status": "unavailable",
        "empty": True,
        "members": [],
        "batch": None,
        "batch_status": "unavailable",
        "acceptance_evidence": "completed_report"
        if completed_report
        else "atomic_go_ack_receipt",
    }
    try:
        at = datetime.now(UTC) if read_at is None else read_at
        require(
            type(at) is datetime and at.utcoffset() == timedelta(0),
            "read_clock_invalid",
        )
        require(type(completed_report) is bool, "read_mode_invalid")
        result["read_at"] = at.isoformat()
        with _directory(session_dir) as fd:
            if completed_report:
                value, legacy_rounds = _completed_report(fd)
            else:
                value = decode_json(_load(fd, RECEIPT_NAME, MAX_RECEIPT_BYTES))
                legacy_rounds = None
            return _read(fd, value, at, result.copy(), legacy_rounds)
    except _ReadError as exc:
        result["classification"] = str(exc)
        if str(exc) == "receipt_missing":
            result["status"] = "no_data"
    except OSError:
        result["classification"] = "local_artifact_unavailable"
    except (
        ContractError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        OverflowError,
        RecursionError,
    ):
        result["classification"] = "invalid_acceptance_evidence"
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir", required=True, type=Path)
    parser.add_argument("--completed-report", action="store_true")
    args = parser.parse_args()
    result = read_latest_watchlist(
        args.session_dir, completed_report=args.completed_report
    )
    print(json.dumps(result, allow_nan=False, separators=(",", ":")))
    return 0 if result["status"] == "observed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
