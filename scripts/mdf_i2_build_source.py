"""Offline build of independent I-2 discovery source; grants no runtime trust."""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
from hashlib import sha256
from pathlib import Path

from build_tdx_driver import REVISIONS, validate_checkout


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sdk-checkout", type=Path, required=True)
    parser.add_argument("--go", type=Path, required=True)
    parser.add_argument("--module-cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sdk = args.sdk_checkout.resolve()
    validate_checkout(sdk, "v1.0.0")
    source = Path(__file__).parent
    root = Path.cwd() / "artifacts"
    output = args.output.absolute()
    if (
        not output.resolve().is_relative_to(root.resolve())
        or output.is_symlink()
        or output.exists()
        or output.with_suffix(".build.json").exists()
    ):
        raise ValueError("new_private_output_required")
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = dict(
        os.environ,
        GOPROXY="off",
        GOSUMDB="off",
        GOTOOLCHAIN="local",
        GOENV="off",
        GOFLAGS="",
        GOWORK="off",
        GOMODCACHE=str(args.module_cache.resolve()),
        GOCACHE="/tmp/mdf-i2-go-build",
    )
    go = str(args.go.resolve())
    if subprocess.check_output([go, "version"], env=env).split()[2] != b"go1.25.3":
        raise ValueError("toolchain_mismatch")
    report = {
        "source": "tdx-v100-protocol-discovery-i2-r1",
        "upstream_revision": REVISIONS["v1.0.0"],
        "dependency_network": "GOPROXY=off,GOSUMDB=off",
        "test_transport": "in_memory_only",
        "driver_trust": "INDEPENDENT_CANDIDATE_REQUIRES_HOST_REVIEW",
        "inputs": {},
    }
    with tempfile.TemporaryDirectory(prefix="mdf-i2-build-") as directory:
        build = Path(directory)
        # Export only the pinned Git tree. An untracked Go init() in the SDK
        # checkout must never enter go test or go build before Host review.
        pinned_sdk = build / "pinned-sdk"
        pinned_sdk.mkdir(mode=0o700)
        archive = subprocess.check_output(
            ["git", "-C", str(sdk), "archive", "--format=tar", REVISIONS["v1.0.0"]],
            timeout=30,
        )
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as members:
            members.extractall(pinned_sdk, filter="data")
        hosts_raw = (pinned_sdk / "hosts.go").read_bytes()
        hosts = ",".join(
            re.findall(r'"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)"', hosts_raw.decode())
        )
        if not hosts:
            raise ValueError("pinned_host_evidence_missing")
        link_flags = (
            f"-X main.verifiedHosts={hosts} "
            f"-X mdf.local/i2-discovery.verifiedHosts={hosts}"
        )
        report["endpoint_evidence_sha256"] = sha256(hosts_raw).hexdigest()
        report["sdk_archive_sha256"] = sha256(archive).hexdigest()
        report["sdk_build_input"] = "isolated_pinned_git_archive"
        # Only the protocol package is imported. No Client or waiter is used.
        for name in ("mdf_i2_source.go", "mdf_i2_source_test.go"):
            payload = (source / name).read_bytes()
            report["inputs"][name] = sha256(payload).hexdigest()
            (build / name).write_bytes(payload)
        module = (
            (pinned_sdk / "go.mod")
            .read_text()
            .replace(
                "module github.com/injoyai/tdx", "module mdf.local/i2-discovery", 1
            )
        )
        module += "\nrequire github.com/injoyai/tdx v1.0.0\n"
        module += f"replace github.com/injoyai/tdx => {pinned_sdk}\n"
        (build / "go.mod").write_text(module)
        shutil.copyfile(pinned_sdk / "go.sum", build / "go.sum")
        subprocess.run(
            [
                go,
                "test",
                "-mod=readonly",
                "-race",
                "-count=1",
                "-timeout=60s",
                "-ldflags",
                link_flags,
                ".",
            ],
            cwd=build,
            env=env,
            check=True,
        )
        subprocess.run(
            [
                go,
                "build",
                "-mod=readonly",
                "-trimpath",
                "-buildvcs=false",
                "-ldflags",
                link_flags,
                "-o",
                str(build / "source"),
                ".",
            ],
            cwd=build,
            env=env,
            check=True,
        )
        # Recheck checkout before materializing a candidate; no adjacent sidecar trust.
        validate_checkout(sdk, "v1.0.0")
        payload = (build / "source").read_bytes()
        with output.open("xb") as stream:
            os.fchmod(stream.fileno(), 0o500)
            stream.write(payload)
        report.update(binary_sha256=sha256(payload).hexdigest(), tests="passed")
    with output.with_suffix(".build.json").open("x") as stream:
        json.dump(report, stream, indent=2)
    print(json.dumps({"candidate_sha256": report["binary_sha256"]}))


if __name__ == "__main__":
    main()
