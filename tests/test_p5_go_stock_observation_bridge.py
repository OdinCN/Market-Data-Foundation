"""Independent author-created envelopes; never read retained provider material."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from market_data_foundation.candidates.go_stock_observation_bridge import (
    MAX_WIRE_BYTES,
    ORIGIN,
    SCHEMA,
    GoStockObservationBridge,
    encode_synthetic_batch,
    synthetic_offline_demonstration,
)
from market_data_foundation.candidates.stock_snapshot import (
    SDK_RELEASE,
    UNITS,
    Clocks,
    StockSnapshotCandidate,
)
from market_data_foundation.canonical.model import ContractError

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def envelope(n=0, *, request=None, price=24680):
    """All values and clocks invented here, independently of source fixtures."""
    rows = []
    for exchange, code in ((1, "600519"), (0, "000001")):
        rows.append(
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
                    {"Buy": True, "Price": price - i * 2, "Number": i * 3}
                    for i in range(1, 6)
                ],
                "SellLevel": [
                    {"Buy": False, "Price": price + i * 2, "Number": i * 3}
                    for i in range(1, 6)
                ],
                "ServerTime": "author-created-opaque",
                "Active1": 3,
                "Active2": 4,
            }
        )
    return {
        "schema": SCHEMA,
        "input_origin": ORIGIN,
        "sdk_release": SDK_RELEASE,
        "units": list(UNITS),
        "batch_id": f"synthetic-test-{n}",
        "clock_provenance": ORIGIN,
        "actual_observation_clocks": "UNKNOWN",
        "source_event_clock": "UNKNOWN",
        "clocks": {
            key: (BASE + timedelta(seconds=value)).isoformat()
            for key, value in zip(
                ("requested", "received", "frozen", "known"),
                (n if request is None else request, n + 1, n + 2, n + 3),
                strict=True,
            )
        },
        "mode": "observe",
        "failure": None,
        "parser_revision": "synthetic-parser-r1",
        "supersedes_batch": None,
        "rows": rows,
    }


def ingest(bridge, body):
    return bridge.ingest(encode_synthetic_batch(body))


def test_roundtrip_frozen_atomic_identity_units_and_clock_separation():
    body = envelope()
    body["rows"].reverse()
    wire = encode_synthetic_batch(body)
    assert json.loads(wire) == body
    body["rows"][0]["Kline"]["Close"] = 1
    bridge = GoStockObservationBridge()
    observation = bridge.ingest(wire)
    quote = observation.record.quotes[0]
    assert tuple(q.security for q in observation.record.quotes) == (
        "sh600519",
        "sz000001",
    )
    assert quote.close_cny == Decimal("24.680")
    assert quote.amount_cny == Decimal("456.789")
    assert quote.volume_sdk == 31 and len(quote.bids) == len(quote.asks) == 5
    assert quote.client_sampled_at == BASE - timedelta(days=1)
    assert observation.clock_provenance == observation.input_origin == ORIGIN
    assert observation.sdk_release == SDK_RELEASE
    assert observation.actual_observation_clocks == "UNKNOWN"
    assert observation.source_event_clock == "UNKNOWN"
    assert bridge.exact(body["batch_id"]) is observation
    assert bridge.as_of(observation.record.clocks.known) is observation
    view = bridge.read("sh600519", at=observation.record.clocks.known)
    assert view.state.observed_at == BASE + timedelta(seconds=1)
    assert view.source_event_at is None and view.source_freshness == "UNKNOWN"
    assert view.rights == "UNKNOWN / EXCLUDED_FROM_PRODUCT_EFFECT"


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("schema", "other", "batch_schema_mismatch"),
        ("input_origin", "retained_provider", "synthetic_origin_required"),
        ("sdk_release", "injoyai/tdx v0.0.88", "sdk_release_mismatch"),
        ("units", ["CNY", "shares", "lots"], "unit_declaration_mismatch"),
        ("units", list(UNITS[:2]), "unit_declaration_mismatch"),
        ("batch_id", "live-batch-1", "batch_identity_invalid"),
        ("batch_id", "synthetic-" + "x" * 55, "batch_identity_invalid"),
        ("batch_id", ["synthetic-a"], "batch_identity_invalid"),
        ("clock_provenance", "sdk_time_now", "synthetic_clocks_required"),
        ("actual_observation_clocks", "PROVEN", "real_clock_claim_forbidden"),
        ("source_event_clock", "Kline.Time", "real_clock_claim_forbidden"),
        ("mode", [], "batch_mode_invalid"),
        ("failure", "raw exception text", "failure_classification"),
        ("failure", {}, "failure_classification"),
        ("parser_revision", [], "parser_revision"),
        ("parser_revision", True, "parser_revision"),
        ("parser_revision", "", "parser_revision"),
        ("parser_revision", "x" * 65, "parser_revision"),
        ("supersedes_batch", 1, "batch_identity_invalid"),
        ("clocks", None, "clock_shape_invalid"),
    ],
)
def test_envelope_rejection_has_no_candidate_effect(field, value, classification):
    bridge = GoStockObservationBridge()
    first = ingest(bridge, envelope())
    body = envelope(10)
    body[field] = value
    # Bypass encoder validation to exercise the incoming wire boundary itself.
    wire = json.dumps(body).encode()
    with pytest.raises(ContractError, match=f"^{classification}$"):
        bridge.ingest(wire)
    assert bridge.history == (first,)
    assert (
        bridge.read("sz000001", at=BASE + timedelta(seconds=13)).state
        == (first.record.states[1])
    )
    accepted = ingest(bridge, envelope(10))
    assert accepted.record.acquisition == "sample-2"


@pytest.mark.parametrize("key", list(envelope()))
def test_missing_envelope_field_fails_closed(key):
    body = envelope()
    del body[key]
    with pytest.raises(ContractError, match="^batch_shape_invalid$"):
        GoStockObservationBridge().ingest(json.dumps(body).encode())


@pytest.mark.parametrize(
    "wire,classification",
    [
        (b"", "wire_size_bound"),
        (b" " * (MAX_WIRE_BYTES + 1), "wire_size_bound"),
        ("{}", "wire_bytes_required"),
        (bytearray(b"{}"), "wire_bytes_required"),
        (b"\xff", "malformed_batch_json"),
        (b"{", "malformed_batch_json"),
        (b"{} trailing", "malformed_batch_json"),
        (b'{"x":1,"x":2}', "duplicate_json_key"),
        (b'{"x":{"Code":1,"Code":2}}', "duplicate_json_key"),
        (b'{"x":NaN}', "nonfinite_json_constant"),
        (b'{"x":Infinity}', "nonfinite_json_constant"),
        (b"[]", "batch_shape_invalid"),
    ],
)
def test_wire_failures_are_classifications_only(wire, classification):
    bridge = GoStockObservationBridge()
    with pytest.raises(ContractError) as caught:
        bridge.ingest(wire)
    assert str(caught.value) == classification
    assert bridge.history == ()


@pytest.mark.parametrize("value", [object(), float("nan"), float("inf")])
def test_serialization_failures_do_not_expose_input(value):
    with pytest.raises(ContractError, match="^batch_serialization_invalid$"):
        encode_synthetic_batch(value)


def test_serialization_circular_input_and_oversize():
    body = envelope()
    body["rows"].append(body)
    with pytest.raises(ContractError, match="^batch_serialization_invalid$"):
        encode_synthetic_batch(body)
    body = envelope()
    body["rows"][0]["ServerTime"] = "x" * MAX_WIRE_BYTES
    with pytest.raises(ContractError, match="^wire_size_bound$"):
        encode_synthetic_batch(body)


@pytest.mark.parametrize(
    "clock,value,classification",
    [
        ("requested", None, "clock_shape_invalid"),
        ("received", "x" * 65, "clock_shape_invalid"),
        ("known", "not a timestamp", "clock_encoding_invalid"),
        ("received", "2026-01-01T00:00:01", "naive_time"),
        ("known", "2025-01-01T00:00:00+00:00", "clock_order_invalid"),
    ],
)
def test_explicit_clocks_are_required_not_inferred(clock, value, classification):
    body = envelope()
    body["clocks"][clock] = value
    with pytest.raises(ContractError, match=f"^{classification}$"):
        ingest(GoStockObservationBridge(), body)


def test_extra_keys_clock_members_and_batch_reuse():
    body = envelope()
    body["extra"] = True
    with pytest.raises(ContractError, match="^batch_shape_invalid$"):
        ingest(GoStockObservationBridge(), body)
    body = envelope()
    del body["clocks"]["known"]
    with pytest.raises(ContractError, match="^clock_shape_invalid$"):
        ingest(GoStockObservationBridge(), body)
    bridge = GoStockObservationBridge()
    first = ingest(bridge, envelope())
    body = envelope(10)
    body["batch_id"] = first.batch_id
    with pytest.raises(ContractError, match="^batch_identity_reused$"):
        ingest(bridge, body)
    assert bridge.history == (first,)


# Independently specified SDK representation limits, exercised through JSON.
INTEGER_FIELDS = (
    [
        (("Kline", name), (1 << 63) - 1)
        for name in ("Last", "Open", "High", "Low", "Close", "Amount", "Volume")
    ]
    + [
        ((side, level, name), maximum)
        for side in ("BuyLevel", "SellLevel")
        for level in range(5)
        for name, maximum in (("Price", (1 << 63) - 1), ("Number", (1 << 31) - 1))
    ]
    + [((name,), (1 << 16) - 1) for name in ("Active1", "Active2")]
)


def set_field(row, path, value):
    for key in path[:-1]:
        row = row[key]
    row[path[-1]] = value


UNPROJECTED_PATHS = (
    ("Unprojected",),
    ("Kline", "Unprojected"),
    ("BuyLevel", 0, "Unprojected"),
    ("SellLevel", 4, "Unprojected"),
)


def numeric_token_wire(body, path, token):
    """Inject a literal JSON token into this author's synthetic fixture only."""
    set_field(body["rows"][1], path, "synthetic-number-token")
    wire = json.dumps(body).encode()
    assert wire.count(b'"synthetic-number-token"') == 1
    return wire.replace(b'"synthetic-number-token"', token.encode())


@pytest.mark.parametrize("path", UNPROJECTED_PATHS)
@pytest.mark.parametrize(
    "token,classification",
    [
        ("1e999", "nonfinite_json_number"),
        ("-1e999", "nonfinite_json_number"),
        ("1e+999", "nonfinite_json_number"),
        ("-1e+999", "nonfinite_json_number"),
        ("Infinity", "nonfinite_json_constant"),
    ],
)
def test_nonfinite_decode_rejects_without_history_state_age_or_ordinal_effect(
    path, token, classification
):
    bridge = GoStockObservationBridge(capacity=2, ttl=timedelta(seconds=10))
    control = GoStockObservationBridge(capacity=2, ttl=timedelta(seconds=10))
    first = ingest(bridge, envelope())
    ingest(control, envelope())
    history_before = bridge.history
    candidate_history_before = bridge._candidate.history
    at = BASE + timedelta(seconds=13)
    views_before = {s: bridge.read(s, at=at) for s in ("sh600519", "sz000001")}
    for view in views_before.values():
        assert view.state.observed_at == BASE + timedelta(seconds=1)
        assert view.age == timedelta(seconds=12) and view.status == "stale"

    with pytest.raises(ContractError, match=f"^{classification}$"):
        bridge.ingest(numeric_token_wire(envelope(10), path, token))

    assert bridge.history == history_before == (first,)
    assert bridge._candidate.history == candidate_history_before == (first.record,)
    assert bridge.exact(first.batch_id) is first
    with pytest.raises(ContractError, match="^batch_exact_not_retained$"):
        bridge.exact(envelope(10)["batch_id"])
    for seconds in (3, 12, 13, 23):
        when = BASE + timedelta(seconds=seconds)
        assert bridge.as_of(when) is first
        for security in views_before:
            assert bridge.read(security, at=when) == control.read(security, at=when)
            assert bridge.read(security, at=when, batch_id=first.batch_id) == (
                control.read(security, at=when, batch_id=first.batch_id)
            )
    for security, view in views_before.items():
        assert bridge.read(security, at=at) == view
    # Same ID and clocks can still be accepted: neither ordinal nor visibility
    # watermark was consumed by the rejected attempt.
    accepted = ingest(bridge, envelope(10))
    expected = ingest(control, envelope(10))
    assert accepted == expected
    assert accepted.record.acquisition == "sample-2"
    assert accepted.record.outcome == "duplicate"
    assert bridge.history == control.history
    assert bridge._candidate.history == control._candidate.history


@pytest.mark.parametrize("path", UNPROJECTED_PATHS)
@pytest.mark.parametrize("token", ["1e308", "-1e308", "1e-999", "1.5"])
def test_finite_unprojected_numbers_preserve_duplicate_behavior(path, token):
    bridge = GoStockObservationBridge(ttl=timedelta(seconds=10))
    first = ingest(bridge, envelope())
    accepted = bridge.ingest(numeric_token_wire(envelope(10), path, token))
    assert accepted.record.outcome == "duplicate"
    assert accepted.record.quotes == first.record.quotes
    view = bridge.read("sz000001", at=accepted.record.clocks.known)
    assert view.age == timedelta(seconds=2) and view.status == "observed"


def test_finite_projected_float_keeps_existing_atomic_integer_rejection():
    bridge = GoStockObservationBridge()
    first = ingest(bridge, envelope())
    failed = bridge.ingest(numeric_token_wire(envelope(10), ("Kline", "Close"), "1e0"))
    assert failed.record.acquisition == "sample-2"
    assert failed.record.outcome == "failed"
    assert failed.record.classification == "invalid_integer_field"
    assert failed.record.quotes == ()
    for index, security in enumerate(("sh600519", "sz000001")):
        view = bridge.read(security, at=failed.record.clocks.known)
        assert view.status == "unavailable"
        assert view.state.quote == first.record.quotes[index]
        assert view.state.acquisition == first.record.acquisition


@pytest.mark.parametrize("path,maximum", INTEGER_FIELDS)
@pytest.mark.parametrize("boundary", ["zero", "maximum"])
def test_json_preserves_all_integer_boundaries(path, maximum, boundary):
    body = envelope()
    value = 0 if boundary == "zero" else maximum
    set_field(body["rows"][1], path, value)
    wire = encode_synthetic_batch(body)
    decoded = json.loads(wire)["rows"][1]
    for key in path:
        decoded = decoded[key]
    assert type(decoded) is int and decoded == value
    observation = GoStockObservationBridge().ingest(wire)
    assert observation.record.outcome == "sample"
    assert len(observation.record.quotes) == 2


@pytest.mark.parametrize("path,maximum", INTEGER_FIELDS)
@pytest.mark.parametrize("invalid", ["negative", "above_max", "bool", "float", "str"])
def test_integer_failure_retains_both_members_atomically(path, maximum, invalid):
    bridge = GoStockObservationBridge()
    first = ingest(bridge, envelope())
    body = envelope(10, price=35790)
    value = {
        "negative": -1,
        "above_max": maximum + 1,
        "bool": True,
        "float": 1.0,
        "str": "1",
    }[invalid]
    set_field(body["rows"][1], path, value)
    failed = ingest(bridge, body)
    assert failed.record.outcome == "failed"
    assert failed.record.classification == "invalid_integer_field"
    assert failed.record.quotes == ()
    for index, security in enumerate(("sh600519", "sz000001")):
        view = bridge.read(security, at=failed.record.clocks.known)
        assert view.status == "unavailable"
        assert view.state.quote == first.record.quotes[index]
        assert view.state.acquisition == first.record.acquisition
    recovered = ingest(bridge, envelope(20, price=35790))
    assert recovered.record.outcome == "sample"
    assert (
        bridge.read("sz000001", at=recovered.record.clocks.known).status == "observed"
    )


@pytest.mark.parametrize(
    "mutation,classification",
    [
        ("member", "member_set_mismatch"),
        ("scope", "security_out_of_scope"),
        ("missing", "malformed_snapshot"),
        ("levels", "five_levels_required"),
        ("side", "level_side_invalid"),
        ("sample_clock", "naive_time"),
        ("opaque_bound", "server_time_bound"),
    ],
)
def test_quote_rejection_uses_existing_candidate_classifications(
    mutation, classification
):
    body = envelope()
    row = body["rows"][1]
    if mutation == "member":
        body["rows"][1] = deepcopy(body["rows"][0])
    elif mutation == "scope":
        row["Code"] = "510300"
    elif mutation == "missing":
        del row["Kline"]["Amount"]
    elif mutation == "levels":
        row["BuyLevel"].pop()
    elif mutation == "side":
        row["SellLevel"][0]["Buy"] = True
    elif mutation == "sample_clock":
        row["Kline"]["Time"] = "2026-01-01T00:00:00"
    else:
        row["ServerTime"] = "x" * 65
    failed = ingest(GoStockObservationBridge(), body)
    assert failed.record.classification == classification
    assert failed.record.outcome == "failed" and failed.record.quotes == ()


def test_temporal_state_machine_is_identical_to_direct_candidate():
    bridge = GoStockObservationBridge(ttl=timedelta(seconds=10))
    direct = StockSnapshotCandidate(ttl=timedelta(seconds=10))
    bodies = [
        envelope(),
        envelope(5),
        envelope(10, request=0, price=35790),
        envelope(15, request=5, price=35790),
        envelope(20),
        envelope(25, price=35790),
    ]
    bodies[1]["rows"][0]["Kline"]["Time"] = (BASE + timedelta(seconds=6)).isoformat()
    bodies[1]["rows"][0]["Active1"] = 10
    bodies[4].update(rows=None, failure="timeout")
    outcomes = []
    for body in bodies:
        observation = ingest(bridge, body)
        clocks = Clocks(*(datetime.fromisoformat(v) for v in body["clocks"].values()))
        expected = direct.observe(body["rows"], clocks=clocks, failure=body["failure"])
        assert observation.record == expected
        for security in ("sh600519", "sz000001"):
            assert bridge.read(security, at=clocks.known) == direct.read(
                security, at=clocks.known
            )
        outcomes.append(observation.record.outcome)
    assert outcomes == [
        "sample",
        "duplicate",
        "late",
        "order_uncertain",
        "failed",
        "sample",
    ]
    stale = bridge.read("sh600519", at=BASE + timedelta(seconds=18))
    assert stale.status == "stale" and stale.state.acquisition == "sample-2"
    unavailable = bridge.read("sh600519", at=BASE + timedelta(seconds=23))
    assert unavailable.status == "unavailable" and unavailable.stale


def test_correction_exact_asof_retention_and_age():
    bridge = GoStockObservationBridge(capacity=2, ttl=timedelta(seconds=10))
    first = ingest(bridge, envelope())
    body = envelope(10, request=0, price=35790)
    body.update(parser_revision="synthetic-parser-r2", supersedes_batch=first.batch_id)
    corrected = ingest(bridge, body)
    assert corrected.record.outcome == "corrected"
    assert corrected.record.supersedes == first.record.acquisition
    assert corrected.supersedes_batch == first.batch_id
    assert bridge.as_of(BASE + timedelta(seconds=12)) is first
    assert bridge.as_of(BASE + timedelta(seconds=13)) is corrected
    view = bridge.read("sh600519", at=corrected.record.clocks.known)
    assert view.state.observed_at == first.record.clocks.received
    assert view.status == "stale" and view.age == timedelta(seconds=12)
    old = bridge.read(
        "sh600519", at=corrected.record.clocks.known, batch_id=first.batch_id
    )
    assert old.state.quote.close_cny == Decimal("24.680")
    with pytest.raises(ContractError, match="^record_not_yet_known$"):
        bridge.read(
            "sh600519", at=first.record.clocks.known, batch_id=corrected.batch_id
        )
    bad = envelope(20, request=0)
    bad.update(parser_revision="synthetic-parser-r3", supersedes_batch=first.batch_id)
    with pytest.raises(ContractError, match="^correction_not_current$"):
        ingest(bridge, bad)
    assert bridge.history == (first, corrected)
    third = ingest(bridge, envelope(20))
    assert third.record.acquisition == "sample-3" and len(bridge.history) == 2
    with pytest.raises(ContractError, match="^batch_exact_not_retained$"):
        bridge.exact(first.batch_id)
    with pytest.raises(ContractError, match="^candidate_asof_not_retained$"):
        bridge.as_of(first.record.clocks.known)
    body = envelope(30)
    body["supersedes_batch"] = first.batch_id
    with pytest.raises(ContractError, match="^batch_exact_not_retained$"):
        ingest(bridge, body)


def test_forward_known_time_and_ttl_boundary():
    bridge = GoStockObservationBridge(ttl=timedelta(seconds=10))
    assert bridge.read("sz000001", at=BASE).status == "unknown"
    body = envelope()
    first = ingest(bridge, body)
    body["batch_id"] = "synthetic-another"
    with pytest.raises(ContractError, match="^known_time_not_forward$"):
        ingest(bridge, body)
    assert bridge.history == (first,)
    assert bridge.read("sz000001", at=BASE + timedelta(seconds=11)).status == "observed"
    assert bridge.read("sz000001", at=BASE + timedelta(seconds=12)).status == "stale"


def test_reconnect_segment_and_recovery_reuse_candidate():
    bridge = GoStockObservationBridge()
    ingest(bridge, envelope())
    gap = envelope(10)
    gap.update(mode="reconnect", failure="disconnected", rows=None)
    disconnected = ingest(bridge, gap)
    assert disconnected.record.segment == 0
    assert (
        bridge.read("sh600519", at=disconnected.record.clocks.known).status
        == "unavailable"
    )
    late = ingest(bridge, envelope(20, request=5, price=35790))
    assert late.record.outcome == "late" and late.record.segment == 1
    assert bridge.read("sh600519", at=late.record.clocks.known).status == "unavailable"
    recovered = ingest(bridge, envelope(25, price=35790))
    assert recovered.record.segment == 1 and recovered.record.outcome == "sample"
    assert (
        bridge.read("sz000001", at=recovered.record.clocks.known).status == "observed"
    )
    gap = envelope(30, request=0)
    gap.update(mode="reconnect", failure="disconnected", rows=None)
    with pytest.raises(ContractError, match="^reconnect_request_not_forward$"):
        ingest(bridge, gap)
    assert bridge.history[-1] is recovered


@pytest.mark.parametrize(
    "updates,classification",
    [
        ({"failure": "timeout"}, "failure_rows_conflict"),
        ({"mode": "reconnect", "rows": None}, "reconnect_shape_invalid"),
        (
            {
                "mode": "reconnect",
                "rows": None,
                "failure": "disconnected",
                "parser_revision": "r2",
            },
            "reconnect_revision_mismatch",
        ),
    ],
)
def test_conflicting_controls_rejected_before_mutation(updates, classification):
    body = envelope()
    body.update(updates)
    with pytest.raises(ContractError, match=f"^{classification}$"):
        ingest(GoStockObservationBridge(), body)


def test_failure_before_first_sample_and_bounded_configuration():
    bridge = GoStockObservationBridge()
    body = envelope()
    body.update(failure="timeout", rows=None)
    failed = ingest(bridge, body)
    view = bridge.read("sz000001", at=failed.record.clocks.known)
    assert view.status == "unavailable" and view.state.quote is None
    assert view.age is None and not view.stale
    for capacity in (1, 257, True):
        with pytest.raises(ContractError, match="^history_bound_invalid$"):
            GoStockObservationBridge(capacity=capacity)
    with pytest.raises(ContractError, match="^ttl_bound_invalid$"):
        GoStockObservationBridge(ttl=timedelta(0))


def test_demo_is_bounded_synthetic_classified_and_has_error_recovery(capsys):
    summary = synthetic_offline_demonstration()
    observations = summary["observations"]
    assert summary["scope"] == "bounded_synthetic_offline_demonstration"
    assert (
        summary["actual_observation_clocks"]
        == summary["source_event_clock"]
        == "UNKNOWN"
    )
    assert [o["outcome"] for o in observations] == [
        "sample",
        "duplicate",
        "sample",
        "late",
        "order_uncertain",
        "failed",
        "duplicate",
        "failed",
        "duplicate",
        "failed",
        "late",
        "duplicate",
    ]
    assert observations[4]["stale"]
    assert observations[5]["classification"] == "invalid_integer_field"
    assert observations[7]["classification"] == "timeout"
    assert observations[9]["classification"] == "disconnected"
    assert observations[-1]["status"] == "observed"
    assert len({o["batch_id"] for o in observations}) == 12
    assert "Kline" not in json.dumps(summary) and "ServerTime" not in json.dumps(
        summary
    )
    assert capsys.readouterr().out == ""
