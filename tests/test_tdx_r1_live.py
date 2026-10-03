"""Only explicit live selection launches an isolated R1 SDK subprocess."""

import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.live_provider
@pytest.mark.parametrize("implementation", ["tdx-python", "pytdxdata", "mootdx-plus"])
def test_common_endpoint_recent_daily(implementation):
    interpreter = ROOT / "artifacts/tdx-r1/runtime/.venv/bin/python"
    if not interpreter.exists():
        pytest.skip("requires separately provisioned ignored R1 SDK environment")
    output = ROOT / "artifacts/tdx-r1/live-test" / str(uuid4()) / implementation
    output.mkdir(parents=True)
    env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
    spec = importlib.util.spec_from_file_location(
        "tdx_bounds", ROOT / "provider_lab/tdx_bounds.py"
    )
    bounds = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bounds)
    original_pythonpath = os.environ.get("PYTHONPATH")
    os.environ["PYTHONPATH"] = env["PYTHONPATH"]
    try:
        result = bounds.run_guarded(
            [
                str(interpreter),
                str(ROOT / "provider_lab/tdx_r1_probe.py"),
                "--live-provider",
                "--implementation",
                implementation,
                "--output",
                str(output),
            ],
            output,
            hosts={"139.9.52.158"},
            max_attempts=8,
            phase_attempts=8,
            wall_seconds=30,
            phase_seconds=30,
        )
    finally:
        if original_pythonpath is None:
            os.environ.pop("PYTHONPATH", None)
        else:
            os.environ["PYTHONPATH"] = original_pythonpath
    record = json.loads((output / "observation.json").read_text())
    schema = json.loads((ROOT / "provider_lab/observation.schema.json").read_text())
    Draft202012Validator(schema, format_checker=FormatChecker()).validate(record)
    assert result["process_exit"] == 0 and result["stop"] is None, record["exception"]
    assert record["success"] and record["coverage"]["row_count"] == 2
    assert record["attempted_endpoint"]["host"] == "139.9.52.158"
    if implementation == "tdx-python":
        assert record["endpoint"]["host"] is None
    else:
        assert record["endpoint"]["host"] == "139.9.52.158"
