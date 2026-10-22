"""Named, byte-checked directory receipts for each exact planned request."""

import base64
from datetime import datetime

from market_data_foundation.candidates.tdx_exhq_directory import (
    CaptureContext,
    OfflineReceipt,
    PageRequest,
)
from market_data_foundation.candidates.tdx_exhq_directory.schema import (
    EvidenceMode,
)
from market_data_foundation.candidates.tdx_exhq_directory.schema import (
    Operation as DirectoryOperation,
)
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    Operation,
    Request,
    require,
)

from .catalog import associate_sample

DIRECTORY_OPERATIONS = ("ExMarkets", "ExCount", "ExInstruments")


def _receipt(document: dict) -> OfflineReceipt:
    require(
        type(document) is dict
        and set(document)
        == {
            "context",
            "operation",
            "payload",
            "expected_sha256",
            "locator",
            "request",
            "sdk_commit",
            "obtained_at",
            "received_at",
            "known_at",
            "identity",
        },
        "directory_receipt_shape",
    )
    values = dict(document)
    identity = values.pop("identity")
    context = dict(values["context"])
    context["mode"] = EvidenceMode(context["mode"])
    values["context"] = CaptureContext(**context)
    values["operation"] = DirectoryOperation(values["operation"])
    require(
        type(values["payload"]) is str and len(values["payload"]) <= 90000,
        "directory_payload_bound",
    )
    values["payload"] = base64.b64decode(values["payload"], validate=True)
    if values["request"] is not None:
        values["request"] = PageRequest(**values["request"])
    for key in ("obtained_at", "received_at", "known_at"):
        if values[key] is not None:
            values[key] = datetime.fromisoformat(values[key])
    receipt = OfflineReceipt(**values)
    require(receipt.material.identity == identity, "directory_receipt_identity")
    return receipt


def directory_associations(plan, document, *, mode, producer_sha256, require_all=False):
    """A declaration without retained bytes supplies no verified association."""
    associations = {}
    if document is not None:
        require(
            type(document) is dict
            and set(document) == {"schema", "plan_sha256", "entries"}
            and document["schema"] == "mdf.tdx.i4.directory-bindings.v1",
            "directory_bindings_shape",
        )
        from market_data_foundation.candidates.tdx_intraday_v1.model import (
            canonical,
            sha,
        )
        from market_data_foundation.candidates.tdx_intraday_v1.replay import (
            normalized_plan,
        )

        require(
            document["plan_sha256"] == sha(canonical(normalized_plan(plan))),
            "directory_plan_binding",
        )
        entries = document["entries"]
        require(
            type(entries) is list and len(entries) <= len(plan["requests"]),
            "directory_binding_count",
        )
        for entry in entries:
            require(
                type(entry) is dict
                and set(entry) == {"index", "request", "markets", "instruments"},
                "directory_binding_shape",
            )
            index = entry["index"]
            require(
                type(index) is int
                and 0 <= index < len(plan["requests"])
                and index not in associations,
                "directory_binding_index",
            )
            require(
                entry["request"] == plan["requests"][index], "directory_request_binding"
            )
            values = dict(entry["request"])
            values["operation"] = Operation(values["operation"])
            association = associate_sample(
                _receipt(entry["markets"]),
                _receipt(entry["instruments"]),
                Request(**values),
            )
            require(
                (
                    association.source,
                    association.binding,
                    association.mode,
                    association.producer_sha256,
                )
                == (plan["source"], plan["binding"], mode, producer_sha256),
                "directory_source_binding",
            )
            require(
                plan["directory_identity"] in ("", association.identity),
                "directory_declared_digest_conflict",
            )
            associations[index] = association
    if require_all:
        for index, request in enumerate(plan["requests"]):
            if (
                request["operation"].startswith("Ex")
                and request["operation"] not in DIRECTORY_OPERATIONS
            ):
                require(index in associations, "verified_directory_receipts_required")
    return associations
