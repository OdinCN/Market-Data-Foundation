"""Fuyao R1 冻结计划校验与外部审查门禁；单次执行器为独立候选。"""

import argparse
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

from market_data_foundation.qualification import (
    Binding,
    load_fixtures,
    observe,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN = ROOT / "provider_lab/fuyao_r1.request-plan.json"
FIXTURES = ROOT / "provider_lab/fuyao_r1.fixtures.toml"
PLAN_SHA256 = "02da322e7dc5f30a4a8a7d1033b48ca9c775dd5c0a8ccc11d9c56c71fb9c2365"
BLOCKER = "LIVE BLOCKED BY SINGLE-ATTEMPT EXECUTOR GATE"


class SingleAttemptGateBlocked(PermissionError):
    """候选已有离线证明；AO 外部审查与集成裁定尚未完成。"""


def require_single_attempt_executor() -> None:
    """在 config/auth/subprocess/network 操作前失败；不接受 bool/env 自证。"""
    raise SingleAttemptGateBlocked(BLOCKER)


def load_plan() -> dict:
    """冻结字节、fixture 与逐项额度；调用本函数不读取 runtime 或 secrets。"""
    payload = PLAN.read_bytes()
    if hashlib.sha256(payload).hexdigest() != PLAN_SHA256:
        raise ValueError("冻结 request-plan 被改动")
    plan = json.loads(payload)
    if hashlib.sha256(FIXTURES.read_bytes()).hexdigest() != plan["fixture_sha256"]:
        raise ValueError("冻结 fixtures 被改动")
    fixtures = {f.fixture_id: f for f in load_fixtures(FIXTURES)}
    rows = plan["requests"]
    if len({row["id"] for row in rows}) != len(rows):
        raise ValueError("重复 request id")
    command_calls = Counter()
    capability_calls = Counter({cap: 0 for cap in plan["capability_max_calls"]})
    for row in rows:
        if row["max_calls"] != 1 or row["max_http_attempts"] != 1:
            raise ValueError("每项必须 single-attempt")
        command_calls[row["command"]] += 1
        capability_calls[fixtures[row["fixture_id"]].capability.value] += 1
    if dict(command_calls) != plan["command_max_calls"]:
        raise ValueError("command 额度不一致")
    if dict(capability_calls) != plan["capability_max_calls"]:
        raise ValueError("capability 额度不一致")
    if (
        len(rows) != plan["limits"]["total_http_attempts"]
        or len(rows) > 24
        or plan["limits"]["hard_ceiling"] != 35
    ):
        raise ValueError("总 HTTP attempts 超限或不一致")
    if rows[0]["command"] != "market.calendar":
        raise ValueError("首个真实请求必须 calendar")
    return plan


def stop_reason(http_status: int | None, business_code: int | None) -> str | None:
    """针对已由外部执行器验证的 HTTP/provider envelope 状态，失败即 STOP。"""
    if type(http_status) is not int:
        return "unknown_http_status"
    if http_status in (401, 403):
        return "authentication"
    if http_status == 429:
        return "rate_limit"
    if not 200 <= http_status < 300:
        return "http_failure"
    if type(business_code) is not int:
        return "unknown_business_code"
    if 2000 <= business_code <= 2999:
        return "authentication"
    if business_code != 0:
        return "business_failure"
    return None


def select_ths_group(items: list[dict]) -> str | None:
    """仅按已观察的 THS 标识排序；缺失字段 STOP，空 catalog 不猜补。"""
    codes = []
    for item in items:
        code = item.get("thscode")
        if not isinstance(code, str) or not re.fullmatch(
            r"\d{6}\.(SH|SZ|BJ|TI)", code, re.IGNORECASE
        ):
            raise ValueError("THS catalog 缺失可验证的 thscode")
        codes.append(code)
    return min(codes) if codes else None


def blocked_observation() -> dict:
    """复用 observation v2，离线记录 gate 分类，pages=0；不冒充 live evidence。"""
    fixture = load_fixtures(FIXTURES)[0]
    binding = Binding(
        "Fuyao",
        "hithink-finance-cli-pre-live-gate",
        "0.1.13",
        fixture.capability,
        "offline",
    )

    def blocked(_):
        require_single_attempt_executor()

    return observe(binding, fixture, blocked)


def run_live() -> None:
    """当前等待外部审查；零请求硬门禁，不运行 vendor CLI。"""
    require_single_attempt_executor()


def main() -> int:
    parser = argparse.ArgumentParser(description="Fuyao R1 离线 pre-live gate")
    parser.add_argument("--live-provider", action="store_true")
    args = parser.parse_args()
    if args.live_provider:
        try:
            run_live()
        except SingleAttemptGateBlocked:
            print(BLOCKER)
            return 3
    plan = load_plan()
    print(
        json.dumps(
            {
                "status": plan["status"],
                "http_attempts": 0,
                "reserved_http_attempts": plan["limits"]["total_http_attempts"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
