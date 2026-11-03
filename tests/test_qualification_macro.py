"""Macro qualification is canonical without expanding frozen securities scope."""

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker, ValidationError

from market_data_foundation.qualification import (
    Binding,
    Capability,
    Endpoint,
    Evidence,
    MacroCapability,
    load_fixtures,
    load_macro_fixtures,
    observe,
)

ROOT = Path(__file__).resolve().parents[1]
MACRO_PATH = ROOT / "provider_lab/macro-fixtures.toml"
LEGACY_PATH = ROOT / "provider_lab/fixtures.toml"
SCHEMA = json.loads((ROOT / "provider_lab/macro-observation.schema.json").read_text())
LEGACY_SCHEMA = json.loads((ROOT / "provider_lab/observation.schema.json").read_text())
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
LEGACY_VALIDATOR = Draft202012Validator(LEGACY_SCHEMA, format_checker=FormatChecker())
FIXTURE = load_macro_fixtures(MACRO_PATH)[0]
BINDING = Binding("BLS", "synthetic-macro", "r1", FIXTURE.capability, "offline")
STAMP = datetime(2026, 11, 3, tzinfo=UTC)


def test_scoped_fixture_and_schema_preserve_legacy_contract():
    Draft202012Validator.check_schema(SCHEMA)
    assert FIXTURE.capability is MacroCapability.TIMESERIES
    assert len(FIXTURE.symbols) == 8
    assert len(Capability) == 16
    assert set(LEGACY_SCHEMA["properties"]["capability"]["enum"]) == set(Capability)
    assert {f.capability for f in load_fixtures(LEGACY_PATH)} == set(Capability)
    assert set(SCHEMA["properties"]["capability"]["enum"]) == set(MacroCapability)
    with pytest.raises(ValueError, match="scope"):
        load_fixtures(MACRO_PATH)
    with pytest.raises(ValueError, match="scope"):
        load_macro_fixtures(LEGACY_PATH)


def test_macro_and_legacy_observations_use_separate_schema_versions():
    record = observe(
        BINDING,
        FIXTURE,
        lambda _: Evidence(
            True,
            returned_symbols=FIXTURE.symbols,
            row_count=8,
            complete=True,
            units={"rate": "percent", "openings": "thousand_jobs"},
            endpoint=Endpoint("example.invalid", 443),
        ),
        timestamp=STAMP,
    )
    VALIDATOR.validate(record)
    assert record["schema_version"] == 3
    assert record["capability_scope"] == "macro"
    assert record["coverage"]["requested_symbols"] == list(FIXTURE.symbols)
    with pytest.raises(ValidationError):
        LEGACY_VALIDATOR.validate(record)
    legacy_fixture = load_fixtures(LEGACY_PATH)[0]
    legacy = observe(
        replace(BINDING, capability=legacy_fixture.capability),
        legacy_fixture,
        lambda _: Evidence(True),
        timestamp=STAMP,
    )
    LEGACY_VALIDATOR.validate(legacy)
    assert legacy["schema_version"] == 2
    assert "capability_scope" not in legacy
    with pytest.raises(ValidationError):
        VALIDATOR.validate(legacy)


def test_live_opt_in_and_scope_mismatch_precede_any_execution():
    calls = []

    def execute(_):
        calls.append(True)
        return Evidence(True)

    live = replace(BINDING, mode="live")
    with pytest.raises(PermissionError, match="opt-in"):
        observe(live, FIXTURE, execute)
    with pytest.raises(ValueError, match="capability"):
        observe(replace(BINDING, capability=Capability.BARS_DAILY), FIXTURE, execute)
    assert calls == []
    VALIDATOR.validate(
        observe(live, FIXTURE, execute, allow_live=True, timestamp=STAMP)
    )
    assert calls == [True]


@pytest.mark.parametrize(
    ("error_type", "category"),
    [(TimeoutError, "timeout"), (PermissionError, "authentication")],
)
def test_macro_failures_preserve_classification_without_messages(error_type, category):
    def fail(_):
        raise error_type("synthetic-sensitive-message")

    record = observe(BINDING, FIXTURE, fail, timestamp=STAMP)
    VALIDATOR.validate(record)
    assert record["success"] is False
    assert record["exception"]["category"] == category
    assert record["exception"]["type"] == error_type.__name__
    assert "synthetic-sensitive-message" not in json.dumps(record)


def test_macro_schema_rejects_unscoped_or_raw_exception_records():
    record = observe(BINDING, FIXTURE, lambda _: Evidence(False), timestamp=STAMP)
    unscoped = dict(record)
    unscoped.pop("capability_scope")
    for invalid in (
        unscoped,
        dict(record, capability_scope="securities"),
        dict(record, schema_version=2),
        dict(record, raw_exception_message="synthetic-sensitive-message"),
    ):
        with pytest.raises(ValidationError):
            VALIDATOR.validate(invalid)


@pytest.mark.parametrize(
    "change",
    [
        ('capability_scope = "macro"', 'capability_scope = "other"'),
        ('capability = "macro.timeseries"', 'capability = "bars.daily"'),
    ],
)
def test_macro_loader_rejects_foreign_scopes_and_capabilities(tmp_path, change):
    path = tmp_path / "macro.toml"
    path.write_text(MACRO_PATH.read_text().replace(*change))
    with pytest.raises(ValueError):
        load_macro_fixtures(path)
