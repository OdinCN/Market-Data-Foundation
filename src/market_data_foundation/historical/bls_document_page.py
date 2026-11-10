"""BLS calendar/errata requests with complete bounds and existing admission."""

import time
from contextlib import nullcontext
from urllib.parse import urljoin

from market_data_foundation.historical.bls_web_transport import (
    MAX_BYTES,
    MAX_REDIRECTS,
    BlsWebError,
    _url,
)
from market_data_foundation.historical.calendar_resilience import retry_after
from market_data_foundation.historical.public_web_transport import (
    PublicFetchError,
    fetch_public,
)


def fetch_bls_page(
    url,
    *,
    headers=None,
    max_bytes=MAX_BYTES,
    timeout=20,
    allowed_path=None,
    request_control=None,
    observer=None,
):
    _url(url, allowed_path)
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_BYTES
        or type(timeout) not in {int, float}
        or not 0 < timeout <= 20
    ):
        raise BlsWebError("request_limits_invalid")
    # One supervised exchange per ticket. Never move a shared reservation outside
    # the redirect loop or substitute the per-material budget for this admission.
    deadline = time.monotonic() + timeout
    for hop in range(MAX_REDIRECTS + 1):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BlsWebError("timeout")
        _url(url, allowed_path)
        try:
            with (
                request_control.attempt()
                if request_control
                else nullcontext() as ticket
            ):
                response = fetch_public(
                    url,
                    "bls_web",
                    max_bytes=max_bytes,
                    timeout=remaining,
                    accept=(headers or {}).get("Accept", "text/html"),
                    on_headers=ticket.record_response if ticket else None,
                )
                if observer is not None:
                    observer(url, response.status, response.body)
                if response.status in {301, 302, 303, 307, 308}:
                    target = response.headers.get("Location") or response.headers.get(
                        "location"
                    )
                    if not target or hop == MAX_REDIRECTS:
                        raise BlsWebError("redirect_rejected")
                    try:
                        target = urljoin(url, target)
                        _url(target, allowed_path)
                    except ValueError:
                        raise BlsWebError("redirect_rejected") from None
                    url = target
                    continue
                if response.status != 200:
                    raise BlsWebError(
                        f"http_{response.status}",
                        http_status=response.status,
                        retry_at=retry_after(
                            response.headers.get("Retry-After")
                            or response.headers.get("retry-after")
                        ),
                    )
                return response.body
        except PublicFetchError as error:
            classification = (
                "request_outcome_unknown"
                if error.classification
                in {"worker_cleanup_unknown", "worker_outcome_unknown"}
                else error.classification
            )
            raise BlsWebError(
                classification,
                http_status=error.http_status,
                retry_at=retry_after(
                    error.headers.get("Retry-After") or error.headers.get("retry-after")
                ),
            ) from None
