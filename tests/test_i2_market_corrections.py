"""Host R1 regressions: invented evidence, memory transports, no live effects."""

import importlib.util
import io
import json
import signal
import subprocess
import sys
import tarfile
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_i2_market_source import calendar, plan

from market_data_foundation.canonical.model import ContractError
from market_data_foundation.qualification import tdx_market_collect as runner
from market_data_foundation.qualification.tdx_market_plan import (
    after_close_gate,
    calendar_gate,
    encoded,
)

NOW = datetime(2026, 10, 22, 8, tzinfo=UTC)


def test_builder_uses_only_pinned_export_despite_untracked_init(tmp_path, monkeypatch):
    scripts = Path(__file__).resolve().parents[1] / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    spec = importlib.util.spec_from_file_location(
        "i2_builder", scripts / "mdf_i2_build_source.py"
    )
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)
    monkeypatch.chdir(tmp_path)
    sdk = tmp_path / "mutable-sdk"
    (sdk / "protocol").mkdir(parents=True)
    (sdk / "protocol" / "untracked.go").write_text(
        'package protocol\nfunc init() { panic("untracked executed") }\n'
    )
    (sdk / "hosts.go").write_text('"203.0.113.10"')
    (sdk / "go.mod").write_text("untrusted mutable module")
    files = {
        "hosts.go": b'package tdx\nvar hosts = []string{"124.71.187.122"}\n',
        "go.mod": b"module github.com/injoyai/tdx\ngo 1.25\n",
        "go.sum": b"",
        "protocol/pinned.go": b"package protocol\n",
    }
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as stream:
        for name, data in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(data)
            stream.addfile(member, io.BytesIO(data))
    calls = []
    monkeypatch.setattr(
        builder, "validate_checkout", lambda *args: calls.append("validate")
    )

    def check_output(command, **kwargs):
        if command[-1] == "version":
            return b"go version go1.25.3 linux/amd64"
        assert command == [
            "git",
            "-C",
            str(sdk),
            "archive",
            "--format=tar",
            builder.REVISIONS["v1.0.0"],
        ]
        calls.append("pinned_archive")
        return archive.getvalue()

    def run(command, *, cwd, env, check):
        assert check and env["GOPROXY"] == "off" and env["GOWORK"] == "off"
        module = (cwd / "go.mod").read_text()
        exported = cwd / "pinned-sdk"
        assert f"replace github.com/injoyai/tdx => {exported}" in module
        assert str(sdk) not in module
        assert {
            p.relative_to(exported).as_posix()
            for p in exported.rglob("*")
            if p.is_file()
        } == set(files)
        assert (exported / "protocol/pinned.go").read_bytes() == files[
            "protocol/pinned.go"
        ]
        assert "203.0.113.10" not in " ".join(command)
        calls.append(command[1])
        if command[1] == "test":
            assert "-race" in command
        else:
            (cwd / "source").write_bytes(b"invented compiled candidate")

    monkeypatch.setattr(builder.subprocess, "check_output", check_output)
    monkeypatch.setattr(builder.subprocess, "run", run)
    output = tmp_path / "artifacts/build/source"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "build",
            "--sdk-checkout",
            str(sdk),
            "--go",
            str(tmp_path / "go"),
            "--module-cache",
            str(tmp_path / "cache"),
            "--output",
            str(output),
        ],
    )
    builder.main()
    report = json.loads(output.with_suffix(".build.json").read_bytes())
    assert calls == ["validate", "pinned_archive", "test", "build", "validate"]
    assert report["sdk_build_input"] == "isolated_pinned_git_archive"
    assert report["sdk_archive_sha256"] == sha256(archive.getvalue()).hexdigest()
    assert report["endpoint_evidence_sha256"] == sha256(files["hosts.go"]).hexdigest()
    assert report["binary_sha256"] == sha256(output.read_bytes()).hexdigest()
    assert (sdk / "protocol/untracked.go").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_after_close_full_frozen_budget_must_fit(enabled):
    p = plan(source_enabled=enabled)
    midnight = datetime(2026, 10, 22, 16, tzinfo=UTC)
    budget = p.budgets["wall_seconds"]
    for seconds in (1, budget - 1, budget):
        with pytest.raises(ContractError, match="after_close_budget_crosses_day"):
            after_close_gate(
                midnight - timedelta(seconds=seconds), p.observation_day, budget
            )
    gate = after_close_gate(
        midnight - timedelta(seconds=budget, microseconds=1), p.observation_day, budget
    )
    assert datetime.fromisoformat(gate["deadline"]) == midnight
    assert p.budgets["wall_seconds"] == (156 if enabled else 36)


@pytest.mark.parametrize(
    "window,clock,close", [("AM", "01:35", "03:30"), ("PM", "05:30", "07:00")]
)
def test_session_full_budget_fits_expiry_and_close(window, clock, close):
    p = plan(
        mode="session", source_enabled=False, quote_rounds=3, session_window=window
    )
    now = datetime.fromisoformat(f"2026-10-23T{clock}:00+00:00")
    gate = json.loads(calendar())
    for seconds in (1, 30, 35, 36):
        gate["expires_at"] = (now + timedelta(seconds=seconds)).isoformat()
        raw = encoded(gate)
        with pytest.raises(ContractError, match="calendar_approval_expires_during_run"):
            calendar_gate(raw, sha256(raw).hexdigest(), p, now)
    gate["expires_at"] = (now + timedelta(seconds=37)).isoformat()
    raw = encoded(gate)
    result = calendar_gate(raw, sha256(raw).hexdigest(), p, now)
    assert result["deadline"] == gate["expires_at"]
    raw = calendar()
    end = datetime.fromisoformat(f"2026-10-23T{close}:00+00:00")
    for seconds in (1, 35, 36):
        with pytest.raises(ContractError, match="session_budget_crosses_close"):
            calendar_gate(
                raw, sha256(raw).hexdigest(), p, end - timedelta(seconds=seconds)
            )
    result = calendar_gate(raw, sha256(raw).hexdigest(), p, end - timedelta(seconds=37))
    assert datetime.fromisoformat(result["deadline"]) == end


def collect_kwargs(tmp_path):
    return dict(
        sdk=Path("unused"),
        candidate=Path("unused"),
        driver=Path("unused"),
        output=tmp_path / "artifacts/run",
        allow_live=True,
        reviewed_collector_sha256="c" * 64,
    )


@pytest.mark.parametrize(
    "interrupt", [KeyboardInterrupt, SystemExit, runner.TerminationRequested]
)
def test_interruption_retains_each_chunk_and_unknown_before_propagation(
    tmp_path, monkeypatch, interrupt
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    monkeypatch.setattr(runner, "verify_endpoint", lambda *args: None)
    previous_handler = signal.getsignal(signal.SIGTERM)
    chunks = [b'{"completed":1}\n', b'{"incomplete":']
    output = tmp_path / "artifacts/run"
    child = SimpleNamespace(stdin=io.BytesIO(), stdout=io.BytesIO(), poll=lambda: None)
    calls = []
    child.kill = lambda: calls.append("kill")
    child.wait = lambda **kw: calls.append("wait")
    child.stdout = SimpleNamespace(fileno=lambda: 123456, close=lambda: None)
    monkeypatch.setattr(
        runner.subprocess, "Popen", lambda *a, **kw: calls.append("spawn") or child
    )
    monkeypatch.setattr(
        runner,
        "DeadlineFramedProcess",
        lambda *a, **kw: pytest.fail("quote after cancellation"),
    )

    @contextmanager
    def sealed(*args):
        yield "invented-no-exec", 123

    monkeypatch.setattr(runner, "sealed_candidate", sealed)
    read = iter(chunks)
    original_read = runner.os.read
    monkeypatch.setattr(
        runner.os,
        "read",
        lambda fd, size: next(read) if fd == 123456 else original_read(fd, size),
    )

    class Selector:
        n = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def register(self, *args):
            pass

        def select(self, wait):
            # Each prefix must already be durable before the next receive.
            assert (output / "source-stdout-progress.jsonl").read_bytes() == b"".join(
                chunks[: self.n]
            )
            if self.n == len(chunks):
                if interrupt is runner.TerminationRequested:
                    signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
                raise interrupt("private interruption detail")
            self.n += 1
            return [True]

    monkeypatch.setattr(runner.selectors, "DefaultSelector", Selector)
    with pytest.raises(interrupt):
        runner.collect(plan(), **collect_kwargs(tmp_path))
    ledger = json.loads((output / "effect-ledger.json").read_bytes())
    assert ledger["terminal"] == "STOPPED"
    assert ledger["exception_classification"] == interrupt.__name__
    assert ledger["source"] == "ATTEMPT_STARTED_EFFECT_UNKNOWN_UNTIL_RECEIPT"
    assert ledger["quote"] == "NOT_ATTEMPTED" and ledger["provider_retries"] == 0
    assert calls == ["spawn", "kill", "wait"]
    assert signal.getsignal(signal.SIGTERM) is previous_handler
    assert (output / "source-stdout-partial.jsonl").read_bytes() == b"".join(chunks)
    assert ledger["materials"][0]["sha256"] == sha256(b"".join(chunks)).hexdigest()
    assert (
        "private interruption detail" not in (output / "effect-ledger.json").read_text()
    )
    intent = json.loads((output / "source-intent.json").read_bytes())
    assert intent["source"].endswith("EFFECT_UNKNOWN_UNTIL_RECEIPT")


def test_source_rechecks_full_budget_after_sealing_before_spawn(monkeypatch):
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)

    @contextmanager
    def sealed(*args):
        yield "unused", 1

    monkeypatch.setattr(runner, "sealed_candidate", sealed)
    monkeypatch.setattr(
        runner.subprocess,
        "Popen",
        lambda *a, **kw: pytest.fail("spawn after budget no longer fits"),
    )
    with pytest.raises(ContractError, match="source_budget_crosses_deadline"):
        runner.source_once(
            plan(), Path("unused"), deadline=NOW + timedelta(seconds=155)
        )


def test_quote_deadline_caps_io_and_prevents_late_ack(monkeypatch):
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    monkeypatch.setattr(runner.time, "monotonic", lambda: 100.0)
    adapter = object.__new__(runner.DeadlineFramedProcess)
    adapter.absolute_deadline = NOW + timedelta(seconds=10)
    calls = []
    adapter.process = SimpleNamespace(
        poll=lambda: None, kill=lambda: calls.append("kill")
    )
    monkeypatch.setattr(
        runner.FramedProcess,
        "_transfer",
        lambda self, stream, **kw: calls.append(kw["deadline"]),
    )
    adapter._transfer(None, wire=b"ACK", deadline=200)
    assert calls == [110]
    monkeypatch.setattr(runner, "utc_now", lambda: NOW + timedelta(seconds=10))
    with pytest.raises(ContractError, match="execution_deadline_reached"):
        adapter._transfer(None, wire=b"ACK", deadline=200)
    assert calls == [110, "kill"]


def test_quote_start_rechecks_full_budget_before_config_frame(monkeypatch):
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    adapter = object.__new__(runner.DeadlineFramedProcess)
    adapter.absolute_deadline = NOW + timedelta(seconds=35)
    adapter.required_seconds = 36
    monkeypatch.setattr(
        runner.FramedProcess, "send", lambda *a, **kw: pytest.fail("late K frame")
    )
    with pytest.raises(ContractError, match="quote_budget_crosses_deadline"):
        adapter.send(b"K", {}, deadline=100)


def test_quote_close_kills_at_deadline_without_grace(monkeypatch):
    monkeypatch.setattr(runner, "utc_now", lambda: NOW)
    calls = []

    def wait(*, timeout):
        calls.append(timeout)
        if timeout == 0:
            raise subprocess.TimeoutExpired("invented", timeout)

    adapter = object.__new__(runner.DeadlineFramedProcess)
    adapter._closed = False
    adapter.absolute_deadline = NOW
    adapter.process = SimpleNamespace(
        stdin=io.BytesIO(),
        stdout=io.BytesIO(),
        wait=wait,
        kill=lambda: calls.append("kill"),
    )
    adapter.close()
    assert calls == [0, "kill", 2]


def test_new_collection_layout_intakes_without_transformation(tmp_path, monkeypatch):
    from test_tdx_watchlist_runtime import (
        CONFIG,
        Adapter,
        batch,
        binding,
        clocks,
        event,
        quote_events,
        wire,
    )

    from market_data_foundation.qualification.tdx_market_retained import intake_quotes
    from market_data_foundation.runtime.tdx_watchlist import (
        RealPeriodicWatchlistSession,
    )

    monkeypatch.chdir(tmp_path)
    p = plan(
        source_enabled=False, securities=CONFIG.securities, observation_day="2026-01-01"
    )
    known = clocks(1) + timedelta(hours=8)
    monkeypatch.setattr(runner, "utc_now", lambda: known)
    monkeypatch.setattr(runner, "verify_endpoint", lambda *a: None)
    body = batch()
    body.update(
        sdk_release=p.quote_config().driver_sdk_release,
        endpoint=p.endpoint,
        max_rounds=1,
    )
    for group, fields in (
        ("connection", ("requested", "received")),
        ("clocks", ("requested", "received", "frozen")),
    ):
        for key in fields:
            body[group][key] = (
                datetime.fromisoformat(body[group][key]) + timedelta(hours=8)
            ).isoformat()
    messages = [
        ("E", event("probe_ok")),
        ("E", event("connection_attempt")),
        ("C", wire(binding(body))),
    ]
    messages += quote_events(1)
    messages += [
        ("B", wire(body)),
        ("E", event("ack_received", round=1)),
        ("E", event("complete", "budget_exhausted", round=1)),
    ]
    messages = [
        (
            kind,
            encoded(json.loads(payload) | {"endpoint": p.endpoint})
            if kind == "E"
            else payload,
        )
        for kind, payload in messages
    ]
    adapter = Adapter(messages)
    adapter.process = SimpleNamespace(returncode=0)
    monkeypatch.setattr(runner, "DeadlineFramedProcess", lambda *a, **kw: adapter)

    class OfflineSession(RealPeriodicWatchlistSession):
        def __init__(self, config):
            super().__init__(config, offline_fixture_mode=True)

        def run(self, *args, **kwargs):
            return super().run(*args, fixture_clock=lambda: known, **kwargs)

    monkeypatch.setattr(runner, "RealPeriodicWatchlistSession", OfflineSession)
    kwargs = collect_kwargs(tmp_path)
    ledger = runner.collect(p, **kwargs)
    assert ledger["terminal"] == "COMPLETED_BOUNDED_OBSERVATION", ledger
    source = kwargs["output"]
    frozen = {path.name: path.read_bytes() for path in source.iterdir()}
    manifest = json.loads(frozen["quote-manifest.json"])
    assert set(manifest) == {
        "config.json",
        "report.json",
        "read-ready.json",
        "batch-1.json",
    }
    assert all(
        sha256(frozen[name]).hexdigest() == digest for name, digest in manifest.items()
    )
    out = tmp_path / "artifacts/intake"
    summary = intake_quotes(source, out, expected_files=manifest)
    assert summary["original_source_run_id"] == p.run_id
    assert summary["inspection_id"] != p.run_id
    assert (
        summary["new_provider_requests"]
        == summary["pg_writes"]
        == summary["garage_writes"]
        == 0
    )
    assert summary["original_ack_receipts"] == 1
    assert summary["source_event_time"] == "UNKNOWN"
    assert json.loads((out / "manifest.json").read_bytes()) == manifest
    for name, digest in manifest.items():
        assert (out / name).read_bytes() == frozen[name]
        assert summary["materials"][name]["sha256"] == digest
    assert {path.name: path.read_bytes() for path in source.iterdir()} == frozen
    for name in manifest:
        (source / name).write_bytes(frozen[name] + b" ")
        with pytest.raises(ContractError, match="retained_manifest_mismatch"):
            intake_quotes(
                source, tmp_path / "artifacts/rejected", expected_files=manifest
            )
        assert not (tmp_path / "artifacts/rejected").exists()
        (source / name).write_bytes(frozen[name])
