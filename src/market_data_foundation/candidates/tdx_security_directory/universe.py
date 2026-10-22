"""Explicit ports to frozen D1, without Source, Calendar or publication rights."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from market_data_foundation.canonical.model import Key, Kind, instant, require
from market_data_foundation.domain_contracts.market_universe import (
    REAL_QUALIFICATIONS,
    AssetClass,
    DateWindow,
    InstrumentIdentity,
    Lifecycle,
    MembershipMode,
    Provenance,
    SourceMapping,
    UniverseMember,
    UniverseRevision,
    text_fields,
)
from market_data_foundation.qualification.contracts import Capability

from .adapter import DirectoryResponse, PageAssembly, _code, assemble_pages, decode
from .schema import DECODE_REVISION, MAX_ROWS, Exchange


@dataclass(frozen=True)
class MappingAssertion:
    """Caller-supplied identity evidence, never derived from a code prefix.

    Separate nonoverlapping windows may map a reused code to distinct stable
    identities. This port does not assert historical Universe membership.
    """

    instrument: InstrumentIdentity
    exchange: Exchange
    code: str
    validity: DateWindow
    evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        require(
            isinstance(self.instrument, InstrumentIdentity), "stable_identity_required"
        )
        require(isinstance(self.exchange, Exchange), "explicit_exchange_required")
        _code(self.code)
        require(isinstance(self.validity, DateWindow), "mapping_window_required")
        require(
            type(self.evidence) is tuple and bool(self.evidence),
            "mapping_evidence_required",
        )
        text_fields(*self.evidence)


def source_mappings(
    assertions: tuple[MappingAssertion, ...],
    *,
    source: Key,
    binding: Key,
    revision: str,
) -> tuple[SourceMapping, ...]:
    require(type(assertions) is tuple, "immutable_assertions_required")
    require(
        all(type(item) is MappingAssertion for item in assertions),
        "mapping_assertion_required",
    )
    require(
        isinstance(source, Key)
        and isinstance(binding, Key)
        and source.kind == Kind.SOURCE
        and binding.kind == Kind.BINDING,
        "source_binding_required",
    )
    text_fields(revision)
    mappings = tuple(
        SourceMapping(
            item.instrument,
            source,
            binding,
            revision,
            item.exchange.request_namespace,
            item.code,
            item.validity,
            item.evidence,
        )
        for item in assertions
    )
    # Same number on SH and SZ is distinct. Identity may change at a half-open
    # boundary, but two instruments cannot own a request key on the same day.
    for field_name, classification in (
        ("instrument", "ambiguous_source_mapping"),
        ("code", "source_code_collision"),
    ):
        groups = {}
        for item in assertions:
            key = (
                item.instrument
                if field_name == "instrument"
                else (item.exchange, item.code)
            )
            groups.setdefault(key, []).append(item.validity)
        for windows in groups.values():
            ordered = sorted(windows)
            require(
                all(
                    left.end_exclusive <= right.start
                    for left, right in zip(ordered, ordered[1:])
                ),
                classification,
            )
    return tuple(sorted(mappings, key=lambda item: (item.instrument, item.validity)))


@dataclass(frozen=True)
class MemberSelection:
    row_locator: str
    instrument: InstrumentIdentity
    category: AssetClass
    identity_evidence: tuple[str, ...]
    category_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        text_fields(self.row_locator)
        require(
            isinstance(self.instrument, InstrumentIdentity), "stable_identity_required"
        )
        require(isinstance(self.category, AssetClass), "explicit_category_required")
        for evidence in (self.identity_evidence, self.category_evidence):
            require(
                type(evidence) is tuple and bool(evidence),
                "explicit_member_evidence_required",
            )
            text_fields(*evidence)


@dataclass(frozen=True)
class MaterialLink:
    """D3-issued canonical key supplied by the integrator after independent readback."""

    candidate_id: str
    material: Key

    def __post_init__(self) -> None:
        text_fields(self.candidate_id)
        require(
            isinstance(self.material, Key) and self.material.kind == Kind.MATERIAL,
            "canonical_material_key_required",
        )


def snapshot_universe(
    inputs: tuple[DirectoryResponse | PageAssembly, ...],
    selections: tuple[MemberSelection, ...],
    material_links: tuple[MaterialLink, ...],
    *,
    universe_id: str,
    mapping_revision: str,
    known_at: datetime,
) -> UniverseRevision:
    """Produce bounded single-day D1 content from explicit, unqualified inputs.

    The date is the obtained_at local SH/SZ/BJ date, never an arbitrary historical
    window. No CalendarRevision is invented. All category gates remain UNKNOWN,
    including for synthetic inputs. Empty selections and anomalous inputs fail.
    """
    require(type(inputs) is tuple and bool(inputs), "immutable_inputs_required")
    require(type(selections) is tuple and bool(selections), "explicit_members_required")
    require(type(material_links) is tuple, "immutable_material_links_required")
    require(
        all(type(item) is MemberSelection for item in selections),
        "member_selection_required",
    )
    require(
        all(type(item) is MaterialLink for item in material_links),
        "material_link_required",
    )
    require(len(inputs) <= 144 and len(selections) <= MAX_ROWS, "snapshot_budget")
    require(isinstance(known_at, datetime), "instant_required")
    instant(known_at)
    responses, records = [], []
    for item in inputs:
        if type(item) is PageAssembly:
            require(len(item.responses) >= 2, "invalid_page_assembly")
            verified = assemble_pages(
                item.responses[0], item.responses[1:-1], item.responses[-1]
            )
            require(verified == item, "assembly_content_mismatch")
            batch = item.responses
        else:
            require(type(item) is DirectoryResponse, "decoded_input_required")
            batch = (item,)
        for response in batch:
            require(decode(response.material) == response, "decoded_content_mismatch")
        require(not item.issues, "directory_validation_issues")
        responses.extend(batch)
        records.extend(item.records)
        require(
            len(responses) <= 144
            and len(records) <= MAX_ROWS
            and sum(len(response.material.payload) for response in responses)
            <= 64 * 1024 * 1024,
            "snapshot_budget",
        )
    base = responses[0].material
    for response in responses:
        material = response.material
        require(
            (
                material.source,
                material.binding,
                material.mode,
                material.fixture_id,
                material.capability,
                material.request.snapshot_id,
                material.snapshot_day,
                material.implementation_sha256,
                material.upstream_commit,
            )
            == (
                base.source,
                base.binding,
                base.mode,
                base.fixture_id,
                base.capability,
                base.request.snapshot_id,
                base.snapshot_day,
                base.implementation_sha256,
                base.upstream_commit,
            ),
            "snapshot_binding_drift",
        )
        require(
            material.capability == Capability.SECURITY_UNIVERSE,
            "universe_capability_required",
        )
        require(
            known_at.astimezone(UTC) >= material.known_at.astimezone(UTC),
            "known_at_backdated",
        )
    locators = {record.row_locator: record for record in records}
    require(len(locators) == len(records), "duplicate_input_row")
    require(
        len({(record.exchange, record.code) for record in records}) == len(records),
        "duplicate_input_code",
    )
    ids = {response.material.material_id for response in responses}
    links = {link.candidate_id: link.material for link in material_links}
    require(
        len(links) == len(material_links)
        and set(links) == ids
        and len(set(links.values())) == len(links),
        "exact_canonical_material_links_required",
    )
    require(
        len({item.row_locator for item in selections}) == len(selections),
        "duplicate_member_selection",
    )
    window = DateWindow(base.snapshot_day, base.snapshot_day + timedelta(days=1))
    members, assertions = [], []
    for selection in selections:
        require(selection.row_locator in locators, "selected_row_not_found")
        row = locators[selection.row_locator]
        require(
            row.origin != "UPSTREAM_HARDCODED_SEED",
            "hardcoded_seed_not_membership_evidence",
        )
        evidence = (
            (row.row_locator,)
            + selection.identity_evidence
            + selection.category_evidence
        )
        assertions.append(
            MappingAssertion(
                selection.instrument,
                row.exchange,
                row.code,
                window,
                evidence,
            )
        )
        members.append(
            UniverseMember(
                selection.instrument,
                selection.category,
                row.exchange.value,
                row.code,
                row.exchange.request_namespace,
                (window,),
                Lifecycle(None, None, None, ()),
            )
        )
    obtained_at = max(
        (response.material.obtained_at for response in responses),
        key=lambda value: value.astimezone(UTC),
    )
    return UniverseRevision(
        universe_id=universe_id,
        schema_revision=DECODE_REVISION,
        effective=window,
        history_window=window,
        known_at=known_at,
        mode=MembershipMode.SNAPSHOT,
        inclusion_rule="explicit row selection with caller identity/category evidence",
        exclusion_rule="unselected rows excluded only from this bounded snapshot",
        authority_scope="offline content candidate; no Source/Calendar/usage admission",
        enumeration_evidence=tuple(sorted(ids)),
        members=tuple(members),
        calendars=(),
        mappings=source_mappings(
            tuple(assertions),
            source=base.source,
            binding=base.binding,
            revision=mapping_revision,
        ),
        provenance=Provenance(
            Capability.SECURITY_UNIVERSE,
            base.source,
            base.binding,
            mapping_revision,
            tuple(links[key] for key in sorted(links)),
            obtained_at,
            None,
            base.mode,
        ),
        qualifications=REAL_QUALIFICATIONS,
    )
