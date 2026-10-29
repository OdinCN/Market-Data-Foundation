"""Conservative BLS Errata discovery and explicitly verified CPI notice imports.

Discovery is deliberately not a publication archive or a natural-language scope
inference engine. A relevant link lacking structured verification needs review.
"""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit

from market_data_foundation.historical.bls_cpi_direct import SERIES_IDS
from market_data_foundation.historical.bls_web_transport import (
    BlsWebError,
    fetch_bls_page,
)
from market_data_foundation.historical.cpi_timeseries_source import validate_month

ERRATA_URL = "https://www.bls.gov/errata/"
MAX_SIGNAL_BYTES = 2_000_000


class CpiSignalError(ValueError):
    def __init__(self, classification):
        self.classification = classification
        super().__init__(classification)


def evidence_url(value):
    """Accept public BLS document URLs only, without userinfo or hidden suffixes."""
    if not isinstance(value, str) or len(value) > 2048:
        raise CpiSignalError("signal_evidence_url_invalid")
    try:
        parts = urlsplit(value)
        if (
            parts.scheme != "https"
            or parts.netloc not in {"www.bls.gov", "bls.gov"}
            or parts.username is not None
            or parts.password is not None
            or parts.port is not None
            or not parts.path.startswith("/")
            or any(ord(c) < 33 or ord(c) == 127 for c in value)
            or any(c in value for c in "\\%")
            or parts.query
            or parts.fragment
        ):
            raise ValueError
    except ValueError:
        raise CpiSignalError("signal_evidence_url_invalid") from None
    return "https://www.bls.gov" + parts.path


def fetch_official_errata():
    try:
        raw = fetch_bls_page(
            ERRATA_URL,
            headers={
                "User-Agent": "Market-Data-Foundation CPI signals/1.0",
                "Accept": "text/html",
            },
            max_bytes=MAX_SIGNAL_BYTES,
            timeout=20,
        )
        if not raw:
            raise CpiSignalError("signal_payload_invalid")
        return raw
    except BlsWebError as error:
        suffix = (
            "payload_invalid"
            if error.classification == "payload_too_large"
            else error.classification
        )
        raise CpiSignalError("signal_" + suffix) from None
    except CpiSignalError:
        raise
    except HTTPError as error:
        raise CpiSignalError(f"signal_http_{error.code}") from None
    except TimeoutError:
        raise CpiSignalError("signal_timeout") from None
    except URLError as error:
        raise CpiSignalError(
            "signal_timeout"
            if isinstance(error.reason, TimeoutError)
            else "signal_transport_failed"
        ) from None
    except Exception:
        raise CpiSignalError("signal_fetch_failed") from None


class _Links(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links = []
        self.text = []
        self.active = None
        self.rows = []
        self.row = None
        self.cell = None
        self.table_depth = 0
        self.errata_depth = None
        self.has_errata_table = False
        self.in_thead = False

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.table_depth += 1
            if attributes.get("id") == "errata_datatable":
                self.errata_depth = self.table_depth
                self.has_errata_table = True
        if tag == "thead":
            self.in_thead = True
        if tag == "tr" and self.errata_depth is not None and not self.in_thead:
            self.row = []
        elif tag in {"td", "th"} and self.row is not None:
            self.cell = {"text": [], "links": []}
        if tag == "a":
            self.active = [attributes.get("href", ""), []]
            if self.cell is not None:
                self.cell["links"].append(attributes.get("href", ""))

    def handle_data(self, data):
        self.text.append(data)
        if self.active is not None:
            self.active[1].append(data)
        if self.cell is not None:
            self.cell["text"].append(data)

    def handle_endtag(self, tag):
        if tag == "thead":
            self.in_thead = False
        if tag == "a" and self.active is not None:
            self.links.append((self.active[0], " ".join(self.active[1])))
            self.active = None
        if tag in {"td", "th"} and self.cell is not None:
            self.cell["text"] = " ".join(" ".join(self.cell["text"]).split())
            self.row.append(self.cell)
            self.cell = None
        elif tag == "tr" and self.row is not None:
            self.rows.append(self.row)
            self.row = None
        elif tag == "table":
            if self.errata_depth == self.table_depth:
                self.errata_depth = None
            self.table_depth = max(0, self.table_depth - 1)


def _errata_rows(rows):
    notices = {}
    for row in rows:
        texts = [cell["text"] for cell in row]
        if not re.search(r"\bcpi\b|consumer price index", " ".join(texts), re.I):
            continue
        if len(row) != 6:
            raise CpiSignalError("signal_row_unrecognized")
        date, program, product, title, description, status = texts
        try:
            notice_date = datetime.strptime(date, "%m/%d/%Y").date().isoformat()
        except ValueError:
            raise CpiSignalError("signal_row_date_unrecognized") from None
        ids = sorted(set(re.findall(r"\b(?:CU|CW|AP)[A-Z0-9]{8,}\b", description)))
        if ids and not set(ids).intersection(SERIES_IDS):
            continue  # Explicit city/other series cannot affect subscriptions.
        links = []
        for cell in row[3:5]:
            for href in cell["links"]:
                try:
                    links.append(evidence_url(urljoin(ERRATA_URL, href)))
                except CpiSignalError:
                    pass
        if (
            links
            and all("/regions/" in url for url in links)
            and not set(ids).intersection(SERIES_IDS)
        ):
            continue  # An explicitly regional news release, not a national series.
        identity = hashlib.sha256(
            json.dumps(
                [notice_date, program, title, description],
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        notices[identity] = {
            "signal_id": identity,
            "evidence_url": ERRATA_URL,
            "notice_date": notice_date,
            "program": program,
            "product_type": product,
            "title": title,
            "description": description,
            "correction_status": status,
            "mentioned_series_ids": ids,
            "related_urls": sorted(set(links)),
            "state": "review_required",
            "classification": "signal_scope_unverified",
            "verified": False,
        }
    return list(notices.values())


def signal_id(url):
    return hashlib.sha256(evidence_url(url).encode()).hexdigest()


def parse_official_errata(raw):
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_SIGNAL_BYTES:
        raise CpiSignalError("signal_payload_invalid")
    parser = _Links()
    try:
        parser.feed(raw.decode("utf-8"))
    except UnicodeError, ValueError:
        raise CpiSignalError("signal_payload_invalid") from None
    if not parser.has_errata_table:
        raise CpiSignalError("signal_page_unrecognized")
    return _errata_rows(parser.rows)


def _value(value):
    if type(value) is not str:
        raise CpiSignalError("signal_expected_value_invalid")
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise CpiSignalError("signal_expected_value_invalid") from None
    if not number.is_finite() or number <= 0:
        raise CpiSignalError("signal_expected_value_invalid")
    return str(number)


def validate_signal(notice):
    """Validate an operator's verified transcription; never infer national scope."""
    if not isinstance(notice, dict):
        raise CpiSignalError("signal_notice_invalid")
    url = evidence_url(notice.get("evidence_url"))
    kind = notice.get("kind")
    if kind not in {"correction", "withdrawal", "annual_revision"}:
        raise CpiSignalError("signal_kind_invalid")
    if notice.get("verified") is not True or notice.get("scope") != "national":
        raise CpiSignalError("signal_scope_unverified")
    series = notice.get("series_ids")
    if (
        not isinstance(series, (list, tuple))
        or not series
        or any(type(s) is not str or s not in SERIES_IDS for s in series)
        or len(set(series)) != len(series)
    ):
        raise CpiSignalError("signal_series_invalid")
    if kind == "annual_revision" and any(not s.startswith("CUSR") for s in series):
        raise CpiSignalError("signal_annual_scope_invalid")
    start, end = notice.get("start_month"), notice.get("end_month")
    try:
        validate_month(start)
        validate_month(end)
        if start > end:
            raise ValueError
    except ValueError, RuntimeError:
        raise CpiSignalError("signal_range_invalid") from None
    try:
        effective = datetime.fromisoformat(notice["effective_at"])
        if effective.tzinfo is None or effective.utcoffset() is None:
            raise ValueError
    except KeyError, TypeError, ValueError:
        raise CpiSignalError("signal_effective_at_invalid") from None
    excerpt = notice.get("evidence_text")
    words = (
        r"withdraw\w*|retract\w*|remov\w*|suppress\w*"
        if kind == "withdrawal"
        else r"correct\w*|revis\w*"
    )
    if (
        not isinstance(excerpt, str)
        or not 1 <= len(excerpt) <= 20000
        or not re.search(words, excerpt, re.I)
    ):
        raise CpiSignalError("signal_evidence_insufficient")
    mentioned = set(re.findall(r"\b(?:CU|CW|AP)[A-Z0-9]{8,}\b", excerpt))
    if mentioned and not set(series) <= mentioned:
        raise CpiSignalError("signal_evidence_scope_mismatch")
    mapping = notice.get("scope_mapping")
    if mapping is not None:
        # An explicit operator-verified transcription links the verbatim prose
        # and companion tables to machine-readable scope. Do not rewrite BLS
        # English month names or substitute our series IDs into the original.
        if (
            not isinstance(mapping, dict)
            or mapping.get("verified") is not True
            or mapping.get("series_ids") != list(series)
            or mapping.get("start_month") != start
            or mapping.get("end_month") != end
        ):
            raise CpiSignalError("signal_scope_mapping_invalid")
        if not mentioned and not re.search(
            r"U\.?S\.? city average|United States|\bnational\b", excerpt, re.I
        ):
            raise CpiSignalError("signal_evidence_scope_mismatch")
    else:

        def month_present(month):
            natural = datetime.strptime(month, "%Y-%m").strftime("%B %Y")
            return month in excerpt or natural.casefold() in excerpt.casefold()

        if not set(series) <= mentioned or not all(
            month_present(m) for m in (start, end)
        ):
            raise CpiSignalError("signal_evidence_insufficient")
    expected = notice.get("expected_points", [])
    if not isinstance(expected, list) or len(expected) > 10000:
        raise CpiSignalError("signal_expected_points_invalid")
    points, seen = [], set()
    for point in expected:
        if not isinstance(point, dict):
            raise CpiSignalError("signal_expected_points_invalid")
        sid, month = point.get("series_id"), point.get("month")
        if sid not in series or not isinstance(month, str) or not start <= month <= end:
            raise CpiSignalError("signal_expected_points_invalid")
        try:
            validate_month(month)
        except RuntimeError:
            raise CpiSignalError("signal_expected_points_invalid") from None
        if (sid, month) in seen:
            raise CpiSignalError("signal_expected_points_invalid")
        seen.add((sid, month))
        old, new = _value(point.get("previous_value")), _value(point.get("index_value"))
        if mapping is None and any(
            not re.search(r"(?<![\w.])" + re.escape(value) + r"(?!\w|\.\d)", excerpt)
            for value in (old, new)
        ):
            raise CpiSignalError("signal_expected_evidence_insufficient")
        points.append(
            dict(series_id=sid, month=month, previous_value=old, index_value=new)
        )
    discovered = notice.get("discovered_signal_id")
    if discovered is not None and (
        not isinstance(discovered, str) or not re.fullmatch(r"[0-9a-f]{64}", discovered)
    ):
        raise CpiSignalError("signal_discovery_id_invalid")
    return {
        "signal_id": discovered or signal_id(url),
        "discovered_signal_id": discovered,
        "evidence_url": url,
        "kind": kind,
        "verified": True,
        "scope": "national",
        "series_ids": sorted(series),
        "start_month": start,
        "end_month": end,
        "effective_at": effective.astimezone(UTC).isoformat(),
        "evidence_text": excerpt,
        "scope_mapping": mapping,
        "expected_points": sorted(points, key=lambda p: (p["series_id"], p["month"])),
    }
