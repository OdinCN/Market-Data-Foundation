"""Offline cross-lane clock regressions using the reviewed synthetic fixtures."""

from dataclasses import replace
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from market_data_foundation.candidates.tdx_exhq_directory import (
    DirectoryError,
    build_directory,
)
from market_data_foundation.candidates.tdx_security_directory import (
    assemble_pages,
    decode,
    snapshot_universe,
)
from market_data_foundation.canonical.market_projection import ProjectionError, ReadCode
from market_data_foundation.canonical.market_publication import (
    MarketPublication,
    active_at,
    validate_history,
)
from market_data_foundation.canonical.model import ContractError, Decision
from market_data_foundation.domain_contracts.market_universe import (
    canonical_bytes,
    content_id,
)
from tests import test_r5_i6_a_ex_dir as ex
from tests import test_r5_i6_a_std as std
from tests.test_r5_i6_d3_material_projection import Scenario

NY = ZoneInfo("America/New_York")


def clock(hour, minute, zone=NY):
    return datetime(2024, 11, 3, hour, minute, tzinfo=UTC).astimezone(zone)


def scenario(zone=NY, *, obtained=None, frozen=None, evaluated=None):
    s = Scenario()
    s.clock.value = clock(6, 30, zone)
    s.receipt = replace(
        s.receipt,
        requested_at=clock(5, 0, zone),
        obtained_at=obtained or clock(5, 5, zone),
        received_at=clock(5, 10, zone),
        known_at=clock(5, 15, zone),
    )
    s.ledger = s.fresh()
    s.ledger.retain(s.receipt, s.raw)
    s.assessment = s.assess(evaluated_at=evaluated or clock(5, 15, zone))
    s.version = replace(
        s.version, assessment=s.assessment, frozen_at=frozen or clock(5, 20, zone)
    )
    s.ledger.rebuild("fold-g1", (s.month,))
    return s


RECEIPT_CLOCKS = ("requested_at", "obtained_at", "received_at", "known_at")


def swapped_receipt(s, field):
    original = getattr(s.receipt, field)
    swapped = replace(s.receipt, **{field: original.replace(fold=1)})
    # Python equality is the bug: the complete canonical identities differ.
    assert swapped == s.receipt
    assert canonical_bytes(swapped) != canonical_bytes(s.receipt)
    return swapped


@pytest.mark.parametrize("field", RECEIPT_CLOCKS)
def test_canonical_receipt_fold_swap_blocks_version_commit(field):
    s = scenario()
    s.ledger = s.fresh()
    s.ledger.retain(swapped_receipt(s, field), s.raw)
    before = canonical_bytes(s.ledger.checkpoint())
    with pytest.raises(ContractError, match="^canonical_material_missing$"):
        s.ledger.commit_version(s.version)
    assert canonical_bytes(s.ledger.checkpoint()) == before


@pytest.mark.parametrize("decision", (Decision.PUBLISH, Decision.RESTORE))
@pytest.mark.parametrize("field", RECEIPT_CLOCKS)
def test_canonical_receipt_fold_swap_blocks_publication(field, decision):
    s = scenario()
    s.ledger.commit_version(s.version)
    s.admit()
    predecessor = None
    if decision == Decision.RESTORE:
        first = s.request()
        s.ledger.publish(first)
        withdrawn = s.request("withdraw", Decision.WITHDRAW, first.key)
        s.clock.value = clock(6, 31)
        s.ledger.publish(withdrawn)
        s.clock.value = clock(6, 32)
        predecessor = withdrawn.key
    # Corrupt only canonical receipt storage after a valid commit, so this
    # independently reaches the publication gate rather than the commit gate.
    s.ledger._state.materials[s.receipt.key] = swapped_receipt(s, field)
    before = canonical_bytes(s.ledger.checkpoint())
    with pytest.raises(ContractError, match="^canonical_material_missing$"):
        s.ledger.publish(s.request("swapped", decision, predecessor))
    assert canonical_bytes(s.ledger.checkpoint()) == before


@pytest.mark.parametrize("operation", ("lineage", "content"))
@pytest.mark.parametrize("field", RECEIPT_CLOCKS)
def test_canonical_receipt_fold_swap_blocks_reader(field, operation):
    s = scenario()
    s.publish()
    s.ledger._state.materials[s.receipt.key] = swapped_receipt(s, field)
    with pytest.raises(ProjectionError) as caught:
        getattr(s.view(), operation)(s.version.key)
    assert caught.value.code == ReadCode.INCONSISTENT


@pytest.mark.parametrize("operation", ("restore", "rebuild"))
def test_canonical_receipt_fold_swap_blocks_recovery(operation):
    s = scenario()
    s.publish()
    swapped = swapped_receipt(s, "obtained_at")
    state = s.ledger._state
    state.materials[swapped.key] = swapped
    identity = ("material", content_id(swapped.key))
    # Model a receipt set accepted by the original writer: both individual
    # commit receipts are valid, but the cross-object material identity is not.
    state.receipts[identity] = replace(
        state.receipts[identity],
        payload_digest=content_id(swapped),
        result_digest=content_id(swapped),
    )
    target = s.fresh() if operation == "restore" else s.ledger
    before = canonical_bytes(target.checkpoint())
    with pytest.raises(ContractError, match="^canonical_material_missing$"):
        if operation == "restore":
            target.restore(s.ledger.checkpoint(), "swap-restored")
        else:
            target.rebuild("swap-rebuilt", (s.month,))
    assert canonical_bytes(target.checkpoint()) == before


@pytest.mark.parametrize("zone", (NY, UTC))
def test_canonical_receipt_equivalent_instants_survive_all_gates(zone):
    s = scenario(zone)
    other = UTC if zone == NY else NY
    equivalent = replace(
        s.receipt,
        **{
            field: getattr(s.receipt, field).astimezone(other)
            for field in RECEIPT_CLOCKS
        },
    )
    assert canonical_bytes(equivalent) == canonical_bytes(s.receipt)
    s.ledger = s.fresh()
    s.ledger.retain(equivalent, s.raw)
    s.ledger.rebuild("equivalent-g1", (s.month,))
    first = s.publish()
    withdrawn = s.request("withdraw", Decision.WITHDRAW, first.key)
    s.clock.value = clock(6, 31)
    s.ledger.publish(withdrawn)
    s.clock.value = clock(6, 32)
    s.ledger.publish(s.request("restore", Decision.RESTORE, withdrawn.key))
    s.ledger.rebuild("equivalent-g2", (s.month,))
    recovered = s.fresh()
    recovered.restore(s.ledger.checkpoint(), "equivalent-restored")
    for ledger in (s.ledger, recovered):
        view = ledger.reader.open("offline.synthetic.ledger")
        assert canonical_bytes(view.lineage(s.version.key)) == canonical_bytes(
            (equivalent,)
        )
        assert view.content(s.version.key) == s.assessment.content


@pytest.mark.parametrize("now_zone", (NY, UTC))
@pytest.mark.parametrize("as_of_zone", (NY, UTC))
@pytest.mark.parametrize("now", ((5, 50), (6, 10)))
@pytest.mark.parametrize("at", ((5, 30), (5, 50), (6, 10), (6, 50)))
def test_as_of_uses_utc_instants(now_zone, as_of_zone, now, at):
    s = scenario()
    s.clock.value = clock(5, 25)
    s.publish()
    s.clock.value = clock(*now, now_zone)
    view = s.view()
    as_of = clock(*at, as_of_zone)
    # The view's captured clock, not a later ledger clock, bounds the request.
    s.clock.value = clock(7, 0)
    if at > now:
        with pytest.raises(ProjectionError) as caught:
            view.select(s.slot, as_of)
        assert caught.value.code == ReadCode.INCONSISTENT
    else:
        assert view.select(s.slot, as_of) == ("SELECTED", (s.version.key,))


@pytest.mark.parametrize("zone", (NY, UTC))
def test_as_of_naive_input_keeps_contract_error(zone):
    s = scenario()
    s.publish()
    with pytest.raises(ContractError, match="^naive_time$"):
        s.view().select(s.slot, clock(5, 50, zone).replace(tzinfo=None))


@pytest.mark.parametrize("zone", (NY, UTC))
@pytest.mark.parametrize("gate", ("decision", "usable", "effective"))
def test_future_publication_gate_has_identical_instant_selection(zone, gate):
    s = scenario(zone)
    # Keep the setup valid even under the old freeze/usable comparisons, so this
    # regression reaches the selection gate in the original implementation.
    s.version = replace(s.version, frozen_at=s.version.frozen_at.astimezone(UTC))
    s.ledger.commit_version(s.version)
    s.admit()
    s.clock.value = clock(6, 10, zone) if gate == "decision" else clock(5, 25, UTC)
    request = s.request()
    if gate != "decision":
        request = replace(request, **{gate + "_at": clock(6, 10, zone)})
    s.ledger.publish(request)
    s.clock.value = clock(6, 30, zone)
    view = s.view()
    for representation in (NY, UTC):
        assert view.select(s.slot, clock(5, 30, representation)) == (
            "NO_PUBLISHED_CANDIDATE",
            (),
        )
        assert view.select(s.slot, clock(6, 30, representation)) == (
            "SELECTED",
            (s.version.key,),
        )


@pytest.mark.parametrize("zone", (NY, UTC))
def test_material_obtained_after_freeze_cannot_commit(zone):
    s = scenario(zone, obtained=clock(6, 10, zone))
    s.clock.value = clock(5, 30, zone)
    with pytest.raises(ContractError, match="material_acquisition_timing_unverified"):
        s.ledger.commit_version(s.version)
    assert not s.ledger.checkpoint().versions


@pytest.mark.parametrize("zone", (NY, UTC))
def test_material_requested_after_obtained_cannot_commit(zone):
    s = scenario(zone)
    s.receipt = replace(s.receipt, requested_at=clock(6, 0, zone))
    s.assessment = replace(s.assessment, materials=(s.receipt,))
    s.version = replace(s.version, assessment=s.assessment)
    with pytest.raises(ContractError, match="material_acquisition_timing_unverified"):
        s.ledger.commit_version(s.version)


@pytest.mark.parametrize("zone", (NY, UTC))
def test_known_time_after_assessment_is_rejected(zone):
    s = Scenario()
    receipt = replace(s.receipt, known_at=clock(6, 10, zone))
    with pytest.raises(ContractError, match="material_known_at_unknown_or_future"):
        s.assess(receipts=(receipt,), evaluated_at=clock(5, 30, zone))


@pytest.mark.parametrize("zone", (NY, UTC))
def test_version_cannot_freeze_before_assessment(zone):
    s = Scenario()
    assessment = replace(s.assessment, evaluated_at=clock(6, 10, zone))
    with pytest.raises(ContractError, match="version_before_assessment"):
        replace(s.version, assessment=assessment, frozen_at=clock(5, 30, zone))


@pytest.mark.parametrize("zone", (NY, UTC))
def test_future_freeze_cannot_commit(zone):
    s = scenario(zone, frozen=clock(6, 10, zone))
    s.clock.value = clock(5, 30, zone)
    with pytest.raises(ContractError, match="version_frozen_in_future"):
        s.ledger.commit_version(s.version)


@pytest.mark.parametrize("operation", ("commit", "rebuild"))
def test_normalization_does_not_infer_timezone_for_naive_ledger_clock(operation):
    s = scenario()
    if operation == "rebuild":
        s.publish()
    s.clock.value = clock(6, 30).replace(tzinfo=None)
    # The original comparisons also reject naive clocks (with TypeError).
    with pytest.raises((ContractError, TypeError)):
        if operation == "commit":
            s.ledger.commit_version(s.version)
        else:
            s.ledger.rebuild("naive-clock-generation", (s.month,))


@pytest.mark.parametrize("zone", (NY, UTC))
def test_publication_cannot_precede_freeze(zone):
    s = scenario(zone, frozen=clock(6, 10, zone))
    s.ledger.commit_version(s.version)
    s.admit()
    s.clock.value = clock(5, 30, zone)
    with pytest.raises(
        ContractError, match="version_frozen_in_future|publication_before_version"
    ):
        s.ledger.publish(s.request())


@pytest.mark.parametrize("zone", (NY, UTC))
def test_usable_time_cannot_precede_decision(zone):
    s = Scenario()
    with pytest.raises(ContractError, match="publication_time_backdated"):
        MarketPublication(s.request(), clock(6, 10, zone), clock(5, 30, zone))


def test_history_usable_time_distinguishes_identical_wall_times():
    s = Scenario()
    event = MarketPublication(
        replace(s.request(), usable_at=clock(5, 30)), clock(5, 0), clock(6, 30)
    )
    with pytest.raises(ContractError, match="publication_usable_time_mismatch"):
        validate_history(s.version.key, (event,))


@pytest.mark.parametrize("zone", (NY, UTC))
def test_history_across_fold_rebuild_and_restore_preserve_order(zone):
    s = scenario(zone)
    s.clock.value = clock(5, 50, zone)
    first = s.publish()
    s.clock.value = clock(6, 10, zone)
    s.ledger.publish(s.request("withdraw", Decision.WITHDRAW, first.key))
    s.clock.value = clock(6, 20, zone)
    s.ledger.rebuild("fold-g2", (s.month,))
    history = s.view().histories((s.version.key,))[0]
    assert [event.decision_at.astimezone(UTC) for event in history] == [
        clock(5, 50, UTC),
        clock(6, 10, UTC),
    ]
    assert active_at(history, clock(6, 0))
    assert not active_at(history, clock(6, 20))
    recovered = s.fresh()
    recovered.restore(s.ledger.checkpoint(), "fold-restored")
    view = recovered.reader.open("offline.synthetic.ledger")
    assert view.histories((s.version.key,))[0] == history


@pytest.mark.parametrize("zone", (NY, UTC))
def test_reversed_publication_history_is_rejected(zone):
    s = Scenario()
    first = MarketPublication(s.request(), clock(6, 10, zone), clock(6, 10, zone))
    second = MarketPublication(
        s.request("withdraw", Decision.WITHDRAW, first.key),
        clock(5, 30, zone),
        clock(5, 30, zone),
    )
    with pytest.raises(ContractError, match="publication_time_not_monotonic"):
        validate_history(s.version.key, (first, second))


@pytest.mark.parametrize("zone", (NY, UTC))
@pytest.mark.parametrize("adapter", ("std", "ex"))
def test_reversed_directory_receipt_clocks_are_rejected(zone, adapter):
    base = std.material({"Count": 0}) if adapter == "std" else ex.capture()[0]
    error = ContractError if adapter == "std" else DirectoryError
    with pytest.raises(error, match="material_clock_order|receipt_clock_order"):
        replace(
            base,
            obtained_at=clock(6, 10, zone),
            received_at=clock(6, 20, zone),
            known_at=clock(5, 30, zone),
        )


@pytest.mark.parametrize("adapter", ("std", "ex"))
def test_valid_directory_receipt_across_fold_preserves_evidence(adapter):
    base = std.material({"Count": 0}) if adapter == "std" else ex.capture()[0]
    times = dict(
        obtained_at=clock(5, 50), received_at=clock(6, 10), known_at=clock(6, 20)
    )
    receipt = replace(base, **times)
    for name, value in times.items():
        assert getattr(receipt, name) is value
    utc = replace(
        base, **{name: value.astimezone(UTC) for name, value in times.items()}
    )
    assert (receipt.material_id if adapter == "std" else receipt.material.identity) == (
        utc.material_id if adapter == "std" else utc.material.identity
    )


def std_at(response, at):
    return decode(
        replace(response.material, obtained_at=at, received_at=at, known_at=at)
    )


@pytest.mark.parametrize("reverse", (False, True))
def test_std_page_capture_order_uses_instants(reverse):
    before = std_at(std.count(1), clock(6, 10) if reverse else clock(5, 50))
    page = std_at(std.page([std.native()]), clock(5, 50) if reverse else clock(6, 10))
    after = std_at(std.count(1), clock(6, 20))
    if reverse:
        with pytest.raises(ContractError, match="page_clock_order"):
            assemble_pages(before, (page,), after)
    else:
        assert not assemble_pages(before, (page,), after).issues


@pytest.mark.parametrize("reverse", (False, True))
def test_ex_capture_order_uses_instants(reverse):
    markets, before, pages, after = ex.capture()
    originals = (markets, before, *pages, after)
    times = (clock(6, 10), clock(5, 50)) if reverse else (clock(5, 50), clock(6, 10))
    values = tuple(
        replace(r, obtained_at=t, received_at=t, known_at=t)
        for r, t in zip(
            originals,
            (
                *times,
                *(clock(6, 15 + index * 5) for index in range(len(originals) - 2)),
            ),
            strict=True,
        )
    )
    args = (values[0], values[1], values[2:-1], values[-1])
    if reverse:
        with pytest.raises(DirectoryError, match="capture_clock_order"):
            build_directory(*args)
    else:
        result = build_directory(*args)
        assert result.known_at is values[-1].known_at
        assert result.known_at.astimezone(UTC) == clock(6, 25, UTC)


def test_ex_unknown_clocks_remain_unknown():
    markets, before, pages, after = ex.capture()
    unknown = replace(markets, obtained_at=None, received_at=None, known_at=None)
    result = build_directory(unknown, before, pages, after)
    assert result.known_at is None
    assert result.qualification == "UNKNOWN"


@pytest.mark.parametrize("backdated", (False, True))
def test_std_snapshot_known_and_aggregate_obtained_use_instants(backdated):
    before = std_at(std.count(1), clock(5, 50))
    page = std_at(std.page([std.native()]), clock(6, 10))
    after = std_at(std.count(1), clock(6, 20))
    assembly = assemble_pages(before, (page,), after)
    args = (
        (assembly,),
        (std.selection(page.records[0]),),
        std.links(*assembly.responses),
    )
    kwargs = dict(
        universe_id="fold snapshot",
        mapping_revision="fold-r1",
        known_at=clock(5, 55) if backdated else clock(6, 30),
    )
    if backdated:
        with pytest.raises(ContractError, match="known_at_backdated"):
            snapshot_universe(*args, **kwargs)
    else:
        result = snapshot_universe(*args, **kwargs)
        assert result.provenance.obtained_at is after.material.obtained_at
