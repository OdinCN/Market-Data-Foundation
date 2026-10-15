"""Producer-only atomic acceptance materialization, never a Publication."""

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from market_data_foundation.runtime.tdx_watchlist import EVENT_REASONS

RECEIPT_REVISION = "mdf.nonproduction.watchlist.acceptance.v1"
RECEIPT_NAME = "read-ready.json"
STATES = {"starting", "pending_ack", "accepted", "ended", "unavailable"}
CLASSIFICATIONS = EVENT_REASONS | {
    "contract_rejected",
    "runtime_error",
    "ipc_timeout",
    "driver_disconnected",
    "child_exit_nonzero",
}


def _classification(value):
    return value if value in CLASSIFICATIONS else "contract_rejected"


def _last_errors(events):
    errors = {}
    for key, event_class in (
        ("connection", "connection_failed"),
        ("sampling", "quote_failed"),
    ):
        last = next(
            (e for e in reversed(events) if e["classification"] == event_class), None
        )
        errors[key] = (
            None
            if last is None
            else {
                "classification": _classification(last["reason"] or "source_error"),
                "round": last["round"],
                "segment": last["segment"],
            }
        )
    return errors


class AcceptanceReceiptWriter:
    """Explicit producer callback for one new private session directory.

    The runtime invokes this after the Go ACK receipt and on health changes.
    Batch/config archival must finish before this callback. The callback never
    copies rows, creates a DatasetVersion, or manufactures a Go ACK.
    """

    def __init__(self, session_dir: Path):
        self.session_dir = session_dir

    def __call__(self, session, state, classification):
        receipt = {
            "revision": RECEIPT_REVISION,
            "config": asdict(session.config),
            "input_origin": (
                "author_created_offline_fixture"
                if session.ingress.offline
                else "real_go_sdk_decoded_quote"
            ),
            "rounds": [
                {
                    k: r[k]
                    for k in (
                        "round",
                        "sha256",
                        "byte_count",
                        "read_at",
                        "outcome",
                        "classification",
                    )
                }
                | {"known": r["clocks"]["known"]}
                for r in session.rounds
                if r["go_ack_received"]
            ],
            "producer_state": state,
            "classification": (
                None if classification is None else _classification(classification)
            ),
            "last_errors": _last_errors(session.events),
        }
        raw = json.dumps(receipt, allow_nan=False, separators=(",", ":")).encode()
        # New temp inode, 0600 from creation. fsync file before replace, then
        # directory: readers see one complete old/new receipt, never a prefix.
        fd, temporary = tempfile.mkstemp(prefix=".read-ready-", dir=self.session_dir)
        try:
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.session_dir / RECEIPT_NAME)
            directory = os.open(self.session_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
