"""Finite T07 official-document identities; provider time is never availability."""

from dataclasses import dataclass
from datetime import UTC, date, datetime
from hashlib import sha256

from market_data_foundation.canonical.model import DatasetDefinition, Scope

CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
DOCUMENT_ID = "FOMC:STATEMENT:2026-09-16"
MEETING_START = date(2026, 9, 15)
MEETING_END = date(2026, 9, 16)
OFFICIAL_PUBLISHED_AT = datetime(2026, 9, 16, 18, tzinfo=UTC)
EXTRACTION_REVISION = "t07-fomc-text-r1"
MAX_BODY_BYTES = 5 * 1024 * 1024


@dataclass(frozen=True)
class CalendarDiscovery:
    calendar_raw_sha256: str
    calendar_raw_size: int
    statement_url: str
    meeting_start: date = MEETING_START
    meeting_end: date = MEETING_END


@dataclass(frozen=True)
class StatementDocument:
    source_url: str
    raw_sha256: str
    raw_size: int
    text: str
    last_update: str | None
    document_id: str = DOCUMENT_ID
    document_kind: str = "FOMC_STATEMENT"
    meeting_start: date = MEETING_START
    meeting_end: date = MEETING_END
    official_published_at: datetime = OFFICIAL_PUBLISHED_AT
    extraction_revision: str = EXTRACTION_REVISION

    @property
    def text_sha256(self) -> str:
        return sha256(self.text.encode("utf-8")).hexdigest()


# Static candidate definition; local publication does not qualify the Source.

PROVIDER = "official federalreserve.gov"
STATEMENT_DOCUMENT = DatasetDefinition(
    identity="mdf.macro.fomc-statement-document",
    revision="t07-r1",
    domain_contract="OFFICIAL_DOCUMENT/FOMC_STATEMENT",
    purpose="replayable official FOMC statement document",
    source_qualified=False,
    key_semantics="FOMC:STATEMENT:2026-09-16",
    unit_semantics="official document and extracted UTF-8 text",
    temporal_semantics=(
        "official publication metadata independent of acquisition, "
        "freeze, decision and usability"
    ),
    revision_semantics=(
        "raw identities and extraction revision; no provider revision inferred"
    ),
    completeness_semantics="exact calendar and discovered statement HTML for one event",
    scope=Scope(),
)
