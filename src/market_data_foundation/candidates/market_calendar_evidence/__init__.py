"""A-CAL offline evidence adapter. Importing this package has no I/O effects."""

from .adapter import (
    CalendarEvidence,
    CompletionCutoff,
    D1CalendarInput,
    SessionAssignment,
)
from .model import (
    Claim,
    Completion,
    DayEvidence,
    EvidenceKind,
    MaterialReceipt,
    ParsedMaterial,
    Session,
    State,
)
from .parser import parse_material
from .tdx import TdxWorkdayObservation, parse_tdx_workday

__all__ = [
    "CalendarEvidence",
    "Claim",
    "Completion",
    "CompletionCutoff",
    "D1CalendarInput",
    "DayEvidence",
    "EvidenceKind",
    "MaterialReceipt",
    "ParsedMaterial",
    "Session",
    "SessionAssignment",
    "State",
    "TdxWorkdayObservation",
    "parse_material",
    "parse_tdx_workday",
]
