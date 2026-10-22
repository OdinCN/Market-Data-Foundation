"""A-STD offline source candidates and explicit D1 integration ports."""

from .adapter import (
    DirectoryObservation,
    DirectoryRecord,
    DirectoryRequest,
    DirectoryResponse,
    OfflineMaterial,
    PageAssembly,
    ValidationIssue,
    assemble_pages,
    decode,
    observe,
)
from .schema import (
    DECODE_REVISION,
    FIELD_FACTS,
    SDK_VERSION,
    UPSTREAM_COMMIT,
    Exchange,
    Operation,
    Route,
    category_hints,
)
from .universe import (
    MappingAssertion,
    MaterialLink,
    MemberSelection,
    snapshot_universe,
    source_mappings,
)

__all__ = [
    "DECODE_REVISION",
    "FIELD_FACTS",
    "SDK_VERSION",
    "UPSTREAM_COMMIT",
    "DirectoryObservation",
    "DirectoryRecord",
    "DirectoryRequest",
    "DirectoryResponse",
    "Exchange",
    "MappingAssertion",
    "MaterialLink",
    "MemberSelection",
    "OfflineMaterial",
    "Operation",
    "PageAssembly",
    "Route",
    "ValidationIssue",
    "assemble_pages",
    "category_hints",
    "decode",
    "observe",
    "snapshot_universe",
    "source_mappings",
]
