"""Bounded DOL material reads over shared admission/retry/retention."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from market_data_foundation.historical.calendar_resilience import AcquiredCalendar
from market_data_foundation.historical.claims_calendar import (
    BACKUP_URL,
    CURRENT_REPORT_URL,
    PRIMARY_URL,
)
from market_data_foundation.historical.claims_contract import ClaimsError
from market_data_foundation.historical.claims_source import (
    ClaimsFredSource,
    ClaimsResponse,
)
from market_data_foundation.historical.cpi_release_schedule import _write_atomic


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        raise ClaimsError("claims_dol_redirect_rejected")


def public_transport(request):
    if request.url not in {PRIMARY_URL, BACKUP_URL, CURRENT_REPORT_URL}:
        raise ClaimsError("claims_dol_url_invalid")
    try:
        response = build_opener(_NoRedirect()).open(
            Request(request.url, headers={"Accept-Encoding": "identity"}), timeout=12
        )
    except HTTPError as error:
        response = error
    with response:
        if response.headers.get("Content-Encoding", "identity").lower() != "identity":
            raise ClaimsError("claims_dol_encoding_rejected")
        raw = response.read(8 * 1024 * 1024 + 1)
        if not raw or len(raw) > 8 * 1024 * 1024:
            raise ClaimsError("claims_dol_size_invalid")
        return ClaimsResponse(response.status, raw, dict(response.headers))


def material_fetcher(
    directory,
    control_path,
    *,
    live_opt_in=False,
    transport=public_transport,
    clock=lambda: datetime.now(UTC),
):
    """Construction does no IO. Each call is one logical read, max three attempts."""
    directory = Path(directory)

    def fetch(url):
        if live_opt_in is not True:
            raise ClaimsError("claims_dol_live_opt_in_required")
        if url not in {PRIMARY_URL, BACKUP_URL, CURRENT_REPORT_URL}:
            raise ClaimsError("claims_dol_url_invalid")

        def retain(request, response, obtained, attempts):
            sha = hashlib.sha256(response.body).hexdigest()
            directory.mkdir(parents=True, exist_ok=True)
            path = directory / (sha + ".bin")
            if path.exists() and path.read_bytes() != response.body:
                raise ClaimsError("claims_dol_retention_conflict")
            if not path.exists():
                _write_atomic(path, response.body)
            meta = dict(
                url=url,
                sha256=sha,
                obtained_at=obtained.isoformat(),
                status=response.status,
                headers={
                    k: v
                    for k, v in response.headers.items()
                    if k.lower()
                    in {
                        "content-type",
                        "content-length",
                        "content-range",
                        "last-modified",
                        "etag",
                        "date",
                        "retry-after",
                    }
                },
                attempts=attempts,
            )
            key = hashlib.sha256((url + obtained.isoformat()).encode()).hexdigest()
            _write_atomic(directory / (key + ".json"), json.dumps(meta).encode())

        source = ClaimsFredSource(
            transport=transport,
            request_control_path=control_path,
            raw_sink=retain,
            max_requests=1,
            clock=clock,
        )
        raw, obtained, _ = source._read(SimpleNamespace(url=url))
        value = AcquiredCalendar(raw, obtained)
        value.http_attempts = source.attempts
        return value

    return fetch
