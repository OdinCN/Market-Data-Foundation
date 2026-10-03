"""冻结 R1 的 stdlib 单次 HTTPS 执行原语；公开入口仍等待外部审查。"""

import argparse
import hashlib
import http.client
import json
import os
import re
import signal
import ssl
import stat
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlencode

from provider_lab import fuyao_r1 as r1

HOST = "fuyao.aicubes.cn"
PORT = 443
TIMEOUT = 30
BODY_CAP = 2 * 1024 * 1024
KEY_CAP = 16 * 1024
SEAL_CAP = 4096
BASELINE = "175d1a0037312ebe734b73763f601d2469b0bb01"
TRANSACTION = "MDF-P2-FUYAO-R1"
ACCEPTED_EFFECT = "AUTHORIZE_R02_R24_LIVE_TRANSPORT"
CANONICAL_PYTHON = Path("/srv/market-data-foundation/.venv/bin/python")
_PROCESS_USED = False

# endpoint 来自 remote-capabilities.js；参数名来自冻结 schema/queryFor。
COMMANDS = MappingProxyType(
    {
        "market.calendar": ("/api/a-share/calendar/trading-days", ()),
        "symbol.list": (
            "/api/meta/tickers/list",
            (
                ("--exchange", "exchange"),
                ("--asset-type", "asset_type"),
                ("--limit", "limit"),
                ("--offset", "offset"),
            ),
        ),
        "symbol.search": (
            "/api/meta/tickers/search",
            (("--q", "q"), ("--asset-type", "asset_type"), ("--limit", "limit")),
        ),
        "market.snapshot": (
            "/api/a-share/prices/snapshot",
            (("--thscodes", "thscodes"), ("--limit", "limit"), ("--offset", "offset")),
        ),
        "market.history": (
            "/api/a-share/prices/historical",
            (
                ("--thscode", "thscode"),
                ("--start-ms", "start"),
                ("--end-ms", "end"),
                ("--adjust", "adjust"),
            ),
        ),
        "market.corporate-actions": (
            "/api/a-share/corporate-actions/adjustment-factors",
            (("--thscode", "thscode"), ("--from-date", "from"), ("--to-date", "to")),
        ),
        "financials.balance-sheet": (
            "/api/a-share/financials/balance-sheets",
            (("--thscode", "thscode"), ("--period", "period"), ("--limit", "limit")),
        ),
        "financials.income": (
            "/api/a-share/financials/income-statements",
            (("--thscode", "thscode"), ("--period", "period"), ("--limit", "limit")),
        ),
        "financials.cash-flow": (
            "/api/a-share/financials/cash-flow-statements",
            (("--thscode", "thscode"), ("--period", "period"), ("--limit", "limit")),
        ),
        "index.catalog": (
            "/api/a-share-index/catalog/ths-index-list",
            (("--tag", "tag"),),
        ),
        "index.constituents": (
            "/api/a-share-index/constituents/ths-stock-list",
            (("--thscode", "thscode"),),
        ),
    }
)


class _HTTPSConnection(http.client.HTTPSConnection):
    def putrequest(self, method, url, skip_host=False, skip_accept_encoding=False):
        # 仅保留标准 Host 与三个契约 header，取消 stdlib 隐式 Accept-Encoding。
        super().putrequest(method, url, skip_host=skip_host, skip_accept_encoding=True)


@dataclass
class Result:
    """仅 metadata 可直接输出；data 留在内存供独立能力验证。"""

    metadata: dict
    data: dict | None = None


def _metadata() -> dict:
    return {
        "schema_version": 1,
        "executor": "fuyao-single-attempt-v1",
        "plan_sha256": r1.PLAN_SHA256,
        "request_id": None,
        "planned_command": None,
        "target": None,
        "http_request_invocations": 0,
        "http_status": None,
        "business_code": None,
        "provider_request_id": None,
        "byte_count": 0,
        "stop_reason": None,
        "transport_success": False,
        "capability_pass": False,
        "exception_category": None,
        "connected_endpoint": None,
        "endpoint_unobservable_reason": "未采集实际远端 IP；target 仅为固定请求目标",
    }


def resolve_request(request_id: str, catalog: Result | None = None) -> tuple:
    """只按冻结 id 取行；动态 id 必须由对应先前 catalog 结果确定。"""
    rows = r1.load_plan()["requests"]
    row = next((row for row in rows if row["id"] == request_id), None)
    if row is None:
        raise ValueError("invalid_request")
    path, names = COMMANDS[row["command"]]
    argv = row["argv"]
    if tuple(argv[::2]) != tuple(flag for flag, _ in names):
        raise ValueError("invalid_mapping")
    query = {name: value for (_, name), value in zip(names, argv[1::2], strict=True)}
    if row["command"] == "market.history":
        query["interval"] = "1d"
    if "selection" in row:
        prior_id = {"r22": "r21", "r24": "r23"}[request_id]
        prior_row, prior_target = resolve_request(prior_id)
        if (
            not isinstance(catalog, Result)
            or catalog.metadata.get("request_id") != prior_id
            or catalog.metadata.get("planned_command") != prior_row["command"]
            or catalog.metadata.get("target") != prior_target
            or catalog.metadata.get("plan_sha256") != r1.PLAN_SHA256
            or catalog.metadata.get("http_request_invocations") != 1
            or catalog.metadata.get("transport_success") is not True
            or catalog.metadata.get("stop_reason") is not None
            or r1.stop_reason(
                catalog.metadata.get("http_status"),
                catalog.metadata.get("business_code"),
            )
            is not None
            or not _valid_data(catalog.data)
        ):
            raise ValueError("invalid_catalog")
        code = r1.select_ths_group(catalog.data["item"])
        if code is None:
            return row, None
        query["thscode"] = code
    return row, {
        "scheme": "https",
        "host": HOST,
        "port": PORT,
        "method": "GET",
        "path": path,
        "query": query,
    }


def _valid_data(data) -> bool:
    # 冻结 R1 各命令均为 vendor itemOutput，不替代字段/单位/覆盖能力验证。
    return (
        isinstance(data, dict)
        and isinstance(data.get("item"), list)
        and all(isinstance(item, dict) for item in data["item"])
    )


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_json_key")
        result[key] = value
    return result


def _invalid_constant(_):
    raise ValueError("invalid_json_constant")


def _decode(body):
    return json.loads(
        body.decode("utf-8"),
        object_pairs_hook=_unique_object,
        parse_constant=_invalid_constant,
    )


@contextmanager
def _deadline():
    """Unix 主线程的总墙钟上限，包含慢速持续响应；不延长外层截止时间。"""
    previous = signal.getsignal(signal.SIGALRM)
    old_delay, old_interval = signal.getitimer(signal.ITIMER_REAL)
    started = time.monotonic()

    def expired(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, min(TIMEOUT, old_delay or TIMEOUT))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
        if old_delay:
            signal.setitimer(
                signal.ITIMER_REAL,
                max(0.000001, old_delay - (time.monotonic() - started)),
                old_interval,
            )


def _valid_key(key) -> bool:
    return (
        isinstance(key, str)
        and 0 < len(key) <= KEY_CAP
        and all(33 <= ord(char) <= 126 for char in key)
    )


def contains_key(value, key):
    """检查解析字符串本身，不能依赖 JSON 转义后的子串比较。"""
    if isinstance(value, str):
        return key in value
    if isinstance(value, dict):
        return any(
            contains_key(k, key) or contains_key(v, key) for k, v in value.items()
        )
    if isinstance(value, list):
        return any(contains_key(item, key) for item in value)
    return False


def execute_one(
    request_id: str, api_key: str, *, catalog: Result | None = None
) -> Result:
    """审查后供外部串行调度器调用；每个进程终身至多调用 request 一次。"""
    global _PROCESS_USED
    meta = _metadata()
    result = Result(meta)
    connection = None
    try:
        row, target = resolve_request(request_id, catalog)
        meta.update(request_id=row["id"], planned_command=row["command"], target=target)
        if not _valid_key(api_key):
            meta["stop_reason"] = "invalid_key_input"
            return result
        # 防止目标/标识恰好等于或包含凭据；不输出任何凭据值。
        if contains_key(meta, api_key):
            result.metadata = _metadata()
            result.metadata["stop_reason"] = "credential_collision"
            return result
        if _PROCESS_USED:
            meta["stop_reason"] = "process_already_used"
            return result
        _PROCESS_USED = True
        if target is None:
            meta["stop_reason"] = "empty_catalog_skip"
            return result
        with _deadline():
            connection = _HTTPSConnection(
                HOST, PORT, timeout=TIMEOUT, context=ssl.create_default_context()
            )
            path = target["path"]
            if target["query"]:
                path += "?" + urlencode(target["query"])
            headers = {
                "X-api-key": api_key,
                "X-THS-Skill-Id": "Hithink-Finance-CLI",
                "Accept": "application/json",
            }
            meta["http_request_invocations"] = 1
            connection.request("GET", path, headers=headers)
            response = connection.getresponse()
            meta["http_status"] = response.status
            chunks = []
            # 上限处仅额外读取一个探测字节；超限立刻 STOP，绝不 drain。
            while True:
                chunk = response.read1(min(65536, BODY_CAP + 1 - meta["byte_count"]))
                meta["byte_count"] += len(chunk)
                if meta["byte_count"] > BODY_CAP:
                    meta["stop_reason"] = "body_cap_exceeded"
                    return result
                if not chunk:
                    if getattr(response, "length", None) not in (None, 0):
                        raise http.client.IncompleteRead(b"")
                    break
                chunks.append(chunk)
            try:
                envelope = _decode(b"".join(chunks))
            except ValueError, UnicodeError, RecursionError:
                meta["stop_reason"] = (
                    r1.stop_reason(response.status, None)
                    if not 200 <= response.status < 300
                    else "invalid_json"
                )
                return result
            valid = (
                isinstance(envelope, dict)
                and type(envelope.get("code")) is int
                and abs(envelope["code"]) <= 2**53 - 1
                and isinstance(envelope.get("message"), str)
                and "data" in envelope
                and (
                    "request_id" not in envelope
                    or isinstance(envelope["request_id"], str)
                )
            )
            if isinstance(envelope, dict):
                code = envelope.get("code")
                if type(code) is int and abs(code) <= 2**53 - 1:
                    meta["business_code"] = code
                provider_id = envelope.get("request_id")
                if (
                    isinstance(provider_id, str)
                    and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", provider_id)
                    and api_key not in provider_id
                ):
                    meta["provider_request_id"] = provider_id
            meta["stop_reason"] = r1.stop_reason(response.status, meta["business_code"])
            if meta["stop_reason"] is not None:
                return result
            if not valid:
                meta["stop_reason"] = "invalid_envelope"
                return result
            if not _valid_data(envelope["data"]):
                meta["stop_reason"] = "invalid_data"
                return result
            if contains_key(envelope, api_key):
                meta["stop_reason"] = "credential_echo"
                return result
            meta["transport_success"] = True
            result.data = envelope["data"]
    except TimeoutError:
        meta.update(
            stop_reason="timeout", exception_category="timeout", transport_success=False
        )
    except OSError, ssl.SSLError, http.client.HTTPException:
        meta.update(
            stop_reason="network_error",
            exception_category="network",
            transport_success=False,
        )
    except Exception:
        # 不保存异常文本、动态异常类名或响应 message。
        meta.update(
            stop_reason="executor_error",
            exception_category="internal",
            transport_success=False,
        )
    finally:
        if not meta["transport_success"]:
            result.data = None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                meta.update(
                    stop_reason="close_error",
                    exception_category="internal",
                    transport_success=False,
                )
                result.data = None
    return result


class _SafeParser(argparse.ArgumentParser):
    def error(self, _):
        raise ValueError("invalid_arguments")


def _private_fd(fd: int) -> int:
    info = os.fstat(fd)
    if (
        fd < 3
        or not stat.S_ISFIFO(info.st_mode)
        or info.st_mode & 0o077
        or info.st_uid != os.geteuid()
    ):
        raise ValueError("invalid_private_fd")
    return fd


def _read_fd(fd: int, cap: int) -> bytes:
    with os.fdopen(os.dup(fd), "rb") as stream:
        value = stream.read(cap + 1)
    if len(value) > cap:
        raise ValueError("input_cap_exceeded")
    return value


def executor_sha256() -> str:
    return hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


def validate_seal(seal: dict) -> None:
    """只验证外部调度器经私有 FD 交付的 seal，不发现控制路径。"""
    if not isinstance(seal, dict) or any(
        seal.get(name) != expected
        for name, expected in {
            "review": "AO_EXTERNAL_SINGLE_ATTEMPT_REVIEW_PASS",
            "executor_sha256": executor_sha256(),
            "plan_sha256": r1.PLAN_SHA256,
            "baseline": BASELINE,
            "transaction": TRANSACTION,
            "accepted_effect": ACCEPTED_EFFECT,
        }.items()
    ):
        raise r1.SingleAttemptGateBlocked("invalid_review_seal")
    plan = r1.load_plan()
    if plan["baseline"] != BASELINE:
        raise r1.SingleAttemptGateBlocked("invalid_baseline")


def main() -> int:
    """唯一 live CLI 门禁为继承的私有 review pipe；r01 已消费。"""
    meta = _metadata()
    try:
        parser = _SafeParser(description="Fuyao R1 外部 seal 单次执行器")
        parser.add_argument("--request-id", required=True)
        parser.add_argument("--review-seal-fd", type=int)
        parser.add_argument("--key-fd", type=int)
        parser.add_argument("--catalog-fd", type=int)
        parser.add_argument("--response-fd", type=int)
        args = parser.parse_args()
        with _deadline():
            if (
                sys.version_info[:3] != (3, 14, 8)
                or Path(sys.executable).resolve() != CANONICAL_PYTHON.resolve()
            ):
                raise ValueError("canonical_python_required")
            if args.review_seal_fd is None:
                raise r1.SingleAttemptGateBlocked("external_review_required")
            seal_fd = _private_fd(args.review_seal_fd)
            validate_seal(_decode(_read_fd(seal_fd, SEAL_CAP)))
            if args.request_id not in tuple(f"r{i:02}" for i in range(2, 25)):
                raise ValueError("consumed_or_invalid_request")
            key_fd = 0 if args.key_fd is None else _private_fd(args.key_fd)
            # stdin 也只能是 pipe/FIFO，不能借 stdin 传文件路径来源。
            info = os.fstat(key_fd)
            if (
                not stat.S_ISFIFO(info.st_mode)
                or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
            ):
                raise ValueError("invalid_stdin_pipe")
            fds = [seal_fd, key_fd, _private_fd(args.response_fd)]
            if args.catalog_fd is not None:
                fds.append(_private_fd(args.catalog_fd))
            if len(set(fds)) != len(fds):
                raise ValueError("fd_collision")
            key = _read_fd(key_fd, KEY_CAP).decode("ascii").rstrip("\r\n")
            catalog = None
            if args.catalog_fd is not None:
                prior = _decode(_read_fd(args.catalog_fd, BODY_CAP * 6 + 65536))
                catalog = Result(prior["metadata"], prior["data"])
            result = execute_one(args.request_id, key, catalog=catalog)
            meta = result.metadata
            if result.data is not None:
                with os.fdopen(os.dup(args.response_fd), "w") as stream:
                    json.dump({"metadata": meta, "data": result.data}, stream)
    except r1.SingleAttemptGateBlocked:
        meta["stop_reason"] = "external_review_required"
    except TimeoutError:
        meta.update(
            stop_reason="timeout", exception_category="timeout", transport_success=False
        )
    except Exception:
        meta.update(
            stop_reason="invalid_input",
            exception_category="validation",
            transport_success=False,
        )
    print(json.dumps(meta, ensure_ascii=False))
    return 0 if meta["transport_success"] else 3


if __name__ == "__main__":
    raise SystemExit(main())
