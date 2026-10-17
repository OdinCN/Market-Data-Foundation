"""Single bounded wire adaptation seam, pending retained current official evidence.

Adapters must validate original bytes, not rewrite responses into a synthetic
format. Build, validate and local Read share the corrected strict parser. SDMX-ML
StructureSpecificData, JSON, ZIP and unscoped reference variants remain
fail-closed. Only the exact ECB data-header DSD URN and Eurostat flow usage
chain are admitted; package-qualified Refs retain class-specific validation.
No caller-supplied parser can bypass canonical reconstruction.
"""

from market_data_foundation.canonical.model import require

from .sdmx_official import _identity, _metadata, _one, _reference, _xml, project
from .sdmx_schema import validate_structure


def flow_reference(surface, raw):
    document = _xml(raw, "Structure")
    node = _one(
        document,
        "message:Structures/structure:Dataflows/structure:Dataflow",
    )
    require(_identity(node)[:2] == (surface.agency, surface.flow), "sdmx_flow_mismatch")
    ref = _reference(node, "structure:Structure", "DataStructure")
    require(ref[0] == surface.agency, "sdmx_dsd_agency_mismatch")
    validate_structure(document, surface, "flow")
    return ref


def metadata(surface, entities):
    return _metadata(surface, entities)


def unit_reference(surface, raw):
    """Read only the exact UNIT reference, never invent a version or alias."""
    from .sdmx_official import NS

    dsd = _one(
        _xml(raw, "Structure"),
        "message:Structures/structure:DataStructures/structure:DataStructure",
    )
    dims = _one(dsd, "structure:DataStructureComponents/structure:DimensionList")
    dimensions = dims.findall("structure:Dimension", NS)
    require(
        tuple(d.get("id") for d in dimensions) == surface.dimensions,
        "sdmx_dimension_order_mismatch",
    )
    if surface.provider == "ECB":
        return None
    ref = _reference(
        dimensions[1], "structure:LocalRepresentation/structure:Enumeration", "Codelist"
    )
    require(ref[:2] == ("ESTAT", "UNIT"), "sdmx_unit_reference_mismatch")
    return ref


def validate_candidate(candidate):
    return project(candidate)


def dsd_structure(surface, ref, raw):
    document = _xml(raw, "Structure")
    node = _one(
        document,
        "message:Structures/structure:DataStructures/structure:DataStructure",
    )
    require(
        _identity(node) == ref and ref[0] == surface.agency,
        "sdmx_dsd_reference_mismatch",
    )
    validate_structure(document, surface, "dsd")
