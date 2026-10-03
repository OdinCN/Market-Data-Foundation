"""串行 runner 的离线证明：合成响应、私有 FD 与真实 Python 子进程。"""

import json
import os
import shutil
import signal
import subprocess
import sys
import uuid

import pytest

from provider_lab import fuyao_r1 as r1
from provider_lab import fuyao_r1_live_runner as runner
from provider_lab import fuyao_single_attempt as single

KEY = "synthetic-only-private-pipe"


def seal():
    return {
        "review": "AO_EXTERNAL_SINGLE_ATTEMPT_REVIEW_PASS",
        "executor_sha256": single.executor_sha256(),
        "plan_sha256": r1.PLAN_SHA256,
        "baseline": single.BASELINE,
        "transaction": single.TRANSACTION,
        "accepted_effect": single.ACCEPTED_EFFECT,
    }


def evidence(request_id, items=None, catalog=None):
    row, target = single.resolve_request(request_id, catalog)
    meta = single._metadata()
    meta.update(
        request_id=request_id,
        planned_command=row["command"],
        target=target,
        http_request_invocations=1,
        http_status=200,
        business_code=0,
        transport_success=True,
    )
    if items is None:
        items = (
            [{"thscode": target["query"]["q"]}]
            if request_id in ("r08", "r09")
            else [{"thscode": "880001.TI"}]
        )
    return {"metadata": meta, "data": {"item": items}}


@pytest.fixture
def output():
    parent = r1.ROOT / "artifacts" / f"synthetic-runner-{uuid.uuid4().hex}"
    yield parent / "phase"
    shutil.rmtree(parent, ignore_errors=True)


def fake_launch(calls, *, failure=None, catalogs=None, mismatch=None):
    def launch(request_id, key, seal_bytes, catalog, timeout):
        assert key == KEY.encode()
        single.validate_seal(single._decode(seal_bytes))
        assert 0 < timeout <= 30
        calls.append(request_id)
        if request_id == failure:
            meta = evidence(request_id)["metadata"]
            meta.update(
                transport_success=False, http_status=503, stop_reason="http_failure"
            )
            return runner.ChildResult(3, meta)
        prior = None
        if catalog:
            obj = single._decode(catalog)
            prior = single.Result(obj["metadata"], obj["data"])
        items = (catalogs or {}).get(request_id)
        if request_id == mismatch:
            items = [{"thscode": "wrong"}]
        obj = evidence(request_id, items, prior)
        return runner.ChildResult(0, obj["metadata"], json.dumps(obj).encode())

    return launch


def collect(output, launch, **kwargs):
    return runner.collect(
        KEY,
        json.dumps(seal()).encode(),
        evidence("r01"),
        output,
        launch=launch,
        **kwargs,
    )


@pytest.mark.parametrize("field", list(seal()))
def test_seal_rejects_all_binding_tampering(field):
    good = seal()
    single.validate_seal(good)
    good[field] = "tampered"
    with pytest.raises(r1.SingleAttemptGateBlocked):
        single.validate_seal(good)


def test_seal_executor_bytes_and_plan_baseline_tampering(monkeypatch, output):
    reviewed = seal()
    monkeypatch.setattr(single, "executor_sha256", lambda: "changed-file-bytes")
    with pytest.raises(r1.SingleAttemptGateBlocked):
        single.validate_seal(reviewed)
    monkeypatch.undo()
    plan = r1.load_plan()
    monkeypatch.setattr(r1, "load_plan", lambda: dict(plan, baseline="other"))
    with pytest.raises(r1.SingleAttemptGateBlocked):
        single.validate_seal(seal())


def test_order_and_budget_never_schedules_consumed_r01(output):
    calls = []
    result = collect(output, fake_launch(calls))
    assert calls == list(runner.ORDER)
    assert result["cumulative_attempts"] == 24
    assert result["capability_pass"] is False
    assert result["status"] == "EVIDENCE_COLLECTED"
    assert [e["sequence"] for e in result["ledger"]] == list(range(2, 25))
    assert all(
        e["executor_sha256"] == single.executor_sha256() for e in result["ledger"]
    )
    assert all(e["plan_sha256"] == r1.PLAN_SHA256 for e in result["ledger"])
    for file in output.rglob("*.json"):
        assert KEY not in file.read_text()
        assert file.resolve().is_relative_to(output.resolve())
        assert file.stat().st_mode & 0o077 == 0
    with pytest.raises(FileExistsError):
        collect(output, fake_launch(calls))
    assert calls == list(runner.ORDER)


@pytest.mark.parametrize("failure", ["r02", "r09", "r23"])
def test_stop_on_first_child_failure_no_restart(output, failure):
    calls = []
    result = collect(output, fake_launch(calls, failure=failure))
    assert calls == list(runner.ORDER[: runner.ORDER.index(failure) + 1])
    assert result["stop_reason"] == "child_failed"
    assert result["ledger"][-1]["http_status"] == 503
    assert result["cumulative_attempts"] == len(calls) + 1


@pytest.mark.parametrize("mismatch", ["r08", "r09"])
def test_identity_failure_before_fixed_code_requests(output, mismatch):
    calls = []
    result = collect(output, fake_launch(calls, mismatch=mismatch))
    assert result["stop_reason"] == "symbol_identity_mismatch"
    assert calls[-1] == mismatch and "r10" not in calls
    assert not (output / mismatch).exists()


@pytest.mark.parametrize("catalog_id,next_id", [("r21", "r22"), ("r23", "r24")])
@pytest.mark.parametrize("items", [[{"name": "missing"}], [1], [{"thscode": "evil"}]])
def test_invalid_catalog_stops_before_dynamic(output, catalog_id, next_id, items):
    calls = []
    result = collect(output, fake_launch(calls, catalogs={catalog_id: items}))
    assert result["status"] == "STOP"
    assert calls[-1] == catalog_id and next_id not in calls


def test_empty_catalog_skips_zero_attempt_no_reallocation(output):
    calls = []
    result = collect(output, fake_launch(calls, catalogs={"r21": [], "r23": []}))
    assert len(calls) == 21 and "r22" not in calls and "r24" not in calls
    assert result["cumulative_attempts"] == 22
    assert len(result["ledger"]) == 23
    for entry in result["ledger"]:
        if entry["request_id"] in ("r22", "r24"):
            assert entry["status"] == "SKIP"
            assert entry["http_request_invocations"] == 0
            assert entry["child_exit"] is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_sha256", "wrong"),
        ("http_request_invocations", 0),
        ("http_request_invocations", 2),
        ("http_request_invocations", True),
        ("transport_success", False),
        ("request_id", "r02"),
    ],
)
def test_r01_prerequisite_invalid_no_child(output, field, value):
    prior = evidence("r01")
    prior["metadata"][field] = value
    calls = []
    with pytest.raises(runner.PhaseStop):
        runner.collect(
            KEY, json.dumps(seal()).encode(), prior, output, launch=fake_launch(calls)
        )
    assert calls == [] and not output.exists()


@pytest.mark.parametrize(
    "kind", ["child_timeout", "invalid_child_metadata", "child_output_cap"]
)
def test_uncertain_attempt_accounted_and_stops(output, kind):
    calls = []

    def fail(*args):
        calls.append(args[0])
        return runner.ChildResult(None, None, reason=kind)

    result = collect(output, fail)
    assert calls == ["r02"]
    assert result["cumulative_attempts"] == 2
    assert result["ledger"][0]["http_request_invocations"] is None


def test_total_timeout_stops_before_next_child(output):
    times = iter([0, 0, 1201])
    calls = []
    result = collect(output, fake_launch(calls), clock=lambda: next(times))
    assert calls == ["r02"]
    assert result["stop_reason"] == "total_timeout"
    assert result["cumulative_attempts"] == 2


def test_total_deadline_signal_cleanup():
    with pytest.raises(runner.PhaseStop, match="total_timeout"):
        with runner.total_deadline(1):
            signal.raise_signal(signal.SIGALRM)
    assert signal.getitimer(signal.ITIMER_REAL)[0] == 0


@pytest.mark.parametrize(
    "root", ["provider_lab/invalid", "artifacts/../tracked", "artifacts"]
)
def test_output_root_confinement(root):
    with pytest.raises(runner.PhaseStop):
        runner.prepare_root(r1.ROOT / root)


def test_output_symlink_rejected(output):
    output.parent.mkdir(parents=True)
    output.symlink_to(r1.ROOT, target_is_directory=True)
    with pytest.raises(runner.PhaseStop):
        runner.prepare_root(output)


def test_escaped_key_detection():
    key = 'synthetic"\\key'
    assert single.contains_key({"item": [{"echo": key}]}, key)


SYNTHETIC_CHILD = """
import argparse, json, os
from provider_lab import fuyao_single_attempt as s
p = argparse.ArgumentParser()
p.add_argument('--request-id')
for n in ('key', 'review-seal', 'catalog', 'response'):
    p.add_argument('--' + n + '-fd', type=int)
a = p.parse_args()
seal = s._decode(s._read_fd(s._private_fd(a.review_seal_fd), s.SEAL_CAP))
s.validate_seal(seal)
key = s._read_fd(s._private_fd(a.key_fd), s.KEY_CAP)
assert key == b'synthetic-only-private-pipe'
prior = None
if a.catalog_fd is not None:
    obj = s._decode(s._read_fd(s._private_fd(a.catalog_fd), 13000000))
    prior = s.Result(obj['metadata'], obj['data'])
row, target = s.resolve_request(a.request_id, prior)
meta = s._metadata()
meta.update(request_id=a.request_id, planned_command=row['command'], target=target,
    http_request_invocations=1, http_status=200, business_code=0,
    transport_success=True)
items = ([{'thscode': target['query']['q']}]
    if a.request_id in ('r08','r09') else [{'thscode':'880001.TI'}])
# 超过管道容量；证明父进程持续读取，不因 wait-before-read 死锁。
data = {'item': items, 'synthetic_padding': 'x' * 100000}
with os.fdopen(os.dup(s._private_fd(a.response_fd)), 'w') as stream:
    json.dump({'metadata':meta, 'data':data}, stream)
print(json.dumps(meta))
"""


def synthetic_popen(monkeypatch, code=SYNTHETIC_CHILD):
    original = subprocess.Popen
    processes = []

    def launch(argv, **kwargs):
        assert KEY not in str(argv)
        assert set(kwargs["env"]) == {"PYTHONPATH", "PYTHONDONTWRITEBYTECODE"}
        assert kwargs["stdin"] == subprocess.DEVNULL
        for fd in kwargs["pass_fds"]:
            single._private_fd(fd)
        child = original([argv[0], "-c", code, *argv[3:]], **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(runner.subprocess, "Popen", launch)
    return processes


def test_fresh_real_python_child_per_request_private_pipes(monkeypatch, output):
    # prepare_root 的 git 子进程不属于 transport，先保留原验证函数。
    original_prepare = runner.prepare_root
    original_popen = subprocess.Popen
    processes = synthetic_popen(monkeypatch)

    def prepare(root):
        patched = subprocess.Popen
        monkeypatch.setattr(subprocess, "Popen", original_popen)
        try:
            return original_prepare(root)
        finally:
            monkeypatch.setattr(subprocess, "Popen", patched)

    monkeypatch.setattr(runner, "prepare_root", prepare)
    result = collect(output, runner.spawn_child)
    assert result["status"] == "EVIDENCE_COLLECTED"
    assert len(processes) == len({p.pid for p in processes}) == 23
    assert all(p.returncode == 0 for p in processes)


def test_real_child_timeout_kills_no_restart(monkeypatch):
    processes = synthetic_popen(monkeypatch, "import time; time.sleep(10)")
    result = runner.spawn_child(
        "r02", KEY.encode(), json.dumps(seal()).encode(), None, 0.1
    )
    assert result.reason == "child_timeout"
    assert len(processes) == 1 and processes[0].poll() is not None


def pipe(payload):
    read_fd, write_fd = os.pipe()
    os.write(write_fd, payload)
    os.close(write_fd)
    return read_fd


def test_executor_missing_seal_cannot_open_via_env(monkeypatch, capsys):
    monkeypatch.setenv("FUYAO_LIVE_AUTHORIZED", "1")
    monkeypatch.setattr(sys, "argv", ["single", "--request-id", "r02"])
    assert single.main() == 3
    assert (
        json.loads(capsys.readouterr().out)["stop_reason"] == "external_review_required"
    )


@pytest.mark.parametrize("request_id", ["r01", "r25"])
def test_executor_rejects_consumed_request_before_key_read(
    monkeypatch, capsys, request_id
):
    seal_fd = pipe(json.dumps(seal()).encode())
    monkeypatch.setattr(
        sys,
        "argv",
        ["single", "--request-id", request_id, "--review-seal-fd", str(seal_fd)],
    )
    try:
        assert single.main() == 3
        assert json.loads(capsys.readouterr().out)["http_request_invocations"] == 0
    finally:
        os.close(seal_fd)


def test_seal_regular_file_rejected_before_key_read(monkeypatch, output, capsys):
    output.parent.mkdir(parents=True)
    file = output.parent / "synthetic-seal.json"
    file.write_text(json.dumps(seal()))
    with file.open("rb") as stream:
        monkeypatch.setattr(
            sys,
            "argv",
            ["single", "--request-id", "r02", "--review-seal-fd", str(stream.fileno())],
        )
        assert single.main() == 3
    assert json.loads(capsys.readouterr().out)["http_request_invocations"] == 0


def test_canonical_python_jsonschema_gates():
    import jsonschema

    assert sys.version_info[:3] == (3, 14, 8)
    schema = json.loads((r1.ROOT / "provider_lab/observation.schema.json").read_text())
    jsonschema.Draft202012Validator(
        schema, format_checker=jsonschema.FormatChecker()
    ).validate(r1.blocked_observation())


@pytest.mark.parametrize(
    "kind", ["invalid_json", "metadata_mismatch", "key_echo", "zero_attempt_success"]
)
def test_bad_child_output_stops_without_artifact(output, kind):
    calls = []

    def launch(*args):
        calls.append(args[0])
        obj = evidence(args[0])
        meta = dict(obj["metadata"])
        if kind == "invalid_json":
            response = b"invalid"
        else:
            if kind == "metadata_mismatch":
                obj["metadata"]["business_code"] = 9
            if kind == "key_echo":
                obj["data"]["item"] = [{"echo": KEY}]
            if kind == "zero_attempt_success":
                obj["metadata"]["http_request_invocations"] = 0
                meta = obj["metadata"]
            response = json.dumps(obj).encode()
        return runner.ChildResult(0, meta, response)

    result = collect(output, launch)
    assert calls == ["r02"]
    assert result["status"] == "STOP"
    assert not (output / "r02").exists()
    assert KEY not in (output / "ledger.json").read_text()


def test_dynamic_input_is_exact_corresponding_accepted_bytes(output):
    accepted = {}
    calls = []
    original = fake_launch(calls)

    def launch(request_id, key, seal_bytes, catalog, timeout):
        if request_id in ("r22", "r24"):
            assert catalog == accepted[{"r22": "r21", "r24": "r23"}[request_id]]
        else:
            assert catalog is None
        child = original(request_id, key, seal_bytes, catalog, timeout)
        if request_id in ("r21", "r23"):
            accepted[request_id] = child.response
        return child

    assert collect(output, launch)["status"] == "EVIDENCE_COLLECTED"


def test_executor_valid_seal_private_key_response_pipes(monkeypatch, capsys):
    seal_fd = pipe(json.dumps(seal()).encode())
    key_fd = pipe(KEY.encode())
    response_read, response_write = os.pipe()
    calls = []

    def execute(request_id, key, *, catalog):
        calls.append((request_id, key))
        obj = evidence(request_id)
        return single.Result(obj["metadata"], obj["data"])

    monkeypatch.setattr(single, "execute_one", execute)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "single",
            "--request-id",
            "r02",
            "--key-fd",
            str(key_fd),
            "--review-seal-fd",
            str(seal_fd),
            "--response-fd",
            str(response_write),
        ],
    )
    try:
        assert single.main() == 0
        os.close(response_write)
        response_write = None
        assert single._decode(os.read(response_read, 65536))["data"]["item"]
        assert calls == [("r02", KEY)]
        assert KEY not in capsys.readouterr().out
    finally:
        for fd in (seal_fd, key_fd, response_read, response_write):
            if fd is not None:
                os.close(fd)


def test_executor_stdin_must_be_private_pipe(monkeypatch, capsys):
    seal_fd = pipe(json.dumps(seal()).encode())
    key_fd = pipe(KEY.encode())
    response_read, response_write = os.pipe()
    original_fstat = os.fstat
    original_read = single._read_fd
    monkeypatch.setattr(
        single.os, "fstat", lambda fd: original_fstat(key_fd if fd == 0 else fd)
    )
    monkeypatch.setattr(
        single,
        "_read_fd",
        lambda fd, cap: original_read(key_fd if fd == 0 else fd, cap),
    )
    monkeypatch.setattr(
        single,
        "execute_one",
        lambda request_id, key, catalog: single.Result(**evidence(request_id)),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "single",
            "--request-id",
            "r02",
            "--review-seal-fd",
            str(seal_fd),
            "--response-fd",
            str(response_write),
        ],
    )
    try:
        assert single.main() == 0
        assert KEY not in capsys.readouterr().out
    finally:
        for fd in (seal_fd, key_fd, response_read, response_write):
            os.close(fd)


def test_fifo_seal_fd_and_public_fifo_rejection(output):
    output.parent.mkdir(parents=True)
    fifo = output.parent / "synthetic-fifo"
    os.mkfifo(fifo, 0o600)
    fd = os.open(fifo, os.O_RDWR | os.O_NONBLOCK)
    try:
        assert single._private_fd(fd) == fd
        os.chmod(fifo, 0o666)
        with pytest.raises(ValueError):
            single._private_fd(fd)
    finally:
        os.close(fd)


def test_runner_cli_fd_only_and_gate_precedes_key(monkeypatch, capsys, output):
    seal_fd = pipe(json.dumps(dict(seal(), baseline="invalid")).encode())
    key_fd = pipe(KEY.encode())
    r01_fd = pipe(json.dumps(evidence("r01")).encode())
    seen = []
    original = single._read_fd

    def read(fd, cap):
        seen.append(fd)
        return original(fd, cap)

    monkeypatch.setattr(single, "_read_fd", read)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "runner",
            "--key-fd",
            str(key_fd),
            "--review-seal-fd",
            str(seal_fd),
            "--r01-artifact-fd",
            str(r01_fd),
            "--output-root",
            str(output),
        ],
    )
    try:
        assert runner.main() == 3
        assert seen == [seal_fd]
        assert KEY not in capsys.readouterr().out
        assert not output.exists()
    finally:
        for fd in (seal_fd, key_fd, r01_fd):
            os.close(fd)


def test_deadline_during_child_keeps_durable_attempt_reservation(output):
    calls = []

    def interrupted(*args):
        calls.append(args[0])
        assert (output / "reservation-02.json").is_file()
        raise runner.PhaseStop("total_timeout")

    result = collect(output, interrupted)
    assert calls == ["r02"]
    assert result["cumulative_attempts"] == 2
    assert result["ledger"][0]["http_request_invocations"] is None
    assert result["stop_reason"] == "total_timeout"
