"""仅绑定已观察 r17 的延续门禁；外部审核后只能采集 r18–r24。"""

import hashlib
import json
import sys
import time
from pathlib import Path

from provider_lab import fuyao_r1 as r1
from provider_lab import fuyao_r1_live_runner as runner
from provider_lab import fuyao_single_attempt as single

PRIOR_LEDGER_SHA256 = "8563db638f8977cd599362135db3c7f3a1e94d582cc80291486c9023288b4ad6"
EXECUTOR_SHA256 = "8afc502c15920e8a6c7c7732b252402fcecacd487623d645d043b37ac90f8165"
PLAN_SHA256 = "02da322e7dc5f30a4a8a7d1033b48ca9c775dd5c0a8ccc11d9c56c71fb9c2365"
ORDER = tuple(f"r{i:02}" for i in range(18, 25))
PREVIOUS_ATTEMPTS = 17
PRIOR_CAP = 65536


def _exact_fields(actual, expected):
    # 严格区分整数和 bool，拒绝缺字段及任意附加字段。
    return (
        isinstance(actual, dict)
        and actual.keys() == expected.keys()
        and all(
            type(actual[name]) is type(value) and actual[name] == value
            for name, value in expected.items()
        )
    )


def _validate_prior_semantics(prior):
    expected = {
        "transaction": single.TRANSACTION,
        "r01_external_attempts": 1,
        "r01_external_executor_sha256": runner.EXTERNAL_R01_EXECUTOR_SHA256,
        "r01_plan_sha256": PLAN_SHA256,
        "r01_artifact_sha256": (
            "6b13135ab36502e8b70818bcadd5ea5db33f3132dde021b17d9d51b02e168c5a"
        ),
        "cumulative_attempts": PREVIOUS_ATTEMPTS,
        "reserved_ceiling": 24,
        "hard_ceiling": 35,
        "capability_pass": False,
        "status": "STOP",
        "stop_reason": "child_failed",
    }
    if not isinstance(prior, dict):
        raise runner.PhaseStop("invalid_prior_ledger")
    rows = prior.get("ledger")
    summary = {name: value for name, value in prior.items() if name != "ledger"}
    if (
        not _exact_fields(summary, expected)
        or not isinstance(rows, list)
        or len(rows) != 16
    ):
        raise runner.PhaseStop("invalid_prior_ledger")
    for sequence, row in enumerate(rows, 2):
        failed = sequence == 17
        expected = {
            "sequence": sequence,
            "request_id": f"r{sequence:02}",
            "executor_sha256": EXECUTOR_SHA256,
            "plan_sha256": PLAN_SHA256,
            "child_exit": 3 if failed else 0,
            "http_request_invocations": 1,
            "attempts_accounted": 1,
            "http_status": 200,
            "business_code": 3002 if failed else 0,
            "status": "STOP" if failed else "COLLECTED",
            "stop_reason": "child_failed" if failed else None,
            "child_stop_reason": "business_failure" if failed else None,
            "cumulative_attempts": sequence,
        }
        if not _exact_fields(row, expected):
            raise runner.PhaseStop("invalid_prior_ledger")


def validate_prior(prior_bytes):
    """只验证调用方提供的原始只读 bytes；不查找历史账本或控制状态。"""
    if (
        type(prior_bytes) is not bytes
        or len(prior_bytes) > PRIOR_CAP
        or hashlib.sha256(prior_bytes).hexdigest() != PRIOR_LEDGER_SHA256
    ):
        raise runner.PhaseStop("prior_ledger_hash_mismatch")
    try:
        prior = single._decode(prior_bytes)
        _validate_prior_semantics(prior)
    except Exception:
        raise runner.PhaseStop("invalid_prior_ledger") from None
    return prior


def review_binding():
    """公开审核绑定字段；返回值本身不代表审核通过或执行授权。"""
    return {
        "review": "AO_EXTERNAL_R18_R24_CONTINUATION_REVIEW_PASS",
        "accepted_effect": "AUTHORIZE_R18_R24_CONTINUATION_ONLY",
        "baseline": single.BASELINE,
        "transaction": single.TRANSACTION,
        "prior_ledger_sha256": PRIOR_LEDGER_SHA256,
        "previous_attempts": PREVIOUS_ATTEMPTS,
        "request_ids": list(ORDER),
        "executor_sha256": EXECUTOR_SHA256,
        "plan_sha256": PLAN_SHA256,
        "runner_sha256": hashlib.sha256(Path(runner.__file__).read_bytes()).hexdigest(),
        "continuation_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def validate_review(review_bytes, seal_bytes):
    single.validate_seal(single._decode(seal_bytes))
    if (
        single.executor_sha256() != EXECUTOR_SHA256
        or r1.PLAN_SHA256 != PLAN_SHA256
        or not _exact_fields(single._decode(review_bytes), review_binding())
    ):
        raise runner.PhaseStop("invalid_continuation_review")
    plan = r1.load_plan()
    if (
        tuple(row["id"] for row in plan["requests"])
        != tuple(f"r{i:02}" for i in range(1, 25))
        or plan["limits"]["total_http_attempts"] != 24
        or plan["limits"]["hard_ceiling"] != 35
    ):
        raise runner.PhaseStop("invalid_frozen_order_or_budget")
    return plan


def collect(
    key,
    seal_bytes,
    review_bytes,
    prior_bytes,
    root,
    *,
    launch=runner.spawn_child,
    clock=time.monotonic,
):
    """外部私有输入边界；测试只能替换子进程，不能跳过前序门禁。"""
    started = clock()
    plan = validate_review(review_bytes, seal_bytes)
    prior = validate_prior(prior_bytes)
    if not single._valid_key(key):
        raise runner.PhaseStop("invalid_key_input")
    if single.contains_key(prior, key):
        raise runner.PhaseStop("credential_echo")
    _, target = single.resolve_request("r17")
    summary = {
        "transaction": single.TRANSACTION,
        "prior_ledger_sha256": PRIOR_LEDGER_SHA256,
        "previous_attempts": PREVIOUS_ATTEMPTS,
        "continuation_review_sha256": hashlib.sha256(review_bytes).hexdigest(),
        "executor_sha256": EXECUTOR_SHA256,
        "plan_sha256": PLAN_SHA256,
        "request_ids": list(ORDER),
        "r17": {
            "request_id": "r17",
            "capability": "corporate_actions",
            "disposition": "UNRESOLVED_DATA_NOT_READY",
            "attempts_accounted": 1,
            "retry_scheduled": False,
            "provider_request_id": None,  # 原失败账本未记录，不猜造。
            "target": target,
        },
        "capability_pass": False,
    }
    return runner._collect_sequence(
        key,
        seal_bytes,
        root,
        plan,
        ORDER,
        PREVIOUS_ATTEMPTS,
        summary,
        launch=launch,
        clock=clock,
        started=started,
    )


def main():
    try:
        parser = single._SafeParser(description="Fuyao r18–r24 外部审核延续入口")
        for name in ("key", "review-seal", "continuation-review", "prior-ledger"):
            parser.add_argument(f"--{name}-fd", type=int, required=True)
        parser.add_argument("--output-root", required=True)
        args = parser.parse_args()
        if (
            sys.version_info[:3] != (3, 14, 8)
            or Path(sys.executable).resolve() != single.CANONICAL_PYTHON.resolve()
        ):
            raise runner.PhaseStop("canonical_python_required")
        with runner.total_deadline():
            fds = [
                single._private_fd(fd)
                for fd in (
                    args.review_seal_fd,
                    args.continuation_review_fd,
                    args.prior_ledger_fd,
                    args.key_fd,
                )
            ]
            if len(set(fds)) != 4:
                raise runner.PhaseStop("fd_collision")
            seal = single._read_fd(fds[0], single.SEAL_CAP)
            review = single._read_fd(fds[1], single.SEAL_CAP)
            validate_review(review, seal)
            prior = single._read_fd(fds[2], PRIOR_CAP)
            validate_prior(prior)  # 审核和原字节门禁均先于凭据读取。
            key = single._read_fd(fds[3], single.KEY_CAP).decode("ascii").rstrip("\r\n")
            summary = collect(key, seal, review, prior, args.output_root)
        print(
            json.dumps(
                {
                    "status": summary["status"],
                    "stop_reason": summary["stop_reason"],
                    "previous_attempts": PREVIOUS_ATTEMPTS,
                    "new_attempts": summary["new_attempts"],
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
                    "stop_reason": "continuation_input_or_deadline_failure",
                    "capability_pass": False,
                }
            )
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
