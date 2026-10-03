"""R1 taxonomy and adjustment contracts using only synthetic evidence."""

import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

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
EXTENSIONS = (
    Capability.SECTOR_INDUSTRY,
    Capability.SECTOR_CONCEPT,
    Capability.SECTOR_MEMBERSHIP,
    Capability.ADJUSTMENT_FACTORS,
    Capability.BARS_ADJUSTED,
)


@pytest.mark.parametrize("capability", EXTENSIONS)
def test_r1_capability_fixture_and_observation(capability) -> None:
    fixture = next(f for f in FIXTURES if f.capability == capability)
    binding = Binding("synthetic", "r1-contract", "1", capability, "offline")
    record = observe(
        binding, fixture, lambda _: Evidence(True, semantics=dict(fixture.semantics))
    )
    schema = json.loads((ROOT / "provider_lab/observation.schema.json").read_text())
    Draft202012Validator(schema).validate(record)
    assert record["capability"] == capability.value
    if capability in EXTENSIONS[:3]:
        assert record["semantics"]["taxonomy_source"] == "TDX"
        assert "taxonomy_identity" in record["semantics"]
    else:
        assert record["semantics"]["adjustment"] == "qfq"
        assert "native from derived" in record["semantics"]["origin"]


def test_raw_actions_factors_and_adjusted_bars_are_distinct() -> None:
    capabilities = {
        Capability.CORPORATE_ACTIONS,
        Capability.ADJUSTMENT_FACTORS,
        Capability.BARS_ADJUSTED,
    }
    assert len(capabilities) == 3
    assert capabilities <= {f.capability for f in FIXTURES}


def test_attempted_and_connected_endpoint_are_distinct() -> None:
    fixture = FIXTURES[0]
    attempted = Endpoint("attempted.invalid", 7709, None, None)
    actual = Endpoint("connected.invalid", 7709, "synthetic-observed", None)
    binding = Binding(
        "synthetic", "r1-contract", "1", fixture.capability, "offline", attempted
    )
    record = observe(binding, fixture, lambda _: Evidence(True, endpoint=actual))
    schema = json.loads((ROOT / "provider_lab/observation.schema.json").read_text())
    Draft202012Validator(schema).validate(record)
    assert record["attempted_endpoint"]["host"] == "attempted.invalid"
    assert record["endpoint"]["host"] == "connected.invalid"
    assert record["schema_version"] == 2


def test_snapshot_f10_and_financial_statements_are_distinct() -> None:
    capabilities = {
        Capability.FINANCE_SNAPSHOT,
        Capability.FINANCE_F10,
        Capability.FINANCIAL_STATEMENTS,
    }
    assert len(capabilities) == 3
    assert capabilities <= {f.capability for f in FIXTURES}
    for capability in capabilities - {Capability.FINANCIAL_STATEMENTS}:
        fixture = next(f for f in FIXTURES if f.capability == capability)
        assert fixture.start is None and fixture.end is None
        assert "not" in fixture.semantics["history"]
