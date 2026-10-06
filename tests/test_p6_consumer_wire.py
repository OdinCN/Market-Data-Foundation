"""Deterministic transport-neutral P6 consumer wire contract tests."""

import base64
import json
from datetime import timedelta, timezone

import pytest

from market_data_foundation.canonical import consumer_wire
from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    ReadFailure,
    read_as_of,
    read_exact_version,
)
from market_data_foundation.canonical.consumer_wire import WIRE_REVISION, handle_request
from market_data_foundation.canonical.model import digest
from market_data_foundation.canonical.repository import InMemoryLedger
from market_data_foundation.domain_contracts.dense_market import RAW_DAILY_BARS
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from tests.test_p6_consumer_read import _seed_bound_cpi, _seed_daily


def _definition(key) -> dict[str, str]:
    return {
        "kind": key.kind.value,
        "identity": key.identity,
        "revision": key.revision,
    }


def _request(value: dict[str, object]) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()


def _exact_request(definition, version_identity: str, **extra: object) -> bytes:
    return _request(
        {
            "wire_revision": WIRE_REVISION,
            "mode": "exact_version",
            "definition": _definition(definition),
            "version_identity": version_identity,
            **extra,
        }
    )


def _as_of_request(definition, as_of: str) -> bytes:
    return _request(
        {
            "wire_revision": WIRE_REVISION,
            "mode": "as_of",
            "definition": _definition(definition),
            "as_of": as_of,
        }
    )


def _decoded(response: bytes) -> dict[str, object]:
    assert (
        response
        == json.dumps(
            json.loads(response),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
    )
    return json.loads(response)


@pytest.mark.parametrize("family", ["cpi", "bars"])
def test_exact_success_preserves_exact_content_and_distinct_identities(
    family: str,
) -> None:
    if family == "cpi":
        ledger, plan, publication = _seed_bound_cpi()
        definition = BLS_CPI_HEADLINES
    else:
        ledger, _kernel, plan, publication = _seed_daily()
        definition = RAW_DAILY_BARS

    response = handle_request(
        ledger, _exact_request(definition.key, plan.version.identity)
    )
    body = _decoded(response)
    assert body["wire_revision"] == WIRE_REVISION
    assert body["status"] == "ok"
    assert body["mode"] == "exact_version"
    view = body["view"]
    content = view["content"]
    assert content["encoding"] == "base64"
    assert base64.b64decode(content["base64"], validate=True) == plan.content
    assert content["sha256"] == digest(plan.content) == plan.version.content_digest
    assert view["definition"]["identity"] == definition.identity
    assert view["version"]["identity"] == plan.version.identity
    assert view["publication"]["request"]["identity"] == publication.request.identity
    assert view["content_mode"] == "whole_version"

    if family == "cpi":
        assert view["binding"]["identity"] == plan.binding_candidate.identity
        assert view["source"]["identity"] == plan.source.identity
        assert view["acquisitions"][0]["external_receipt_present"] is True
        assert all(item["controlled_ref_present"] for item in view["materials"])
        for private in (
            b"controlled-receipt",
            b"mdf-content://" + plan.version.identity.encode(),
        ):
            assert private not in response
    else:
        assert view["binding"] is None
        assert view["source"] is None
        assert view["acquisitions"] == []


@pytest.mark.parametrize("family", ["cpi", "bars"])
def test_as_of_matches_in_process_exact_resolution(family: str) -> None:
    if family == "cpi":
        ledger, plan, publication = _seed_bound_cpi()
        definition = BLS_CPI_HEADLINES.key
    else:
        ledger, _kernel, plan, publication = _seed_daily()
        definition = RAW_DAILY_BARS.key
    as_of = publication.usable_at + timedelta(microseconds=1)
    expected = read_as_of(ledger, definition, as_of=as_of)

    body = _decoded(
        handle_request(ledger, _as_of_request(definition, as_of.isoformat()))
    )
    assert body["mode"] == "as_of"
    assert body["view"]["version"]["identity"] == expected.version.identity
    assert body["view"]["content"]["sha256"] == expected.version.content_digest
    assert base64.b64decode(body["view"]["content"]["base64"]) == expected.content


def test_response_is_byte_identical_after_snapshot_restore() -> None:
    ledger, _kernel, plan, _publication = _seed_daily("wire-snapshot")
    request = _exact_request(RAW_DAILY_BARS.key, plan.version.identity)
    before = handle_request(ledger, request)
    restored = InMemoryLedger.from_snapshot(ledger.snapshot())
    after = handle_request(restored, request)
    assert before == after


@pytest.mark.parametrize("failure", list(ReadFailure))
def test_all_consumer_failures_pass_through_exactly(
    monkeypatch, failure: ReadFailure
) -> None:
    def fail(*_args, **_kwargs):
        raise ReadContractError(failure)

    monkeypatch.setattr(consumer_wire, "read_exact_version", fail)
    request = _exact_request(RAW_DAILY_BARS.key, "bounded-version")
    assert handle_request(InMemoryLedger(), request) == _request(
        {
            "wire_revision": WIRE_REVISION,
            "status": "error",
            "classification": failure.value,
        }
    )


@pytest.mark.parametrize(
    "payload,classification",
    [
        (b"not-json secret input", "invalid_json"),
        (b"\xff", "invalid_json"),
        (b'{"mode":"as_of","mode":"exact_version"}', "invalid_json"),
        (b"NaN", "invalid_json"),
        (b"[]", "invalid_request"),
        (_request({"mode": "exact_version"}), "invalid_request"),
        (
            _request(
                {
                    "wire_revision": "future-secret-revision",
                    "mode": "exact_version",
                }
            ),
            "unsupported_wire_revision",
        ),
        (
            _exact_request(RAW_DAILY_BARS.key, "version", unknown="private-value"),
            "invalid_request",
        ),
        (
            _request(
                {
                    "wire_revision": WIRE_REVISION,
                    "mode": "latest",
                    "definition": _definition(RAW_DAILY_BARS.key),
                }
            ),
            "invalid_request",
        ),
        (
            _as_of_request(RAW_DAILY_BARS.key, "2026-10-06T00:00:00"),
            "invalid_timestamp",
        ),
        (
            _as_of_request(RAW_DAILY_BARS.key, "not-a-time private-value"),
            "invalid_timestamp",
        ),
    ],
)
def test_malformed_requests_have_closed_stable_errors(
    payload: bytes, classification: str
) -> None:
    response = handle_request(InMemoryLedger(), payload)
    assert response == _request(
        {
            "wire_revision": WIRE_REVISION,
            "status": "error",
            "classification": classification,
        }
    )
    assert b"secret" not in response
    assert b"private-value" not in response


def test_optional_exact_as_of_is_canonicalized_to_utc() -> None:
    ledger, _kernel, plan, publication = _seed_daily("wire-offset")
    request = _exact_request(
        RAW_DAILY_BARS.key,
        plan.version.identity,
        as_of=publication.usable_at.astimezone(
            timezone(timedelta(hours=8))
        ).isoformat(),
    )
    body = _decoded(handle_request(ledger, request))
    assert body["view"]["publication"]["decision_at"].endswith("Z")
    assert body["view"]["version"]["frozen_at"].endswith(".000000Z")


def test_wire_does_not_add_transport_selection_or_private_locator_fields() -> None:
    ledger, plan, _publication = _seed_bound_cpi()
    body = _decoded(
        handle_request(
            ledger, _exact_request(BLS_CPI_HEADLINES.key, plan.version.identity)
        )
    )
    assert not ({"latest", "fallback", "page", "page_size", "next_page"} & body.keys())
    encoded = json.dumps(body, sort_keys=True)
    for forbidden_field in (
        '"external_receipt"',
        '"controlled_ref"',
        '"postgres"',
        '"s3_endpoint"',
        '"bucket"',
        '"object_key"',
        '"local_path"',
    ):
        assert forbidden_field not in encoded
    assert read_exact_version(ledger, BLS_CPI_HEADLINES.key, plan.version.key).content
