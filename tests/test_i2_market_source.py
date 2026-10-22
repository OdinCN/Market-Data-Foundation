"""Invented protocol/market evidence only. No network and no source adoption."""

import base64
import json
import struct
from dataclasses import asdict, replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from r5_i6_market_fixture import job

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.domain_contracts.market_planning import (
    PagePlan,
    plan_job,
)
from market_data_foundation.historical.market_daily_source import PageRequest
from market_data_foundation.qualification import tdx_market_collect as runner
from market_data_foundation.qualification.tdx_market_evidence import (
    admit_line,
    daily_page,
    inspect_daily,
    qualification_observation,
    quote_changes,
    request_wire,
    source_binding,
)
from market_data_foundation.qualification.tdx_market_plan import (
    UPSTREAM,
    Plan,
    Request,
    after_close_gate,
    calendar_gate,
    encoded,
)


def plan(**changes):
    if changes.get("mode") == "session":
        changes.setdefault("observation_day", "2026-10-23")
        changes.setdefault("session_window", "AM")
    return replace(
        Plan(
            "a" * 32,
            "124.71.187.122:7709",
            "b" * 64,
            "c" * 64,
            (
                Request("GetCount", 1),
                Request("GetCode", 1, count=1000),
                Request("GetKline", 1, "600001", count=1),
            ),
            ("sh600001", "sz000001"),
            observation_day="2026-10-22",
        ),
        **changes,
    )


def calendar():
    return encoded(
        {
            "schema": "mdf.i2.calendar-host-intake.v1",
            "day": "2026-10-23",
            "reviewed_at": "2026-10-22T09:00:00+00:00",
            "expires_at": "2026-10-23T08:00:00+00:00",
            "markets": [
                {
                    "market": m,
                    "state": "OPEN",
                    "authority": "invented fixture",
                    "material_sha256": "d" * 64,
                    "sessions": [["09:30", "11:30"], ["13:00", "15:00"]],
                }
                for m in ("SH", "SZ")
            ],
        }
    )


def source_line(p, index, projection):
    request = p.requests[index]
    code = {"GetCount": 0x44E, "GetCode": 0x450, "GetKline": 0x52D}[request.operation]
    sent = request_wire(request, index)
    raw = (
        b"\xb1\xcb\x74\x00\x1c"
        + struct.pack("<I", index + 2)
        + b"\x00"
        + struct.pack("<HHH", code, 2, 2)
        + b"\x00\x00"
    )
    return encoded(
        {
            "index": index,
            "upstream_revision": UPSTREAM,
            "request": asdict(request),
            "request_wire": base64.b64encode(sent).decode(),
            "response_wire": base64.b64encode(raw).decode(),
            "requested_at": "2026-10-22T08:00:00+00:00",
            "received_at": "2026-10-22T08:00:01+00:00",
            "source_projection": projection,
            "classification": "retained_raw",
        }
    )


def daily():
    return {
        "Count": 1,
        "List": [
            {
                "Time": "2024-01-30T15:00:00+08:00",
                "Last": 1000,
                "Open": 1001,
                "Close": 1002,
                "High": 1003,
                "Low": 1000,
                "Volume": 2,
                "Amount": 10000,
                "Order": 0,
                "UpCount": 0,
                "DownCount": 0,
            }
        ],
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"wall_seconds": True},
        {"max_request_bytes": 20},
        {"max_response_bytes": 4194305},
        {"quote_rounds": 9},
        {"quote_wall_seconds": 9},
        {"mode": "unknown"},
        {"securities": ("hk00700",)},
        {"collector_sha256": ""},
        {"requests": (Request("GetCount", 1),) * 65},
        {"source_enabled": 1},
        {"mode": "session", "quote_rounds": 3},
    ],
)
def test_reject_unsafe_plan(changes):
    with pytest.raises(ContractError):
        plan(**changes)


def test_plan_frozen_roundtrip_and_expansion():
    p = plan(securities=("sh600001", "sh601001", "sz000001", "sz300001"))
    assert Plan.load(encoded(asdict(p))) == p
    assert p.quote_config().sdk_candidates is False
    assert p.quote_config().max_failovers == 0
    assert p.quote_config().rounds == 1
    assert p.revision != plan().revision
    with pytest.raises(ContractError, match="malformed_ipc_json"):
        Plan.load(b'{"run_id":"a","run_id":"b"}')


@pytest.mark.parametrize(
    "operation,market,code,count",
    [
        ("GetCodeAll", 1, "", 0),
        ("GetCode", 2, "", 1000),
        ("GetKline", 1, "000001", 32),
        ("GetKline", 1, "600001", 801),
    ],
)
def test_no_unknown_method_or_market(operation, market, code, count):
    with pytest.raises(ContractError):
        Request(operation, market, code, count=count)


@pytest.mark.parametrize(
    "clock", ["2026-10-23T01:35:00+00:00", "2026-10-23T05:30:00+00:00"]
)
def test_market_window_two_separate_proofs(clock):
    raw = calendar()
    p = plan(
        mode="session",
        quote_rounds=3,
        source_enabled=False,
        session_window="PM" if "T05:" in clock else "AM",
    )
    assert (
        calendar_gate(raw, sha256(raw).hexdigest(), p, datetime.fromisoformat(clock))[
            "state"
        ]
        == "HOST_INTAKE_OPEN"
    )


@pytest.mark.parametrize(
    "clock,window,reason",
    [
        ("2026-10-23T01:34:59+00:00", "AM", "outside_observation_window"),
        ("2026-10-23T03:29:50+00:00", "AM", "session_budget_crosses_close"),
        ("2026-10-23T04:00:00+00:00", "AM", "outside_observation_window"),
        ("2026-10-23T06:59:50+00:00", "PM", "session_budget_crosses_close"),
        ("2026-10-24T01:35:00+00:00", "AM", "calendar_day_mismatch"),
    ],
)
def test_calendar_late_lunch_wrong_day_stops(clock, window, reason):
    raw = calendar()
    p = plan(
        mode="session", source_enabled=False, quote_rounds=3, session_window=window
    )
    with pytest.raises(ContractError, match=reason):
        calendar_gate(raw, sha256(raw).hexdigest(), p, datetime.fromisoformat(clock))


def test_calendar_unknown_closed_tampered_and_partial_stop():
    p = plan(mode="session", source_enabled=False, quote_rounds=3)
    for mutate, reason in (
        (
            lambda gate: gate["markets"][0].update(state="UNKNOWN"),
            "market_open_evidence_required",
        ),
        (
            lambda gate: gate["markets"][0].update(state="CLOSED"),
            "market_open_evidence_required",
        ),
        (lambda gate: gate["markets"].pop(), "separate_market_proofs"),
    ):
        gate = json.loads(calendar())
        mutate(gate)
        raw = encoded(gate)
        with pytest.raises(ContractError, match=reason):
            calendar_gate(
                raw,
                sha256(raw).hexdigest(),
                p,
                datetime(2026, 10, 23, 1, 35, tzinfo=UTC),
            )
    with pytest.raises(ContractError, match="calendar_digest_mismatch"):
        calendar_gate(calendar(), "e" * 64, p, datetime.now(UTC))
    with pytest.raises(ContractError, match="after_close_local_window_required"):
        after_close_gate(datetime(2026, 10, 22, 6, tzinfo=UTC), "2026-10-22", 156)


def test_retained_native_directory_and_capability_contract():
    p = plan()
    row, _, _ = admit_line(source_line(p, 0, {"Count": 2000}), p, 0)
    observation = qualification_observation(row, p.requests[0], p, 0)
    assert observation["mode"] == "offline"
    assert observation["capability"] == "security.universe"
    assert observation["coverage"]["complete"] is None
    row, _, _ = admit_line(source_line(p, 1, {"Count": 0, "List": None}), p, 1)
    assert row["directory_assessment"]["historical_membership"] == "UNKNOWN"


def test_wire_identity_and_request_drift_stops():
    p = plan()
    row = json.loads(source_line(p, 0, {"Count": 0}))
    row["request"]["market"] = 0
    with pytest.raises(ContractError, match="request_shape_drift"):
        admit_line(encoded(row), p, 0)
    row = json.loads(source_line(p, 0, {"Count": 0}))
    row["response_wire"] = base64.b64encode(b"x" * 18).decode()
    with pytest.raises(ContractError):
        admit_line(encoded(row), p, 0)


def test_daily_bounds_and_unknown_semantics():
    p = plan()
    value = daily()
    assert inspect_daily(value, p.requests[2])["terminal"] == "UNKNOWN"
    value["List"][0]["High"] = 0
    assert inspect_daily(value, p.requests[2])["issues"] == ["ohlc_order"]
    with pytest.raises(ContractError):
        inspect_daily({"Count": 2, "List": daily()["List"] * 2}, p.requests[2])


def test_d2_exact_retained_request_day_evidence_and_unknowns():
    p = plan()
    spec = job()
    original = plan_job(spec).shards[0]
    source, binding = source_binding(p.requests[2])
    mapping = replace(
        original.mapping,
        source=source,
        binding=binding,
        request_namespace="tdx.standard.SH",
        request_code="600001",
    )
    route = replace(
        original.route,
        source=source,
        binding=binding,
        implementation_identity=p.collector_sha256,
        endpoint_contract=p.endpoint,
        pages=PagePlan(0, 1, 1, "ascending", "anchor", "UNKNOWN"),
    )
    shard = replace(original, mapping=mapping, route=route)
    request = PageRequest(shard, 0, "mdf.market.daily.page.d2.v1", spec.budget)
    line = source_line(p, 2, daily())
    kwargs = dict(
        plan=p,
        source_index=2,
        line=line,
        retained_reference="ignored-artifact",
        expected_line_sha256=sha256(line).hexdigest(),
        known_at=datetime(2026, 10, 22, 9, tzinfo=UTC),
    )
    page = daily_page(
        request, day_assertions=((0, "2024-01-30", "invented-day-proof"),), **kwargs
    )
    assert page.material_kind == "derived"
    assert page.terminal is None and page.source_time is None
    assert all(field.unit == "UNKNOWN" for field in page.rows[0].fields)
    with pytest.raises(ContractError, match="explicit_row_day_proofs_required"):
        daily_page(request, day_assertions=(), **kwargs)
    with pytest.raises(ContractError, match="retained_digest_mismatch"):
        daily_page(request, day_assertions=(), **{**kwargs, "line": line + b" "})


def quote(p, n, close=10, timestamp="invented-time"):
    return encoded(
        {
            "sdk_release": p.quote_config().driver_sdk_release,
            "endpoint": p.endpoint,
            "session_id": p.run_id,
            "securities": p.securities,
            "round": n,
            "failure": None,
            "rows": [
                {
                    "Exchange": market,
                    "Code": code,
                    "Kline": {
                        "Open": 10,
                        "High": 20,
                        "Low": 1,
                        "Close": close,
                        "Volume": 2,
                        "Amount": 20,
                        "Time": timestamp,
                    },
                    "BuyLevel": [],
                    "SellLevel": [],
                }
                for market, code in ((1, "600001"), (0, "000001"))
            ],
            "clocks": {"received": "2026-10-23T01:35:00+00:00"},
        }
    )


def test_refresh_excludes_sdk_clock_and_requires_multiple_transitions():
    p = plan()
    clocks_only = tuple(quote(p, n, timestamp=str(n)) for n in (1, 2, 3))
    assert quote_changes(clocks_only, p)["content_refresh"] == "NOT_PROVEN"
    changed = quote_changes(tuple(quote(p, n, close=10 + n) for n in (1, 2, 3)), p)
    assert changed["content_refresh"] == "OBSERVED_MULTIPLE_TRANSITIONS"
    assert changed["source_event_freshness"] == "UNKNOWN"
    assert changed["stream_live_qualification"] == "UNKNOWN"


def test_preflight_zero_effects_and_classified_failure(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    calls = []
    monkeypatch.setattr(runner, "verify_endpoint", lambda *a: calls.append("verified"))
    kwargs = dict(
        sdk=Path("unused"),
        candidate=Path("unused"),
        driver=Path("unused"),
        output=tmp_path / "artifacts" / "run",
    )
    with pytest.raises(ContractError, match="explicit_live_opt_in_required"):
        runner.collect(plan(), **kwargs)
    assert not calls and not kwargs["output"].exists()
    with pytest.raises(
        ContractError, match="independent_host_candidate_digest_required"
    ):
        runner.collect(plan(), allow_live=True, **kwargs)
    assert not calls
    monkeypatch.setattr(
        runner,
        "after_close_gate",
        lambda *args: {"deadline": "2099-01-01T00:00:00+00:00"},
    )
    monkeypatch.setattr(
        runner,
        "source_once",
        lambda *a, **kw: (_ for _ in ()).throw(PermissionError("private value")),
    )
    ledger = runner.collect(
        plan(), allow_live=True, reviewed_collector_sha256="c" * 64, **kwargs
    )
    assert ledger["source"].startswith("ATTEMPT_STARTED")
    assert ledger["exception_classification"] == "PermissionError"
    assert "private value" not in (kwargs["output"] / "effect-ledger.json").read_text()
    assert ledger["pg_writes"] == ledger["garage_writes"] == 0
    # A different output cannot reuse an exhausted plan/run identity.
    with pytest.raises(FileExistsError):
        runner.collect(
            plan(),
            allow_live=True,
            reviewed_collector_sha256="c" * 64,
            **{**kwargs, "output": tmp_path / "artifacts" / "second"},
        )


def test_candidate_digest_checked_before_exec(tmp_path):
    candidate = tmp_path / "candidate"
    candidate.write_bytes(b"never execute")
    candidate.chmod(0o500)
    with pytest.raises(ContractError, match="candidate_not_host_reviewed"):
        with runner.sealed_candidate(candidate, "f" * 64):
            pytest.fail("untrusted execution")


def test_directory_count_brackets_closure_and_partial():
    from market_data_foundation.qualification.tdx_market_evidence import (
        directory_inputs,
    )

    p = plan(
        requests=(
            Request("GetCount", 1),
            Request("GetCode", 1, count=1000),
            Request("GetCount", 1),
        )
    )
    rows = [
        source_line(p, 0, {"Count": 0}),
        source_line(p, 1, {"Count": 0, "List": []}),
        source_line(p, 2, {"Count": 0}),
    ]
    inputs, revision = directory_inputs(
        (b"handshake", *rows),
        p,
        market=1,
        known_at=datetime(2026, 10, 22, 9, tzinfo=UTC),
    )
    assert revision["closure"] == "PAGE_TERMINATED_COUNT_MATCH"
    assert revision["qualification"] == "UNKNOWN" and len(inputs) == 1
    _, partial = directory_inputs(
        (b"handshake", rows[0]),
        p,
        market=1,
        known_at=datetime(2026, 10, 22, 9, tzinfo=UTC),
    )
    assert partial["closure"] == "PARTIAL_NO_BRACKET"
    rows[-1] = source_line(p, 2, {"Count": 1})
    _, drift = directory_inputs(
        (b"handshake", *rows),
        p,
        market=1,
        known_at=datetime(2026, 10, 22, 9, tzinfo=UTC),
    )
    assert "directory_count_drift" in drift["issues"] and drift["closure"] == "UNKNOWN"


def test_failed_child_partial_material_retained(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "verify_endpoint", lambda *a: None)
    monkeypatch.setattr(
        runner, "after_close_gate", lambda *a: {"deadline": "2099-01-01T00:00:00+00:00"}
    )
    monkeypatch.setattr(
        runner,
        "source_once",
        lambda *a, **kw: (_ for _ in ()).throw(
            runner.SourceStopped(b"partial", "source_wall_budget")
        ),
    )
    out = tmp_path / "artifacts" / "partial"
    ledger = runner.collect(
        plan(),
        sdk=Path("unused"),
        candidate=Path("unused"),
        driver=Path("unused"),
        output=out,
        allow_live=True,
        reviewed_collector_sha256="c" * 64,
    )
    assert ledger["terminal"] == "STOPPED"
    assert (out / "source-stdout-partial.jsonl").read_bytes() == b"partial"


def test_empty_quote_cannot_succeed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "verify_endpoint", lambda *a: None)
    monkeypatch.setattr(
        runner, "after_close_gate", lambda *a: {"deadline": "2099-01-01T00:00:00+00:00"}
    )
    monkeypatch.setattr(runner, "DeadlineFramedProcess", lambda *a, **kw: object())

    class Session:
        def __init__(self, *a):
            pass

        def run(self, *a, **k):
            return {
                "stop_reason": "connection_budget_exhausted",
                "health": "unavailable",
            }

    monkeypatch.setattr(runner, "RealPeriodicWatchlistSession", Session)
    ledger = runner.collect(
        plan(source_enabled=False),
        sdk=Path("unused"),
        candidate=Path("unused"),
        driver=Path("unused"),
        output=tmp_path / "artifacts" / "quote-empty",
        allow_live=True,
    )
    assert ledger["terminal"] == "STOPPED_CLASSIFIED"


def test_retained_manifest_tamper_before_any_replay(tmp_path):
    from market_data_foundation.qualification.tdx_market_retained import intake_quotes

    (tmp_path / "config.json").write_bytes(b"{}")
    with pytest.raises(ContractError, match="retained_manifest_mismatch"):
        intake_quotes(
            tmp_path,
            tmp_path / "artifacts" / "unused",
            expected_files={
                "config.json": "f" * 64,
                "report.json": "e" * 64,
                "read-ready.json": "d" * 64,
            },
        )


def test_golden_wire_matches_pinned_sdk_frames():
    assert request_wire(None, -1).hex() == "0c0100000001030003000d0001"
    assert (
        request_wire(Request("GetCount", 0), 0).hex()
        == "0c0200000001080008004e04000075c73301"
    )
    assert (
        request_wire(Request("GetCode", 1, count=1000), 1).hex()
        == "0c030000000106000600500401000000"
    )


def test_bounded_local_input(tmp_path):
    from market_data_foundation.qualification.tdx_market_plan import bounded_read

    path = tmp_path / "large-input"
    path.write_bytes(b"x" * 101)
    with pytest.raises(ContractError, match="input_file_budget"):
        bounded_read(path, 100)


def test_named_am_plan_cannot_run_in_pm_or_another_day():
    raw = calendar()
    p = plan(mode="session", quote_rounds=3, source_enabled=False)
    with pytest.raises(ContractError, match="outside_observation_window"):
        calendar_gate(
            raw, sha256(raw).hexdigest(), p, datetime(2026, 10, 23, 5, 30, tzinfo=UTC)
        )
    p = replace(p, observation_day="2026-10-24")
    with pytest.raises(ContractError, match="calendar_day_mismatch"):
        calendar_gate(
            raw, sha256(raw).hexdigest(), p, datetime(2026, 10, 23, 1, 35, tzinfo=UTC)
        )
    with pytest.raises(ContractError, match="after_close_day_mismatch"):
        after_close_gate(datetime(2026, 10, 23, 10, tzinfo=UTC), "2026-10-22", 156)
