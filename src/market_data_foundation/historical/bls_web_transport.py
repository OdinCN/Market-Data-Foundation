"""Bounded official BLS HTML/ICS reads with a local TLS advertisement workaround.

Keep Python's verified default context. DEFAULT advertisement matches the tested
BLS edge behavior, but its legacy TLS 1.2 suites must never carry HTTP traffic:
check the negotiated protocol immediately after connect, before request().
This helper does not apply to the BLS data API or any other provider.
"""

import ssl
from http.client import HTTPException, HTTPSConnection
from urllib.parse import urljoin, urlsplit

MAX_BYTES = 2_000_000
MAX_REDIRECTS = 3


class BlsWebError(ValueError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


def _url(url, allowed_path):
    try:
        if not isinstance(url, str) or len(url) > 2048:
            raise ValueError
        parts = urlsplit(url)
        if (
            parts.scheme != "https"
            or parts.netloc
            not in {"www.bls.gov", "bls.gov", "www.bls.gov:443", "bls.gov:443"}
            or parts.username is not None
            or parts.password is not None
            or parts.port not in {None, 443}
            or parts.query
            or parts.fragment
            or any(ord(c) < 33 or ord(c) == 127 or c in "\\%" for c in url)
            or (allowed_path is not None and parts.path != allowed_path)
        ):
            raise ValueError
        return parts
    except ValueError:
        raise BlsWebError("url_invalid") from None


def fetch_bls_page(
    url, *, headers=None, max_bytes=MAX_BYTES, timeout=20, allowed_path=None
):
    """Read official HTML or ICS bytes; validate every hop before opening it."""
    _url(url, allowed_path)
    if (
        type(max_bytes) is not int
        or not 1 <= max_bytes <= MAX_BYTES
        or type(timeout) not in {int, float}
        or not 0 < timeout <= 20
    ):
        raise BlsWebError("request_limits_invalid")
    try:
        context = ssl.create_default_context()
        context.set_ciphers("DEFAULT:@SECLEVEL=2")
        context.set_alpn_protocols(["http/1.1"])
        for hop in range(MAX_REDIRECTS + 1):
            parts = _url(url, allowed_path)
            connection = HTTPSConnection(
                parts.hostname, 443, timeout=timeout, context=context
            )
            try:
                connection.connect()
                if connection.sock is None or connection.sock.version() != "TLSv1.3":
                    raise BlsWebError("tls_version_rejected")
                connection.request(
                    "GET",
                    parts.path or "/",
                    headers={
                        **(headers or {}),
                        "Accept-Encoding": "identity",
                        "Connection": "close",
                    },
                )
                with connection.getresponse() as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        location = response.getheader("Location")
                        if not location or hop == MAX_REDIRECTS:
                            raise BlsWebError("redirect_rejected")
                        try:
                            redirected = urljoin(url, location)
                            _url(redirected, allowed_path)
                        except BlsWebError, ValueError:
                            raise BlsWebError("redirect_rejected") from None
                        url = redirected
                        continue
                    if response.status != 200:
                        raise BlsWebError(f"http_{response.status}")
                    raw = response.read(max_bytes + 1)
                    if len(raw) > max_bytes:
                        raise BlsWebError("payload_too_large")
                    return raw
            finally:
                connection.close()
    except BlsWebError:
        raise
    except TimeoutError:
        raise BlsWebError("timeout") from None
    except ssl.SSLCertVerificationError:
        raise BlsWebError("certificate_verification_failed") from None
    except ssl.SSLError:
        raise BlsWebError("tls_failed") from None
    except OSError, HTTPException:
        raise BlsWebError("transport_failed") from None
    except Exception:
        raise BlsWebError("fetch_failed") from None
