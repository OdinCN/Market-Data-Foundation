"Pinned Go SDK daily capability; explicit live opt-in and retained observations."

import base64
import json
import select
import struct
import subprocess
import time
import zlib
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.qualification.contracts import Capability, Fixture
from market_data_foundation.qualification.harness import (
    Binding,
    Endpoint,
    Evidence,
    observe,
)
from market_data_foundation.qualification.tdx_market_evidence import inspect_daily
from market_data_foundation.runtime.tdx_watchlist import (
    V100_DRIVER_SDK_RELEASE,
    verified_artifact,
)

# This capability has its own exact build; it does not alter watchlist pins.
DAILY_DRIVER_SHA256 = "6b2dbab3a1726bfd45c04d882b74fd47c51971c9ff1bc6b11a180f99fc9c0102"


class TdxError(Exception):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


def classify(error):
    if isinstance(error, ContractError):
        # Repository ContractError contains a machine classification, never
        # an upstream exception message.
        return error.args[0]
    return getattr(error, "classification", type(error).__name__)


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def frames(raw, response=False):
    result = []
    while raw:
        header = 16 if response else 12
        require(len(raw) >= header, "tdx_frame_truncated")
        size = struct.unpack_from("<H", raw, 12 if response else 6)[0]
        size += 16 if response else 10
        require(header <= size <= len(raw), "tdx_frame_size")
        result.append(raw[:size])
        raw = raw[size:]
    return result


def verify_wire(row):
    sent = base64.b64decode(row["request_wire"] or "", validate=True)
    raw = base64.b64decode(row["response_wire"] or "", validate=True)
    requests = frames(sent)
    responses = frames(raw, True)
    by_id = {struct.unpack_from("<I", f, 5)[0]: f for f in responses}
    for request in requests:
        response = by_id.get(struct.unpack_from("<I", request, 1)[0])
        require(response is not None, "tdx_response_missing")
        require(
            response[:4] == bytes.fromhex("b1cb7400")
            and response[10:12] == request[10:12],
            "tdx_response_identity",
        )
    require(bool(requests), "tdx_wire_required")
    request = row["request"]
    if request["operation"] != "GetBJDirectory":
        from market_data_foundation.qualification.tdx_market_evidence import (
            request_wire,
        )

        expected = request_wire(SimpleNamespace(**request), 0)
        # SDK owns the protocol request ID; all remaining request bytes are exact.
        require(
            requests[-1][:1] + requests[-1][5:] == expected[:1] + expected[5:],
            "tdx_wire_request_drift",
        )
        last = by_id[struct.unpack_from("<I", requests[-1], 1)[0]]
        payload = last[16:]
        if len(payload) != struct.unpack_from("<H", last, 14)[0]:
            payload = zlib.decompress(payload)
        require(
            len(payload) == struct.unpack_from("<H", last, 14)[0], "tdx_payload_size"
        )
        if request["operation"] == "GetKline":
            require(
                struct.unpack_from("<H", payload)[0]
                == row["source_projection"]["Count"],
                "tdx_wire_count_drift",
            )
    return sent, raw


class GoDailySource:
    def __init__(self, binary, endpoint, directory, *, live_opt_in=False, timeout=60):
        require(live_opt_in is True, "tdx_live_opt_in_required")
        self.binary = Path(binary)
        self.endpoint = endpoint
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.process = None
        self.requests = 0

    def _send(self, kind, value):
        raw = encoded(value)
        self.process.stdin.write(kind + struct.pack(">I", len(raw)) + raw)
        self.process.stdin.flush()

    def _receive(self):
        deadline = time.monotonic() + self.timeout + 10

        def read(size):
            data = bytearray()
            while len(data) < size:
                wait = deadline - time.monotonic()
                if (
                    wait <= 0
                    or not select.select([self.process.stdout], [], [], wait)[0]
                ):
                    raise TdxError("driver_ipc_timeout")
                piece = self.process.stdout.read(size - len(data))
                if not piece:
                    raise TdxError("driver_ipc_closed")
                data.extend(piece)
            return bytes(data)

        header = read(5)
        size = struct.unpack(">I", header[1:])[0]
        require(header[:1] == b"D" and 0 < size <= 96 * 1024 * 1024, "tdx_ipc_frame")
        return json.loads(read(size))

    def _connect(self):
        if self.process:
            return
        with verified_artifact(self.binary, DAILY_DRIVER_SHA256) as (executable, fd):
            self.process = subprocess.Popen(
                [executable, "--daily-live"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                pass_fds=(fd,),
            )
        self._send(b"C", dict(endpoint=self.endpoint, timeout_seconds=self.timeout))
        result = self._receive()
        require(
            result.get("sdk_release") == V100_DRIVER_SDK_RELEASE, "tdx_sdk_identity"
        )
        if result["classification"] != "connected":
            self.close()
            raise TdxError(result["classification"])

    def request(self, operation, market, code="", start=0, count=0):
        request = dict(
            id=uuid4().hex,
            operation=operation,
            market=market,
            code=code,
            start=start,
            count=count,
        )
        capability = (
            Capability.BARS_DAILY
            if operation == "GetKline"
            else Capability.SECURITY_UNIVERSE
        )
        host, port = self.endpoint.rsplit(":", 1)
        endpoint = Endpoint(host, int(port), unobservable_reason=None)
        fixture = Fixture(
            "p0-" + request["id"],
            capability,
            (f"{market}:{code}",) if code else (),
            None,
            None,
            count or 1,
            {"price_basis": "native_projection"},
        )
        outcome = {}

        def collect(_):
            self._connect()
            self._send(b"R", request)
            self.requests += 1
            row = self._receive()
            raw = encoded(row)
            path = self.directory / (request["id"] + ".json")
            path.write_bytes(raw)
            outcome.update(path=str(path), sha256=sha256(raw).hexdigest(), row=row)
            require(
                row.get("request") == request
                and row.get("sdk_release") == V100_DRIVER_SDK_RELEASE,
                "tdx_response_binding",
            )
            if row["classification"] != "retained_raw":
                self.close()
                raise TdxError(row["classification"])
            verify_wire(row)
            requested, received = (
                datetime.fromisoformat(row[k]) for k in ("requested_at", "received_at")
            )
            require(requested <= received <= datetime.now(UTC), "tdx_clock_order")
            data = row["source_projection"]
            if operation == "GetKline":
                assessment = inspect_daily(data, SimpleNamespace(count=count))
                require(not assessment["issues"], "tdx_page_quality")
            return Evidence(
                True,
                row_count=data["Count"],
                pages=1,
                complete=None,
                exhausted=None,
                endpoint=endpoint,
                units={"price": "0.001 CNY", "amount": "0.001 CNY", "volume": "hand"},
                semantics={
                    "price_basis": "native_projection",
                    "source_event_time": "UNKNOWN",
                },
            )

        def execute(fixture):
            try:
                return collect(fixture)
            except Exception as error:
                outcome["failure"] = classify(error)
                raise

        observation = observe(
            Binding(
                "tdx-v100-primary",
                "go-sdk-p0-r1",
                V100_DRIVER_SDK_RELEASE,
                capability,
                "live",
                endpoint,
            ),
            fixture,
            execute,
            allow_live=True,
        )
        (self.directory / (request["id"] + ".observation.json")).write_bytes(
            encoded(observation)
        )
        if (
            not outcome.get("row")
            or outcome["row"].get("classification") != "retained_raw"
            or not observation["success"]
        ):
            self.close()
            raise TdxError(outcome.get("failure", "source_evidence_invalid"))
        return outcome

    def close(self):
        if self.process:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
            self.process = None
