"""Author-created offline fixtures only; no retained quotes or live execution."""

import importlib
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256

import pytest

from market_data_foundation.candidates import (
    go_stock_real_observation_ingress as module,
)
from market_data_foundation.canonical.model import ContractError

BASE = datetime(2026, 1, 1, tzinfo=UTC)
MEMBERS = ("sh600519", "sz000001")


def fixture(n=1, *, price=24680, requested=None):
    """Invented prices, protocol fields, endpoint declaration and every clock."""
    offset = n * 10
    rows = [
        {
            "Exchange": exchange,
            "Code": code,
            "Kline": {
                **dict.fromkeys(("Last", "Open", "High", "Low", "Close"), price),
                "Amount": 456789,
                "Volume": 31,
                "Time": (BASE - timedelta(days=1)).isoformat(),
            },
            "BuyLevel": [
                {"Buy": True, "Price": price - i, "Number": i} for i in range(1, 6)
            ],
            "SellLevel": [
                {"Buy": False, "Price": price + i, "Number": i} for i in range(1, 6)
            ],
            "ServerTime": "author-created-opaque",
            "Active1": 2,
            "Active2": 3,
        }
        for exchange, code in ((1, "600519"), (0, "000001"))
    ]
    return {
        "schema": "mdf.candidate.go-stock.real-observation.r1",
        "input_origin": "author_created_offline_fixture",
        "clock_provenance": "author_created_offline_fixture",
        "sdk_release": "injoyai/tdx v0.0.87@51c1ed51020fa43727300339dd92cbecf06a7d2e",
        "endpoint": "139.9.52.158:7709",
        "securities": list(MEMBERS),
        "batch_id": f"go-real-r1-{n}",
        "round": n,
        "max_rounds": 3,
        "connection": {
            "requested": BASE.isoformat(),
            "received": (BASE + timedelta(seconds=1)).isoformat(),
            "elapsed_ns": 1_000_000_000,
            "redial": False,
            "attempts": 1,
        },
        "clocks": {
            "requested": (
                BASE + timedelta(seconds=offset if requested is None else requested)
            ).isoformat(),
            "received": (BASE + timedelta(seconds=offset + 1)).isoformat(),
            "frozen": (BASE + timedelta(seconds=offset + 2)).isoformat(),
        },
        "quote_elapsed_ns": 1_000_000_000,
        "source_event_clock": "UNKNOWN",
        "kline_time_semantics": "SDK_CLIENT_SAMPLED",
        "server_time_semantics": "OPAQUE",
        "units": ["integer_li", "sdk_volume_count_UNKNOWN", "sdk_level_count_UNKNOWN"],
        "failure": None,
        "rows": rows,
    }


def ingress(**kwargs):
    return module.GoStockRealObservationIngress(offline_fixture_mode=True, **kwargs)


def wire(body):
    return json.dumps(body, allow_nan=False).encode()


def ingest(target, body, *, known=None):
    return target.ingest(
        wire(body), known_at=known or (BASE + timedelta(seconds=body["round"] * 10 + 3))
    )


def test_frozen_typed_atomic_pair_clock_provenance_and_detached_input():
    target = ingress()
    body = fixture()
    body["rows"].reverse()
    data = wire(body)
    body["rows"][0]["Kline"]["Close"] = 1
    obs = target.ingest(data, known_at=BASE + timedelta(seconds=13))
    assert (
        obs.input_origin
        == obs.clock_provenance
        == obs.known_provenance
        == ("author_created_offline_fixture")
    )
    assert obs.envelope_sha256 == sha256(data).hexdigest()
    assert tuple(q.security for q in obs.record.quotes) == MEMBERS
    assert obs.record.parser_revision == "go-real-decoded-shape-r1"
    quote = obs.record.quotes[0]
    assert quote.close_cny == Decimal("24.680")
    assert quote.amount_cny == Decimal("456.789")
    assert quote.volume_sdk == 31 and quote.bids[0] == (24679, 1)
    assert quote.client_sampled_at == BASE - timedelta(days=1)
    assert quote.server_time_raw == "author-created-opaque"
    assert obs.connection_elapsed_ns == obs.quote_elapsed_ns == 1_000_000_000
    assert obs.connection_received == BASE + timedelta(seconds=1)
    assert obs.record.clocks.known == BASE + timedelta(seconds=13)
    assert target.exact(obs.batch_id) is target.as_of(obs.record.clocks.known) is obs
    view = target.read(MEMBERS[0], at=obs.record.clocks.known)
    assert view.status == "observed" and view.age == timedelta(seconds=2)
    assert view.source_event_at is None and view.source_freshness == "UNKNOWN"
    assert obs.source_event_clock == "UNKNOWN"
    assert view.rights == "UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT"


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("schema", "other", "batch_schema_mismatch"),
        ("input_origin", "author_created_synthetic", "input_origin_mismatch"),
        ("input_origin", "real_go_sdk_decoded_quote", "input_origin_mismatch"),
        ("clock_provenance", "local_probe_wall_clock_utc", "clock_provenance_mismatch"),
        ("sdk_release", "injoyai/tdx v0.0.88", "sdk_release_mismatch"),
        ("endpoint", "another-host:7709", "endpoint_mismatch"),
        ("securities", ["sh600519", "sz000002"], "security_request_mismatch"),
        ("units", ["yuan", "shares", "lots"], "unit_declaration_mismatch"),
        ("source_event_clock", "Kline.Time", "source_clock_claim_forbidden"),
        ("kline_time_semantics", "EXCHANGE_TIME", "source_clock_claim_forbidden"),
        ("server_time_semantics", "UTC", "source_clock_claim_forbidden"),
        ("round", True, "round_budget_invalid"),
        ("round", 0, "round_budget_invalid"),
        ("max_rounds", 4, "round_budget_invalid"),
        ("max_rounds", True, "round_budget_invalid"),
        ("batch_id", "synthetic-2", "batch_identity_invalid"),
        ("batch_id", "go-real-r1-1", "batch_identity_invalid"),
        ("batch_id", [], "batch_identity_invalid"),
        ("connection", {}, "connection_policy_invalid"),
        ("quote_elapsed_ns", True, "latency_bound"),
        ("quote_elapsed_ns", -1, "latency_bound"),
        ("quote_elapsed_ns", 10_000_000_001, "latency_bound"),
        ("clocks", None, "clock_shape_invalid"),
        ("failure", "private raw error", "failure_classification"),
        ("failure", {}, "failure_classification"),
        ("failure", "timeout", "failure_rows_conflict"),
    ],
)
def test_envelope_rejection_has_no_history_state_age_or_ordinal_effect(
    field,
    value,
    classification,
):
    target = ingress(ttl=timedelta(seconds=5))
    first = ingest(target, fixture())
    body = fixture(2)
    body[field] = value
    at = BASE + timedelta(seconds=23)
    before = tuple(target.read(s, at=at) for s in MEMBERS)
    with pytest.raises(ContractError, match=f"^{classification}$"):
        ingest(target, body)
    assert target.history == (first,)
    assert target._candidate.history == (first.record,)
    assert tuple(target.read(s, at=at) for s in MEMBERS) == before
    second = ingest(target, fixture(2))
    assert second.record.acquisition == "sample-2"


@pytest.mark.parametrize("key", list(fixture()))
def test_missing_fields_fail_closed(key):
    body = fixture()
    del body[key]
    with pytest.raises(ContractError, match="^batch_shape_invalid$"):
        ingress().ingest(wire(body), known_at=BASE + timedelta(seconds=13))


@pytest.mark.parametrize(
    "data,classification",
    [
        (b"", "wire_size_bound"),
        (b" " * 32769, "wire_size_bound"),
        ("{}", "wire_bytes_required"),
        (bytearray(b"{}"), "wire_bytes_required"),
        (b"\xff", "malformed_batch_json"),
        (b"{} trailing", "malformed_batch_json"),
        (b"[", "malformed_batch_json"),
        (b"[]", "batch_shape_invalid"),
        (b'{"a":1,"a":2}', "duplicate_json_key"),
        (b'{"a":{"b":1,"b":2}}', "duplicate_json_key"),
        (b'{"a":NaN}', "nonfinite_json_constant"),
        (b'{"a":Infinity}', "nonfinite_json_constant"),
        (b'{"a":1e999}', "nonfinite_json_number"),
    ],
)
def test_wire_error_classes_only_and_no_mutation(data, classification):
    target = ingress()
    with pytest.raises(ContractError) as caught:
        target.ingest(data, known_at=BASE + timedelta(seconds=13))
    assert str(caught.value) == classification
    assert target.history == target._candidate.history == ()


@pytest.mark.parametrize("location", ["top", "kline", "level"])
@pytest.mark.parametrize("token", ["1e999", "-1e999", "Infinity"])
def test_unprojected_nonfinite_fields_cannot_refresh_state(location, token):
    target = ingress()
    first = ingest(target, fixture())
    body = fixture(2)
    row = body["rows"][1]
    destination = (
        row
        if location == "top"
        else (row["Kline"] if location == "kline" else row["SellLevel"][4])
    )
    destination["Unprojected"] = "numeric-token"
    data = wire(body).replace(b'"numeric-token"', token.encode())
    with pytest.raises(ContractError, match="^nonfinite_json_(number|constant)$"):
        target.ingest(data, known_at=BASE + timedelta(seconds=23))
    assert target.history == (first,)
    assert (
        target.read(MEMBERS[1], at=BASE + timedelta(seconds=23)).state
        == (first.record.states[1])
    )
    assert ingest(target, fixture(2)).record.acquisition == "sample-2"


@pytest.mark.parametrize(
    "key,value,classification",
    [
        ("requested", None, "clock_shape_invalid"),
        ("received", "private text", "clock_encoding_invalid"),
        ("frozen", "2026-01-01T00:00:12", "utc_clock_required"),
        ("received", "2026-01-01T08:00:11+08:00", "utc_clock_required"),
        ("frozen", BASE.isoformat(), "clock_order_invalid"),
        ("known", BASE + timedelta(seconds=11), "clock_order_invalid"),
        ("known", datetime(2026, 1, 1), "utc_clock_required"),
        ("known", "not datetime", "offline_known_clock_required"),
    ],
)
def test_clocks_are_explicit_ordered_utc_and_not_inferred(key, value, classification):
    body = fixture()
    known = BASE + timedelta(seconds=13)
    if key == "known":
        known = value
    else:
        body["clocks"][key] = value
    target = ingress()
    with pytest.raises(ContractError, match=f"^{classification}$"):
        target.ingest(wire(body), known_at=known)
    assert target.history == ()


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("redial", True, "connection_policy_invalid"),
        ("attempts", 2, "connection_policy_invalid"),
        ("attempts", True, "connection_policy_invalid"),
        ("elapsed_ns", 1.0, "latency_bound"),
        (
            "received",
            (BASE - timedelta(seconds=1)).isoformat(),
            "connection_clock_order",
        ),
        (
            "received",
            (BASE + timedelta(seconds=12)).isoformat(),
            "connection_clock_order",
        ),
    ],
)
def test_connection_gate_before_state(field, value, classification):
    body = fixture()
    body["connection"][field] = value
    with pytest.raises(ContractError, match=f"^{classification}$"):
        ingest(ingress(), body)


def test_fixture_cannot_enter_real_mode_and_real_known_cannot_be_supplied():
    target = module.GoStockRealObservationIngress()
    with pytest.raises(ContractError, match="^input_origin_mismatch$"):
        target.ingest(wire(fixture()))
    with pytest.raises(ContractError, match="^real_known_override_forbidden$"):
        target.ingest(wire(fixture()), known_at=BASE)
    assert target.history == ()
    with pytest.raises(ContractError, match="^offline_known_clock_required$"):
        ingress().ingest(wire(fixture()))


@pytest.mark.parametrize("failure", ["timeout", "disconnected", "source_error"])
def test_source_failure_unavailable_gap_retains_pair_and_stops(failure):
    target = ingress(ttl=timedelta(seconds=5))
    first = ingest(target, fixture())
    body = fixture(2)
    body.update(failure=failure, rows=None)
    failed = ingest(target, body)
    assert failed.classification == failed.source_failure == failure
    assert failed.record.outcome == "failed" and failed.record.quotes == ()
    assert failed.record.classification == (
        "disconnected" if failure == "source_error" else failure
    )
    for s, old in zip(MEMBERS, first.record.states, strict=True):
        view = target.read(s, at=failed.record.clocks.known)
        assert view.status == "unavailable" and view.stale
        assert view.state.quote is old.quote
        assert view.state.observed_at == old.observed_at
        assert view.state.acquisition == old.acquisition
    with pytest.raises(ContractError, match="^capture_already_stopped$"):
        ingest(target, fixture(3))
    assert target.history == (first, failed)


@pytest.mark.parametrize(
    "mutation,classification",
    [
        ("second_negative", "invalid_integer_field"),
        ("duplicate_member", "member_set_mismatch"),
        ("extra_symbol", "security_out_of_scope"),
        ("missing_member", "two_members_required"),
        ("invalid_side", "level_side_invalid"),
        ("overflow", "invalid_integer_field"),
        ("bool_price", "invalid_integer_field"),
        ("naive_sdk_clock", "naive_time"),
        ("opaque_bound", "server_time_bound"),
        ("missing_field", "malformed_snapshot"),
    ],
)
def test_quality_error_record_is_atomic_and_never_updates_only_first_member(
    mutation,
    classification,
):
    target = ingress()
    first = ingest(target, fixture())
    body = fixture(2, price=34567)
    row = body["rows"][1]
    if mutation == "second_negative":
        row["Kline"]["Volume"] = -1
    elif mutation == "duplicate_member":
        row["Exchange"], row["Code"] = 1, "600519"
    elif mutation == "extra_symbol":
        row["Code"] = "000002"
    elif mutation == "missing_member":
        body["rows"].pop()
    elif mutation == "invalid_side":
        row["SellLevel"][0]["Buy"] = True
    elif mutation == "overflow":
        row["BuyLevel"][0]["Number"] = 1 << 31
    elif mutation == "bool_price":
        row["Kline"]["Close"] = True
    elif mutation == "naive_sdk_clock":
        row["Kline"]["Time"] = "2026-01-01T00:00:00"
    elif mutation == "opaque_bound":
        row["ServerTime"] = "x" * 65
    elif mutation == "missing_field":
        del row["Kline"]["Open"]
    failed = ingest(target, body)
    assert failed.record.outcome == "failed"
    assert failed.classification == classification
    assert failed.record.quotes == ()
    assert tuple(s.quote for s in failed.record.states) == first.record.quotes
    assert all(s.unavailable for s in failed.record.states)
    # An offline next sample can recover quality failure; no live retry is implied.
    recovered = ingest(target, fixture(3, price=34567))
    assert recovered.record.outcome == "sample"
    assert all(not s.unavailable for s in recovered.record.states)


def test_duplicate_renews_local_observation_age_but_not_exchange_freshness():
    target = ingress(ttl=timedelta(seconds=5))
    first = ingest(target, fixture())
    assert target.read(MEMBERS[0], at=BASE + timedelta(seconds=16)).status == "observed"
    assert target.read(MEMBERS[0], at=BASE + timedelta(seconds=17)).status == "stale"
    body = fixture(2)
    for row in body["rows"]:
        row["Kline"]["Time"] = BASE.isoformat()
        row["ServerTime"] = "different-opaque"
        row["Active1"] = 5
    second = ingest(target, body)
    assert second.record.outcome == "duplicate"
    assert second.record.states[0].observed_at == BASE + timedelta(seconds=21)
    assert (
        target.read(MEMBERS[0], at=second.record.clocks.known).source_freshness
        == "UNKNOWN"
    )
    assert target.as_of(first.record.clocks.known) is first
    with pytest.raises(ContractError, match="^record_not_yet_known$"):
        target.read(MEMBERS[0], at=first.record.clocks.known, batch_id=second.batch_id)


@pytest.mark.parametrize("requested,outcome", [(5, "late"), (10, "order_uncertain")])
def test_late_and_ambiguous_requests_do_not_replace_current_pair(requested, outcome):
    target = ingress()
    first = ingest(target, fixture())
    second = ingest(target, fixture(2, price=34567, requested=requested))
    assert second.record.outcome == outcome
    assert second.record.states == first.record.states
    assert (
        target.read(MEMBERS[0], at=second.record.clocks.known).state
        == first.record.states[0]
    )


def test_known_order_rejection_preserves_ordinal_and_bounded_exact_retention():
    target = ingress(capacity=2)
    first = ingest(target, fixture())
    body = fixture(2, requested=10)
    body["clocks"] = fixture()["clocks"]
    with pytest.raises(ContractError, match="^known_time_not_forward$"):
        ingest(target, body, known=first.record.clocks.known)
    second = ingest(target, fixture(2))
    third = ingest(target, fixture(3))
    assert second.record.acquisition == "sample-2"
    assert target.history == (second, third)
    with pytest.raises(ContractError, match="^batch_exact_not_retained$"):
        target.exact(first.batch_id)
    with pytest.raises(ContractError, match="^candidate_asof_not_retained$"):
        target.as_of(first.record.clocks.known)
    with pytest.raises(ContractError, match="^session_budget_exhausted$"):
        ingest(target, fixture(3))


def test_round_connection_and_budget_drift_never_mutate_or_reconnect():
    target = ingress()
    first = ingest(target, fixture())
    for body, classification in [
        (fixture(), "round_not_forward"),
        (fixture(3), "round_not_forward"),
        ({**fixture(2), "max_rounds": 2}, "session_budget_drift"),
        (
            {
                **fixture(2),
                "connection": {**fixture(2)["connection"], "elapsed_ns": 500},
            },
            "connection_drift",
        ),
    ]:
        with pytest.raises(ContractError, match=f"^{classification}$"):
            ingest(target, body)
        assert target.history == (first,)
    assert ingest(target, fixture(2)).record.acquisition == "sample-2"
    one = ingress()
    ingest(one, {**fixture(), "max_rounds": 1})
    with pytest.raises(ContractError, match="^session_budget_exhausted$"):
        ingest(one, fixture(2))


def test_import_and_ingress_need_no_sdk_process_network_or_artifact_reads(monkeypatch):
    import socket
    import subprocess
    from pathlib import Path

    def forbidden(*args, **kwargs):
        pytest.fail("provider or artifact side effect")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "read_text", forbidden)
    importlib.reload(module)
    assert ingest(ingress(), fixture()).record.outcome == "sample"
