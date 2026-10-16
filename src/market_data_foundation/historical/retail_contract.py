"""T05 bounded Census MARTS product definition; no activation is implied."""

from market_data_foundation.canonical.model import DatasetDefinition, Scope

CENSUS_RETAIL = DatasetDefinition(
    identity="mdf.macro.census.retail",
    revision="t05-r1",
    domain_contract=(
        "Census MARTS headline and MDF retail control group v1; control is an "
        "MDF product definition, never a Census named field."
    ),
    purpose="revision-aware retail snapshot with exact operands and local Read",
    source_qualified=False,
    key_semantics="measurement + reference period + retrieval-bound snapshot",
    unit_semantics="SM: million current USD; MPCSM and derived MoM: percent",
    temporal_semantics=(
        "reference, scheduled release, actual release unknown, requested, obtained, "
        "frozen, Publication decision and usable times remain distinct"
    ),
    revision_semantics=(
        "MARTS monthly and annual revisions; current snapshot is not a historical "
        "as-published vintage; raw/parser/formula/acquisition axes independent"
    ),
    completeness_semantics=(
        "2026-09 headline/control; 2026-08/09 SM for 44X72,441,444,447,722; "
        "2026-09 headline MPCSM; 445 included implicitly; "
        "SA yes; error_data no; US only; synthetic shape not live qualified"
    ),
    scope=Scope(),
)
