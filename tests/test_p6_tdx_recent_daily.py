"""Offline count-30 gate: synthetic SDK bytes, no provider or PostgreSQL I/O."""

import base64
import hashlib
import json
import sys
from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from market_data_foundation.canonical.codec import dumps
from market_data_foundation.canonical.consumer_wire import handle_request
from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import ContractError
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import tdx_recent_daily as recent
from provider_lab import tdx_p6_recent_probe as probe
from tests.test_p4_tdx_live_daily import BASE, guard_result, observation, raw_bytes


def receipt(rows=None):
    template = json.loads(raw_bytes())[0]
    if rows is None:
        rows = [
            {
                **template,
                "time": (BASE - timedelta(days=30 - i)).strftime("%Y-%m-%dT15:00:00"),
            }
            for i in range(30)
        ]
    obs = observation()
    obs["fixture_id"] = recent.EXPECTED_FIXTURE
    obs["request"] = deepcopy(recent.EXPECTED_REQUEST)
    obs["coverage"]["row_count"] = len(rows)
    obs["pagination"]["page_size"] = 30
    obs["date_range"]["observed_start"] = rows[0]["time"][:10] if rows else None
    obs["date_range"]["observed_end"] = rows[-1]["time"][:10] if rows else None
    return recent.RecentReceipt(
        obs,
        json.dumps(rows).encode(),
        guard_result(),
        recent.EXPECTED_PROBE_SHA256,
        recent.EXPECTED_GUARD_SHA256,
        "artifact://offline-synthetic-count30",
        BASE,
        BASE + timedelta(seconds=1),
    )


def plan(value=None, identity="offline-count30"):
    return recent.build_recent_plan(
        value or receipt(),
        run_identity=identity + ":run",
        acquisition_identity=identity + ":acquisition",
        material_identity=identity + ":material",
        version_identity=identity + ":version",
        publication_identity=identity + ":publication",
        publication_occurrence=identity + ":occurrence",
        frozen_at=BASE + timedelta(seconds=2),
    )


def seed():
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    candidate = plan()
    recent.persist_recent_plan(kernel, candidate)
    publication = recent.publish_recent_plan(kernel, candidate)
    return ledger, kernel, candidate, publication


def request(candidate, mode, as_of=None):
    definition = candidate.version.definition
    body = {
        "wire_revision": "mdf.consumer.read.v1",
        "mode": mode,
        "definition": {
            "kind": definition.kind.value,
            "identity": definition.identity,
            "revision": definition.revision,
        },
    }
    if mode == "exact_version":
        body["version_identity"] = candidate.version.identity
    if as_of is not None:
        body["as_of"] = as_of
    return json.dumps(body).encode()


def test_new_source_lineage_and_exact_as_of_success_snapshot():
    ledger, _kernel, candidate, publication = seed()
    assert candidate.returned_rows == 30
    assert candidate.acquisition.source == recent.RECENT_SOURCE.key
    assert candidate.acquisition.source_observed_at is None
    assert candidate.acquisition.provider_data_attempts == 1
    assert candidate.version.binding == recent.RECENT_BINDING.key
    assert candidate.material.acquired_from == candidate.acquisition.key
    assert candidate.version.frozen_at == BASE + timedelta(seconds=2)
    assert dict(candidate.acquisition.parameters)["count"] == "30"
    before = ledger.snapshot()
    for mode in ("exact_version", "as_of"):
        wire_request = request(candidate, mode, publication.usable_at.isoformat())
        result = handle_request(ledger, wire_request)
        body = json.loads(result)
        assert body["status"] == "ok"
        assert body["view"]["source"]["identity"] == recent.RECENT_SOURCE.identity
        assert (
            body["view"]["publication"]["request"]["identity"]
            == candidate.publication_request.identity
        )
        assert base64.b64decode(body["view"]["content"]["base64"]) == candidate.content
        assert (
            handle_request(InMemoryLedger.from_snapshot(before), wire_request) == result
        )
    assert ledger.snapshot() == before


@pytest.mark.parametrize("count", [1, 2, 29, 30])
def test_short_returns_remain_bounded_without_filling(count):
    rows = json.loads(receipt().raw_projection)[:count]
    candidate = plan(receipt(rows))
    coverage = json.loads(candidate.content)["coverage"]
    assert coverage["valid_completed_rows"] == count
    assert coverage["missing_requested_rows"] == 30 - count


def test_current_day_is_excluded_without_event_time_inference():
    rows = json.loads(receipt().raw_projection)
    rows[-1]["time"] = BASE.strftime("%Y-%m-%dT15:00:00")
    candidate = plan(receipt(rows))
    assert candidate.excluded_current_day_rows == 1
    assert len(json.loads(candidate.content)["rows"]) == 29


@pytest.mark.parametrize(
    "kind",
    [
        "empty",
        "overflow",
        "duplicate",
        "reversed",
        "future",
        "only_today",
        "timezone",
        "numeric",
        "ohlc",
        "field",
    ],
)
def test_invalid_rows_fail_closed(kind):
    rows = json.loads(receipt().raw_projection)
    if kind == "empty":
        rows = []
    if kind == "overflow":
        rows.append(rows[-1])
    if kind == "duplicate":
        rows[-1] = rows[-2]
    if kind == "reversed":
        rows.reverse()
    if kind == "future":
        rows[-1]["time"] = (BASE + timedelta(days=1)).isoformat()
    if kind == "only_today":
        rows = [{**rows[-1], "time": BASE.strftime("%Y-%m-%dT15:00:00")}]
    if kind == "timezone":
        rows[-1]["time"] += "+08:00"
    if kind == "numeric":
        rows[-1]["open"] = True
    if kind == "ohlc":
        rows[-1]["low"] = 100000
    if kind == "field":
        rows[-1]["source_event_at"] = BASE.isoformat()
    with pytest.raises((ValueError, ContractError)):
        plan(receipt(rows))


@pytest.mark.parametrize(
    "kind",
    [
        "request",
        "old_fixture",
        "count",
        "endpoint",
        "sdk",
        "time",
        "date_range",
        "units",
        "adjustment",
        "probe_hash",
        "guard_hash",
        "guard_stop",
        "peer",
        "attempts",
        "wall",
    ],
)
def test_receipt_shape_source_time_endpoint_guard_fail_closed(kind):
    value = receipt()
    obs = deepcopy(value.observation)
    guard = deepcopy(value.guard_result)
    if kind == "request":
        obs["request"]["count"] = 2
    if kind == "old_fixture":
        obs["fixture_id"] = "tdx-r1-common-endpoint-recent-two"
    if kind == "count":
        obs["coverage"]["row_count"] = 29
    if kind == "endpoint":
        obs["attempted_endpoint"]["host"] = "192.168.10.4"
    if kind == "sdk":
        obs["implementation"] = "pytdxdata"
    if kind == "time":
        obs["observed_at"] = (BASE - timedelta(seconds=1)).isoformat()
    if kind == "date_range":
        obs["date_range"]["observed_end"] = "2026-01-01"
    if kind == "units":
        obs["units"]["volume"] = "lots"
    if kind == "adjustment":
        obs["semantics"]["adjustment"] = "forward"
    if kind == "probe_hash":
        value = replace(value, probe_sha256="0" * 64)
    if kind == "guard_hash":
        value = replace(value, guard_sha256="0" * 64)
    if kind == "guard_stop":
        guard["stop"] = "wall_limit"
    if kind == "peer":
        guard["established_peers"][0]["peer"] = ["1.2.3.4", 7709]
    if kind == "attempts":
        guard["events"] *= 3
    if kind == "wall":
        value = replace(value, guarded_finished_at=BASE + timedelta(seconds=16))
    with pytest.raises((ValueError, ContractError)):
        plan(replace(value, observation=obs, guard_result=guard))


@pytest.mark.parametrize("mode", ["exact_version", "as_of"])
@pytest.mark.parametrize(
    "kind",
    [
        "before_usable",
        "missing_content",
        "digest",
        "source_substitution",
        "version_conflict",
        "publication_conflict",
    ],
)
def test_consumer_negative_cases_fail_closed(mode, kind):
    ledger, _kernel, candidate, publication = seed()
    as_of = publication.usable_at
    if kind == "before_usable":
        as_of -= timedelta(microseconds=1)
    if kind == "missing_content":
        ledger._contents.clear()
    if kind == "digest":
        ledger._contents[candidate.version.identity] = (
            candidate.version.content_digest,
            b"corrupt",
        )
    if kind in {"source_substitution", "version_conflict"}:
        record = (
            replace(
                candidate.acquisition,
                source=replace(recent.RECENT_SOURCE, identity="other-source").key,
            )
            if kind == "source_substitution"
            else replace(candidate.version, mapping="other-mapping")
        )
        key = record.key
        ledger._records[(key.kind.value, key.identity, key.revision)] = dumps(record)
    if kind == "publication_conflict":
        key = publication.key
        ledger._records[(key.kind.value, key.identity, key.revision)] = dumps(
            replace(publication, usable_at=BASE)
        )
    body = json.loads(
        handle_request(ledger, request(candidate, mode, as_of.isoformat()))
    )
    assert body["status"] == "error"
    assert set(body) == {"status", "classification", "wire_revision"}


def test_exact_schema_only_and_probe_byte_pin():
    schema = "mdf_p6_real_cognition_data_readiness_r1"
    assert validate_schema(schema) == schema
    with pytest.raises(ContractError):
        validate_schema(schema + "_other")
    assert (
        hashlib.sha256(Path(probe.__file__).read_bytes()).hexdigest()
        == recent.EXPECTED_PROBE_SHA256
    )


def test_probe_one_mocked_call_and_explicit_opt_in(monkeypatch, tmp_path):
    with pytest.raises(PermissionError):
        probe.probe(tmp_path)
    calls = []
    rows = json.loads(receipt().raw_projection)
    client = SimpleNamespace(
        get_kline=lambda *args: (
            calls.append(("request", args))
            or SimpleNamespace(list=[SimpleNamespace(**row) for row in rows])
        ),
        close=lambda: calls.append(("close",)),
    )
    api = SimpleNamespace(dial=lambda *args: calls.append(("dial", args)) or client)
    monkeypatch.setitem(sys.modules, "tdx_py", SimpleNamespace(api=api))
    monkeypatch.setattr(probe.importlib.metadata, "version", lambda _: "0.1.6")
    monkeypatch.setattr(probe, "__file__", str(tmp_path / "provider_lab/probe.py"))
    result = probe.probe(tmp_path / "artifacts/tdx-r1/mock", allow_live=True)
    assert result["success"] is True
    assert calls == [
        ("dial", ("139.9.52.158:7709", 2000, False)),
        ("request", (9, "sh600000", 0, 30)),
        ("close",),
    ]
