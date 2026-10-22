"""Independent ordered transport accounting, including setup and terminal totals."""

import base64

from .model import canonical, require, sha
from .wire import payload_from_wire, planned_request_wire, setup_wire


def unbase64(value):
    require(type(value) is str and len(value) <= 90000, "base64_bound")
    return base64.b64decode(value, validate=True)


class ReceiptAudit:
    def __init__(self, plan):
        self.plan = plan
        self.intent = False
        self.setup = False
        self.setup_response = False
        self.next_index = 0
        self.pending = None
        self.stopped = False
        self.terminal = None
        self.totals = dict(
            requests=0,
            requested_bytes=0,
            written_bytes=0,
            response_bytes=0,
            decoded_bytes=0,
            frames=0,
        )

    def counters(self, record, fields):
        for field in fields:
            require(
                type(record.get(field)) is int and record[field] == self.totals[field],
                "record_counter_mismatch",
            )

    def budgets(self):
        for counter, budget in (
            ("requests", "max_requests"),
            ("requested_bytes", "max_request_bytes"),
            ("written_bytes", "max_request_bytes"),
            ("response_bytes", "max_response_bytes"),
            ("decoded_bytes", "max_decoded_bytes"),
            ("frames", "max_frames"),
        ):
            require(
                self.totals[counter] <= self.plan[budget], "cumulative_budget_exceeded"
            )

    def write(self, record, expected, *, failed=False):
        require(unbase64(record["request_wire"]) == expected, "request_wire_mismatch")
        require(
            type(record.get("requested_bytes")) is int
            and record["requested_bytes"] == len(expected),
            "requested_bytes_mismatch",
        )
        written = record.get("written_bytes")
        require(
            type(written) is int and 0 <= written <= len(expected),
            "written_bytes_mismatch",
        )
        require(failed or written == len(expected), "partial_write_without_stop")
        self.totals["requested_bytes"] += len(expected)
        self.totals["written_bytes"] += written
        self.budgets()

    def consume(self, record):
        from .replay import normalized_plan

        p = self.plan
        require(type(record) is dict, "record_object_required")
        require(
            record.get("schema") == p["schema"] and record.get("run_id") == p["run_id"],
            "record_run_identity",
        )
        require(self.terminal is None, "record_after_terminal")
        kind = record.get("classification")
        if kind == "stopped":
            require(self.intent, "stop_before_intent")
            self.terminal = "STOPPED"
            self.stopped = True
            return None
        require(not self.stopped, "record_after_failure")
        require(
            record.get("plan_sha256") == sha(canonical(normalized_plan(p)))
            and record.get("sdk_commit") == p["sdk_commit"]
            and record.get("endpoint") == p["endpoint"],
            "record_binding_identity",
        )
        if kind == "":
            require(
                not self.intent
                and record.get("index") == -1
                and record.get("plan") == normalized_plan(p),
                "intent_order_or_plan",
            )
            self.counters(record, tuple(self.totals)[1:])
            require(
                not record.get("request_wire") and not record.get("wire"),
                "intent_has_wire",
            )
            self.intent = True
            return None
        require(self.intent, "missing_intent")
        if kind in ("setup_written", "setup_write_failed"):
            require(
                not self.setup and self.next_index == 0 and record.get("index") == -1,
                "setup_order",
            )
            self.write(record, setup_wire(p["family"]), failed=kind.endswith("failed"))
            self.counters(record, ("response_bytes", "decoded_bytes", "frames"))
            self.setup = True
            self.stopped = kind.endswith("failed")
            return None
        if kind == "budget_complete":
            require(
                self.setup
                and self.next_index == len(p["requests"])
                and self.pending is None,
                "premature_completion",
            )
            require(
                record.get("index") == -1
                and not record.get("request_wire")
                and not record.get("wire"),
                "terminal_shape",
            )
            self.counters(record, tuple(self.totals)[1:])
            self.terminal = "TRANSPORT_BUDGET_COMPLETE_NOT_QUALIFICATION"
            return None
        if kind not in (
            "setup_response",
            "payload_retained_not_semantically_qualified",
            "request_write_failed",
            "response_read_or_validation_failed",
            "response_identity_mismatch",
        ):
            # A classified dial failure is allowed only before setup; never completion.
            require(
                not self.setup and record.get("index") == -1, "unknown_transport_record"
            )
            self.counters(record, tuple(self.totals)[1:])
            self.stopped = True
            return None
        require(self.setup, "response_before_setup")
        index = record.get("index")
        require(
            type(index) is int
            and index == self.next_index
            and index < len(p["requests"]),
            "request_order",
        )
        request = p["requests"][index]
        require(record.get("request") == request, "record_request_mismatch")
        expected = planned_request_wire(request, index + 1)
        if self.pending is None:
            self.write(record, expected, failed=kind == "request_write_failed")
            self.totals["requests"] += 1
            self.pending = index
        else:
            require(
                unbase64(record["request_wire"]) == expected
                and type(record.get("requested_bytes")) is int
                and type(record.get("written_bytes")) is int
                and record["requested_bytes"]
                == record["written_bytes"]
                == len(expected),
                "repeated_request_accounting",
            )
        if kind == "request_write_failed":
            require(not record.get("wire"), "failed_write_has_response")
            self.stopped = True
            return None
        wire = unbase64(record.get("wire", ""))
        require(record.get("wire_sha256") == sha(wire), "record_wire_digest")
        failed = kind in (
            "response_read_or_validation_failed",
            "response_identity_mismatch",
        )
        if kind == "response_read_or_validation_failed":
            payload = b""  # Go retains no decoded bytes when frame validation fails.
        else:
            operation, message_id = request["operation"], index + 1
            if kind == "setup_response":
                require(not self.setup_response, "duplicate_setup_response")
                operation, message_id = (
                    ("ExSetup" if p["family"] == "exhq" else "Setup"),
                    0,
                )
                self.setup_response = True
            if kind == "response_identity_mismatch":
                # STOP; retained payload supplies partial accounting only.
                payload = unbase64(record.get("payload", ""))
            else:
                payload = payload_from_wire(
                    wire, message_id=message_id, operation=operation
                )
        require(
            record.get("payload_sha256") == sha(payload)
            and unbase64(record.get("payload", "")) == payload,
            "record_payload_bytes_or_digest",
        )
        self.totals["response_bytes"] += len(wire)
        self.totals["decoded_bytes"] += len(payload)
        self.totals["frames"] += 1
        self.counters(record, ("response_bytes", "decoded_bytes", "frames"))
        self.budgets()
        if failed:
            self.stopped = True
            return None
        if kind == "setup_response":
            return None
        self.pending = None
        self.next_index += 1
        return index, request, wire, payload
