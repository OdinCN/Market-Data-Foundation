"""Qualification behavior with synthetic data and no provider connections."""

import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from market_data_foundation.qualification import (
    Binding,
    Capability,
    Endpoint,
    Evidence,
    load_fixtures,
    observe,
)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = load_fixtures(ROOT / "provider_lab/fixtures.toml")
SCHEMA = json.loads((ROOT / "provider_lab/observation.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
FIXTURE = next(f for f in FIXTURES if f.capability == Capability.BARS_DAILY)
BINDING = Binding("synthetic", "fixture-executor", "1", FIXTURE.capability, "offline")
STAMP = datetime(2024, 1, 2, tzinfo=timezone.utc)


def test_fixture_and_schema_contracts() -> None:
    Draft202012Validator.check_schema(SCHEMA)
    assert {f.capability for f in FIXTURES} == set(Capability)
    assert len({f.fixture_id for f in FIXTURES}) == len(FIXTURES)
    assert set(SCHEMA["properties"]["capability"]["enum"]) == set(Capability)
    assert FIXTURE.start == date(2024, 1, 2)
    assert FIXTURE.end == date(2024, 1, 10)
    with pytest.raises(ValueError, match="reversed"):
        replace(FIXTURE, start=date(2025, 1, 1))


def test_success_observation_keeps_measured_provenance() -> None:
    ticks = iter((1.0, 1.05))
    endpoint = Endpoint("example.invalid", 7709, "synthetic-server", None)
    record = observe(
        BINDING,
        FIXTURE,
        lambda _: Evidence(
            success=True,
            returned_symbols=FIXTURE.symbols,
            row_count=3,
            complete=True,
            observed_start=date(2024, 1, 2),
            observed_end=date(2024, 1, 4),
            pages=2,
            exhausted=True,
            units={"price": "CNY", "volume": "shares"},
            semantics={"adjustment": "none", "timezone": "Asia/Shanghai"},
            endpoint=endpoint,
        ),
        timestamp=STAMP,
        timer=lambda: next(ticks),
    )
    VALIDATOR.validate(record)
    assert record["latency_ms"] == pytest.approx(50)
    assert record["date_range"]["requested_end"] == "2024-01-10"
    assert record["date_range"]["observed_end"] == "2024-01-04"
    assert record["endpoint"]["server_identity"] == "synthetic-server"
    assert record["coverage"]["row_count"] == 3
    assert record["exception"] is None
    assert json.loads(json.dumps(record)) == record


@pytest.mark.parametrize(
    ("error_type", "category"),
    [
        (TimeoutError, "timeout"),
        (PermissionError, "authentication"),
        (ConnectionError, "network"),
        (ImportError, "dependency"),
        (ValueError, "validation"),
        (RuntimeError, "other"),
    ],
)
def test_failure_has_one_attempt_and_no_exception_message(error_type, category) -> None:
    calls = []

    def fail(fixture):
        calls.append(fixture.fixture_id)
        raise error_type("sensitive-message-must-never-be-exported")

    endpoint = Endpoint(
        "example.invalid", 7709, None, "target known; connection failed"
    )
    record = observe(
        replace(BINDING, endpoint=endpoint), FIXTURE, fail, timestamp=STAMP
    )
    VALIDATOR.validate(record)
    assert calls == [FIXTURE.fixture_id]
    assert not record["success"]
    assert record["exception"]["type"] == error_type.__qualname__
    assert record["exception"]["category"] == category
    assert record["attempted_endpoint"]["host"] == "example.invalid"
    assert record["endpoint"]["host"] is None
    assert "sensitive-message" not in json.dumps(record)


def test_live_opt_in_and_capability_match_precede_execution() -> None:
    calls = []

    def execute(_):
        calls.append(True)
        return Evidence(success=True)

    live = replace(BINDING, mode="live")
    with pytest.raises(PermissionError, match="opt-in"):
        observe(live, FIXTURE, execute)
    with pytest.raises(ValueError, match="capability"):
        observe(
            replace(BINDING, capability=Capability.QUOTE_SNAPSHOT), FIXTURE, execute
        )
    assert not calls
    VALIDATOR.validate(
        observe(live, FIXTURE, execute, allow_live=True, timestamp=STAMP)
    )
    assert calls == [True]


def test_quality_failure_and_unknowns_are_not_reported_as_complete() -> None:
    record = observe(
        BINDING,
        FIXTURE,
        lambda _: Evidence(success=False, row_count=1, missing_fields=("volume",)),
        timestamp=STAMP,
    )
    VALIDATOR.validate(record)
    assert record["coverage"]["complete"] is None
    assert record["pagination"]["exhausted"] is None
    assert record["units"] == {}
    assert record["semantics"] == {}
    assert record["exception"] is None


def test_invalid_executor_result_is_a_classified_failure() -> None:
    record = observe(BINDING, FIXTURE, lambda _: {"raw": "discarded"}, timestamp=STAMP)
    VALIDATOR.validate(record)
    assert record["exception"]["category"] == "validation"
    assert "raw" not in record


def test_schema_rejects_unstructured_and_invalid_provenance() -> None:
    record = observe(BINDING, FIXTURE, lambda _: Evidence(True), timestamp=STAMP)
    for changed in (
        dict(record, raw_exception_message="not allowed"),
        dict(record, latency_ms=-1),
        dict(record, observed_at="not-a-timestamp"),
        dict(record, capability="unknown"),
    ):
        with pytest.raises(ValidationError):
            VALIDATOR.validate(changed)
    with pytest.raises(ValueError, match="timezone"):
        observe(
            BINDING, FIXTURE, lambda _: Evidence(True), timestamp=datetime(2024, 1, 2)
        )
    with pytest.raises(ValueError, match="authentication"):
        Endpoint("https://user:password@example.invalid")


def test_fixture_loader_rejects_duplicate_ids(tmp_path) -> None:
    content = (ROOT / "provider_lab/fixtures.toml").read_text()
    duplicate = content.split("[[fixtures]]")[1]
    path = tmp_path / "fixtures.toml"
    path.write_text(content + "\n[[fixtures]]" + duplicate)
    with pytest.raises(ValueError, match="unique"):
        load_fixtures(path)


@pytest.mark.parametrize(
    "fields",
    [
        {"row_count": -1},
        {"row_count": True},
        {"complete": "unknown"},
        {"returned_symbols": ("600000.XSHG", "600000.XSHG")},
        {"units": {"price": 1000}},
        {"observed_start": "2024-01-02"},
    ],
)
def test_invalid_evidence_becomes_a_safe_failure(fields) -> None:
    record = observe(
        BINDING, FIXTURE, lambda _: Evidence(True, **fields), timestamp=STAMP
    )
    VALIDATOR.validate(record)
    assert record["success"] is False
    assert record["exception"]["category"] == "validation"
