"""Reproduce SDK + tagged I-4 + default Go race tests without network or main edits."""

import argparse
import json
import os
import runpy
import shutil
import subprocess
from hashlib import sha256
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-checkout", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--module-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    builder = runpy.run_path(str(root / "scripts/build_tdx_driver.py"))
    sdk = args.sdk_checkout.resolve()
    builder["validate_checkout"](sdk, "v1.0.0")
    go = str(args.go.resolve())
    if subprocess.check_output([go, "version"], text=True).split()[2] != "go1.25.3":
        raise ValueError("Go baseline mismatch")
    patch = root / "drivers/tdx/sdk-wait-preregister.patch"
    for path, expected in (
        (sdk / "client.go", builder["CLIENT_SHA256"]),
        (patch, builder["PATCH_SHA256"]),
    ):
        if sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("Pinned input mismatch")
    if ".." in args.output.parts:
        raise ValueError("Output parent traversal rejected")
    work = args.output.absolute()
    if not work.is_relative_to(root / "artifacts"):
        raise ValueError("Ignored artifact destination required")
    if any(path.is_symlink() for path in (work, *work.parents)):
        raise ValueError("Output symlink rejected")
    work.mkdir(parents=True, exist_ok=False)
    overlay = work / "sdk"
    build = work / "driver"
    build.mkdir()
    tracked = (
        subprocess.check_output(["git", "-C", str(sdk), "ls-files", "-z"])
        .decode()
        .split("\0")
    )
    for name in filter(None, tracked):
        target = overlay / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(sdk / name, target)
    subprocess.run(["git", "apply", str(patch)], cwd=overlay, check=True)
    if (
        sha256((overlay / "client.go").read_bytes()).hexdigest()
        != builder["DERIVED_CLIENT_SHA256"]
    ):
        raise ValueError("Derived SDK mismatch")
    sources = sorted((root / "drivers/tdx").glob("*.go"))
    inputs = {file.name: sha256(file.read_bytes()).hexdigest() for file in sources}
    for file in sources:
        shutil.copyfile(file, build / file.name)
    module = (
        (sdk / "go.mod")
        .read_text()
        .replace("module github.com/injoyai/tdx", "module mdf.local/tdx-driver", 1)
    )
    (build / "go.mod").write_text(
        module
        + "\nrequire github.com/injoyai/tdx v1.0.0\n"
        + "replace github.com/injoyai/tdx => ../sdk\n"
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
        GOCACHE="/tmp/mdf-go-build",
        MDF_TDX_TEST_SDK_VERSION="v1.0.0",
    )
    report = {
        "sdk_commit": builder["REVISIONS"]["v1.0.0"],
        "network": "disabled",
        "inputs": inputs,
        "commands": [],
        "result": "INCOMPLETE",
        "main_dispatch_present": "if I4Dispatch()" in (build / "main.go").read_text(),
        "binary_built": False,
        "provider_connections": 0,
    }
    try:
        for where, extra, target in (
            (overlay, [], "./protocol"),
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
            report["commands"].append(command)
            result = subprocess.run(command, cwd=where, env=env)
            report["last_exit"] = result.returncode
            if result.returncode:
                report["result"] = "FAIL"
                return result.returncode
        report["result"] = "PASS"
        return 0
    finally:
        current = {file.name: sha256(file.read_bytes()).hexdigest() for file in sources}
        report["source_unchanged_during_test"] = current == inputs
        if current != inputs:
            report["result"] = "SOURCE_CHANGED"
        (work / "receipt.json").write_text(json.dumps(report, indent=2) + "\n")
        if current != inputs:
            raise RuntimeError("Source changed during offline QA")


if __name__ == "__main__":
    raise SystemExit(main())
