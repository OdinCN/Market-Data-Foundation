"""Build/test a declared derived pinned SDK in a temporary overlay, offline."""

import argparse
import json
import os
import shutil
import subprocess
import tempfile
from hashlib import sha256
from pathlib import Path

REVISIONS = {
    "v0.0.87": "51c1ed51020fa43727300339dd92cbecf06a7d2e",
    "v1.0.0": "2a9000681f5a9896ea98e4de8bc4904f1dce78ba",
}
CLIENT_SHA256 = "003444d252c22328d45563c4c85115a07d7ccf1476d0dd206551bbfe7d19658b"
PATCH_SHA256 = "4fea3baa0d53b4a9ba6e1e788c522345c5a8808200072c2487893cddea90eedc"
DERIVED_CLIENT_SHA256 = (
    "5c14068d5ff48a77f969ecd5b68079be803b225815a9ad8d1ecf3a84d8ca8d28"
)
DERIVATION = "mdf-wait-preregister-r1"


def validate_checkout(sdk: Path, version: str) -> None:
    for ref in ("HEAD", f"{version}^{{commit}}"):
        revision = subprocess.check_output(
            ["git", "-C", str(sdk), "rev-parse", ref], text=True
        ).strip()
        if revision != REVISIONS[version]:
            raise ValueError("SDK revision/tag mismatch")
    if subprocess.check_output(["git", "-C", str(sdk), "diff", "HEAD", "--"]):
        raise ValueError("Tracked SDK modifications rejected")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-checkout", type=Path, required=True)
    parser.add_argument("--sdk-version", choices=REVISIONS, default="v1.0.0")
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--module-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--race", action="store_true", help="Go race detector in tests")
    args = parser.parse_args()
    sdk = args.sdk_checkout.resolve()
    go = str(args.go.resolve())
    # Reject aliases and existing destinations, including dangling symlinks.
    if args.output.is_symlink():
        raise ValueError("Output symlink rejected")
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    report_path = output.with_suffix(".build.json")
    if output.exists() or report_path.exists() or report_path.is_symlink():
        raise FileExistsError("Build destinations must be new; rollback is immutable")
    release = (
        f"injoyai/tdx {args.sdk_version}@{REVISIONS[args.sdk_version]}+{DERIVATION}"
    )
    report = {
        "state": "validating",
        "sdk_version": args.sdk_version,
        "sdk_release": release,
        "upstream_revision": REVISIONS[args.sdk_version],
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
        GOENV="off",
        GOFLAGS="",
        GOWORK="off",
        MDF_TDX_TEST_SDK_VERSION=args.sdk_version,
        GOMODCACHE=str(args.module_cache.resolve()),
        GOCACHE="/tmp/mdf-go-build",
    )
    report_stream = report_path.open("x")
    try:
        validate_checkout(sdk, args.sdk_version)
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
            if report["client_derived_sha256"] != DERIVED_CLIENT_SHA256:
                raise ValueError("SDK patch output bytes mismatch")
            for file in source.glob("*.go"):
                shutil.copyfile(file, build / file.name)
            module = (
                (sdk / "go.mod")
                .read_text()
                .replace(
                    "module github.com/injoyai/tdx", "module mdf.local/tdx-driver", 1
                )
            )
            module += f"\nrequire github.com/injoyai/tdx {args.sdk_version}\n"
            module += "replace github.com/injoyai/tdx => ../sdk\n"
            (build / "go.mod").write_text(module)
            shutil.copyfile(sdk / "go.sum", build / "go.sum")
            report["state"] = "testing"
            flags = ["-mod=readonly", "-count=1", "-timeout=120s"]
            if args.race:
                flags.append("-race")
            # Root SDK tests can dial providers. Only the reviewed protocol
            # package and our memory-pipe driver suite are executed offline.
            for cwd, package in ((overlay, "./protocol"), (build, "./...")):
                subprocess.run(
                    [
                        go,
                        "test",
                        *flags,
                        "-ldflags",
                        f"-X 'main.release={release}'",
                        package,
                    ],
                    cwd=cwd,
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
                    "-ldflags",
                    f"-X 'main.release={release}'",
                    "-o",
                    str(build / "tdx-driver"),
                    ".",
                ],
                cwd=build,
                env=env,
                check=True,
            )
            candidate = build / "tdx-driver"
            if (
                subprocess.check_output(
                    [str(candidate), "--version"], text=True
                ).strip()
                != release
            ):
                raise ValueError("Built driver identity mismatch")
            with candidate.open("rb") as binary, output.open("xb") as stream:
                shutil.copyfileobj(binary, stream)
                os.fchmod(stream.fileno(), 0o700)
            report.update(
                state="built", binary_sha256=sha256(output.read_bytes()).hexdigest()
            )
    except Exception as exc:
        report.update(state="failed", exception_classification=type(exc).__name__)
        raise
    finally:
        with report_stream:
            report_stream.write(json.dumps(report, indent=2) + "\n")
    print(f"Derived pinned Go driver built: {args.output}; identity: {report_path}")


if __name__ == "__main__":
    main()
