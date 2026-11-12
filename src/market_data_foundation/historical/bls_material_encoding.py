"""Parser-only handling of verified encoding defects in official BLS archives."""

import hashlib
import re
from html.parser import HTMLParser

from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError

NOTICE_SHA256 = "3a906815343f74ab82dc90311c377c2305a4a6dfe352f09e0e45750d02aa6e7f"
START = b"With the publication of January 2027 CPI"
END = b"\n------------------------"


class _Context(HTMLParser):
    def __init__(self):
        super().__init__()
        self.open = {}

    def handle_starttag(self, tag, attrs):
        self.open[tag] = self.open.get(tag, 0) + 1

    def handle_endtag(self, tag):
        self.open[tag] = max(0, self.open.get(tag, 0) - 1)


def _employment_missing_row(raw):
    # In the January 2026 archive, only the three empty cells in the
    # October row of the 2025 annual revision table use Windows-1252.
    heading = (
        b"Table A. Seasonally adjusted unemployment rates in 2025"
        b" and changes due to revision"
    )
    note = (
        b"NOTE: Data for October 2025 were not collected"
        b" due to the federal government shutdown."
    )
    start = raw.find(heading)
    end = raw.find(note, start)
    table = raw[start:end]
    row = re.search(rb"(?m)^October[.]+(?:[ \t]+\x97){3}[ \t]*\r?$", table)
    if start < 0 or end < 0 or row is None or b"<" in table:
        raise CpiTimeSeriesError("release_material_encoding_invalid")
    offsets = [start + row.start() + i for i, byte in enumerate(row[0]) if byte == 0x97]
    lower, upper = offsets[0], offsets[-1] + 1
    derived = (
        raw[:lower] + raw[lower:upper].replace(b"\x97", "—".encode()) + raw[upper:]
    )
    try:
        text = derived.decode("utf-8-sig")
    except UnicodeError:
        raise CpiTimeSeriesError("release_material_encoding_invalid") from None
    return text, dict(
        policy="employment-20260109-october-missing-cells-v1",
        input_encoding="utf-8-with-three-windows-1252-em-dashes",
        retained_raw_unchanged=True,
        retained_raw_sha256=hashlib.sha256(raw).hexdigest(),
        derived_utf8_sha256=hashlib.sha256(derived).hexdigest(),
        transformations=[
            dict(byte_start=i, byte_end=i + 1, original_hex="97", replacement="U+2014")
            for i in offsets
        ],
        scope="October 2025 missing cells in annual unemployment revision Table A",
    )


def decode_material(raw, family, url):
    try:
        return raw.decode("utf-8-sig"), None
    except UnicodeError:
        pass
    if (
        family == "employment"
        and url == "https://www.bls.gov/news.release/archives/empsit_01092026.htm"
    ):
        return _employment_missing_row(raw)
    # Exact published prose, not a general tolerance for misplaced punctuation.
    # A changed future notice needs independent evidence and review.
    if family != "cpi" or not url.endswith("/archives/cpi_11102026.htm"):
        raise CpiTimeSeriesError("release_material_encoding_invalid")
    start = raw.find(START)
    end = raw.find(END, start)
    notice = raw[start:end] if start >= 0 and end > start else b""
    context = _Context()
    try:
        context.feed(raw[: max(0, start)].decode("utf-8-sig"))
    except UnicodeError:
        raise CpiTimeSeriesError("release_material_encoding_invalid") from None
    if (
        raw.count(START) != 1
        or hashlib.sha256(notice).hexdigest() != NOTICE_SHA256
        # Even the known paragraph is not admitted inside a tag/table/header.
        or not context.open.get("pre")
        or any(
            context.open.get(tag)
            for tag in [
                "table",
                "td",
                "th",
                "caption",
                "thead",
                "title",
                "head",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "script",
                "style",
            ]
        )
        or re.search(rb"<[^>]*$", raw[:start])
        or b"Upcoming Consumer Price Index Publication Changes"
        not in raw[max(0, start - 250) : start]
        or b"Technical Note" not in raw[end : end + 200]
    ):
        raise CpiTimeSeriesError("release_material_encoding_invalid")
    offsets = [start + i for i, value in enumerate(notice) if value == 0x9D]
    if len(offsets) != 6:
        raise CpiTimeSeriesError("release_material_encoding_invalid")
    # Replacement markers are visible in the derived text, with precise original
    # byte offsets. Strict decode of every other byte still fails closed.
    derived = raw[:start] + notice.replace(b"\x9d", "\ufffd".encode()) + raw[end:]
    try:
        text = derived.decode("utf-8-sig")
    except UnicodeError:
        raise CpiTimeSeriesError("release_material_encoding_invalid") from None
    return text, dict(
        policy="cpi-20261110-exact-nonstatistical-notice-v1",
        input_encoding="utf-8-with-six-proven-invalid-prose-bytes",
        retained_raw_unchanged=True,
        retained_raw_sha256=hashlib.sha256(raw).hexdigest(),
        derived_utf8_sha256=hashlib.sha256(derived).hexdigest(),
        notice_sha256=NOTICE_SHA256,
        transformations=[
            dict(byte_start=i, byte_end=i + 1, original_hex="9d", replacement="U+FFFD")
            for i in offsets
        ],
        scope="index-title-change notice only; not numeric/date/table evidence",
    )
