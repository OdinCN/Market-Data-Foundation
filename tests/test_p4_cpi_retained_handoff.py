"""Synthetic opaque bytes/semantics only; no retained runtime data or network."""

import base64
import copy
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from market_data_foundation.canonical.kernel import CanonicalKernel
from market_data_foundation.canonical.model import (
    ContractError,
    Grade,
    Key,
    Kind,
    RunKind,
    digest,
)
from market_data_foundation.canonical.postgres import validate_schema
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.historical import cpi_retained_handoff as cpi

BASE = datetime(2026, 10, 6, tzinfo=UTC)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


@pytest.fixture
def receipt(monkeypatch):
    raw = b"opaque synthetic bytes: deliberately NOT BLS JSON"
    monkeypatch.setattr(cpi, "RAW_SHA256", digest(raw))
    monkeypatch.setattr(cpi, "RAW_SIZE", len(raw))
    measurements = []
    for name, (series, meaning, months) in cpi.MEASURES.items():
        measurements.append(
            {
                "name": name,
                "measure_identity": "bls:cpi:" + name,
                "series_id": series,
                "meaning": meaning,
                "change_months": months,
                "comparison_period": "2026-07" if months == 1 else "2025-08",
                "seasonal_adjustment": "Seasonally Adjusted"
                if months == 1
                else "Not Seasonally Adjusted",
                "reference_period": "2026-08",
                "unit": "percent",
                "classification": "provider_returned_calculation",
                "value": "1.23",
                "availability": "available",
                "unavailable_reason": None,
                "preliminary": False,
                "finality": "not_asserted",
                "footnotes": [],
            }
        )
    revision = {
        "acquisition_identity": cpi.ACQUISITION,
        "artifact_identities": {"data": cpi.RAW_SHA256},
        "observation_change_identity": cpi.OBSERVATION_CHANGE,
        "parser_revision": cpi.PARSER,
        "capability_revision": cpi.CAPABILITY_REVISION,
        "change_kind": "parser_correction",
        "retained_revision_identity": cpi.RETAINED_ID,
        "provider_revision": {"state": "not_declared", "value": None},
    }
    record = {
        "provider": "BLS",
        "reference_period": "2026-08",
        "obtained_at": cpi.OBTAINED,
        "usable_at": cpi.USABLE,
        "provider_released_at": None,
        "revision": revision,
        "data": {
            "product": "CPI",
            "reference_period": "2026-08",
            "measurements": measurements,
            "availability": {
                "state": "available",
                "basis": (
                    "retained_measure_completeness_not_historical_cutoff_eligibility"
                ),
            },
        },
        "retained_id": cpi.RETAINED_ID,
    }
    monkeypatch.setattr(cpi, "SEMANTIC_RECORD_SHA256", digest(encode(record)))
    envelope = {
        "api_contract": cpi.API_CONTRACT,
        "product_revision": cpi.PRODUCT_REVISION,
        "capability_id": cpi.CAPABILITY,
    }
    retained = {
        **envelope,
        "retained": record,
        "retained_semantics": (
            "application_retained_semantic_revision_not_provider_vintage"
        ),
    }
    current = {
        **envelope,
        "current": copy.deepcopy(record),
        "as_of": BASE.isoformat(),
        "health": {"state": "healthy", "observed_at": BASE.isoformat()},
        "freshness": {"state": "unknown", "policy": "not_adopted"},
    }
    lineage = {
        "product_revision": cpi.PRODUCT_REVISION,
        "api_contract": cpi.API_CONTRACT,
        "capability": cpi.CAPABILITY,
        "capability_revision": cpi.CAPABILITY_REVISION,
        "reference_period": "2026-08",
        "retained_id": cpi.RETAINED_ID,
        "acquisition_identity": cpi.ACQUISITION,
        "artifact_sha256": cpi.RAW_SHA256,
        "artifact_size": cpi.RAW_SIZE,
        "observation_change_identity": cpi.OBSERVATION_CHANGE,
        "parser_revision": cpi.PARSER,
        "change_kind": "parser_correction",
        "provider_revision": {"state": "not_declared", "value": None},
        "obtained_at": cpi.OBTAINED,
        "usable_at": cpi.USABLE,
        "provider_released_at": None,
        "provider_vintage": None,
        "capture": {
            "captured_at": BASE.isoformat(),
            "pg_read_only": True,
            "raw_access": "existing_mdd_artifact_store.read",
            "backend": "s3",
            "raw_reparsed": False,
        },
    }
    return cpi.CpiHandoffReceipt(
        encode(current), encode(retained), raw, encode(lineage)
    )


def plan_for(receipt):
    return cpi.build_handoff_plan(
        receipt,
        identity="synthetic-cpi",
        started_at=BASE,
        finished_at=BASE + timedelta(seconds=1),
        frozen_at=BASE + timedelta(seconds=2),
    )


def change_record(receipt, path, value, *, both=True):
    current = json.loads(receipt.current_bytes)
    retained = json.loads(receipt.retained_bytes)
    for record in (
        [current["current"], retained["retained"]] if both else [current["current"]]
    ):
        target = record
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value
    return replace(
        receipt, current_bytes=encode(current), retained_bytes=encode(retained)
    )


@pytest.mark.parametrize("member", ["current_bytes", "retained_bytes"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("product_revision", "wrong"),
        ("api_contract", "wrong"),
        ("capability_id", "bls.ppi"),
    ],
)
def test_envelope_identity_mismatch(receipt, member, field, value):
    envelope = json.loads(getattr(receipt, member))
    envelope[field] = value
    with pytest.raises(ContractError, match="mismatch"):
        cpi.admit(replace(receipt, **{member: encode(envelope)}))


@pytest.mark.parametrize(
    "path,value",
    [
        (("retained_id",), "other"),
        (("revision", "capability_revision"), "other"),
        (("revision", "acquisition_identity"), "other"),
        (("revision", "observation_change_identity"), "other"),
        (("revision", "parser_revision"), "other"),
        (("revision", "retained_revision_identity"), "other"),
        (("revision", "artifact_identities", "data"), "0" * 64),
        (("revision", "change_kind"), "provider_revision"),
        (("revision", "provider_revision"), {"state": "declared", "value": "1"}),
        (("usable_at",), "2026-09-01T00:00:00+00:00"),
        (("obtained_at",), BASE.isoformat()),
        (("provider_released_at",), cpi.OBTAINED),
        (("provider_vintage",), "2026-08"),
        (("reference_period",), "2026-09"),
        (("data", "availability", "basis"), "current_provider_freshness"),
        (("data", "health"), {"state": "healthy"}),
        (("data", "measurements", 0, "name"), "renamed"),
        (("data", "measurements", 0, "name"), []),
        (("data", "measurements", 0, "measure_identity"), "other"),
        (("data", "measurements", 0, "series_id"), "other"),
        (("data", "measurements", 0, "unit"), "index"),
        (("data", "measurements", 0, "classification"), "mdf_calculated"),
        (("data", "measurements", 0, "change_months"), True),
        (("data", "measurements", 0, "value"), "NaN"),
        (("data", "measurements", 0, "value"), "9.99"),
        (("data", "measurements", 0, "preliminary"), True),
        (("data", "measurements", 0, "footnotes"), ["invented"]),
    ],
)
def test_bound_record_rejections(receipt, path, value):
    with pytest.raises(ContractError):
        cpi.admit(change_record(receipt, path, value))


@pytest.mark.parametrize("mutation", ["missing", "extra", "duplicate"])
def test_exact_four_measurements(receipt, mutation):
    values = json.loads(receipt.retained_bytes)["retained"]["data"]["measurements"]
    if mutation == "missing":
        values.pop()
    elif mutation == "extra":
        values.append(copy.deepcopy(values[0]))
    else:
        values[-1] = copy.deepcopy(values[0])
    with pytest.raises(ContractError):
        cpi.admit(change_record(receipt, ("data", "measurements"), values))


def test_current_retained_disagreement(receipt):
    with pytest.raises(ContractError, match="disagreement"):
        cpi.admit(change_record(receipt, ("retained_id",), "other", both=False))


@pytest.mark.parametrize("mutation", ["sha", "size", "projection", "mutable"])
def test_raw_opaque_identity(receipt, mutation):
    raw = {
        "sha": b"X" + receipt.raw_bytes[1:],
        "size": receipt.raw_bytes[:-1],
        "projection": receipt.retained_bytes,
        "mutable": bytearray(receipt.raw_bytes),
    }[mutation]
    with pytest.raises(ContractError):
        cpi.admit(replace(receipt, raw_bytes=raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("artifact_sha256", "0" * 64),
        ("artifact_size", 1),
        ("provider_vintage", "invented"),
        ("provider_released_at", cpi.OBTAINED),
        ("obtained_at", BASE.isoformat()),
        ("usable_at", BASE.isoformat()),
        ("acquisition_identity", "other"),
        ("parser_revision", "other"),
        ("observation_change_identity", "other"),
        ("capability_revision", "other"),
        ("provider_revision", {"state": "declared", "value": "1"}),
        ("capture", {"pg_read_only": False}),
    ],
)
def test_lineage_rejections(receipt, field, value):
    lineage = json.loads(receipt.lineage_bytes)
    lineage[field] = value
    with pytest.raises(ContractError):
        cpi.admit(replace(receipt, lineage_bytes=encode(lineage)))


@pytest.mark.parametrize("raw", [b'{"a":1,"a":2}', b'{"a":NaN}', b"[]", b"\xff"])
def test_json_fail_closed(receipt, raw):
    with pytest.raises(ContractError):
        cpi.admit(replace(receipt, current_bytes=raw))


def test_opaque_and_semantic_roundtrip_publication_time(receipt):
    plan = plan_for(receipt)
    assert plan.run.kind == RunKind.REPROCESS and plan.run.binding is None
    assert plan.version.binding is None and plan.version.mapping is None
    assert plan.version.acquisitions == ()
    assert plan.raw_material.grade == Grade.ORIGINAL
    assert plan.semantic_material.grade == Grade.PROJECTED
    assert (
        plan.raw_material.produced_by
        == plan.semantic_material.produced_by
        == plan.run.key
    )
    assert (
        plan.raw_material.acquired_from is plan.semantic_material.acquired_from is None
    )
    assert len(plan.version.finite_scope.members) == 4
    decision = BASE + timedelta(seconds=3)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: decision)
    assert cpi.persist_handoff(kernel, plan) == plan.version
    publication = cpi.publish_handoff(kernel, plan)
    assert publication.decision_at == publication.usable_at == decision
    restored = InMemoryLedger.from_snapshot(ledger.snapshot())
    kernel = CanonicalKernel(restored, lambda: decision)
    for record in (plan.run, plan.raw_material, plan.semantic_material, plan.version):
        assert restored.get(record.key) == record
    assert restored.get_content(plan.version.key) == plan.content
    assert cpi.readback_materials(restored.get_content(plan.version.key)) == (
        cpi.CpiCanonicalMaterials(receipt.retained_bytes, receipt.raw_bytes)
    )
    assert (
        restored.publication_by_occurrence(plan.publication_request.occurrence)
        == publication
    )
    assert not any(key[0] == Kind.ACQUISITION.value for key in restored._records)
    for at in (
        datetime.fromisoformat(cpi.OBTAINED),
        datetime.fromisoformat(cpi.USABLE),
        decision - timedelta(microseconds=1),
    ):
        assert not kernel.eligible(plan.version, at)
    assert kernel.eligible(plan.version, decision)
    assert cpi.publish_handoff(kernel, plan) == publication
    payload = json.loads(plan.content)
    assert payload["admitted_record"] == json.loads(receipt.retained_bytes)["retained"]
    assert payload["mdd_lineage"]["provider_vintage"] is None
    assert payload["mdd_lineage"]["provider_revision"]["state"] == "not_declared"
    assert "health" not in payload["admitted_record"]


def test_nonsemantic_intake_observations_do_not_change_canonical_identity(receipt):
    before = plan_for(receipt)
    current = json.loads(receipt.current_bytes)
    current["as_of"] = "2099-01-01T00:00:00Z"
    current["health"] = {"state": "unhealthy", "observed_at": "2099-01-01T00:00:00Z"}
    current["freshness"] = {"state": "stale", "policy": "invented"}
    lineage = json.loads(receipt.lineage_bytes)
    lineage["capture"]["captured_at"] = (BASE - timedelta(days=1)).isoformat()
    changed = replace(
        receipt, current_bytes=encode(current), lineage_bytes=encode(lineage)
    )
    after = plan_for(changed)
    assert changed.current_bytes != receipt.current_bytes
    assert changed.lineage_bytes != receipt.lineage_bytes
    assert after.content == before.content
    assert after.version == before.version
    assert after.version.content_digest == before.version.content_digest
    assert after.raw_material == before.raw_material
    assert after.semantic_material == before.semantic_material
    assert after.run.transform == before.run.transform
    assert after.run == before.run
    promoted = change_record(changed, ("usable_at",), current["health"]["observed_at"])
    with pytest.raises(ContractError):
        cpi.admit(promoted)


@pytest.mark.parametrize(
    "target",
    [
        "run_binding",
        "run_kind",
        "version_binding",
        "mapping",
        "acquisition",
        "publication_time",
        "content",
    ],
)
def test_plan_mutation_rejected_before_writes(receipt, target):
    plan = plan_for(receipt)
    binding = Key(Kind.BINDING, "fabricated-active", "r1")
    if target == "run_binding":
        plan = replace(plan, run=replace(plan.run, binding=binding))
    elif target == "run_kind":
        plan = replace(plan, run=replace(plan.run, kind=RunKind.COLLECT))
    elif target == "version_binding":
        plan = replace(plan, version=replace(plan.version, binding=binding))
    elif target == "mapping":
        plan = replace(plan, version=replace(plan.version, mapping="active"))
    elif target == "acquisition":
        plan = replace(
            plan,
            version=replace(
                plan.version, acquisitions=(Key(Kind.ACQUISITION, "fabricated"),)
            ),
        )
    elif target == "publication_time":
        plan = replace(
            plan,
            publication_request=replace(
                plan.publication_request, usable_at=datetime.fromisoformat(cpi.USABLE)
            ),
        )
    else:
        plan = replace(plan, content=b"changed")
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=5))
    snapshot = ledger.snapshot()
    with pytest.raises(ContractError, match="plan_modified"):
        cpi.persist_handoff(kernel, plan)
    assert ledger.snapshot() == snapshot
    with pytest.raises(ContractError, match="plan_modified"):
        cpi.publish_handoff(kernel, plan)
    assert ledger.snapshot() == snapshot


def test_backdating_rejected(receipt):
    old = datetime.fromisoformat(cpi.USABLE)
    with pytest.raises(ContractError, match="backdated"):
        cpi.build_handoff_plan(
            receipt, identity="old", started_at=old, finished_at=old, frozen_at=old
        )
    plan = plan_for(receipt)
    ledger = InMemoryLedger()
    cpi.persist_handoff(
        CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3)), plan
    )
    for at in (old, plan.version.frozen_at - timedelta(microseconds=1)):
        with pytest.raises(ContractError, match="publication_backdated"):
            cpi.publish_handoff(CanonicalKernel(ledger, lambda: at), plan)
    assert ledger.publications_for_version(plan.version.key) == ()
    with pytest.raises(ContractError, match="freeze_in_future"):
        cpi.persist_handoff(CanonicalKernel(InMemoryLedger(), lambda: BASE), plan)


def test_publication_requires_exact_persisted_carrier(receipt):
    plan = plan_for(receipt)
    ledger = InMemoryLedger()
    kernel = CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3))
    with pytest.raises(ContractError, match="stored_plan_mismatch"):
        cpi.publish_handoff(kernel, plan)
    cpi.persist_handoff(kernel, plan)
    ledger._contents[plan.version.identity] = (digest(b"altered"), b"altered")
    with pytest.raises(ContractError, match="stored_content_mismatch"):
        cpi.publish_handoff(kernel, plan)
    assert ledger.publications_for_version(plan.version.key) == ()


@pytest.mark.parametrize("mutation", ["metadata", "member", "extra"])
def test_content_readback_rejects_tampering(receipt, mutation):
    payload = json.loads(plan_for(receipt).content)
    if mutation == "metadata":
        payload["mdd_lineage"]["parser_revision"] = "other"
    elif mutation == "member":
        payload["materials"]["original.bin"] = base64.b64encode(b"altered").decode()
    else:
        payload["provider_vintage"] = "invented"
    with pytest.raises(ContractError):
        cpi.readback_materials(encode(payload))


def test_postgres_namespace_is_exact():
    assert validate_schema("mdf_p4_macro_bls_cpi_retained_handoff_r1")
    for name in (
        "public",
        "mdf_p4_macro_bls_cpi_retained_handoff_r1_extra",
        "mdf_p4_macro",
    ):
        with pytest.raises(ContractError):
            validate_schema(name)


def test_publication_resamples_guarded_clock(receipt):
    plan = plan_for(receipt)
    ledger = InMemoryLedger()
    cpi.persist_handoff(
        CanonicalKernel(ledger, lambda: BASE + timedelta(seconds=3)), plan
    )
    samples = iter([BASE + timedelta(seconds=4), datetime.fromisoformat(cpi.USABLE)])
    with pytest.raises(ContractError, match="publication_backdated"):
        cpi.publish_handoff(CanonicalKernel(ledger, lambda: next(samples)), plan)
    assert ledger.publications_for_version(plan.version.key) == ()
