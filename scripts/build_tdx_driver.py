"""Build/test a declared derived pinned SDK in a temporary overlay, offline."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

REVISION = "51c1ed51020fa43727300339dd92cbecf06a7d2e"
CLIENT_SHA256 = "003444d252c22328d45563c4c85115a07d7ccf1476d0dd206551bbfe7d19658b"
PATCH_SHA256 = "4fea3baa0d53b4a9ba6e1e788c522345c5a8808200072c2487893cddea90eedc"
DERIVATION = "mdf-wait-preregister-r1"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-checkout", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--module-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--race", action="store_true", help="Go race detector in tests")
    args = parser.parse_args()
    sdk = args.sdk_checkout.resolve()
    go = str(args.go.resolve())
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path = output.with_suffix(".build.json")
    report = {
        "state": "validating",
        "upstream_revision": REVISION,
        "derivation": DERIVATION,
        "patch_sha256": PATCH_SHA256,
        "client_input_sha256": CLIENT_SHA256,
        "network": "disabled",
    }
    env = dict(
        os.environ,
        GOPROXY="off",
        GOSUMDB="off",
        GOTOOLCHAIN="local",
        GOMODCACHE=str(args.module_cache.resolve()),
        GOCACHE="/tmp/mdf-go-build",
    )
    try:
        revision = subprocess.check_output(
            ["git", "-C", str(sdk), "rev-parse", "HEAD"], text=True
        ).strip()
        if revision != REVISION:
            raise ValueError("SDK revision mismatch")
        if subprocess.check_output(["git", "-C", str(sdk), "diff", "HEAD", "--"]):
            raise ValueError("Tracked SDK modifications rejected")
        if (
            subprocess.check_output([go, "version"], env=env, text=True).split()[2]
            != "go1.25.3"
        ):
            raise ValueError("Go toolchain mismatch")
        source = Path(__file__).resolve().parents[1] / "drivers" / "tdx"
        report["driver_inputs"] = {
            file.name: sha256(file.read_bytes()).hexdigest()
            for file in sorted(source.glob("*.go"))
        }
        report["module_inputs"] = {
            name: sha256((sdk / name).read_bytes()).hexdigest()
            for name in ("go.mod", "go.sum")
        }
        report["race_detector"] = args.race
        patch = source / "sdk-wait-preregister.patch"
        if sha256(patch.read_bytes()).hexdigest() != PATCH_SHA256:
            raise ValueError("Patch bytes mismatch")
        if sha256((sdk / "client.go").read_bytes()).hexdigest() != CLIENT_SHA256:
            raise ValueError("SDK patch input bytes mismatch")
        with tempfile.TemporaryDirectory(prefix="mdf-tdx-build-") as directory:
            build = Path(directory) / "driver"
            overlay = Path(directory) / "sdk"
            build.mkdir()
            # Copy tracked inputs only. Original checkout and private untracked
            # commands are never changed or included in the derived SDK.
            tracked = (
                subprocess.check_output(["git", "-C", str(sdk), "ls-files", "-z"])
                .decode()
                .split("\0")
            )
            for name in filter(None, tracked):
                target = overlay / name
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(sdk / name, target)
            subprocess.run(
                ["git", "apply", "--check", str(patch)], cwd=overlay, check=True
            )
            subprocess.run(["git", "apply", str(patch)], cwd=overlay, check=True)
            report["client_derived_sha256"] = sha256(
                (overlay / "client.go").read_bytes()
            ).hexdigest()
            for file in source.glob("*.go"):
                shutil.copyfile(file, build / file.name)
            module = (
                (sdk / "go.mod")
                .read_text()
                .replace(
                    "module github.com/injoyai/tdx", "module mdf.local/tdx-driver", 1
                )
            )
            module += "\nrequire github.com/injoyai/tdx v0.0.87\n"
            module += "replace github.com/injoyai/tdx => ../sdk\n"
            (build / "go.mod").write_text(module)
            shutil.copyfile(sdk / "go.sum", build / "go.sum")
            report["state"] = "testing"
            report_path.write_text(json.dumps(report, indent=2) + "\n")
            subprocess.run(
                [
                    go,
                    "test",
                    "-mod=readonly",
                    "-count=1",
                    *(["-race"] if args.race else []),
                    "./...",
                ],
                cwd=build,
                env=env,
                check=True,
            )
            report["tests"] = "passed"
            subprocess.run(
                [
                    go,
                    "build",
                    "-mod=readonly",
                    "-trimpath",
                    "-buildvcs=false",
                    "-o",
                    str(output),
                    ".",
                ],
                cwd=build,
                env=env,
                check=True,
            )
            report.update(
                state="built", binary_sha256=sha256(output.read_bytes()).hexdigest()
            )
    except Exception as exc:
        report.update(state="failed", exception_classification=type(exc).__name__)
        raise
    finally:
        report_path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"Derived pinned Go driver built: {args.output}; identity: {report_path}")


if __name__ == "__main__":
    main()
