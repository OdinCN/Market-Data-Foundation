"""外部调度器的串行证据采集入口；无 provider binding、重试或能力 PASS。"""

import hashlib
import json
import os
import selectors
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from provider_lab import fuyao_r1 as r1
from provider_lab import fuyao_single_attempt as single

ORDER = tuple(f"r{i:02}" for i in range(2, 25))
TOTAL_SECONDS = 1200
CHILD_SECONDS = 30
RESPONSE_CAP = single.BODY_CAP * 6 + 65536
CANONICAL_PYTHON = single.CANONICAL_PYTHON
EXTERNAL_R01_EXECUTOR_SHA256 = (
    "220b24521809434f6002bce50338ed98945b908bbd004dd218082d3b5b4e2a29"
)
SAFE_CHILD_REASONS = frozenset(
    {
        "authentication",
        "rate_limit",
        "http_failure",
        "unknown_http_status",
        "unknown_business_code",
        "business_failure",
        "invalid_key_input",
        "credential_collision",
        "process_already_used",
        "body_cap_exceeded",
        "invalid_json",
        "invalid_envelope",
        "invalid_data",
        "credential_echo",
        "timeout",
        "network_error",
        "executor_error",
        "close_error",
        "external_review_required",
        "invalid_input",
    }
)


class PhaseStop(Exception):
    """只携带固定分类，禁止异常原文落盘。"""


@dataclass
class ChildResult:
    exit_code: int | None
    metadata: dict | None
    response: bytes = b""
    reason: str | None = None
    launched: bool = True


@contextmanager
def total_deadline(seconds=TOTAL_SECONDS):
    previous = signal.getsignal(signal.SIGALRM)

    def expired(*_):
        raise PhaseStop("total_timeout")

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def spawn_child(request_id, key, seal, catalog, timeout):
    """每次全新 Python 进程；同时泵送有界管道，避免大 catalog/response 死锁。"""
    pipes = []
    parent_fds = []
    child_fds = []
    process = None
    started = time.monotonic()
    outputs = {"metadata": bytearray(), "response": bytearray()}
    try:
        inputs = {"key": key, "review-seal": seal}
        if catalog is not None:
            inputs["catalog"] = catalog
        argv = [str(CANONICAL_PYTHON), "-m", "provider_lab.fuyao_single_attempt"]
        argv += ["--request-id", request_id]
        pending = {}
        for name, payload in inputs.items():
            read_fd, write_fd = os.pipe()
            pipes.extend((read_fd, write_fd))
            child_fds.append(read_fd)
            parent_fds.append(write_fd)
            argv += [f"--{name}-fd", str(read_fd)]
            pending[write_fd] = memoryview(payload)
        response_read, response_write = os.pipe()
        pipes.extend((response_read, response_write))
        child_fds.append(response_write)
        parent_fds.append(response_read)
        argv += ["--response-fd", str(response_write)]
        # 不继承调用者环境中的凭据；FD 数字是唯一私有输入引用。
        process = subprocess.Popen(
            argv,
            cwd=r1.ROOT,
            env={"PYTHONPATH": "src:.", "PYTHONDONTWRITEBYTECODE": "1"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            pass_fds=tuple(child_fds),
            close_fds=True,
        )
        for fd in child_fds:
            os.close(fd)
            pipes.remove(fd)
        with selectors.DefaultSelector() as selector:
            for fd in pending:
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_WRITE, "input")
            for fd, name in (
                (response_read, "response"),
                (process.stdout.fileno(), "metadata"),
            ):
                os.set_blocking(fd, False)
                selector.register(fd, selectors.EVENT_READ, name)
            while selector.get_map():
                remaining = timeout - (time.monotonic() - started)
                if remaining <= 0:
                    return ChildResult(None, None, reason="child_timeout")
                for event, _ in selector.select(remaining):
                    fd, name = event.fd, event.data
                    if name == "input":
                        try:
                            amount = os.write(fd, pending[fd][:65536])
                            pending[fd] = pending[fd][amount:]
                        except BrokenPipeError:
                            pending[fd] = b""
                        if not pending[fd]:
                            selector.unregister(fd)
                            os.close(fd)
                            pipes.remove(fd)
                    else:
                        chunk = os.read(fd, 65536)
                        if not chunk:
                            selector.unregister(fd)
                            continue
                        outputs[name].extend(chunk)
                        cap = RESPONSE_CAP if name == "response" else 65536
                        if len(outputs[name]) > cap:
                            return ChildResult(None, None, reason="child_output_cap")
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                return ChildResult(None, None, reason="child_timeout")
            try:
                code = process.wait(timeout=remaining)
            except subprocess.TimeoutExpired:
                return ChildResult(None, None, reason="child_timeout")
        try:
            meta = single._decode(bytes(outputs["metadata"]))
            if not isinstance(meta, dict):
                raise ValueError
        except Exception:
            return ChildResult(code, None, reason="invalid_child_metadata")
        return ChildResult(code, meta, bytes(outputs["response"]))
    except PhaseStop:
        raise
    except Exception:
        return ChildResult(
            None, None, reason="child_io_failure", launched=process is not None
        )
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.wait()
            process.stdout.close()
        for fd in pipes:
            os.close(fd)


def validate_success(request_id, meta, data, catalog=None):
    row, target = single.resolve_request(request_id, catalog)
    if (
        not isinstance(meta, dict)
        or meta.get("request_id") != request_id
        or meta.get("planned_command") != row["command"]
        or meta.get("target") != target
        or meta.get("plan_sha256") != r1.PLAN_SHA256
        or type(meta.get("http_request_invocations")) is not int
        or meta["http_request_invocations"] != 1
        or meta.get("transport_success") is not True
        or meta.get("capability_pass") is not False
        or meta.get("stop_reason") is not None
        or r1.stop_reason(meta.get("http_status"), meta.get("business_code"))
        is not None
        or not single._valid_data(data)
    ):
        raise PhaseStop("invalid_success_evidence")
    if request_id in ("r08", "r09"):
        expected = target["query"]["q"]
        if not any(item.get("thscode") == expected for item in data["item"]):
            raise PhaseStop("symbol_identity_mismatch")
    if request_id in ("r21", "r23"):
        try:
            r1.select_ths_group(data["item"])
        except ValueError:
            raise PhaseStop("invalid_catalog") from None


def validate_r01(prior):
    if not isinstance(prior, dict):
        raise PhaseStop("invalid_r01_prerequisite")
    try:
        validate_success("r01", prior["metadata"], prior["data"])
    except KeyError, ValueError, PhaseStop:
        raise PhaseStop("invalid_r01_prerequisite") from None


def prepare_root(root):
    """调用者选择 candidate/artifacts 下的新目录；拒绝符号链接与续跑。"""
    root = Path(root).absolute()
    base = r1.ROOT / "artifacts"
    if not root.is_relative_to(base) or root == base:
        raise PhaseStop("invalid_artifact_root")
    for path in (root, *root.parents):
        if path.is_symlink():
            raise PhaseStop("invalid_artifact_root")
    if root.resolve() != root:
        raise PhaseStop("invalid_artifact_root")
    ignored = subprocess.run(
        ["git", "check-ignore", "--quiet", "--no-index", str(root / "ledger.json")],
        cwd=r1.ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if ignored.returncode != 0:
        raise PhaseStop("artifact_root_not_ignored")
    root.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.mkdir(mode=0o700)  # 已存在即拒绝，不能 restart。
    return root


def write_json(path, value):
    # 私有目录内独占创建；不覆盖或跟随已存在的链接。
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        json.dump(value, stream, ensure_ascii=False, allow_nan=False)


def safe_metadata(meta):
    # 不持久化任意 child 字段、provider 文本或原始异常。
    return {name: meta.get(name) for name in single._metadata()}


def collect(
    key, seal_bytes, prerequisite, root, *, launch=spawn_child, clock=time.monotonic
):
    """只供已获得外部 FD 输入的 CLI 调用；测试可替换进程边界。"""
    started = clock()
    single.validate_seal(single._decode(seal_bytes))
    if not single._valid_key(key):
        raise PhaseStop("invalid_key_input")
    validate_r01(prerequisite)
    if single.contains_key(prerequisite, key):
        raise PhaseStop("credential_echo")
    plan = r1.load_plan()
    if tuple(row["id"] for row in plan["requests"]) != ("r01", *ORDER):
        raise PhaseStop("invalid_frozen_order")
    summary = {
        "transaction": single.TRANSACTION,
        "r01_external_attempts": 1,
        "r01_external_executor_sha256": EXTERNAL_R01_EXECUTOR_SHA256,
        "r01_plan_sha256": r1.PLAN_SHA256,
        "r01_artifact_sha256": hashlib.sha256(
            json.dumps(prerequisite, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
    }
    return _collect_sequence(
        key,
        seal_bytes,
        root,
        plan,
        ORDER,
        1,
        summary,
        launch=launch,
        clock=clock,
        started=started,
    )


def _collect_sequence(
    key,
    seal_bytes,
    root,
    plan,
    order,
    previous_attempts,
    summary,
    *,
    launch,
    clock,
    started,
):
    """两个已验证入口共用原严格采集循环；没有 CLI 自定义顺序或额度。"""
    root = prepare_root(root)
    if "prior_ledger_sha256" in summary:
        write_json(root / "continuation-gate.json", summary)
    ledger = []
    attempts = previous_attempts
    catalogs = {}
    stop = None
    executor_hash = single.executor_sha256()
    for request_id in order:
        sequence = int(request_id[1:])
        entry = {
            "sequence": sequence,
            "request_id": request_id,
            "executor_sha256": executor_hash,
            "plan_sha256": r1.PLAN_SHA256,
            "child_exit": None,
            "http_request_invocations": 0,
            "attempts_accounted": 0,
            "http_status": None,
            "business_code": None,
            "status": "STOP",
            "stop_reason": None,
            "child_stop_reason": None,
        }
        child = None
        launched = False
        try:
            remaining = TOTAL_SECONDS - (clock() - started)
            if remaining <= 0:
                raise PhaseStop("total_timeout")
            if attempts >= min(24, plan["limits"]["hard_ceiling"]):
                raise PhaseStop("attempt_budget")
            if single.executor_sha256() != executor_hash:
                raise PhaseStop("executor_changed")
            catalog = catalogs.get({"r22": "r21", "r24": "r23"}.get(request_id))
            prior = None
            if catalog is not None:
                obj = single._decode(catalog)
                prior = single.Result(obj["metadata"], obj["data"])
            _, target = single.resolve_request(request_id, prior)
            if target is None:
                entry.update(status="SKIP", stop_reason="empty_catalog_skip")
            else:
                # 在进程边界前先落保守预约；异步截止时间不能留下未记账请求。
                entry.update(http_request_invocations=None, attempts_accounted=1)
                write_json(
                    root / f"reservation-{sequence:02}.json",
                    {
                        "sequence": sequence,
                        "request_id": request_id,
                        "executor_sha256": executor_hash,
                        "plan_sha256": r1.PLAN_SHA256,
                        "status": "RESERVED_PENDING_CHILD",
                        "http_request_invocations": None,
                        "cumulative_attempts_upper_bound": attempts + 1,
                    },
                )
                launched = True
                child = launch(
                    request_id,
                    key.encode(),
                    seal_bytes,
                    catalog,
                    min(CHILD_SECONDS, remaining),
                )
                launched = child.launched
                entry["child_exit"] = child.exit_code
                meta = child.metadata
                # 缺失/损坏回执按一次可能尝试记账，不能回收额度。
                count = (
                    meta.get("http_request_invocations")
                    if isinstance(meta, dict)
                    else None
                )
                if type(count) is not int or count not in (0, 1):
                    entry["http_request_invocations"] = None
                    entry["attempts_accounted"] = int(launched)
                else:
                    entry["http_request_invocations"] = count
                    entry["attempts_accounted"] = count
                if isinstance(meta, dict):
                    if meta.get("stop_reason") in SAFE_CHILD_REASONS:
                        entry["child_stop_reason"] = meta["stop_reason"]
                    for name in ("http_status", "business_code"):
                        value = meta.get(name)
                        if type(value) is int and abs(value) <= 2**53 - 1:
                            entry[name] = value
                if child.reason:
                    raise PhaseStop(child.reason)
                if child.exit_code != 0:
                    raise PhaseStop("child_failed")
                obj = single._decode(child.response)
                if obj["metadata"] != meta:
                    raise PhaseStop("response_metadata_mismatch")
                validate_success(request_id, meta, obj["data"], prior)
                if single.contains_key(obj, key):
                    raise PhaseStop("credential_echo")
                entry.update(
                    http_status=meta["http_status"], business_code=meta["business_code"]
                )
                directory = root / request_id
                directory.mkdir(mode=0o700)
                write_json(directory / "metadata.json", safe_metadata(meta))
                write_json(directory / "data.json", obj["data"])
                if request_id in ("r21", "r23"):
                    catalogs[request_id] = child.response  # 只用对应已接受的响应字节。
                entry["status"] = "COLLECTED"
        except PhaseStop as error:
            stop = str(error)
        except Exception:
            stop = "runner_validation_or_io_failure"
        if launched and child is None:
            entry.update(http_request_invocations=None, attempts_accounted=1)
        attempts += entry["attempts_accounted"]
        entry["cumulative_attempts"] = attempts
        if stop:
            entry["stop_reason"] = stop
        ledger.append(entry)
        # 每项独立落账；先失败再退出，不启动后续子进程。
        write_json(root / f"attempt-{sequence:02}.json", entry)
        if stop:
            break
    summary = {
        **summary,
        "previous_attempts": previous_attempts,
        "new_attempts": attempts - previous_attempts,
        "cumulative_attempts": attempts,
        "reserved_ceiling": 24,
        "hard_ceiling": 35,
        "capability_pass": False,
        "status": "STOP" if stop else "EVIDENCE_COLLECTED",
        "stop_reason": stop,
        "ledger": ledger,
    }
    write_json(root / "ledger.json", summary)
    return summary


def main():
    try:
        parser = single._SafeParser(description="Fuyao r02-r24 串行证据采集")
        parser.add_argument("--key-fd", type=int, required=True)
        parser.add_argument("--review-seal-fd", type=int, required=True)
        parser.add_argument("--r01-artifact-fd", type=int, required=True)
        parser.add_argument("--output-root", required=True)
        args = parser.parse_args()
        if (
            sys.version_info[:3] != (3, 14, 8)
            or Path(sys.executable).resolve() != CANONICAL_PYTHON.resolve()
        ):
            raise PhaseStop("canonical_python_required")
        with total_deadline():
            fds = [
                single._private_fd(fd)
                for fd in (args.review_seal_fd, args.key_fd, args.r01_artifact_fd)
            ]
            if len(set(fds)) != 3:
                raise PhaseStop("fd_collision")
            seal = single._read_fd(fds[0], single.SEAL_CAP)
            single.validate_seal(single._decode(seal))  # 凭据读取前门禁。
            prior = single._decode(single._read_fd(fds[2], RESPONSE_CAP))
            validate_r01(prior)
            key = single._read_fd(fds[1], single.KEY_CAP).decode("ascii").rstrip("\r\n")
            summary = collect(key, seal, prior, args.output_root)
        print(
            json.dumps(
                {
                    "status": summary["status"],
                    "stop_reason": summary["stop_reason"],
                    "cumulative_attempts": summary["cumulative_attempts"],
                    "capability_pass": False,
                }
            )
        )
        return 3 if summary["stop_reason"] else 0
    except Exception:
        print(
            json.dumps(
                {
                    "status": "STOP",
                    "stop_reason": "runner_input_or_deadline_failure",
                    "capability_pass": False,
                }
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
