"""DOL regular-state Claims release evidence, independent of continuous revisions.

PDF extraction uses pypdf; table/narrative adapters fail closed on unknown layouts.
Text adapters accept page-separated official text with the same evidence contract.
"""

import copy
import re
from datetime import datetime
from decimal import Decimal
from io import BytesIO
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from psycopg.types.json import Jsonb
from pypdf import PdfReader

from market_data_foundation.historical.claims_contract import (
    BY_ID,
    SERIES_IDS,
    ClaimsError,
    day,
    instant,
)
from market_data_foundation.historical.claims_store import storage_boundary
from market_data_foundation.historical.cpi_timeseries_store import canonical, digest

PARSER = "dol-regular-state-claims-v3"
NUMBER = r"[0-9][0-9,]*(?:\.[0-9]+)?"
MONTH_DAY = (
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December)\s+\d{1,2}"
)


def _error(reason):
    return ClaimsError("claims_release_" + reason)


def _number(value):
    return str(Decimal(value.replace("−", "-").replace(",", "")))


def parse_release_pages(pages, source_url):
    url = urlsplit(source_url)
    if (
        url.scheme != "https"
        or url.netloc not in {"www.dol.gov", "dol.gov"}
        or (url.query and not (url.path == "/ui/data.pdf" and url.query == "os=v0"))
        or url.fragment
        or not (
            url.path.startswith("/sites/dolgov/files/OPA/newsreleases/ui-claims/")
            or url.path.startswith("/newsroom/releases/eta/")
            or url.path == "/ui/data.pdf"
        )
    ):
        raise _error("named_official_url_required")
    pages = [" ".join(page.split()) for page in pages]
    if not pages or len(pages) > 20:
        raise _error("pages_invalid")
    embargo = re.search(
        r"8:30 A\.M\. \(Eastern\) (?:Monday|Tuesday|Wednesday|Thursday|Friday), "
        r"(" + MONTH_DAY + r", \d{4})",
        pages[0],
    )
    if not embargo or "UNEMPLOYMENT INSURANCE WEEKLY CLAIMS" not in pages[0]:
        raise _error("report_header_unrecognized")
    report_day = datetime.strptime(embargo[1], "%B %d, %Y").date()
    scheduled = datetime.combine(report_day, datetime.min.time()).replace(
        hour=8, minute=30, tzinfo=ZoneInfo("America/New_York")
    )

    def week(text):
        dt = datetime.strptime(text + f" {report_day.year}", "%B %d %Y").date()
        if dt > report_day:
            dt = dt.replace(year=dt.year - 1)
        if dt.weekday() != 5:
            raise _error("reference_week_invalid")
        return dt.isoformat()

    candidates = [
        (i + 1, text)
        for i, text in enumerate(pages)
        if "UNEMPLOYMENT INSURANCE DATA FOR REGULAR STATE PROGRAMS" in text
    ]
    if len(candidates) != 1:
        raise _error("regular_state_table_unrecognized")
    table_page, table = candidates[0]
    table = table.split("INITIAL CLAIMS FILED IN FEDERAL PROGRAMS")[0]
    headers = list(
        re.finditer(
            r"WEEK ENDING (" + MONTH_DAY + r") (" + MONTH_DAY + r") Change", table
        )
    )
    if len(headers) != 2:
        raise _error("table_headers_unrecognized")
    claims = []
    narrative = pages[0]
    split = re.search(
        r"The advance seasonally adjusted insured unemployment rate", narrative
    )
    if not split:
        raise _error("narrative_sections_unrecognized")
    narratives = (narrative[: split.start()], narrative[split.start() :])
    groups = (
        (
            ("ICSA", "Initial Claims (SA)"),
            ("ICNSA", "Initial Claims (NSA)"),
            ("IC4WSA", "4-Wk Moving Average (SA)"),
        ),
        (
            ("CCSA", "Insured Unemployment (SA)"),
            ("CCNSA", "Insured Unemployment (NSA)"),
            ("CC4WSA", "4-Wk Moving Average (SA)"),
        ),
    )
    for g, header in enumerate(headers):
        current, prior = week(header[1]), week(header[2])
        if (day(current) - day(prior)).days != 7:
            raise _error("table_week_order_invalid")
        block = table[
            header.end() : headers[g + 1].start()
            if g + 1 < len(headers)
            else len(table)
        ]
        for sid, label in groups[g]:
            match = re.search(
                re.escape(label)
                + r"\s+("
                + NUMBER
                + r")\s+("
                + NUMBER
                + r")\s+([+−-]?"
                + NUMBER
                + r")",
                block,
            )
            if not match:
                raise _error("table_row_unrecognized")
            actual, previous, change = (_number(match[i]) for i in (1, 2, 3))
            if Decimal(actual) - Decimal(previous) != Decimal(change):
                raise _error("table_change_mismatch")
            original, original_fragment = None, None
            if BY_ID[sid].seasonal_adjustment == "SA":
                noun = "average" if sid in {"IC4WSA", "CC4WSA"} else "level"
                rev = re.search(
                    r"previous week's "
                    + noun
                    + r" was revised (up|down) by ("
                    + NUMBER
                    + r") from ("
                    + NUMBER
                    + r") to ("
                    + NUMBER
                    + r")",
                    narratives[g],
                )
                if rev:
                    original = _number(rev[3])
                    signed = Decimal(_number(rev[2])) * (1 if rev[1] == "up" else -1)
                    if (
                        _number(rev[4]) != previous
                        or Decimal(previous) - Decimal(original) != signed
                    ):
                        raise _error("narrative_revision_mismatch")
                    original_fragment = rev[0]
                # Cross-check the table's SA actual against the corresponding narrative.
                pattern = (
                    r"4-week moving average was (" + NUMBER + r")"
                    if noun == "average"
                    else (
                        r"seasonally adjusted initial claims was (" + NUMBER + r")"
                        if g == 0
                        else r"insured unemployment during the week ending "
                        + MONTH_DAY
                        + r" was ("
                        + NUMBER
                        + r")"
                    )
                )
                actual_text = re.search(pattern, narratives[g])
                if not actual_text or _number(actual_text[1]) != actual:
                    raise _error("narrative_actual_mismatch")
                stated_week = re.search(
                    r"week ending (" + MONTH_DAY + r")", narratives[g]
                )
                if not stated_week or week(stated_week[1]) != current:
                    raise _error("narrative_week_mismatch")
            values = (
                ("actual", current, actual, table_page, match[0]),
                (
                    "prior_as_reported_in_this_event",
                    prior,
                    previous,
                    table_page,
                    match[0],
                ),
                (
                    "official_previously_reported_prior",
                    prior,
                    original,
                    1 if original is not None else None,
                    original_fragment,
                ),
            )
            for role, reference, value, page, fragment in values:
                claims.append(
                    dict(
                        series_id=sid,
                        week_ending=reference,
                        statistic=BY_ID[sid].statistic,
                        role=role,
                        value=value,
                        unit="Number",
                        confirmation_state="confirmed"
                        if value is not None
                        else "unknown",
                        missing_reason=None
                        if value is not None
                        else "official_original_prior_not_stated",
                        evidence=dict(
                            source_name="DOL-ETA Unemployment Insurance Weekly Claims",
                            source_url=source_url,
                            page=page,
                            fragment=fragment,
                            reference_header=header[0],
                        ),
                        value_identity=digest(
                            canonical([sid, reference, role, value]).encode()
                        )
                        if value is not None
                        else None,
                    )
                )
    order = {sid: i for i, sid in enumerate(SERIES_IDS)}
    claims.sort(key=lambda c: order[c["series_id"]])
    return dict(
        event_id="dol-claims-" + report_day.isoformat(),
        source_date=report_day.isoformat(),
        source_version=dict(
            value=report_day.isoformat(),
            precision="date",
            basis="official_report_header",
        ),
        scheduled_publication_at=scheduled.isoformat(),
        schedule_basis="embargo_header",
        official_actual_publication_at=None,
        official_actual_publication_precision="unknown",
        parser_revision=PARSER,
        source_url=source_url,
        evidence_kind="official_current_report_snapshot"
        if url.path == "/ui/data.pdf"
        else "official_archive",
        original_agency="DOL-ETA",
        claims=claims,
        consensus=None,
        surprise=None,
        surprise_missing=["named_consensus_unavailable"],
        actual_available=True,
        mdf_pre_event_available_prior=None,
        mdf_pre_event_prior_missing="no_verified_actual_publication_cutoff",
        continuous_version_bound=False,
    )


def parse_release(raw, source_url):
    if type(raw) is not bytes or not 0 < len(raw) <= 8 * 1024 * 1024:
        raise _error("material_invalid")
    try:
        if raw.startswith(b"%PDF-"):
            reader = PdfReader(BytesIO(raw), strict=True)
            if reader.is_encrypted or len(reader.pages) > 20:
                raise ValueError
            pages = [p.extract_text() for p in reader.pages]
        else:
            pages = raw.decode("utf-8").split("\f")
        return parse_release_pages(pages, source_url)
    except ClaimsError:
        raise
    except Exception:
        raise _error("parse_failed") from None


class ClaimsReleaseRecords:
    def __init__(self, store):
        self.store = store

    @storage_boundary("claims_release_storage_read_failed")
    def _rows(self, cutoff):
        rows = self.store.connection.execute(
            "SELECT evidence_id,raw_sha256,raw_locator,obtained_at,"
            "available_at,document "
            f"FROM {self.store.schema}.release_evidence WHERE available_at IS NOT NULL "
            "AND available_at<=%s ORDER BY available_at DESC,evidence_id",
            (cutoff,),
        ).fetchall()
        return [
            dict(
                doc,
                evidence_kind=doc.get("evidence_kind", "official_archive"),
                evidence_id=eid,
                raw_sha256=sha,
                raw_locator=locator,
                obtained_at=obtained.isoformat(),
                obtained_precision="instant",
                available_at=available.isoformat(),
                available_precision="instant",
            )
            for eid, sha, locator, obtained, available, doc in rows
        ]

    @storage_boundary("claims_release_import_outcome_unknown")
    def import_material(self, raw, source_url, *, obtained_at):
        obtained_at = instant(obtained_at)
        now = instant(self.store.clock())
        doc = parse_release(raw, source_url)
        if obtained_at > now or obtained_at.date() < day(doc["source_date"]):
            raise _error("acquisition_time_invalid")
        eid = digest(
            canonical([doc["event_id"], digest(raw), PARSER, source_url]).encode()
        )
        locator = self.store.raw_store.put_verified(raw)
        with self.store.operation():
            with self.store.connection.transaction():
                self.store.connection.execute(
                    f"INSERT INTO {self.store.schema}.release_events VALUES(%s,%s) "
                    "ON CONFLICT DO NOTHING",
                    (doc["event_id"], day(doc["source_date"])),
                )
                self.store.connection.execute(
                    f"INSERT INTO {self.store.schema}.release_evidence "
                    "(evidence_id,event_id,raw_sha256,raw_locator,"
                    "obtained_at,document) "
                    "VALUES(%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                    (
                        eid,
                        doc["event_id"],
                        digest(raw),
                        locator,
                        obtained_at,
                        Jsonb(doc),
                    ),
                )
            # Earlier material becomes consumable after this import commits.
            self.store.connection.execute(
                f"UPDATE {self.store.schema}.release_evidence "
                "SET available_at=clock_timestamp() "
                "WHERE evidence_id=%s AND available_at IS NULL",
                (eid,),
            )
        return next(
            row
            for row in self.read(read_mode="source_history")["events"]
            if row["evidence_id"] == eid
        )

    def read(
        self,
        *,
        as_of=None,
        read_mode="event",
        event_id=None,
        reference_week=None,
        source_version=None,
    ):
        if read_mode not in {"event", "source_history"}:
            raise _error("read_mode_invalid")
        if event_id is not None:
            if not re.fullmatch(r"dol-claims-\d{4}-\d{2}-\d{2}", event_id):
                raise _error("event_id_invalid")
            day(event_id.removeprefix("dol-claims-"))
        if reference_week is not None and day(reference_week).weekday() != 5:
            raise _error("reference_week_invalid")
        if source_version is not None:
            day(source_version)
        cutoff = instant(
            datetime.fromisoformat(as_of)
            if isinstance(as_of, str)
            else as_of
            if as_of is not None
            else self.store.clock()
        )
        with self.store.read_snapshot():
            rows = self._rows(cutoff)
        rows = [
            r
            for r in rows
            if (event_id is None or r["event_id"] == event_id)
            and (
                source_version is None or r["source_version"]["value"] == source_version
            )
            and (
                reference_week is None
                or any(c["week_ending"] == reference_week for c in r["claims"])
            )
        ]
        if read_mode == "source_history":
            events = rows
        else:
            grouped = {}
            for row in rows:
                grouped.setdefault(row["event_id"], []).append(row)
            events = [merge_evidence(proofs) for proofs in grouped.values()]
        return dict(events=events, as_of=cutoff.isoformat(), read_mode=read_mode)

    def link_result(self, result):
        result = copy.deepcopy(result)
        events = self.read(as_of=result.get("as_of"))["events"]
        for row in result["rows"]:
            row["related_releases"] = [
                dict(
                    event_id=e["event_id"],
                    evidence_id=e["evidence_id"],
                    source_version=e["source_version"],
                    reference_week=row["week_ending"],
                    roles=sorted(
                        {
                            c["role"]
                            for c in e["claims"]
                            if c["series_id"] == row["series_id"]
                            and c["week_ending"] == row["week_ending"]
                        }
                    ),
                    scheduled_publication_at=e.get("scheduled_publication_at")
                    if any(
                        c["series_id"] == row["series_id"]
                        and c["week_ending"] == row["week_ending"]
                        and c["role"] == "actual"
                        and c["confirmation_state"] == "confirmed"
                        for c in e["claims"]
                    )
                    else None,
                    schedule_basis=e.get("schedule_basis"),
                    association="scope_only",
                    continuous_version_bound=False,
                )
                for e in events
                if any(
                    c["series_id"] == row["series_id"]
                    and c["week_ending"] == row["week_ending"]
                    for c in e["claims"]
                )
            ]
        return result


def merge_evidence(proofs):
    """Conflicting official evidence stays visible instead of choosing a value."""
    event = copy.deepcopy(proofs[0])
    event["evidence_ids"] = [p["evidence_id"] for p in proofs]
    event["claims"], event["conflicts"] = [], []
    grouped = {}
    for proof in proofs:
        for claim in proof["claims"]:
            key = claim["series_id"], claim["week_ending"], claim["role"]
            grouped.setdefault(key, []).append((claim, proof["evidence_id"]))
    for key, candidates in grouped.items():
        claim = copy.deepcopy(candidates[0][0])
        confirmed = [(c, eid) for c, eid in candidates if c["value"] is not None]
        if confirmed:
            claim = copy.deepcopy(confirmed[0][0])
        claim["evidence_ids"] = sorted({eid for _, eid in candidates})
        if len({Decimal(c["value"]) for c, _ in confirmed}) > 1:
            claim.update(
                value=None,
                value_identity=None,
                confirmation_state="conflict",
                missing_reason="official_evidence_conflict",
            )
            event["conflicts"].append(
                dict(scope=list(key), reason="official_evidence_conflict")
            )
        event["claims"].append(claim)
    event["actual_available"] = all(
        any(
            c["series_id"] == sid
            and c["role"] == "actual"
            and c["confirmation_state"] == "confirmed"
            for c in event["claims"]
        )
        for sid in SERIES_IDS
    )
    event["surprise_missing"] = ["named_consensus_unavailable"]
    if not event["actual_available"]:
        event["surprise_missing"].append("event_actual_unconfirmed")
    return event
