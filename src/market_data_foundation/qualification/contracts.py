"""Provider-independent capability and fixture identifiers."""

import tomllib
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path


class Capability(StrEnum):
    """Frozen securities qualification scope used by the TDX/Fuyao contracts."""

    SECURITY_UNIVERSE = "security.universe"
    SECURITY_METADATA = "security.metadata"
    QUOTE_SNAPSHOT = "quote.snapshot"
    BARS_DAILY = "bars.daily"
    BARS_INTRADAY = "bars.intraday"
    TRADES_INTRADAY = "trades.intraday"
    CORPORATE_ACTIONS = "corporate_actions"
    SECTOR_INDUSTRY = "sector.industry"
    SECTOR_CONCEPT = "sector.concept"
    SECTOR_MEMBERSHIP = "sector.membership"
    ADJUSTMENT_FACTORS = "adjustment.factors"
    BARS_ADJUSTED = "bars.adjusted"
    FINANCE_SNAPSHOT = "finance.snapshot"
    FINANCE_F10 = "finance.f10"
    FINANCIAL_STATEMENTS = "financial.statements"
    TRADING_CALENDAR = "calendar.trading_days"


class MacroCapability(StrEnum):
    """Canonical macro scope; never expands securities budgets or matrices."""

    TIMESERIES = "macro.timeseries"


@dataclass(frozen=True)
class Fixture:
    fixture_id: str
    capability: Capability | MacroCapability
    symbols: tuple[str, ...]
    start: date | None
    end: date | None
    page_size: int
    semantics: dict[str, str]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.fixture_id, str)
            or not self.fixture_id
            or type(self.page_size) is not int
            or self.page_size < 1
        ):
            raise ValueError("fixture identifier and positive page size are required")
        if not isinstance(self.capability, (Capability, MacroCapability)):
            raise ValueError("fixture requires a canonical capability")
        if any(
            type(value) is not date
            for value in (self.start, self.end)
            if value is not None
        ):
            raise ValueError("fixture bounds must be dates")
        if (self.start is None) != (self.end is None):
            raise ValueError("fixture date range must have both bounds or neither")
        if self.start and self.end and self.start > self.end:
            raise ValueError("fixture date range is reversed")
        if len(set(self.symbols)) != len(self.symbols):
            raise ValueError("fixture symbols must be unique")
        if any(not isinstance(symbol, str) or not symbol for symbol in self.symbols):
            raise ValueError("fixture symbols must be nonempty strings")


def load_fixtures(path: Path) -> tuple[Fixture, ...]:
    """Load the frozen securities fixture format and capability scope."""
    return _load_fixtures(path, Capability, scope=None)


def load_macro_fixtures(path: Path) -> tuple[Fixture, ...]:
    """Load explicitly scoped macro fixtures without changing legacy inputs."""
    return _load_fixtures(path, MacroCapability, scope="macro")


def _load_fixtures(path, capability_type, *, scope):
    with path.open("rb") as file:
        document = tomllib.load(file)
    if document.get("schema_version") != 1:
        raise ValueError("unsupported fixture schema version")
    if document.get("capability_scope") != scope:
        raise ValueError("fixture capability scope does not match loader")
    fixtures = tuple(
        Fixture(
            fixture_id=row["id"],
            capability=capability_type(row["capability"]),
            symbols=tuple(row["symbols"]),
            start=row.get("start"),
            end=row.get("end"),
            page_size=row["page_size"],
            semantics=dict(row["semantics"]),
        )
        for row in document["fixtures"]
    )
    if len({fixture.fixture_id for fixture in fixtures}) != len(fixtures):
        raise ValueError("fixture identifiers must be unique")
    return fixtures
