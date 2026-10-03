"""Opt-in, process-bounded reconciliation retests, never a production binding."""

import argparse
import json
from pathlib import Path

from tdx_bounds import run_guarded

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    "bj-go": ("tdx-python", "bj", 12, 4, 25, 8),
    "bj-py": ("pytdxdata", "bj", 16, 5, 30, 8),
    "bj-mootdx": ("mootdx-plus", "bj", 12, 4, 25, 8),
    "bj-aggregate": ("tdx-python", "bj-aggregate", 8, 4, 20, 8),
    "py-default": ("pytdxdata", "py-default", 20, 8, 30, 8),
    "py-good-dead": ("pytdxdata", "py-good-dead", 16, 5, 30, 8),
    "py-dead-only": ("pytdxdata", "py-dead-only", 8, 3, 20, 8),
    "go-redial-off": ("tdx-python", "go-redial-off", 2, 2, 15, 8),
    "go-redial-on": ("tdx-python", "go-redial-on", 2, 2, 15, 8),
    "go-close": ("tdx-python", "go-close", 4, 3, 20, 8),
    "mootdx-raising": ("mootdx-plus", "mootdx-raising", 10, 4, 25, 8),
    "mootdx-nonraising": ("mootdx-plus", "mootdx-nonraising", 10, 4, 25, 8),
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live-provider", action="store_true")
    parser.add_argument("--case", choices=CASES, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.live_provider:
        parser.error("explicit --live-provider opt-in required")
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / "artifacts/tdx-r1") or output.exists():
        parser.error("new unique ignored R1 output directory required")
    name, case, attempts, phase_attempts, wall, phase_wall = CASES[args.case]
    interpreter = ROOT / "artifacts/tdx-r1/runtime/.venv/bin/python"
    if not interpreter.exists():
        parser.error("requires separately provisioned exact R1 SDK environment")
    hosts = set() if args.case == "py-default" else {"139.9.52.158", "124.70.176.52"}
    if args.case.startswith("bj"):
        hosts = {"139.9.52.158"}
    command = [
        str(interpreter),
        "-B",
        str(ROOT / "provider_lab/tdx_r1_worker.py"),
        "--live-provider",
        "--case",
        case,
        "--implementation",
        name,
        "--output",
        str(output),
    ]
    result = run_guarded(
        command,
        output,
        hosts=hosts,
        max_attempts=attempts,
        phase_attempts=phase_attempts,
        wall_seconds=wall,
        phase_seconds=phase_wall,
    )
    print(
        json.dumps(
            {
                "case": args.case,
                "stop": result["stop"],
                "exit": result.get("process_exit"),
                "attempts": result["connect_attempts_sent"],
                "elapsed": round(result["elapsed_seconds"], 3),
            }
        )
    )
    if result.get("process_exit") not in (0, -9):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
