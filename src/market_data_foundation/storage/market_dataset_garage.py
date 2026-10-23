"""Bounded Garage DEV material port; exclusively one owned probe prefix.

Conditional creation is tested against a disposable sentinel before materials.
There is no list, retry, redirect, credential discovery or unconditional PUT.
"""

import hashlib
import hmac
import re
import time
from datetime import UTC, datetime
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from market_data_foundation.canonical.market_material import byte_digest
from market_data_foundation.canonical.model import ContractError, require
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials

BUCKET = "mdf-artifacts"
ENDPOINT = "http://192.168.10.8:3900"
REGION = "garage"
MAX_BYTES = 8 * 1024 * 1024
RUN = re.compile(r"i3-[a-f0-9]{32}")
DIGEST = re.compile(r"[a-f0-9]{64}")


class StorageUnknown(ContractError):
    """A lost acknowledgement must be reconciled by the original locator."""


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _transport(method, url, headers, body, maximum, timeout):
    request = Request(
        url, data=body if method == "PUT" else None, headers=headers, method=method
    )
    opener = build_opener(ProxyHandler({}), _NoRedirect)
    try:
        response = opener.open(request, timeout=timeout)
    except HTTPError as exc:
        response = exc
    with response:
        if not 200 <= response.code < 300:
            return response.code, b""  # S3 error XML is not a material payload.
        raw = response.read(maximum + 1)
        require(len(raw) <= maximum, "garage_response_capacity")
        return response.code, raw


class GarageMarketMaterials:
    def __init__(
        self,
        credentials: GarageCredentials,
        run_id: str,
        *,
        transport=None,
        max_requests=96,
        wall_seconds=120,
    ):
        require(RUN.fullmatch(run_id) is not None, "invalid_probe_run")
        require(1 <= max_requests <= 96 and 0 < wall_seconds <= 120, "probe_budget")
        self.run_id = run_id
        self.prefix = f"market-datasets/v1/probes/{run_id}/"
        self._credentials = credentials
        self._transport = transport or _transport
        self._deadline = time.monotonic() + wall_seconds
        self._max_requests = max_requests
        self.requests = 0
        self.conditional_verified = False
        self.effects = []
        self._owned = set()
        self.on_effect = lambda: None

    def locator(
        self, level: str, source: str, binding: str, semantic: str, digest: str
    ) -> str:
        require(
            level in ("raw", "source-projection", "status", "metadata"),
            "material_level",
        )
        require(
            all(DIGEST.fullmatch(x) for x in (source, binding, semantic, digest)),
            "material_address_digest",
        )
        return (
            f"s3://{BUCKET}/{self.prefix}{level}/{source}/{binding}/"
            f"{semantic}/sha256/{digest}"
        )

    def _key(self, locator):
        prefix = f"s3://{BUCKET}/{self.prefix}"
        require(locator.startswith(prefix), "garage_namespace_mismatch")
        tail = locator[len(prefix) :]
        require(
            re.fullmatch(
                r"(?:raw|source-projection|status|metadata)/(?:[a-f0-9]{64}/){3}"
                r"sha256/[a-f0-9]{64}",
                tail,
            )
            is not None,
            "garage_key_invalid",
        )
        return self.prefix + tail

    def _send(self, method, key, body=b"", *, conditional=False, maximum=MAX_BYTES):
        require(key.startswith(self.prefix) and ".." not in key, "garage_scope")
        remaining = self._deadline - time.monotonic()
        require(
            self.requests < self._max_requests and remaining > 0,
            "garage_budget_exhausted",
        )
        self.requests += 1
        now = datetime.now(UTC)
        day, stamp = now.strftime("%Y%m%d"), now.strftime("%Y%m%dT%H%M%SZ")
        path = "/" + quote(f"{BUCKET}/{key}", safe="/-_.~")
        payload_hash = byte_digest(body)
        headers = {
            "host": "192.168.10.8:3900",
            "x-amz-date": stamp,
            "x-amz-content-sha256": payload_hash,
        }
        if conditional:
            headers["if-none-match"] = "*"
        names = ";".join(sorted(headers))
        canonical = "".join(f"{name}:{headers[name]}\n" for name in sorted(headers))
        wire = "\n".join((method, path, "", canonical, names, payload_hash))
        scope = f"{day}/{REGION}/s3/aws4_request"
        signed = "\n".join(
            ("AWS4-HMAC-SHA256", stamp, scope, byte_digest(wire.encode()))
        )
        key_bytes = ("AWS4" + self._credentials.secret_key).encode()
        for part in (day, REGION, "s3", "aws4_request"):
            key_bytes = hmac.new(key_bytes, part.encode(), hashlib.sha256).digest()
        signature = hmac.new(key_bytes, signed.encode(), hashlib.sha256).hexdigest()
        headers["authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={self._credentials.access_key}/{scope}, "
            f"SignedHeaders={names}, Signature={signature}"
        )
        entry = {"method": method, "key": key, "effect": "UNKNOWN"}
        self.effects.append(entry)
        self.on_effect()
        try:
            status, raw = self._transport(
                method, ENDPOINT + path, headers, body, maximum, min(5, remaining)
            )
            require(
                type(raw) is bytes and len(raw) <= maximum, "garage_response_capacity"
            )
        except Exception:
            raise StorageUnknown("garage_transport_or_response_unknown") from None
        entry.update(status=status, effect="OBSERVED")
        self.on_effect()
        return status, raw

    def qualify_conditional_create(self):
        """Only disposable sentinel can receive the deliberately conflicting PUT."""
        require(not self.conditional_verified, "conditional_probe_already_run")
        key = self.prefix + "conditional-sentinel"
        status, _ = self._send("GET", key, maximum=1024)
        require(status == 404, "probe_prefix_conflict_or_unreadable")
        self._owned.add(key)  # Intent retained even if the first PUT loses its ACK.
        status, _ = self._send(
            "PUT", key, b"i3-sentinel-original", conditional=True, maximum=1024
        )
        require(status in (200, 201), "conditional_create_rejected")
        status, _ = self._send(
            "PUT", key, b"i3-sentinel-conflict", conditional=True, maximum=1024
        )
        require(status == 412, "conditional_create_not_enforced")
        status, raw = self._send("GET", key, maximum=1024)
        require(
            status == 200 and raw == b"i3-sentinel-original",
            "conditional_probe_readback_mismatch",
        )
        self.conditional_verified = True

    def put(self, locator: str, raw: bytes) -> None:
        key = self._key(locator)
        require(self.conditional_verified, "conditional_create_unqualified")
        require(
            type(raw) is bytes
            and 0 < len(raw) <= MAX_BYTES
            and key.endswith("/" + byte_digest(raw)),
            "material_address_mismatch",
        )
        # All keys are inside a fresh, Host-checked run prefix; never adopt old keys.
        if key not in self._owned:
            status, _ = self._send("GET", key, maximum=MAX_BYTES)
            require(status == 404, "material_preexisting_or_unreadable")
            self._owned.add(key)
        status, _ = self._send("PUT", key, raw, conditional=True, maximum=1024)
        if status >= 500:
            raise StorageUnknown("garage_write_unknown")
        require(status in (200, 201, 412), "garage_conditional_write_rejected")
        require(self.read(locator, len(raw)) == raw, "garage_exact_readback_mismatch")

    def read(self, locator: str, maximum: int) -> bytes:
        require(type(maximum) is int and 0 < maximum <= MAX_BYTES, "material_capacity")
        key = self._key(locator)
        status, raw = self._send("GET", key, maximum=maximum)
        require(
            status == 200 and key.endswith("/" + byte_digest(raw)),
            "garage_exact_readback_mismatch",
        )
        return raw

    def cleanup_owned(self):
        """No enumeration: only exact keys from this instance's intent ledger."""
        for key in sorted(self._owned):
            status, _ = self._send("DELETE", key, maximum=1024)
            require(status in (200, 204, 404), "garage_cleanup_unconfirmed")
            status, _ = self._send("GET", key, maximum=1024)
            require(status == 404, "garage_cleanup_readback_failed")
        self._owned.clear()


class SimpleGarageMaterials(GarageMarketMaterials):
    """Fixed bytes per content address; bounded retries, no CAS or deletion.

    Trusted writers only. This does not provide WORM or physical exactly-once IO.
    Budgets account for write intents, including writes with unknown outcomes.
    """

    def __init__(self, credentials, run_id, **kwargs):
        super().__init__(credentials, run_id, **kwargs)
        self.prefix = f"market-datasets/dev-simplified/{run_id}/"
        self.intents = {}

    def _send(self, method, key, body=b"", **kwargs):
        require(method in ("GET", "HEAD", "PUT"), "simple_storage_method_forbidden")
        require(not kwargs.get("conditional"), "simple_storage_no_cas")
        if method == "PUT":
            self._key(f"s3://{BUCKET}/{key}")
            self._bind_bytes(key, body)
        return super()._send(method, key, body, **kwargs)

    def qualify_conditional_create(self):
        raise ContractError("simple_storage_no_cas")

    def cleanup_owned(self):
        raise ContractError("simple_storage_no_delete")

    def _observe(self, locator, raw):
        status, observed = self._send("GET", self._key(locator), maximum=len(raw))
        if status >= 500:
            raise StorageUnknown("garage_read_unknown")
        require(status in (200, 404), "garage_read_rejected")
        if status == 200:
            require(observed == raw, "garage_exact_readback_mismatch")
            return True
        return False

    def _bind_bytes(self, key, raw):
        require(
            type(raw) is bytes
            and 0 < len(raw) <= 4 * 1024 * 1024
            and key.endswith("/" + byte_digest(raw)),
            "material_address_mismatch",
        )
        if key not in self.intents:
            require(len(self.intents) < 16, "garage_object_budget")
            require(
                sum(self.intents.values()) + len(raw) <= 4 * 1024 * 1024,
                "garage_byte_budget",
            )
            self.intents[key] = len(raw)

    def put(self, locator, raw, *, max_puts=2):
        key = self._key(locator)
        self._bind_bytes(key, raw)
        require(type(max_puts) is int and 1 <= max_puts <= 3, "put_retry_budget")
        for attempt in range(max_puts + 1):
            # A GET error/403 never means absent and never authorizes a PUT.
            if self._observe(locator, raw):
                return
            if attempt == max_puts:
                raise StorageUnknown("garage_write_not_yet_observed")
            if attempt:
                time.sleep(0.05 * attempt)
            try:
                status, _ = self._send("PUT", key, raw, maximum=1024)
            except StorageUnknown:
                continue
            if status >= 500:
                continue
            require(status in (200, 201), "garage_write_rejected")
        raise StorageUnknown("garage_write_unknown")
