"""Verified Garage bytes, shared across period-native macro stores."""

import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import quote

from market_data_foundation.historical.bls_cpi_material_store import (
    BUCKET,
    GARAGE_ENDPOINT,
    _signed_headers,
)
from market_data_foundation.storage.market_dataset_garage import _transport

MAX_RAW_BYTES = 8 * 1024 * 1024


class VerifiedGarageRawStore:
    """GET-before-PUT and exact readback; an uncertain write is never retried."""

    def __init__(
        self,
        credentials,
        *,
        raw_prefix,
        error_factory=RuntimeError,
        error_prefix="raw_material",
        transport=None,
        clock=None,
    ):
        if type(raw_prefix) is not str or not re.fullmatch(
            r"[a-z0-9][a-z0-9/_-]{0,199}/", raw_prefix
        ):
            raise error_factory(error_prefix + "_raw_prefix_invalid")
        self.raw_prefix = raw_prefix
        self._error_factory, self._error_prefix = error_factory, error_prefix
        self._credentials = credentials
        self._transport = transport or _transport
        self._clock = clock or (lambda: datetime.now(UTC))

    def _error(self, suffix):
        return self._error_factory(self._error_prefix + "_" + suffix)

    def locator(self, sha):
        return f"mdf-s3://{BUCKET}/{self.raw_prefix}{sha}/raw-response.bin"

    def _send(self, method, sha, raw=b"", maximum=MAX_RAW_BYTES):
        path = "/" + quote(
            f"{BUCKET}/{self.raw_prefix}{sha}/raw-response.bin", safe="/-_.~"
        )
        headers = _signed_headers(
            method,
            path,
            "192.168.10.8:3900",
            hashlib.sha256(raw).hexdigest(),
            self._clock().astimezone(UTC),
            self._credentials,
        )
        try:
            return self._transport(
                method, GARAGE_ENDPOINT + path, headers, raw, maximum, 10.0
            )
        except Exception:
            raise self._error("raw_transport_failed") from None

    def _read(self, sha, raw):
        status, found = self._send("GET", sha, maximum=len(raw))
        if status == 404:
            return False
        if status != 200:
            raise self._error("raw_read_failed")
        if found != raw or hashlib.sha256(found).hexdigest() != sha:
            raise self._error("raw_mismatch")
        return True

    def put_verified(self, raw):
        if type(raw) is not bytes or not 0 < len(raw) <= MAX_RAW_BYTES:
            raise self._error("raw_invalid")
        sha = hashlib.sha256(raw).hexdigest()
        if self._read(sha, raw):
            return self.locator(sha)
        write_unknown = False
        try:
            status, _ = self._send("PUT", sha, raw, maximum=1024)
        except self._error_factory:
            write_unknown = True
        else:
            if status not in (200, 201, 204) and status < 500:
                raise self._error("raw_write_rejected")
            write_unknown = status >= 500
        try:
            verified = self._read(sha, raw)
        except self._error_factory:
            raise self._error("raw_outcome_unknown") from None
        if not verified:
            raise self._error(
                "raw_outcome_unknown" if write_unknown else "raw_readback_missing"
            )
        return self.locator(sha)
