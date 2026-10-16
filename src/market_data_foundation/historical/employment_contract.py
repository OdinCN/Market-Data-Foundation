"""Producer-local candidate Definition; generic consumer admission is unchanged."""

from market_data_foundation.canonical.model import DatasetDefinition, Scope

BLS_EMPLOYMENT = DatasetDefinition(
    identity="mdf.macro.bls.employment.ces-cps",
    revision="t03-r1",
    domain_contract=(
        "Exact CES0000000001, LNS14000000, CES0500000003 retained candidate"
    ),
    purpose="Revisioned NFP change, unemployment rate and AHE MoM/YoY snapshot",
    source_qualified=False,
    key_semantics="survey + series + measurement + reference; retrieval is not vintage",
    unit_semantics=(
        "CES thousand payroll jobs; CPS percent of people; AHE USD/hour levels"
    ),
    temporal_semantics=(
        "reference, unknown release, obtained, frozen, usable and decision separate"
    ),
    revision_semantics=(
        "immutable three-month snapshots; CES and CPS revisions independent"
    ),
    completeness_semantics=(
        "2026-09 plus prior two months with source values and operands"
    ),
    scope=Scope(),
)
