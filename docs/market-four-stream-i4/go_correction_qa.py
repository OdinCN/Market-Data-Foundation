"""Offline correction QA against the retained derived SDK, without Git or SDK edits."""

import argparse
import hashlib
import json
import os
import runpy
import shutil
import subprocess
from pathlib import Path


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-overlay", required=True, type=Path)
    parser.add_argument("--go", required=True, type=Path)
    parser.add_argument("--module-cache", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    builder = runpy.run_path(str(root / "scripts/build_tdx_driver.py"))
    sdk = args.sdk_overlay.resolve()
    if digest(sdk / "client.go") != builder["DERIVED_CLIENT_SHA256"]:
        raise ValueError("Retained derived client mismatch")
    # The entire retained SDK input is recorded before and after the run.
    sdk_inputs = {
        str(p.relative_to(sdk)): digest(p)
        for p in sorted(sdk.rglob("*"))
        if p.is_file()
    }
    go = str(args.go.resolve())
    if subprocess.check_output([go, "version"], text=True).split()[2] != "go1.25.3":
        raise ValueError("Go baseline mismatch")
    work = args.output.absolute()
    if (
        ".." in work.parts
        or not work.is_relative_to(root / "artifacts")
        or work == root / "artifacts"
    ):
        raise ValueError("New ignored output required")
    if any(p.is_symlink() for p in (work, *work.parents)):
        raise ValueError("Symlink output rejected")
    work.mkdir(parents=True, exist_ok=False)
    build = work / "driver"
    build.mkdir()
    sources = sorted((root / "drivers/tdx").glob("*.go"))
    inputs = {str(p.relative_to(root)): digest(p) for p in sources}
    for p in sources:
        shutil.copyfile(p, build / p.name)
    module = (
        (sdk / "go.mod")
        .read_text()
        .replace(
            "module github.com/injoyai/tdx", "module mdf.local/i4-correction-qa", 1
        )
    )
    (build / "go.mod").write_text(
        module
        + "\nrequire github.com/injoyai/tdx v1.0.0\n"
        + f"replace github.com/injoyai/tdx => {sdk}\n"
    )
    shutil.copyfile(sdk / "go.sum", build / "go.sum")
    env = dict(
        os.environ,
        GOPROXY="off",
        GOSUMDB="off",
        GOTOOLCHAIN="local",
        GOENV="off",
        GOFLAGS="",
        GOWORK="off",
        GOMAXPROCS="2",
        GOMODCACHE=str(args.module_cache.resolve()),
        GOCACHE="/tmp/mdf-i4-correction-go-cache",
        MDF_TDX_TEST_SDK_VERSION="v1.0.0",
        MDF_I4_MOCK_RECORDS=str(work / "mock-records.jsonl"),
    )
    report = dict(
        schema="mdf.i4.correction-go-qa.v1",
        sdk_commit=builder["REVISIONS"]["v1.0.0"],
        sdk_inputs=sdk_inputs,
        inputs=inputs,
        commands=[],
        result="INCOMPLETE",
        main_integrated=False,
        runtime_binary_built=False,
        provider_connections=0,
    )
    try:
        for where, extra, target in (
            (sdk, [], "./protocol"),
            (build, ["-tags=mdf_i4"], "./..."),
            (build, [], "./..."),
        ):
            command = [
                go,
                "test",
                "-mod=readonly",
                "-count=1",
                "-timeout=120s",
                "-race",
                "-p=1",
                *extra,
                target,
            ]
            result = subprocess.run(command, cwd=where, env=env, timeout=150)
            report["commands"].append(
                dict(command=command, exit_code=result.returncode)
            )
            if result.returncode:
                report["result"] = "FAIL"
                return result.returncode
        report["result"] = "PASS"
        return 0
    finally:
        report["source_unchanged"] = inputs == {
            str(p.relative_to(root)): digest(p) for p in sources
        }
        report["sdk_unchanged"] = sdk_inputs == {
            str(p.relative_to(sdk)): digest(p)
            for p in sorted(sdk.rglob("*"))
            if p.is_file()
        }
        if not report["source_unchanged"] or not report["sdk_unchanged"]:
            report["result"] = "INPUT_CHANGED"
        (work / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
