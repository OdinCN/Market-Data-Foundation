"""Synthetic-only ExHq payloads; no provider, file capture or service dependency."""

import hashlib
import socket
import struct
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta, timezone

import pytest

from market_data_foundation.candidates.tdx_exhq_directory import (
    EXTENSION_ENTRIES,
    FIELD_EVIDENCE,
    SDK_COMMIT,
    STATIC_MARKET_REFERENCES,
    CaptureContext,
    DirectoryError,
    EvidenceMode,
    OfflineReceipt,
    Operation,
    PageRequest,
    build_directory,
    decode_count,
    decode_instruments,
    decode_markets,
    observe_directory,
)
from market_data_foundation.candidates.tdx_exhq_directory.schema import (
    MAX_DIRECTORY_BYTES,
    MAX_DIRECTORY_PAGES,
    MAX_PAYLOAD_BYTES,
    content_hash,
)
from market_data_foundation.qualification.contracts import Capability, Fixture

NOW = datetime(2026, 10, 22, 1, tzinfo=UTC)
CONTEXT = CaptureContext(
    "synthetic:exhq",
    "synthetic:binding",
    "mapping-r1",
    "capture-r1",
    EvidenceMode.SYNTHETIC,
)


def fixed(text, width):
    raw = text.encode("gbk")
    assert len(raw) <= width
    return raw.ljust(width, b"\0")


def market_row(market=31, category=2, name="模拟港股", short="港"):
    return (
        bytes([category])
        + fixed(name, 32)
        + bytes([market])
        + fixed(short, 2)
        + bytes(28)
    )


def instrument_row(market=31, category=2, code="00700", name="模拟品种", desc="说明"):
    return (
        bytes([category, market])
        + bytes(3)
        + fixed(code, 9)
        + fixed(name, 17)
        + fixed(desc, 9)
        + bytes(24)
    )


def markets_payload(*rows, declared=None):
    return struct.pack("<H", len(rows) if declared is None else declared) + b"".join(
        rows
    )


def page_payload(*rows, declared=None):
    # First four response bytes have no evidenced offset semantics.
    return (
        b"\x91\x92\x93\x94"
        + struct.pack("<H", len(rows) if declared is None else declared)
        + b"".join(rows)
    )


def count_payload(total):
    return bytes(19) + struct.pack("<I", total)


def receipt(operation, payload, locator, request=None, context=CONTEXT, known_at=NOW):
    return OfflineReceipt(
        context,
        operation,
        payload,
        hashlib.sha256(payload).hexdigest(),
        locator,
        NOW,
        NOW,
        known_at,
        request,
    )


def capture(rows=None, market_rows=None, page_size=2, total=None):
    if rows is None:
        rows = (
            instrument_row(),
            instrument_row(48, 2),
            instrument_row(30, 3, "au2612"),
        )
    if market_rows is None:
        market_rows = (
            market_row(),
            market_row(48, 2, "模拟市场48"),
            market_row(30, 3, "模拟期货", "期"),
        )
    if total is None:
        total = len(rows)
    pages = tuple(
        receipt(
            Operation.INSTRUMENTS,
            page_payload(*rows[start : start + page_size]),
            f"synthetic:page:{start}",
            PageRequest(start, page_size),
        )
        for start in range(0, len(rows), page_size)
    )
    return (
        receipt(Operation.MARKETS, markets_payload(*market_rows), "synthetic:markets"),
        receipt(Operation.COUNT, count_payload(total), "synthetic:count:before"),
        pages,
        receipt(Operation.COUNT, count_payload(total), "synthetic:count:after"),
    )


def with_payload(value, payload):
    return replace(
        value, payload=payload, expected_sha256=hashlib.sha256(payload).hexdigest()
    )


def test_native_fields_and_independent_market_identities():
    inputs = capture()
    snapshot = build_directory(*inputs)
    hk31 = snapshot.mapping_for(31, 2, "00700")
    hk48 = snapshot.mapping_for(48, 2, "00700")
    assert hk31.instrument_id != hk48.instrument_id
    assert hk31.market_record.market == 31
    assert hk48.market_record.market == 48
    assert hk31.code == "00700"
    assert snapshot.instruments[0].name == "模拟品种"
    assert snapshot.instruments[0].desc == "说明"
    assert snapshot.markets[0].shortName == "港"
    assert snapshot.total_count == 3
    assert snapshot.known_at == NOW
    assert snapshot.membership_mode == hk31.membership_mode == "snapshot"
    assert snapshot.qualification == hk31.qualification == "UNKNOWN"
    assert hk31.valid_from is hk31.valid_until is None
    assert snapshot.unresolved_associations == ()
    assert hk31.instrument_evidence.byte_offset == 6
    assert hk48.instrument_evidence.byte_offset == 70
    assert hk31.market_evidence.byte_offset == 2
    assert hk31.instrument_evidence.raw_sha256 == inputs[2][0].expected_sha256
    assert snapshot.mapping_for(30, 3, "au2612").instrument_evidence.byte_offset == 6
    assert snapshot.materials[2].request == PageRequest(0, 2)
    assert snapshot.materials[2].sdk_commit == SDK_COMMIT


def test_fields_match_pinned_ast_index():
    # Portable expected facts copied from the pinned AST, no host-specific reads.
    assert [
        (f.response_struct, f.source_field_name, f.json_name, f.go_type, f.source_ref)
        for f in FIELD_EVIDENCE
    ] == [
        ("ExMarket", "Market", "market", "uint16", "protocol/model_ex.go:31"),
        ("ExMarket", "Category", "category", "uint8", "protocol/model_ex.go:32"),
        ("ExMarket", "Name", "name", "string", "protocol/model_ex.go:33"),
        ("ExMarket", "ShortName", "shortName", "string", "protocol/model_ex.go:34"),
        ("ExInstrument", "Category", "category", "uint8", "protocol/model_ex.go:39"),
        ("ExInstrument", "Market", "market", "uint8", "protocol/model_ex.go:40"),
        ("ExInstrument", "Code", "code", "string", "protocol/model_ex.go:41"),
        ("ExInstrument", "Name", "name", "string", "protocol/model_ex.go:42"),
        ("ExInstrument", "Desc", "desc", "string", "protocol/model_ex.go:43"),
    ]


def test_code9_gbk_first_null_spaces_case_and_unknown_category():
    raw_rows = (
        instrument_row(255, 255, "000123456"),
        instrument_row(255, 255, " Abc "),
        instrument_row(255, 254, "abc"),
    )
    page = decode_instruments(page_payload(*raw_rows), PageRequest(70_000, 3))
    assert [row.code for row in page.rows] == ["000123456", " Abc ", "abc"]
    # Bytes after first NUL are ignored for native text but remain in raw digest.
    raw = bytearray(instrument_row(code="00001"))
    raw[11:14] = b"XYZ"
    assert (
        decode_instruments(page_payload(bytes(raw)), PageRequest(0, 1)).rows[0].code
        == "00001"
    )
    snapshot = build_directory(*capture(raw_rows, market_rows=()))
    assert len(snapshot.unresolved_associations) == 3
    assert snapshot.mapping_for(255, 255, " Abc ").code == " Abc "


def test_uint32_start_uint16_count_and_request_bytes():
    request = PageRequest(70_000, 65535)
    assert request.payload == b"\x70\x11\x01\x00\xff\xff"
    assert PageRequest(2**32 - 1, 1).payload == b"\xff\xff\xff\xff\x01\x00"
    assert PageRequest(0, 0).payload == bytes(6)
    assert decode_count(count_payload(2**32 - 1)) == 2**32 - 1


@pytest.mark.parametrize(
    "start,count",
    [
        (-1, 1),
        (2**32, 1),
        (0, -1),
        (0, 65536),
        (True, 1),
        (0, False),
        (0.0, 1),
        (0, "2"),
        (2**32 - 1, 2),
    ],
)
def test_request_rejects_coercion_and_overflow(start, count):
    with pytest.raises(DirectoryError):
        PageRequest(start, count)


@pytest.mark.parametrize(
    "operation,minimum",
    [
        (Operation.MARKETS, 2),
        (Operation.COUNT, 23),
        (Operation.INSTRUMENTS, 6),
    ],
)
def test_every_short_frame_is_rejected_instead_of_nil_or_zero(operation, minimum):
    for size in range(minimum):
        with pytest.raises(DirectoryError, match="^short_frame_nil_or_zero$"):
            if operation == Operation.MARKETS:
                decode_markets(bytes(size))
            elif operation == Operation.COUNT:
                decode_count(bytes(size))
            else:
                decode_instruments(bytes(size), PageRequest(0, 1))


@pytest.mark.parametrize("missing", [1, 24, 63, 64, 65])
def test_market_and_instrument_declared_count_truncation(missing):
    with pytest.raises(DirectoryError, match="truncated_declared_rows"):
        decode_markets(markets_payload(market_row(), market_row(48))[:-missing])
    with pytest.raises(DirectoryError, match="truncated_declared_rows"):
        decode_instruments(
            page_payload(instrument_row(), instrument_row(48))[:-missing],
            PageRequest(0, 2),
        )


@pytest.mark.parametrize("extra", [b"x", bytes(64)])
def test_undeclared_tail_is_not_silently_ignored(extra):
    with pytest.raises(DirectoryError, match="unexpected_trailing_bytes"):
        decode_markets(markets_payload(market_row()) + extra)
    with pytest.raises(DirectoryError, match="unexpected_trailing_bytes"):
        decode_instruments(page_payload(instrument_row()) + extra, PageRequest(0, 1))


def test_market_placeholder_count_is_physical_count_not_output_count():
    payload = markets_payload(market_row(), bytes(64), market_row(48))
    table = decode_markets(payload)
    assert table.declared_count == 3
    assert len(table.rows) == 2
    assert table.skipped_slots == (1,)
    assert table.rows[1].row_index == 2
    inputs = list(capture(market_rows=(market_row(), bytes(64), market_row(48))))
    snapshot = build_directory(*inputs)
    assert snapshot.market_declared_count == 3
    assert snapshot.mapping_for(48, 2, "00700").market_evidence.byte_offset == 130


def test_only_both_zero_market_category_is_a_placeholder():
    table = decode_markets(
        markets_payload(
            market_row(0, 2),
            market_row(31, 0),
            market_row(0, 0),
        )
    )
    assert [row.key for row in table.rows] == [(0, 2), (31, 0)]
    assert table.skipped_slots == (2,)


def test_category_is_part_of_join_and_identity_no_quote_list_inference():
    snapshot = build_directory(
        *capture(
            (instrument_row(31, 2), instrument_row(31, 3)),
            (market_row(31, 2),),
        )
    )
    assert snapshot.mapping_for(31, 2, "00700").market_record is not None
    mismatch = snapshot.mapping_for(31, 3, "00700")
    assert mismatch.market_record is mismatch.market_evidence is None
    assert mismatch.association_status == "UNKNOWN_MARKET_CATEGORY"
    assert snapshot.mapping_for(31, 2, "00700").instrument_id != mismatch.instrument_id
    # Both categories can occur under the same raw market without merging them.
    matched = build_directory(
        *capture(
            (instrument_row(31, 2), instrument_row(31, 3)),
            (market_row(31, 2), market_row(31, 3)),
        )
    )
    assert matched.unresolved_associations == ()


def test_static_exchange_enum_is_not_server_market_table_or_venue_mapping():
    snapshot = build_directory(
        *capture((instrument_row(74, 99, "ABC"),), market_rows=())
    )
    assert snapshot.markets == ()
    assert snapshot.mapping_for(74, 99, "ABC").market_record is None
    assert [ref.symbol for ref in STATIC_MARKET_REFERENCES if ref.market == 30] == [
        "ExchangeSHF",
        "ExchangeINE",
    ]
    assert all(
        ref.evidence_kind == "UPSTREAM_STATIC_ENUM_ONLY"
        for ref in STATIC_MARKET_REFERENCES
    )


def test_unknown_economics_and_extension_entries_cannot_upgrade_qualification():
    snapshot = build_directory(*capture())
    for mapping in snapshot.mappings:
        economics = mapping.economics
        assert economics.currency is economics.lot is economics.multiplier is None
        assert economics.expiry is economics.calendar_revision is economics.unit is None
        assert economics.status == economics.source_rights == "UNKNOWN"
    assert {entry.family for entry in EXTENSION_ENTRIES} == {"hk", "futures", "global"}
    assert all(
        entry.qualification == entry.source_rights == "UNKNOWN"
        for entry in EXTENSION_ENTRIES
    )


@pytest.mark.parametrize(
    "decoder,payload",
    [
        (decode_markets, markets_payload(market_row(), market_row())),
        (
            lambda raw: decode_instruments(raw, PageRequest(0, 2)),
            page_payload(instrument_row(), instrument_row()),
        ),
    ],
)
def test_duplicate_exact_records_rejected(decoder, payload):
    with pytest.raises(DirectoryError, match="duplicate"):
        decoder(payload)


def test_duplicate_identity_across_pages_including_changed_name_rejected():
    inputs = capture((instrument_row(), instrument_row(name="改名")), page_size=1)
    with pytest.raises(DirectoryError, match="duplicate_instrument_identity"):
        build_directory(*inputs)


@pytest.mark.parametrize(
    "change,classification",
    [
        ("count_drift", "count_drift"),
        ("missing", "incomplete_directory_pages"),
        ("out_of_order", "page_gap_overlap_or_order"),
        ("overlap", "page_gap_overlap_or_order"),
        ("gap", "page_gap_overlap_or_order"),
        ("short", "short_page_or_count_mismatch"),
        ("empty", "short_page_or_count_mismatch"),
        ("zero", "zero_count_page"),
        ("extra", "unexpected_page_after_total"),
    ],
)
def test_page_plan_and_count_negative_cases(change, classification):
    markets, before, pages, after = capture()
    if change == "count_drift":
        after = with_payload(after, count_payload(4))
    elif change == "missing":
        pages = pages[:-1]
    elif change == "out_of_order":
        pages = pages[::-1]
    elif change in ("overlap", "gap"):
        pages = (
            pages[0],
            replace(pages[1], request=PageRequest(1 if change == "overlap" else 3, 2)),
        )
    elif change in ("short", "empty"):
        rows = (instrument_row(),) if change == "short" else ()
        pages = (with_payload(pages[0], page_payload(*rows)), pages[1])
    elif change == "zero":
        pages = (replace(pages[0], request=PageRequest(0, 0)), pages[1])
    elif change == "extra":
        pages += (replace(pages[1], locator="extra", request=PageRequest(3, 2)),)
    with pytest.raises(DirectoryError, match=f"^{classification}$"):
        build_directory(markets, before, pages, after)


def test_page_declared_count_cannot_exceed_request():
    with pytest.raises(DirectoryError, match="declared_exceeds_requested_count"):
        decode_instruments(
            page_payload(instrument_row(), instrument_row(48)), PageRequest(0, 1)
        )


def test_final_page_cannot_overrun_global_count_even_if_request_allows_it():
    markets, before, pages, after = capture()
    extra = with_payload(
        pages[1],
        page_payload(
            instrument_row(30, 3, "au2612"),
            instrument_row(30, 3, "au2701"),
        ),
    )
    with pytest.raises(DirectoryError, match="short_page_or_count_mismatch"):
        build_directory(markets, before, (pages[0], extra), after)


def test_empty_directory_needs_valid_zero_count_bookends_and_no_pages():
    snapshot = build_directory(*capture((), market_rows=()))
    assert snapshot.total_count == 0
    assert snapshot.instruments == snapshot.mappings == ()
    assert decode_instruments(page_payload(), PageRequest(0, 0)).rows == ()
    inputs = list(capture((), market_rows=()))
    inputs[1] = with_payload(inputs[1], b"")
    with pytest.raises(DirectoryError, match="short_frame_nil_or_zero"):
        build_directory(*inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("source", "different-source"),
        ("binding", "different-binding"),
        ("mapping_revision", "r2"),
        ("capture_id", "capture-r2"),
        ("mode", EvidenceMode.RETAINED),
        ("producer_derived_sha256", "a" * 64),
    ],
)
def test_cross_capture_material_cannot_be_spliced(field, value):
    markets, before, pages, after = capture()
    context = replace(CONTEXT, **{field: value})
    pages = (replace(pages[0], context=context), pages[1])
    with pytest.raises(DirectoryError, match="mixed_capture_context"):
        build_directory(markets, before, pages, after)


def test_receipt_guards_digest_sdk_operation_clocks_and_offline_mode():
    markets, before, pages, after = capture()
    for updates, classification in (
        ({"payload": b"do-not-disclose"}, "material_digest_mismatch"),
        ({"sdk_commit": "untrusted"}, "unsupported_sdk_commit"),
        ({"expected_sha256": "A" * 64}, "invalid_sha256"),
        ({"operation": "ExMarkets"}, "operation_required"),
        ({"request": PageRequest(0, 1)}, "operation_request_mismatch"),
        (
            {"obtained_at": datetime(2026, 10, 22)},
            "aware_timestamp_or_unknown_required",
        ),
        ({"known_at": NOW - timedelta(seconds=1)}, "receipt_clock_order"),
        ({"payload": bytearray(markets.payload)}, "immutable_payload_bytes_required"),
    ):
        with pytest.raises(DirectoryError, match=f"^{classification}$") as error:
            replace(markets, **updates)
        assert "do-not-disclose" not in str(error.value)
    with pytest.raises(DirectoryError, match="offline_evidence_mode_required"):
        replace(CONTEXT, mode="live")
    with pytest.raises(DirectoryError, match="operation_request_mismatch"):
        replace(pages[0], request=None)
    with pytest.raises(DirectoryError, match="duplicate_material_locator"):
        build_directory(markets, before, pages, replace(after, locator=before.locator))
    with pytest.raises(DirectoryError, match="operation_sequence_mismatch"):
        build_directory(before, markets, pages, after)
    with pytest.raises(DirectoryError, match="immutable_pages_required"):
        build_directory(markets, before, list(pages), after)


def test_unknown_clocks_stay_unknown_and_capture_clock_order_is_enforced():
    markets, before, pages, after = capture()
    snapshot = build_directory(
        markets, before, (replace(pages[0], known_at=None), pages[1]), after
    )
    assert snapshot.known_at is snapshot.mappings[0].known_at is None
    assert snapshot.materials[2].known_at is None
    assert all(material.source_published_at is None for material in snapshot.materials)
    future = NOW + timedelta(seconds=1)
    with pytest.raises(DirectoryError, match="capture_clock_order"):
        build_directory(replace(markets, known_at=future), before, pages, after)


def test_hashes_are_stable_provenance_and_raw_sensitive_utc_normalized():
    inputs = capture()
    snapshot = build_directory(*inputs)
    assert snapshot.directory_hash == build_directory(*capture()).directory_hash
    assert len(snapshot.directory_hash) == len(snapshot.mapping_hash) == 64
    assert snapshot.market_table_hash == inputs[0].expected_sha256
    with pytest.raises(FrozenInstanceError):
        snapshot.total_count = 99
    # Reserved bytes are not economically interpreted but must affect identity.
    raw = inputs[2][0].payload[:-1] + b"x"
    changed = build_directory(
        inputs[0], inputs[1], (with_payload(inputs[2][0], raw), inputs[2][1]), inputs[3]
    )
    assert changed.instruments == snapshot.instruments
    assert changed.directory_hash != snapshot.directory_hash
    assert changed.mapping_hash != snapshot.mapping_hash
    changed_table = with_payload(inputs[0], inputs[0].payload[:-1] + b"x")
    changed = build_directory(changed_table, *inputs[1:])
    assert changed.market_table_hash != snapshot.market_table_hash
    assert changed.mapping_hash != snapshot.mapping_hash
    shifted = NOW.astimezone(timezone(timedelta(hours=8)))
    assert content_hash((NOW,)) == content_hash((shifted,))


@pytest.mark.parametrize(
    "market,category,code",
    [
        (31, 2, "700"),
        (31, 3, "00700"),
        (31, 2, 700),
        (True, 2, "00700"),
        (31, 2, "00700 "),
    ],
)
def test_exact_mapping_lookup_never_coerces(market, category, code):
    snapshot = build_directory(*capture())
    with pytest.raises(DirectoryError):
        snapshot.mapping_for(market, category, code)


def test_retained_input_uses_same_validation_and_remains_unqualified():
    markets, before, pages, after = capture()
    context = replace(CONTEXT, mode=EvidenceMode.RETAINED)
    snapshot = build_directory(
        replace(markets, context=context),
        replace(before, context=context),
        tuple(replace(page, context=context) for page in pages),
        replace(after, context=context),
    )
    assert snapshot.qualification == "UNKNOWN"
    assert snapshot.context.producer_derived_sha256 is None
    assert b"\0" not in repr(markets).encode()
    assert "payload=" not in repr(markets)


def test_invalid_gbk_and_empty_code_are_classified_without_raw_exception():
    malformed = bytearray(instrument_row())
    malformed[5:14] = b"\xff" + bytes(8)
    with pytest.raises(DirectoryError, match="^invalid_gbk_field$"):
        decode_instruments(page_payload(bytes(malformed)), PageRequest(0, 1))
    with pytest.raises(DirectoryError, match="empty_instrument_code"):
        decode_instruments(page_payload(instrument_row(code="")), PageRequest(0, 1))
    malformed = bytearray(market_row())
    malformed[1:33] = b"\xff" + bytes(31)
    with pytest.raises(DirectoryError, match="^invalid_gbk_field$"):
        decode_markets(markets_payload(bytes(malformed)))


def test_bounded_memory_admission_rejects_pages_rows_bytes_and_mutable_raw():
    markets, before, pages, after = capture()
    with pytest.raises(DirectoryError, match="payload_budget_exceeded"):
        decode_markets(bytes(MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(DirectoryError, match="payload_budget_exceeded"):
        with_payload(before, bytes(MAX_PAYLOAD_BYTES + 1))
    with pytest.raises(DirectoryError, match="page_budget_exceeded"):
        build_directory(markets, before, (pages[0],) * (MAX_DIRECTORY_PAGES + 1), after)
    with pytest.raises(DirectoryError, match="directory_row_budget_exceeded"):
        build_directory(
            markets,
            with_payload(before, count_payload(2**32 - 1)),
            (),
            with_payload(after, count_payload(2**32 - 1)),
        )
    large = bytes(MAX_PAYLOAD_BYTES)
    many = tuple(
        with_payload(replace(pages[0], locator=f"large:{i}"), large)
        for i in range(MAX_DIRECTORY_BYTES // MAX_PAYLOAD_BYTES + 1)
    )
    with pytest.raises(DirectoryError, match="directory_byte_budget_exceeded"):
        build_directory(markets, before, many, after)
    with pytest.raises(DirectoryError, match="immutable_payload_bytes_required"):
        decode_count(bytearray(23))


def test_count_extra_bytes_are_opaque_and_bound_by_material_digest():
    markets, before, pages, after = capture()
    extra = with_payload(before, before.payload + b"opaque")
    assert decode_count(extra.payload) == 3
    assert (
        build_directory(markets, extra, pages, after).directory_hash
        != build_directory(markets, before, pages, after).directory_hash
    )


def test_offline_path_has_zero_socket_effect(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network effect attempted")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    assert build_directory(*capture()).total_count == 3


def qualification_fixture(symbols=(), page_size=2):
    return Fixture(
        "r5-i6-a-ex-dir-synthetic",
        Capability.SECURITY_UNIVERSE,
        symbols,
        None,
        None,
        page_size,
        {"membership": "snapshot"},
    )


def test_existing_qualification_contract_reports_only_bounded_offline_evidence():
    inputs = capture()
    observation = observe_directory(
        qualification_fixture(("31:2:00700", "48:2:00700")),
        *inputs,
        observed_at=NOW,
        timer=lambda: 0.0,
    )
    assert observation["schema_version"] == 2
    assert observation["mode"] == "offline"
    assert observation["capability"] == Capability.SECURITY_UNIVERSE.value
    assert observation["fixture_id"] == "r5-i6-a-ex-dir-synthetic"
    assert observation["success"] is True
    assert observation["coverage"]["row_count"] == 3
    assert observation["coverage"]["complete"] is True
    assert observation["pagination"]["exhausted"] is None
    assert observation["exception"] is None
    assert (
        observation["semantics"]["directory_hash"]
        == build_directory(*inputs).directory_hash
    )
    assert observation["semantics"]["source_rights"] == "UNKNOWN"
    assert observation["endpoint"]["host"] is None
    assert "payload" not in observation
    assert inputs[0].material.capability == Capability.SECURITY_UNIVERSE


def test_observation_missing_exact_symbol_does_not_report_complete():
    observation = observe_directory(
        qualification_fixture(("31:2:700",)),
        *capture(),
        observed_at=NOW,
        timer=lambda: 0.0,
    )
    assert observation["success"] is observation["coverage"]["complete"] is False


def test_bad_payload_observation_has_classification_and_no_raw_message():
    markets, before, pages, after = capture()
    markets = with_payload(markets, markets.payload[:-1])
    observation = observe_directory(
        qualification_fixture(),
        markets,
        before,
        pages,
        after,
        observed_at=NOW,
        timer=lambda: 0.0,
    )
    assert observation["success"] is False
    assert observation["exception"] == {
        "category": "validation",
        "module": "market_data_foundation.candidates.tdx_exhq_directory.schema",
        "type": "DirectoryError",
    }
    assert "truncated_declared_rows" not in str(observation)
    assert "raw_sha256" not in observation


def test_qualification_history_and_fixture_page_budget_do_not_expand_scope():
    history = replace(qualification_fixture(), start=NOW.date(), end=NOW.date())
    for fixture in (history, qualification_fixture(page_size=1)):
        observation = observe_directory(
            fixture,
            *capture(),
            observed_at=NOW,
            timer=lambda: 0.0,
        )
        assert observation["success"] is False
        assert observation["exception"]["category"] == "validation"
    with pytest.raises(ValueError, match="binding capability does not match fixture"):
        observe_directory(
            replace(qualification_fixture(), capability=Capability.BARS_DAILY),
            *capture(),
            observed_at=NOW,
        )
