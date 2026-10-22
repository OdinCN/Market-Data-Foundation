"""Freeze explicit bounded Host plans; this module opens no network connection."""

import argparse
import re
from pathlib import Path

from .model import SDK_COMMIT, Operation, Request, canonical, require, sha
from .replay import normalized_plan, read_bounded, strict_json
from .wire import request_wire


def validate_plan(plan: dict) -> dict:
    require(type(plan) is dict, "plan_object_required")
    fields = {
        "schema",
        "sdk_commit",
        "run_id",
        "source",
        "binding",
        "endpoint",
        "family",
        "directory_identity",
        "requests",
        "max_requests",
        "max_request_bytes",
        "max_response_bytes",
        "max_decoded_bytes",
        "max_frames",
        "wall_seconds",
        "operation_seconds",
        "round_interval_seconds",
    }
    require(set(plan) == fields, "exact_plan_fields_required")
    require(
        plan["schema"] == "mdf.tdx.i4.raw-probe.v1"
        and plan["sdk_commit"] == SDK_COMMIT,
        "plan_revision",
    )
    require(
        type(plan["run_id"]) is str
        and bool(re.fullmatch(r"[a-f0-9]{32}", plan["run_id"])),
        "plan_run_id",
    )
    require(plan["family"] in ("standard", "exhq"), "plan_family")
    for field in ("source", "binding"):
        require(
            type(plan[field]) is str and 1 <= len(plan[field]) <= 128, "plan_binding"
        )
    require(
        type(plan["endpoint"]) is str
        and bool(re.fullmatch(r"[0-9.]+:(7709|7727)", plan["endpoint"])),
        "explicit_endpoint_required",
    )
    require(
        plan["endpoint"].endswith(":7727" if plan["family"] == "exhq" else ":7709"),
        "endpoint_family_port",
    )
    require(
        plan["directory_identity"] == ""
        or bool(re.fullmatch(r"[a-f0-9]{64}", plan["directory_identity"])),
        "directory_digest",
    )
    require(
        type(plan["requests"]) is list and 1 <= len(plan["requests"]) <= 24,
        "request_count_bound",
    )
    require(
        type(plan["max_requests"]) is int
        and plan["max_requests"] == len(plan["requests"]),
        "request_count_exact",
    )
    for field, minimum, maximum in (
        ("max_request_bytes", 1, 65536),
        ("max_response_bytes", 16, 4194304),
        ("max_decoded_bytes", 1, 4194304),
        ("max_frames", len(plan["requests"]), 2 * len(plan["requests"]) + 1),
    ):
        require(
            type(plan[field]) is int and minimum <= plan[field] <= maximum,
            "integer_budget_bound",
        )
    for field, minimum, maximum in (
        ("wall_seconds", 1, 180),
        ("operation_seconds", 0.1, 10),
        ("round_interval_seconds", 0, 10),
    ):
        require(
            type(plan[field]) in (int, float) and minimum <= plan[field] <= maximum,
            "time_budget_bound",
        )
    size = 120 if plan["family"] == "exhq" else 13
    for index, raw in enumerate(plan["requests"]):
        require(type(raw) is dict, "request_object_required")
        require(
            set(raw)
            == {
                "operation",
                "market",
                "code",
                "category",
                "start",
                "count",
                "date",
                "period",
                "kind",
            },
            "exact_request_fields_required",
        )
        require(
            raw["operation"].startswith("Ex") == (plan["family"] == "exhq"),
            "mixed_family",
        )
        if raw["operation"] in ("ExMarkets", "ExCount", "ExInstruments"):
            require(
                raw["market"] == 0
                and type(raw["market"]) is int
                and raw["code"] == ""
                and raw["category"] is None
                and raw["date"] == ""
                and raw["period"] == 0
                and raw["kind"] == "",
                "directory_request_shape",
            )
            require(
                type(raw["start"]) is int
                and type(raw["count"]) is int
                and 0 <= raw["start"] <= 65535,
                "directory_page_type",
            )
            if raw["operation"] == "ExInstruments":
                require(
                    1 <= raw["count"] <= 2000 and raw["start"] + raw["count"] <= 65536,
                    "directory_page_bound",
                )
                size += 18
            else:
                require(raw["start"] == raw["count"] == 0, "unused_directory_page")
                size += 12
        else:
            request = dict(raw)
            request["operation"] = Operation(request["operation"])
            size += len(request_wire(Request(**request), index + 1))
    require(size <= plan["max_request_bytes"], "preflight_request_bytes")
    # Exact SDK endpoint membership is rechecked inside Go before dialing.
    return {
        "plan_sha256": sha(canonical(normalized_plan(plan))),
        "request_bytes_upper_bound": size,
        "endpoint_evidence": "REQUIRES_GO_PINNED_SDK_MEMBERSHIP_RECHECK",
        "connections": 1,
        "failovers": 0,
        "retries": 0,
        "calendar": "UNKNOWN_UNTIL_EXACT_SESSION_EVIDENCE",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    plan = strict_json(read_bounded(args.input, 65536))
    receipt = validate_plan(plan)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("xb") as stream:
        stream.write(canonical(normalized_plan(plan)) + b"\n")
    print(canonical(receipt).decode())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
