"""Bounded Host receipt intake into a new ignored artifact directory."""

import argparse
import json
from datetime import datetime
from pathlib import Path

from market_data_foundation.candidates.tdx_exhq_directory.wire import (
    PageRequest,
    decode_count,
    decode_instruments,
    decode_markets,
)
from market_data_foundation.qualification.harness import classify_exception

from .decode import decode
from .model import (
    SDK_COMMIT,
    Material,
    Operation,
    Request,
    canonical,
    require,
    sha,
)
from .receipt_audit import ReceiptAudit
from .wire import request_wire


def strict_json(raw: bytes) -> object:
    def unique(items):
        result = {}
        for key, value in items:
            require(key not in result, "duplicate_json_key")
            result[key] = value
        return result

    return json.loads(
        raw,
        object_pairs_hook=unique,
        parse_constant=lambda _: require(False, "nonfinite_json"),
    )


def normalized_plan(plan: dict) -> dict:
    # Go encoding/json encodes integral float64 values without a decimal point.
    return {
        key: int(value) if type(value) is float and value.is_integer() else value
        for key, value in plan.items()
    }


def read_bounded(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        raw = stream.read(limit + 1)
    require(len(raw) <= limit, "artifact_input_bound")
    return raw


def replay(
    plan: dict,
    records: tuple[dict, ...],
    output: Path,
    *,
    producer_sha256: str,
    evidence_mode: str,
    directory_receipts: Path | None = None,
) -> dict:
    from .plan import validate_plan

    require(
        evidence_mode in ("synthetic", "retained"), "explicit_evidence_mode_required"
    )
    validate_plan(plan)
    require(
        type(plan) is dict and plan.get("sdk_commit") == SDK_COMMIT, "plan_sdk_mismatch"
    )
    require(plan.get("schema") == "mdf.tdx.i4.raw-probe.v1", "plan_schema")
    require(
        type(plan.get("requests")) is list and 1 <= len(plan["requests"]) <= 24,
        "plan_request_bound",
    )
    require(len(records) <= 2 * len(plan["requests"]) + 6, "record_count_bound")
    frozen = canonical(normalized_plan(plan))
    plan_hash = sha(frozen)
    require(".." not in output.parts, "artifact_parent_traversal_rejected")
    destination = output.absolute()
    root = (Path.cwd() / "artifacts").absolute()
    require(
        destination.is_relative_to(root) and destination != root,
        "ignored_artifacts_output_required",
    )
    for parent in (destination, *destination.parents):
        require(not parent.is_symlink(), "artifact_symlink_rejected")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    (destination / "plan.json").write_bytes(frozen + b"\n")
    # Retain all received records before semantic intake, including malformed ones.
    (destination / "records.jsonl").write_bytes(
        b"".join(canonical(r) + b"\n" for r in records)
    )
    summary = {
        "schema": "mdf.tdx.i4.intake.v1",
        "run_id": plan["run_id"],
        "plan_sha256": plan_hash,
        "producer_sha256": producer_sha256,
        "evidence_mode": evidence_mode,
        "producer_attestation": "CALLER_DECLARED_REQUIRES_HOST_PIN_RECEIPT",
        "effects": {
            "provider_requests_by_replay": 0,
            "pg_writes": 0,
            "garage_writes": 0,
        },
        "source_freshness": "UNKNOWN",
        "active_refresh": "NOT_PROVEN",
        "pages": [],
        "failures": [],
        "terminal": "INCOMPLETE",
    }
    from market_data_foundation.candidates.tdx_exhq_v1.catalog import attach_association
    from market_data_foundation.candidates.tdx_exhq_v1.retained import (
        directory_associations,
    )

    associations = {}
    if directory_receipts is not None:
        try:
            document = strict_json(read_bounded(directory_receipts, 4 * 1024 * 1024))
            (destination / "directory-receipts.json").write_bytes(
                canonical(document) + b"\n"
            )
            associations = directory_associations(
                plan, document, mode=evidence_mode, producer_sha256=producer_sha256
            )
        except Exception as error:
            summary["failures"].append(
                {"directory_exception": classify_exception(error)}
            )
    seen = set()
    audit = ReceiptAudit(plan)
    for number, record in enumerate(records):
        try:
            result = audit.consume(record)
            if result is None:
                continue
            index, request, wire, payload = result
            (destination / f"{index:03d}.wire.bin").write_bytes(wire)
            (destination / f"{index:03d}.payload.bin").write_bytes(payload)
            operation = request["operation"]
            if operation in ("ExMarkets", "ExCount", "ExInstruments"):
                if operation == "ExMarkets":
                    parsed = decode_markets(payload)
                    count = len(parsed.rows)
                elif operation == "ExCount":
                    count = decode_count(payload)
                else:
                    parsed = decode_instruments(
                        payload, PageRequest(request["start"], request["count"])
                    )
                    count = len(parsed.rows)
                seen.add(index)
                summary["pages"].append(
                    {
                        "index": index,
                        "operation": operation,
                        "rows": count,
                        "raw_sha256": sha(payload),
                        "qualification": "DIRECTORY_STRUCTURE_ONLY",
                    }
                )
                continue
            request = dict(request)
            request["operation"] = Operation(request["operation"])
            req = Request(**request)
            material = Material(
                req,
                payload,
                sha(payload),
                str(destination / f"{index:03d}.payload.bin"),
                plan["source"],
                plan["binding"],
                plan["run_id"],
                evidence_mode,
                datetime.fromisoformat(record["requested_at"]),
                datetime.fromisoformat(record["received_at"]),
                None,
                producer_sha256=producer_sha256,
                wire_sha256=sha(wire),
                endpoint=plan["endpoint"],
                peer=record.get("peer"),
                request_wire_sha256=sha(request_wire(req, index + 1)),
            )
            if index in associations:
                material = attach_association(material, associations[index])
            page = decode(material)
            projection = {
                "receipt": material.receipt(),
                "page_identity": page.identity,
                "content_identity": page.content_identity,
                "findings": page.findings,
                "rows": [
                    {
                        "identity": r.identity,
                        "index": r.index,
                        "byte_offset": r.byte_offset,
                        "byte_length": r.byte_length,
                        "raw_sha256": r.raw_sha256,
                        "native": r.native,
                        "source_local": r.source_local,
                        "source_utc": r.source_utc,
                        "clock_status": r.clock_status,
                    }
                    for r in page.rows
                ],
            }
            (destination / f"{index:03d}.projection.json").write_bytes(
                canonical(projection) + b"\n"
            )
            seen.add(index)
            summary["pages"].append(
                {
                    "index": index,
                    "operation": operation,
                    "rows": len(page.rows),
                    "page_identity": page.identity,
                    "raw_sha256": sha(payload),
                    "findings": page.findings,
                }
            )
        except Exception as error:
            audit.stopped = True
            summary["failures"].append(
                {"record": number, "exception": classify_exception(error)}
            )
    summary["terminal"] = (
        "STOPPED"
        if audit.stopped or summary["failures"]
        else audit.terminal or "INCOMPLETE"
    )
    summary["recomputed_totals"] = audit.totals
    summary["accounting_status"] = (
        "COMPLETE"
        if summary["terminal"] == "TRANSPORT_BUDGET_COMPLETE_NOT_QUALIFICATION"
        else "PARTIAL_RETAINED_RECORDS_ONLY"
    )
    summary["received_request_pages"] = len(seen)
    summary["all_requested_pages_received"] = len(seen) == len(plan["requests"])
    (destination / "summary.json").write_bytes(canonical(summary) + b"\n")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--records", type=Path, required=True)
    parser.add_argument("--producer-sha256", required=True)
    parser.add_argument(
        "--evidence-mode", choices=("synthetic", "retained"), required=True
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--directory-receipts", type=Path)
    args = parser.parse_args()
    try:
        plan = strict_json(read_bounded(args.plan, 65536))
        raw = read_bounded(args.records, 6 * 1024 * 1024)
        records = tuple(strict_json(line) for line in raw.splitlines() if line)
        summary = replay(
            plan,
            records,
            args.output,
            producer_sha256=args.producer_sha256,
            evidence_mode=args.evidence_mode,
            directory_receipts=args.directory_receipts,
        )
        print(
            json.dumps(
                {
                    "pages": len(summary["pages"]),
                    "failures": len(summary["failures"]),
                    "terminal": summary["terminal"],
                }
            )
        )
        return (
            0
            if not summary["failures"]
            and summary["all_requested_pages_received"]
            and summary["terminal"] == "TRANSPORT_BUDGET_COMPLETE_NOT_QUALIFICATION"
            else 1
        )
    except Exception as error:
        print(json.dumps({"exception": classify_exception(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
