"""Reuse public original BEA material bytes, with separate asset interpretation.

Cache acquisition is explicit; constructing a host or reading queries does no IO.
Immutable release URL + release date key; every interpretation is revalidated.
"""

import fcntl
import hashlib
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_foundation.historical.calendar_resilience import AcquiredCalendar
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError


def _write(path, raw):
    temporary = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def cached_material(directory, selection, family, fetcher):
    from market_data_foundation.historical.macro_release_material import parse_material

    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    key = hashlib.sha256(
        (selection["source_url"] + "|" + selection["source_date"]).encode()
    ).hexdigest()
    with (directory / (key + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        body, meta = directory / (key + ".html"), directory / (key + ".json")
        fetched = not body.exists()
        if fetched:
            raw = fetcher(selection["source_url"], family)
        else:
            raw = body.read_bytes()
            try:
                if (
                    json.loads(meta.read_text())["sha256"]
                    != hashlib.sha256(raw).hexdigest()
                ):
                    raise ValueError
            except ValueError, KeyError, OSError:
                raise CpiTimeSeriesError(
                    "bea_shared_material_integrity_invalid"
                ) from None
        doc = parse_material(raw, selection["source_url"], family)
        if (
            doc["source_version"]["value"] != selection["source_date"]
            or selection["reference_month"] not in doc["reference_months"]
        ):
            raise CpiTimeSeriesError("release_material_calendar_identity_mismatch")
        if fetched:
            _write(body, raw)
            _write(
                meta,
                json.dumps(
                    {
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "source_url": selection["source_url"],
                        "obtained_at": datetime.now(UTC).isoformat(),
                        "retention": "original_public_http_body",
                    }
                ).encode(),
            )
        return raw, fetched


def calendar_fetcher(
    path, *, fetcher=None, parser=None, clock=lambda: datetime.now(UTC)
):
    from market_data_foundation.historical.pce_release_schedule import (
        fetch_official_calendar,
        parse_official_ics,
    )

    path = Path(path)

    def fetch():
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            now = clock()
            if path.exists():
                try:
                    saved = json.loads(path.read_text())
                    obtained = datetime.fromisoformat(saved["obtained_at"])
                    raw = saved["raw"].encode()
                    if (
                        obtained <= now < obtained + timedelta(hours=6)
                        and hashlib.sha256(raw).hexdigest() == saved["sha256"]
                    ):
                        return AcquiredCalendar(raw, obtained)
                except ValueError, KeyError, OSError:
                    raise CpiTimeSeriesError("bea_shared_calendar_invalid") from None
            raw = (fetcher or fetch_official_calendar)()
            (parser or parse_official_ics)(raw)
            _write(
                path,
                json.dumps(
                    {
                        "raw": raw.decode(),
                        "sha256": hashlib.sha256(raw).hexdigest(),
                        "obtained_at": now.isoformat(),
                    }
                ).encode(),
            )
            return AcquiredCalendar(raw, now)

    return fetch
