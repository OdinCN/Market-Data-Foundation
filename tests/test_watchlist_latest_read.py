"""Author-created envelopes, explicit clocks and local IPC; no provider access."""

import json
import os
import subprocess
import sys
import threading
from copy import deepcopy
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from types import SimpleNamespace

import pytest
from test_tdx_watchlist_runtime import (
    CHILD,
    CONFIG,
    Adapter,
    batch,
    binding,
    clocks,
    event,
    quote_events,
    stream_prefix,
    wire,
)

from market_data_foundation.runtime.tdx_watchlist import (
    DRIVER_SDK_RELEASE,
    DRIVER_SDK_RELEASES,
    FramedProcess,
    RealPeriodicWatchlistSession,
)
from market_data_foundation.runtime.watchlist_read import (
    MAX_RECEIPT_BYTES,
    READ_REVISION,
    read_latest_watchlist,
)
from market_data_foundation.runtime.watchlist_receipt import (
    RECEIPT_NAME,
    AcceptanceReceiptWriter,
)


def produce(path, bodies=None, *, mutate_messages=None, callback=None, config=CONFIG):
    bodies = deepcopy(bodies or [batch(n) for n in range(1, 5)])
    config = replace(config, rounds=len(bodies))
    for body in bodies:
        body["max_rounds"] = config.rounds
    session = RealPeriodicWatchlistSession(config, offline_fixture_mode=True)
    messages = stream_prefix(bodies[0])
    segment = 0
    for body in bodies:
        n = body["round"]
        if body["segment"] != segment:
            segment = body["segment"]
            messages += [
                (
                    "E",
                    event(
                        "connection_attempt",
                        endpoint=body["endpoint"],
                        segment=segment,
                        attempt=segment + 1,
                    ),
                ),
                ("C", wire(binding(body))),
            ]
        messages += quote_events(n, segment)
        if body["failure"]:
            messages.append(
                (
                    "E",
                    event(
                        "quote_failed",
                        body["failure"],
                        round=n,
                        segment=segment,
                        endpoint=body["endpoint"],
                    ),
                )
            )
        messages += [
            ("B", wire(body)),
            (
                "E",
                event(
                    "ack_received", round=n, segment=segment, endpoint=body["endpoint"]
                ),
            ),
        ]
    messages.append(
        ("E", event("complete", "budget_exhausted", round=len(bodies), segment=segment))
    )
    if mutate_messages:
        messages = mutate_messages(messages)
    (path / "config.json").write_text(json.dumps(asdict(config)))

    def archive(name, raw):
        (path / name).write_bytes(raw)

    writer = AcceptanceReceiptWriter(path)

    def ready(session, state, classification):
        writer(session, state, classification)
        if callback:
            callback(session, state, classification)

    now = iter(clocks(n) for n in range(1, len(bodies) + 1))
    adapter = Adapter(messages)
    adapter.process = SimpleNamespace(returncode=0)
    report = session.run(
        adapter,
        archive=archive,
        fixture_clock=lambda: next(now),
        on_read_ready=ready,
    )
    (path / "report.json").write_text(json.dumps(report))
    return session, report


def receipt(path):
    return json.loads((path / RECEIPT_NAME).read_bytes())


def alter_receipt(path, change):
    value = receipt(path)
    change(value)
    (path / RECEIPT_NAME).write_text(json.dumps(value))


@pytest.mark.parametrize("release", DRIVER_SDK_RELEASES)
def test_pinned_release_receipt_replay_preserves_frozen_bytes(tmp_path, release):
    body = batch()
    body["sdk_release"] = release
    _, report = produce(
        tmp_path, [body], config=replace(CONFIG, driver_sdk_release=release)
    )
    assert report["session_complete"]
    frozen = (tmp_path / "batch-1.json").read_bytes()
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "observed"
    assert value["batch"]["sdk_release"] == release
    assert value["batch"]["sha256"] == sha256(frozen).hexdigest()
    # Explicit final-report compatibility also follows the configured pin.
    saved_receipt = (tmp_path / RECEIPT_NAME).read_bytes()
    (tmp_path / RECEIPT_NAME).unlink()
    legacy_report = read_latest_watchlist(
        tmp_path, read_at=clocks(1), completed_report=True
    )
    assert legacy_report["status"] == "observed"
    assert legacy_report["batch"]["sdk_release"] == release
    (tmp_path / RECEIPT_NAME).write_bytes(saved_receipt)
    # Historical receipts predate the parent-only release policy field.
    alter_receipt(tmp_path, lambda r: r["config"].pop("driver_sdk_release"))
    old = read_latest_watchlist(tmp_path, read_at=clocks(1))
    if release == DRIVER_SDK_RELEASE:
        assert old["status"] == "observed"
        assert old["batch"]["sdk_release"] == DRIVER_SDK_RELEASE
    else:
        assert old["status"] == "unavailable"
    assert (tmp_path / "batch-1.json").read_bytes() == frozen


def test_latest_exact_values_identity_units_and_local_clocks(tmp_path):
    _, report = produce(tmp_path)
    at = clocks(4) + timedelta(seconds=1)
    value = read_latest_watchlist(tmp_path, read_at=at)
    assert value["read_revision"] == READ_REVISION
    assert value["status"] == "observed" and value["batch_status"] == "duplicate"
    assert value["producer_state"] == "ended" and not value["empty"]
    assert value["session_id"] == CONFIG.session_id
    assert value["fixture_id"] == report["fixture_id"]
    b = value["batch"]
    raw = (tmp_path / "batch-4.json").read_bytes()
    assert b["sha256"] == sha256(raw).hexdigest() == report["rounds"][-1]["sha256"]
    assert b["byte_count"] == len(raw)
    assert (b["batch_id"], b["acquisition"], b["round"], b["segment"]) == (
        f"{CONFIG.session_id}-4",
        "sample-4",
        4,
        0,
    )
    assert b["sdk_release"] == DRIVER_SDK_RELEASE and b["endpoint"] == "mock-a:7709"
    assert b["peer"] == "203.0.113.1:7709"
    assert b["clocks"]["known"] == clocks(4).isoformat()
    assert value["read_at"] == at.isoformat() and value["local_known_age_seconds"] == 1
    assert value["securities"] == list(CONFIG.securities)
    assert len(value["members"]) == 3
    for member in value["members"]:
        assert member["status"] == "observed" and not member["stale"]
        assert member["local_received_age_seconds"] == 3
        assert member["value_acquisition"] == "sample-4"
        quote = member["quote"]
        assert quote["prices_cny"] == dict.fromkeys(
            ("last", "open", "high", "low", "close"), "24.680"
        )
        assert quote["amount_cny"] == "456.789" and quote["volume_sdk"] == 31
        assert quote["volume_unit"] == "sdk_volume_count_UNKNOWN"
        assert quote["bids_sdk"] == [[24680 - i, i] for i in range(1, 6)]
        assert quote["asks_sdk"] == [[24680 + i, i] for i in range(1, 6)]
    assert (
        value["publication_status"] == "UNPUBLISHED"
        and value["dataset_version"] is None
    )
    assert value["source_admission"] == "NOT_GRANTED"
    assert all(
        value[k] == "UNKNOWN"
        for k in (
            "source_event_clock",
            "source_freshness",
            "server_time",
            "upstream_freshness",
        )
    )
    text = json.dumps(value)
    assert str(tmp_path) not in text
    assert all(
        k not in text
        for k in ("ServerTime", "Active1", "Kline", "author-created-opaque")
    )


@pytest.mark.parametrize("extra,stale", [(28, False), (28.000001, True)])
def test_ttl_is_local_received_age_boundary(tmp_path, extra, stale):
    produce(tmp_path, [batch()])
    value = read_latest_watchlist(
        tmp_path, read_at=clocks(1) + timedelta(seconds=extra)
    )
    assert value["status"] == ("stale" if stale else "observed")
    assert value["local_known_age_seconds"] == extra
    assert all(m["stale"] == stale for m in value["members"])


@pytest.mark.parametrize("failure", ["timeout", "disconnected", "source_error"])
def test_latest_failure_never_falls_back_to_old_success(tmp_path, failure):
    produce(tmp_path, [batch(), batch(2, failure=failure)])
    value = read_latest_watchlist(tmp_path, read_at=clocks(2))
    assert value["batch"]["round"] == 2 and value["batch_status"] == "failure"
    assert value["status"] == "unavailable" and value["classification"] == failure
    assert all(
        m["status"] == "unavailable" and m["quote"] is None for m in value["members"]
    )
    assert all(
        m["value_acquisition"] == "sample-1" and m["local_received_age_seconds"] == 12
        for m in value["members"]
    )
    assert value["last_errors"]["sampling"]["classification"] == failure


def test_failover_has_new_segment_and_sample_baseline(tmp_path):
    recovered = batch(3, segment=1, endpoint="mock-b:7709")
    recovered["connection"]["requested"] = clocks(2).isoformat()
    recovered["connection"]["received"] = (clocks(2) + timedelta(seconds=1)).isoformat()
    produce(tmp_path, [batch(), batch(2, failure="disconnected"), recovered])
    value = read_latest_watchlist(tmp_path, read_at=clocks(3))
    assert value["status"] == "observed" and value["batch_status"] == "sample"
    assert (
        value["batch"]["segment"] == 1 and value["batch"]["endpoint"] == "mock-b:7709"
    )
    assert value["last_errors"]["sampling"]["classification"] == "disconnected"


def test_empty_and_unacked_raw_batch_are_not_acceptance(tmp_path):
    (tmp_path / "batch-1.json").write_bytes(wire(batch()))
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "no_data" and value["classification"] == "receipt_missing"
    session = RealPeriodicWatchlistSession(CONFIG, offline_fixture_mode=True)
    AcceptanceReceiptWriter(tmp_path)(session, "starting", None)
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "no_data" and value["empty"] and value["batch"] is None


def test_bad_member_or_unacked_latest_blocks_old_success(tmp_path):
    bad = batch(2)
    bad["rows"][1]["BuyLevel"][0]["Number"] = -1
    _, report = produce(tmp_path, [batch(), bad])
    assert report["stop_reason"] == "read_quality_rejected"
    assert len(receipt(tmp_path)["rounds"]) == 1
    value = read_latest_watchlist(tmp_path, read_at=clocks(2))
    assert (
        value["status"] == "unavailable"
        and value["classification"] == "contract_rejected"
    )
    assert value["batch"]["round"] == 1 and value["empty"]


def test_ack_sent_is_not_go_ack_and_bad_ack_never_publishes(tmp_path):
    def mutation(messages):
        return [
            (
                kind,
                event("ack_received", round=2)
                if kind == "E" and json.loads(raw)["classification"] == "ack_received"
                else raw,
            )
            for kind, raw in messages
        ]

    _, report = produce(tmp_path, [batch()], mutate_messages=mutation)
    assert (
        report["rounds"][0]["ack_sent"] and not report["rounds"][0]["go_ack_received"]
    )
    assert receipt(tmp_path)["rounds"] == []
    assert read_latest_watchlist(tmp_path, read_at=clocks(1))["status"] == "unavailable"


@pytest.mark.parametrize(
    "reason", ["disconnected", "duration_exhausted", "ack_rejected_or_timeout"]
)
def test_stop_after_success_is_explicit_unavailable(tmp_path, reason):
    def mutation(messages):
        return messages[:-1] + [("E", event("stopped", reason, round=1))]

    produce(tmp_path, [batch()], mutate_messages=mutation)
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "unavailable" and value["classification"] == reason
    assert value["batch"]["round"] == 1 and value["empty"]


def test_connection_failure_after_success_is_visible_immediately(tmp_path):
    def mutation(messages):
        return messages[:-1] + [
            ("E", event("stopped", "connection_budget_exhausted", round=1))
        ]

    session, _ = produce(tmp_path, [batch()], mutate_messages=mutation)
    session.events.append(
        json.loads(
            event(
                "connection_failed",
                "refused",
                endpoint="mock-b:7709",
                segment=1,
                attempt=2,
            )
        )
    )
    AcceptanceReceiptWriter(tmp_path)(session, "unavailable", "refused")
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert (
        value["status"] == "unavailable"
        and value["last_errors"]["connection"]["classification"] == "refused"
    )


@pytest.mark.parametrize(
    "change",
    [
        lambda v: v.update(revision="mdf.consumer.read.v1"),
        lambda v: v.update(input_origin="invented_source"),
        lambda v: v.update(producer_state="starting"),
        lambda v: v["config"].update(rounds=121),
        lambda v: v["config"].update(session_id="../../private"),
        lambda v: v["config"].update(securities=["sh600519", "sh600519"]),
        lambda v: v["rounds"][0].update(round=True),
        lambda v: v["rounds"][0].update(byte_count=131073),
        lambda v: v["rounds"][0].update(sha256="../private"),
        lambda v: v["rounds"][0].update(known=clocks(2).isoformat()),
        lambda v: v["rounds"][0].update(
            read_at=(clocks(1) - timedelta(seconds=1)).isoformat()
        ),
        lambda v: v["rounds"].append(deepcopy(v["rounds"][0])),
        lambda v: v.update(
            last_errors={
                "connection": {
                    "classification": "/private/raw error",
                    "round": 0,
                    "segment": 0,
                },
                "sampling": None,
            }
        ),
    ],
)
def test_invalid_receipt_is_fixed_failure_with_no_partial_output(tmp_path, change):
    produce(tmp_path, [batch()])
    alter_receipt(tmp_path, change)
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert (
        value["status"] == "unavailable"
        and value["batch"] is None
        and value["members"] == []
    )
    assert value["classification"] == "invalid_acceptance_evidence"
    assert "/private" not in json.dumps(value)


@pytest.mark.parametrize(
    "raw",
    [
        b"{",
        b"[]",
        b'{"revision":1,"revision":2}',
        b'{"x":NaN}',
        b'{"x":1e999}',
        b"\xff",
        b"[" * 10000,
        b" " * (MAX_RECEIPT_BYTES + 1),
    ],
)
def test_malformed_oversized_receipt(tmp_path, raw):
    (tmp_path / RECEIPT_NAME).write_bytes(raw)
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "unavailable" and value["batch"] is None


@pytest.mark.parametrize(
    "mutation", ["change", "truncate", "append", "missing", "collision"]
)
def test_batch_exact_sha_size_and_collision(tmp_path, mutation):
    produce(tmp_path)
    file = tmp_path / "batch-4.json"
    raw = file.read_bytes()
    if mutation == "change":
        file.write_bytes(raw.replace(b"24680", b"24681"))
    elif mutation == "truncate":
        file.write_bytes(raw[:-1])
    elif mutation == "append":
        file.write_bytes(raw + b" ")
    elif mutation == "missing":
        file.unlink()
    else:
        alter_receipt(
            tmp_path, lambda v: v["rounds"][3].update(sha256=v["rounds"][0]["sha256"])
        )
    value = read_latest_watchlist(tmp_path, read_at=clocks(4))
    assert (
        value["status"] == "unavailable"
        and value["members"] == []
        and value["batch"] is None
    )


@pytest.mark.parametrize(
    "target",
    [
        "receipt",
        "batch",
        "session",
        "ancestor",
        "traversal",
        "relative",
        "hardlink",
        "fifo",
    ],
)
def test_filesystem_boundary(tmp_path, target):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    produce(session_dir, [batch()])
    selected = session_dir
    if target in {"receipt", "batch", "hardlink", "fifo"}:
        file = session_dir / (
            RECEIPT_NAME
            if target in {"receipt", "hardlink", "fifo"}
            else "batch-1.json"
        )
        outside = tmp_path / "outside.json"
        file.rename(outside)
        if target == "hardlink":
            os.link(outside, file)
        elif target == "fifo":
            os.mkfifo(file)
        else:
            file.symlink_to(outside)
    elif target in {"session", "ancestor"}:
        link = tmp_path / "link"
        link.symlink_to(
            session_dir if target == "session" else tmp_path, target_is_directory=True
        )
        selected = link if target == "session" else link / "session"
    elif target == "traversal":
        selected = session_dir / ".." / "session"
    else:
        selected = "artifacts/session"
    value = read_latest_watchlist(selected, read_at=clocks(1))
    assert value["status"] == "unavailable" and value["batch"] is None
    assert str(tmp_path) not in json.dumps(value)


def test_no_report_fallback_and_explicit_legacy_final_read(tmp_path):
    produce(tmp_path)
    (tmp_path / RECEIPT_NAME).unlink()
    assert read_latest_watchlist(tmp_path, read_at=clocks(4))["status"] == "no_data"
    value = read_latest_watchlist(tmp_path, read_at=clocks(4), completed_report=True)
    assert (
        value["status"] == "observed"
        and value["acceptance_evidence"] == "completed_report"
    )
    report = json.loads((tmp_path / "report.json").read_bytes())
    report["rounds"][-1]["go_ack_received"] = False
    (tmp_path / "report.json").write_text(json.dumps(report))
    assert (
        read_latest_watchlist(tmp_path, read_at=clocks(4), completed_report=True)[
            "status"
        ]
        == "unavailable"
    )


def test_atomic_simultaneous_producer_receipt_reads_and_permissions(tmp_path):
    session, _ = produce(tmp_path, [batch()])
    writer = AcceptanceReceiptWriter(tmp_path)
    failures = []
    barrier = threading.Barrier(2)

    def publish():
        try:
            barrier.wait()
            for _ in range(80):
                writer(session, "accepted", None)
        except BaseException as error:
            failures.append(type(error).__name__)

    thread = threading.Thread(target=publish)
    thread.start()
    barrier.wait()
    for _ in range(80):
        assert (
            read_latest_watchlist(tmp_path, read_at=clocks(1))["status"] == "observed"
        )
    thread.join(timeout=5)
    assert not thread.is_alive() and not failures
    assert (tmp_path / RECEIPT_NAME).stat().st_mode & 0o777 == 0o600
    assert not list(tmp_path.glob(".read-ready-*"))


def test_real_framed_child_latest_visible_only_after_go_ack_and_after_exit(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(
        "RELEASE="
        + repr(DRIVER_SDK_RELEASE)
        + "\nbodies="
        + repr([batch(n) for n in range(1, 5)])
        + "\ndef template(n): return bodies[n-1]\n"
        + CHILD
    )
    config = replace(CONFIG, duration_seconds=5)
    adapter = FramedProcess([sys.executable, str(child)], config)
    session = RealPeriodicWatchlistSession(config, offline_fixture_mode=True)
    writer = AcceptanceReceiptWriter(tmp_path)
    seen = []

    def ready(session, state, classification):
        writer(session, state, classification)
        value = read_latest_watchlist(tmp_path)
        if state == "accepted":
            assert value["status"] == "observed"
            seen.append(value["batch"]["round"])
            if len(seen) < 4:
                assert adapter.process.poll() is None
        if state == "pending_ack":
            assert len(receipt(tmp_path)["rounds"]) == len(seen)
            assert value["status"] in {"unavailable", "no_data"}

    report = session.run(
        adapter,
        archive=lambda n, raw: (tmp_path / n).write_bytes(raw),
        fixture_clock=lambda: datetime.now(UTC),
        on_read_ready=ready,
    )
    assert report["stop_reason"] == "budget_exhausted" and seen == [1, 2, 3, 4]
    assert adapter.process.poll() == 0
    assert read_latest_watchlist(tmp_path)["batch"]["round"] == 4


def test_reader_import_api_and_cli_have_no_implicit_effects(tmp_path):
    produce(tmp_path, [batch()])
    script = r"""
import sys, os
sys.dont_write_bytecode = True
sys.path.insert(0, "src")
def audit(event, args):
    if event.startswith(("socket.", "subprocess.", "os.system", "os.exec", "os.spawn")):
        raise AssertionError("implicit external effect")
    if event in {"os.mkdir", "os.remove", "os.rename", "os.chmod",
                 "os.truncate", "os.link", "os.symlink"}:
        raise AssertionError("implicit write")
    if event == "open":
        flags = args[2]
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            raise AssertionError("write open")
    if event == "import" and any(s in args[0].lower() for s in
                                ("tdxpy", "pytdx", "mootdx", "akshare", "injoyai")):
        raise AssertionError("SDK import")
sys.addaudithook(audit)
from market_data_foundation.runtime.watchlist_read import read_latest_watchlist, main
value = read_latest_watchlist(sys.argv[1])
assert value["status"] == "stale", value
sys.argv = ["read", "--session-dir", sys.argv[1]]
assert main() == 1
"""
    before = {
        f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in tmp_path.iterdir()
    }
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path)], text=True, capture_output=True
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "stale"
    assert before == {
        f.name: (f.read_bytes(), f.stat().st_mtime_ns) for f in tmp_path.iterdir()
    }


def test_invalid_read_clock_is_classified(tmp_path):
    for at in (
        "now",
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=UTC).replace(tzinfo=None),
    ):
        assert (
            read_latest_watchlist(tmp_path, read_at=at)["classification"]
            == "invalid_acceptance_evidence"
        )


@pytest.mark.parametrize(
    "change",
    [
        lambda b: b.update(sdk_release="unqualified_release"),
        lambda b: b.update(batch_id="collision"),
        lambda b: b.update(input_origin="real_go_sdk_decoded_quote"),
        lambda b: b.update(clock_provenance="local_probe_wall_clock_utc"),
        lambda b: b.update(units=["CNY", "shares", "lots"]),
        lambda b: b.update(segment=1),
        lambda b: b["rows"].pop(),
        lambda b: b["rows"].append(deepcopy(b["rows"][0])),
        lambda b: b["rows"][0]["Kline"].update(Volume=-1),
        lambda b: b["connection"].update(peer="/private/raw-feed"),
        lambda b: b["connection"].update(peer="[fe80::1%/private]:7709"),
        lambda b: b["clocks"].update(frozen=clocks(2).isoformat()),
        lambda b: b["clocks"].update(received=clocks(1).isoformat()),
    ],
)
def test_reconstruction_rejects_invalid_frozen_evidence_even_with_matching_sha(
    tmp_path, change
):
    produce(tmp_path, [batch()])
    body = json.loads((tmp_path / "batch-1.json").read_bytes())
    change(body)
    raw = wire(body)
    (tmp_path / "batch-1.json").write_bytes(raw)
    alter_receipt(
        tmp_path,
        lambda v: v["rounds"][0].update(
            sha256=sha256(raw).hexdigest(), byte_count=len(raw)
        ),
    )
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "unavailable" and value["batch"] is None


@pytest.mark.parametrize(
    "field", ["endpoint", "batch_id", "sdk_release", "reads", "clocks"]
)
def test_legacy_report_identity_and_member_read_drift(tmp_path, field):
    produce(tmp_path, [batch()])
    report = json.loads((tmp_path / "report.json").read_bytes())
    report["rounds"][0][field] = (
        [] if field == "reads" else {} if field == "clocks" else "drift"
    )
    (tmp_path / "report.json").write_text(json.dumps(report))
    value = read_latest_watchlist(tmp_path, read_at=clocks(1), completed_report=True)
    assert value["status"] == "unavailable" and value["members"] == []


def test_file_drift_during_read_is_detected(tmp_path, monkeypatch):
    produce(tmp_path, [batch()])
    original = os.read
    changed = False

    def drift(fd, n):
        nonlocal changed
        raw = original(fd, n)
        if raw and not changed:
            changed = True
            (tmp_path / RECEIPT_NAME).write_bytes(b"{}")
        return raw

    monkeypatch.setattr(os, "read", drift)
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert changed and value["classification"] == "artifact_changed"


def test_future_known_and_backward_round_clocks_are_rejected(tmp_path):
    produce(tmp_path)
    assert read_latest_watchlist(tmp_path, read_at=clocks(3))["status"] == "unavailable"
    alter_receipt(
        tmp_path, lambda v: v["rounds"][2].update(known=clocks(1).isoformat())
    )
    assert read_latest_watchlist(tmp_path, read_at=clocks(4))["status"] == "unavailable"


def test_producer_fsync_before_replace_and_directory_after(tmp_path, monkeypatch):
    session, _ = produce(tmp_path, [batch()])
    order = []
    original_sync, original_replace = os.fsync, os.replace

    def sync(fd):
        import stat

        order.append(
            "directory_sync" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file_sync"
        )
        return original_sync(fd)

    def replace_file(src, dst):
        order.append("replace")
        return original_replace(src, dst)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", replace_file)
    AcceptanceReceiptWriter(tmp_path)(session, "accepted", None)
    assert order == ["file_sync", "replace", "directory_sync"]


@pytest.mark.parametrize("count", [1, 32])
def test_reader_membership_minimum_and_maximum(tmp_path, count):
    members = tuple(f"sz{i:06d}" for i in range(1, count + 1))
    body = batch()
    template = body["rows"][1]
    body["rows"] = [{**deepcopy(template), "Code": m[2:]} for m in members]
    body["securities"] = list(members)
    produce(tmp_path, [body], config=replace(CONFIG, securities=members))
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert value["status"] == "observed"
    assert [m["security"] for m in value["members"]] == list(members)


def test_maximum_rounds_fit_bounded_receipt(tmp_path):
    produce(tmp_path, [batch(n) for n in range(1, 121)])
    assert (tmp_path / RECEIPT_NAME).stat().st_size < MAX_RECEIPT_BYTES
    value = read_latest_watchlist(tmp_path, read_at=clocks(120))
    assert value["status"] == "observed" and value["batch"]["round"] == 120


def test_close_failure_cannot_leave_healthy_receipt(tmp_path, monkeypatch):
    def fail_close(self):
        raise RuntimeError("private cleanup detail")

    monkeypatch.setattr(Adapter, "close", fail_close)
    _, report = produce(tmp_path, [batch()])
    assert report["stop_reason"] == "runtime_error"
    value = read_latest_watchlist(tmp_path, read_at=clocks(1))
    assert (
        value["status"] == "unavailable" and value["classification"] == "runtime_error"
    )
    assert "private cleanup detail" not in json.dumps(value)


def assert_invalid_evidence(value, path):
    assert value["status"] == "unavailable"
    assert value["classification"] == "invalid_acceptance_evidence"
    assert value["batch"] is None and value["members"] == [] and value["empty"]
    assert str(path) not in json.dumps(value)


@pytest.mark.parametrize("state", ["accepted", "ended"])
@pytest.mark.parametrize("kind", ["sampling", "connection"])
def test_healthy_receipt_cannot_hide_newer_unaccepted_failure(tmp_path, state, kind):
    produce(tmp_path, [batch()] if state == "ended" else [batch(), batch(2)])

    def change(v):
        v["rounds"] = v["rounds"][:1]
        v.update(producer_state=state, classification=None)
        if state == "ended":
            v["classification"] = "budget_exhausted"
        v["last_errors"][kind] = {
            "classification": "timeout",
            "round": 2 if kind == "sampling" else 0,
            "segment": 0 if kind == "sampling" else 1,
        }

    alter_receipt(tmp_path, change)
    assert_invalid_evidence(
        read_latest_watchlist(tmp_path, read_at=clocks(2)), tmp_path
    )


@pytest.mark.parametrize("state", ["pending_ack", "unavailable"])
@pytest.mark.parametrize("kind", ["sampling", "connection"])
def test_current_failure_states_keep_old_identity_without_quote(tmp_path, state, kind):
    produce(tmp_path)

    def change(v):
        v["rounds"] = v["rounds"][:1]
        v.update(
            producer_state=state,
            classification="timeout" if state == "unavailable" else None,
        )
        v["last_errors"][kind] = {
            "classification": "timeout",
            "round": 2 if kind == "sampling" else 0,
            "segment": 0 if kind == "sampling" else 1,
        }

    alter_receipt(tmp_path, change)
    value = read_latest_watchlist(tmp_path, read_at=clocks(4))
    assert value["status"] == "unavailable" and value["batch"]["round"] == 1
    assert value["last_errors"][kind]["classification"] == "timeout"
    assert all(m["quote"] is None for m in value["members"])


@pytest.mark.parametrize("state", ["accepted", "ended"])
def test_recovered_receipt_preserves_both_historical_errors(tmp_path, state):
    # Initial dial failure and round 1 sampling failure; round 2 recovers.
    failed = batch(failure="timeout")
    failed["connection"]["attempts"] = 2
    recovered = batch(2, segment=1, endpoint="mock-b:7709")
    recovered["connection"]["attempts"] = 3

    def retry(messages):
        messages = (
            messages[:3]
            + [
                ("E", event("connection_failed", "refused")),
                ("E", event("connection_attempt", attempt=2)),
            ]
            + messages[3:]
        )
        return [
            (kind, wire({**json.loads(raw), "attempt": 3}))
            if kind == "E"
            and json.loads(raw)["classification"] == "connection_attempt"
            and json.loads(raw)["segment"] == 1
            else (kind, raw)
            for kind, raw in messages
        ]

    _, report = produce(tmp_path, [failed, recovered], mutate_messages=retry)
    assert report["session_complete"]

    def change(v):
        v.update(
            producer_state=state,
            classification=None if state == "accepted" else "budget_exhausted",
        )

    alter_receipt(tmp_path, change)
    value = read_latest_watchlist(tmp_path, read_at=clocks(2))
    assert value["status"] == "observed" and value["batch_status"] == "sample"
    assert value["batch"]["round"] == 2 and value["batch"]["segment"] == 1
    assert value["last_errors"] == receipt(tmp_path)["last_errors"]
    assert value["last_errors"]["sampling"]["round"] == 1
    assert value["last_errors"]["connection"]["classification"] == "refused"


@pytest.mark.parametrize(
    "kind,error",
    [
        ("sampling", {"classification": "timeout", "round": 0, "segment": 0}),
        ("sampling", {"classification": "timeout", "round": 1, "segment": 0}),
        ("sampling", {"classification": "timeout", "round": 3, "segment": 0}),
        ("sampling", {"classification": "timeout", "round": 2, "segment": 1}),
        ("sampling", {"classification": "budget_exhausted", "round": 2, "segment": 0}),
        ("connection", {"classification": "refused", "round": 1, "segment": 0}),
        ("connection", {"classification": "refused", "round": 0, "segment": 2}),
        ("sampling", {"classification": "timeout", "round": 5, "segment": 0}),
        ("sampling", {"classification": "timeout", "round": True, "segment": 0}),
        ("connection", {"classification": "refused", "round": 0, "segment": 4}),
        ("connection", {"classification": "refused", "round": 0, "segment": False}),
    ],
)
def test_error_coordinates_and_semantics_are_scoped_to_progress(tmp_path, kind, error):
    produce(tmp_path, config=replace(CONFIG, max_failovers=3))

    def change(v):
        v["rounds"] = v["rounds"][:1]
        v.update(producer_state="unavailable", classification="timeout")
        v["last_errors"][kind] = error

    alter_receipt(tmp_path, change)
    assert_invalid_evidence(
        read_latest_watchlist(tmp_path, read_at=clocks(4)), tmp_path
    )


@pytest.mark.parametrize("change", ["round", "segment", "classification", "missing"])
def test_sampling_ledger_must_match_actual_last_accepted_failure(tmp_path, change):
    recovered = batch(3, segment=1, endpoint="mock-b:7709")
    recovered["connection"]["requested"] = clocks(2).isoformat()
    recovered["connection"]["received"] = (clocks(2) + timedelta(seconds=1)).isoformat()
    produce(tmp_path, [batch(), batch(2, failure="timeout"), recovered])

    def alter(v):
        if change == "missing":
            v["last_errors"]["sampling"] = None
        else:
            v["last_errors"]["sampling"][change] = {
                "round": 1,
                "segment": 1,
                "classification": "disconnected",
            }[change]

    alter_receipt(tmp_path, alter)
    assert_invalid_evidence(
        read_latest_watchlist(tmp_path, read_at=clocks(3)), tmp_path
    )


@pytest.mark.parametrize(
    "field,changed",
    [
        ("connection_peer", "203.0.113.99:7709"),
        ("quote_elapsed_ns", 1),
        ("quote_elapsed_ns", True),
        ("known_provenance", "/private/changed-provenance"),
    ],
)
@pytest.mark.parametrize("failure", [None, "timeout"])
def test_legacy_report_source_fields_match_frozen_shared_observation(
    tmp_path, field, changed, failure
):
    produce(tmp_path, [batch(failure=failure)])
    raw_before = (tmp_path / "batch-1.json").read_bytes()
    report_path = tmp_path / "report.json"
    report = json.loads(report_path.read_bytes())
    report["rounds"][0][field] = changed
    report_path.write_text(json.dumps(report))
    assert_invalid_evidence(
        read_latest_watchlist(tmp_path, read_at=clocks(1), completed_report=True),
        tmp_path,
    )
    assert (tmp_path / "batch-1.json").read_bytes() == raw_before


@pytest.mark.parametrize(
    "change",
    [
        lambda r: r.update(child_exit=1),
        lambda r: r.update(child_exit=None),
        lambda r: r.update(child_exit=False),
        lambda r: r.update(health="unavailable"),
        lambda r: r.update(session_complete=False),
        lambda r: r["events"][-1].update(
            classification="stopped", reason="disconnected"
        ),
        lambda r: r.update(events=[]),
    ],
)
def test_legacy_success_terminal_evidence_must_agree(tmp_path, change):
    produce(tmp_path, [batch()])
    path = tmp_path / "report.json"
    report = json.loads(path.read_bytes())
    change(report)
    path.write_text(json.dumps(report))
    assert_invalid_evidence(
        read_latest_watchlist(tmp_path, read_at=clocks(1), completed_report=True),
        tmp_path,
    )


@pytest.mark.parametrize("failure", [None, "timeout"])
def test_legacy_unchanged_complete_success_and_failure_batches(tmp_path, failure):
    produce(tmp_path, [batch(failure=failure)])
    value = read_latest_watchlist(tmp_path, read_at=clocks(1), completed_report=True)
    assert value["batch"]["round"] == 1
    assert value["status"] == ("unavailable" if failure else "observed")
    assert value["batch_status"] == ("failure" if failure else "sample")


def test_legacy_empty_success_report_is_typed_unavailable(tmp_path):
    produce(tmp_path, [batch()])
    path = tmp_path / "report.json"
    report = json.loads(path.read_bytes())
    report["rounds"] = []
    report["config"]["rounds"] = 0
    report["events"][-1]["round"] = 0
    path.write_text(json.dumps(report))
    assert_invalid_evidence(
        read_latest_watchlist(tmp_path, read_at=clocks(1), completed_report=True),
        tmp_path,
    )


@pytest.mark.parametrize(
    "reason", ["disconnected", "duration_exhausted", "ipc_timeout"]
)
def test_legacy_unsuccessful_historical_health_never_exposes_old_quote(
    tmp_path, reason
):
    def mutation(messages):
        return messages[:-1] + [("E", event("stopped", "disconnected", round=1))]

    produce(tmp_path, [batch()], mutate_messages=mutation)
    path = tmp_path / "report.json"
    report = json.loads(path.read_bytes())
    # Old reports derive health only from the last consumed batch, even on stop.
    assert report["health"] == "readable_snapshot" and not report["session_complete"]
    report["stop_reason"] = reason
    path.write_text(json.dumps(report))
    value = read_latest_watchlist(tmp_path, read_at=clocks(1), completed_report=True)
    assert value["status"] == "unavailable" and value["batch"]["round"] == 1
    assert value["classification"] == reason
    assert all(m["quote"] is None for m in value["members"])
