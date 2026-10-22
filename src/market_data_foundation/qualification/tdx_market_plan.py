"""Frozen independent I-2 DEV budgets. No provider effects on import/construction."""

import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

from market_data_foundation.canonical.model import require
from market_data_foundation.qualification.contracts import Capability, Fixture
from market_data_foundation.runtime.tdx_watchlist import (
    V100_DRIVER_SDK_RELEASE,
    SessionConfig,
    decode_json,
)

UPSTREAM = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
SOURCE = "tdx-v100-protocol-discovery-i2-r1"
SEMANTICS = "mdf.i2.source-projection.v1"


def encoded(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()


def bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "input_file_budget")
    return raw


def integer(value, low, high):
    require(type(value) is int and low <= value <= high, "budget_invalid")


@dataclass(frozen=True)
class Request:
    operation: str
    market: int
    code: str = ""
    start: int = 0
    count: int = 0

    def __post_init__(self):
        integer(self.market, 0, 1)
        integer(self.start, 0, 65535)
        require(type(self.code) is str, "code_invalid")
        if self.operation == "GetCount":
            require(self.code == "" and self.start == 0, "count_shape_invalid")
            integer(self.count, 0, 0)
        elif self.operation == "GetCode":
            require(self.code == "", "directory_shape_invalid")
            integer(self.count, 1000, 1000)
        else:
            require(self.operation == "GetKline", "unverified_operation")
            require(bool(re.fullmatch(r"[0-9]{6}", self.code)), "code_invalid")
            require(
                self.code.startswith("6" if self.market else ("0", "3")),
                "stock_decoder_only",
            )
            integer(self.count, 1, 800)

    @property
    def capability(self):
        return (
            Capability.BARS_DAILY
            if self.operation == "GetKline"
            else Capability.SECURITY_UNIVERSE
        )

    @property
    def request_bytes(self):
        return {"GetCount": 18, "GetCode": 16, "GetKline": 38}[self.operation]

    def fixture(self, run_id: str, index: int) -> Fixture:
        symbol = ("sh" if self.market else "sz") + self.code
        return Fixture(
            f"i2-{run_id}-{index}",
            self.capability,
            (symbol,) if self.code else (),
            None,
            None,
            self.count or 1,
            {"schema": SEMANTICS, "units": "UNKNOWN", "source_clock": "UNKNOWN"},
        )


@dataclass(frozen=True)
class Plan:
    run_id: str
    endpoint: str
    endpoint_evidence_sha256: str
    collector_sha256: str
    requests: tuple[Request, ...]
    securities: tuple[str, ...]
    source_enabled: bool = True
    mode: str = "after_close"
    observation_day: str = ""
    session_window: str = ""
    max_request_bytes: int = 8192
    max_response_bytes: int = 4194304
    wall_seconds: int = 120
    quote_rounds: int = 1
    quote_period_seconds: int = 5
    quote_wall_seconds: int = 30
    schema: str = "mdf.i2.plan.v1"

    def __post_init__(self):
        require(
            bool(re.fullmatch(r"[a-f0-9]{32}", self.run_id)), "run_identity_invalid"
        )
        require(self.schema == "mdf.i2.plan.v1", "plan_schema_invalid")
        require(self.mode in {"after_close", "session"}, "mode_invalid")
        require(type(self.source_enabled) is bool, "source_mode_invalid")
        require(
            date.fromisoformat(self.observation_day).isoformat()
            == self.observation_day,
            "observation_day_required",
        )
        require(
            (self.mode == "session" and self.session_window in {"AM", "PM"})
            or (self.mode == "after_close" and self.session_window == ""),
            "explicit_session_window_required",
        )
        require(
            self.mode != "session" or not self.source_enabled, "session_quotes_only"
        )
        require(
            bool(re.fullmatch(r"[0-9.]+:7709", self.endpoint))
            and bool(re.fullmatch(r"[a-f0-9]{64}", self.endpoint_evidence_sha256)),
            "endpoint_evidence_required",
        )
        require(type(self.requests) is tuple, "frozen_requests_required")
        require(
            1 <= len(self.requests) <= 64
            and all(type(item) is Request for item in self.requests),
            "request_budget_invalid",
        )
        require(
            all(
                self.requests.count(item) == 1 or item.operation == "GetCount"
                for item in self.requests
            ),
            "duplicate_request",
        )
        require(
            bool(re.fullmatch(r"[a-f0-9]{64}", self.collector_sha256)),
            "collector_identity_required",
        )
        integer(self.max_request_bytes, 13, 8192)
        integer(self.max_response_bytes, 16, 4194304)
        integer(self.wall_seconds, 1, 120)
        integer(self.quote_rounds, 1 if self.mode == "after_close" else 3, 8)
        integer(self.quote_period_seconds, 1, 30)
        integer(self.quote_wall_seconds, 10, 120)
        require(
            (self.quote_rounds - 1) * self.quote_period_seconds + 10
            <= self.quote_wall_seconds,
            "quote_schedule_budget",
        )
        require(
            13 + sum(item.request_bytes for item in self.requests)
            <= self.max_request_bytes,
            "request_bytes_budget",
        )
        require(sum(item.count or 1 for item in self.requests) <= 64000, "item_budget")
        self.quote_config()

    def quote_config(self):
        return SessionConfig(
            securities=self.securities,
            endpoints=(self.endpoint,),
            sdk_candidates=False,
            max_probes=1,
            max_connections=1,
            max_failovers=0,
            rounds=self.quote_rounds,
            period_seconds=self.quote_period_seconds,
            duration_seconds=self.quote_wall_seconds,
            session_id=self.run_id,
            driver_sdk_release=V100_DRIVER_SDK_RELEASE,
        )

    @property
    def revision(self):
        return sha256(encoded(asdict(self))).hexdigest()

    @property
    def budgets(self):
        return {
            "source_requests": len(self.requests) if self.source_enabled else 0,
            "source_handshakes": int(self.source_enabled),
            "source_tcp_connections": int(self.source_enabled),
            "source_request_bytes": (
                13 + sum(r.request_bytes for r in self.requests)
                if self.source_enabled
                else 0
            ),
            "source_response_bytes": self.max_response_bytes
            if self.source_enabled
            else 0,
            "source_items": sum(r.count or 1 for r in self.requests)
            if self.source_enabled
            else 0,
            "quote_probes": 1,
            "quote_tcp_connections": 1,
            "quote_requests": self.quote_rounds,
            "quote_request_bytes": 13
            + self.quote_rounds * (22 + 7 * len(self.securities)),
            "quote_ipc_payload_bytes": self.quote_rounds * 131072,
            "quote_transport_response_bytes": "UNKNOWN_EXISTING_DRIVER_NO_READ_COUNTER",
            "wall_seconds": (self.wall_seconds if self.source_enabled else 0)
            + self.quote_wall_seconds
            + 6,
            "failovers": 0,
            "concurrency": 1,
        }

    def source_config(self):
        return {
            key: asdict(self)[key]
            for key in (
                "endpoint",
                "requests",
                "max_request_bytes",
                "max_response_bytes",
                "wall_seconds",
            )
        }

    @classmethod
    def load(cls, raw: bytes):
        require(len(raw) <= 65536, "plan_bytes_budget")
        value = decode_json(raw)
        value["requests"] = tuple(Request(**row) for row in value["requests"])
        value["securities"] = tuple(value["securities"])
        return cls(**value)


def verify_endpoint(plan: Plan, sdk: Path):
    """Require exact local pinned checkout evidence, never endpoint scanning."""
    import subprocess

    for ref in ("HEAD", "v1.0.0^{commit}"):
        actual = (
            subprocess.check_output(
                ["git", "-C", str(sdk), "rev-parse", ref], timeout=5
            )
            .decode()
            .strip()
        )
        require(actual == UPSTREAM, "sdk_identity_mismatch")
    require(
        not subprocess.check_output(
            ["git", "-C", str(sdk), "diff", "HEAD", "--"], timeout=5
        ),
        "sdk_modified",
    )
    raw = bounded_read(sdk / "hosts.go", 1024 * 1024)
    require(sha256(raw).hexdigest() == plan.endpoint_evidence_sha256, "endpoint_drift")
    hosts = re.findall(rb'"([0-9]+\.[0-9]+\.[0-9]+\.[0-9]+)"', raw)
    require(plan.endpoint.encode().split(b":")[0] in hosts, "unverified_endpoint")


def calendar_gate(raw: bytes, expected_sha256: str, plan: Plan, now: datetime):
    """Host-pinned intake of independently reviewed SH/SZ session evidence.

    This verifies intake identity and bounds, not exchange authority authenticity.
    Host review must supply the hash; the collector never creates this assertion.
    """
    require(len(raw) <= 16384, "calendar_bytes_budget")
    require(sha256(raw).hexdigest() == expected_sha256, "calendar_digest_mismatch")
    require(plan.mode == "session", "session_plan_required")
    gate = decode_json(raw)
    require(
        set(gate) == {"schema", "day", "reviewed_at", "expires_at", "markets"}
        and gate["schema"] == "mdf.i2.calendar-host-intake.v1",
        "calendar_intake_invalid",
    )
    from zoneinfo import ZoneInfo

    require(now.tzinfo is not None, "aware_clock_required")
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    require(
        gate["day"] == local.date().isoformat() == plan.observation_day,
        "calendar_day_mismatch",
    )
    reviewed = datetime.fromisoformat(gate["reviewed_at"])
    expires = datetime.fromisoformat(gate["expires_at"])
    require(
        reviewed.tzinfo is not None
        and expires.tzinfo is not None
        and reviewed <= now < expires
        and now - reviewed <= timedelta(days=7),
        "calendar_intake_stale",
    )
    require(
        (expires - now).total_seconds() > plan.budgets["wall_seconds"],
        "calendar_approval_expires_during_run",
    )
    markets = gate["markets"]
    require(type(markets) is list and len(markets) == 2, "separate_market_proofs")
    require({row["market"] for row in markets} == {"SH", "SZ"}, "market_scope_invalid")
    for row in markets:
        require(
            set(row) == {"market", "state", "authority", "material_sha256", "sessions"}
            and row["state"] == "OPEN"
            and bool(row["authority"])
            and bool(re.fullmatch(r"[a-f0-9]{64}", row["material_sha256"])),
            "market_open_evidence_required",
        )
        require(
            row["sessions"] == [["09:30", "11:30"], ["13:00", "15:00"]],
            "unreviewed_session_arrangement",
        )
    # Intentionally narrower observation windows. Entire budget must fit, with margin.
    minute = local.hour * 60 + local.minute
    lower, end = (
        (9 * 60 + 35, 11 * 60 + 30)
        if plan.session_window == "AM"
        else (13 * 60 + 30, 15 * 60)
    )
    require(lower <= minute < end, "outside_observation_window")
    remaining = (end - minute) * 60 - local.second - local.microsecond / 1_000_000
    require(plan.budgets["wall_seconds"] < remaining, "session_budget_crosses_close")
    close = local.replace(hour=end // 60, minute=end % 60, second=0, microsecond=0)
    return {
        "revision": expected_sha256,
        "day": gate["day"],
        "state": "HOST_INTAKE_OPEN",
        "deadline": min(close, expires).astimezone(UTC).isoformat(),
    }


def after_close_gate(
    now: datetime, observation_day: str, required_remaining_seconds: int
):
    from zoneinfo import ZoneInfo

    require(now.tzinfo is not None, "aware_clock_required")
    local = now.astimezone(ZoneInfo("Asia/Shanghai"))
    require(local.date().isoformat() == observation_day, "after_close_day_mismatch")
    require((local.hour, local.minute) >= (15, 5), "after_close_local_window_required")
    integer(required_remaining_seconds, 1, 246)
    midnight = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    seconds_until_midnight = (midnight - local).total_seconds()
    require(
        seconds_until_midnight > required_remaining_seconds,
        "after_close_budget_crosses_day",
    )
    return {
        "state": "LOCAL_AFTER_CLOSE_WINDOW",
        "calendar_qualification": "UNKNOWN",
        "deadline": midnight.astimezone(UTC).isoformat(),
    }


def utc_now():
    return datetime.now(UTC)
