"""Synthetic A-STD fault cases; no SDK initialization or external service."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest

from market_data_foundation.candidates.tdx_security_directory import (
    FIELD_FACTS,
    UPSTREAM_COMMIT,
    DirectoryRequest,
    Exchange,
    MappingAssertion,
    MaterialLink,
    MemberSelection,
    OfflineMaterial,
    Operation,
    Route,
    assemble_pages,
    category_hints,
    decode,
    observe,
    snapshot_universe,
    source_mappings,
)
from market_data_foundation.candidates.tdx_security_directory.schema import MAX_ROWS
from market_data_foundation.canonical.model import ContractError, Key, Kind
from market_data_foundation.domain_contracts.market_universe import (
    AssetClass,
    DateWindow,
    EvidenceMode,
    InstrumentIdentity,
    MembershipMode,
    Qualification,
)
from market_data_foundation.qualification.contracts import Capability

NOW = datetime(2026, 10, 22, 8, tzinfo=UTC)
SOURCE = Key(Kind.SOURCE, "synthetic.tdx.directory", "v1")
BINDING = Key(Kind.BINDING, "synthetic.binding", "v1")


def request(op=Operation.GET_CODE_ALL, ex=Exchange.SH, **kwargs):
    return DirectoryRequest(
        op,
        kwargs.pop("exchanges", (ex,)),
        kwargs.pop("route", Route.STANDARD),
        kwargs.pop("snapshot_id", "synthetic-snapshot"),
        **kwargs,
    )


def native(code="600001", **kwargs):
    return (
        dict(
            Name="synthetic security",
            Code=code,
            Multiple=100,
            Decimal=2,
            LastPrice=123.25,
        )
        | kwargs
    )


def material(value, req=None, **kwargs):
    payload = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, ensure_ascii=False).encode()
    )
    return OfflineMaterial(
        request=req or request(),
        payload=payload,
        expected_sha256=sha256(payload).hexdigest(),
        source=SOURCE,
        binding=BINDING,
        fixture_id="synthetic-a-std",
        mode=EvidenceMode.SYNTHETIC,
        obtained_at=NOW,
        received_at=NOW,
        known_at=NOW,
        implementation_sha256="a" * 64,
        upstream_commit=UPSTREAM_COMMIT,
        **kwargs,
    )


def response(rows=None, req=None):
    rows = [native()] if rows is None else rows
    return decode(material({"Count": len(rows), "List": rows}, req))


def count(value, ex=Exchange.SH):
    return decode(material({"Count": value}, request(Operation.GET_COUNT, ex)))


def page(rows, start=0, ex=Exchange.SH):
    return response(rows, request(Operation.GET_CODE, ex, start=start))


def issues(result):
    return {item.classification for item in result.issues}


def selection(row, category=AssetClass.A_SHARE, identity="stable-1"):
    return MemberSelection(
        row.row_locator,
        InstrumentIdentity("synthetic.stable", identity),
        category,
        ("synthetic identity evidence",),
        ("synthetic independent category evidence",),
    )


def links(*responses):
    materials = {item.material.material_id for item in responses}
    return tuple(
        MaterialLink(key, Key(Kind.MATERIAL, key)) for key in sorted(materials)
    )


def universe(result, selections=None, **kwargs):
    responses = result.responses if hasattr(result, "responses") else (result,)
    return snapshot_universe(
        (result,),
        selections
        if selections is not None
        else ((selection(result.records[0]),) if result.records else ()),
        links(*responses),
        universe_id="synthetic bounded universe",
        mapping_revision="mapping-1",
        known_at=NOW,
        **kwargs,
    )


def test_native_field_types_are_exact_pinned_ast_facts():
    facts = {
        (item.response_struct, item.source_field_name): item for item in FIELD_FACTS
    }
    assert {
        name: item.go_type for (struct, name), item in facts.items() if struct == "Code"
    } == {
        "Name": "string",
        "Code": "string",
        "Multiple": "uint16",
        "Decimal": "int8",
        "LastPrice": "float64",
    }
    assert facts["CodeModel", "LastPrice"].go_type == "float64"
    assert facts["CodeModel", "ID"].go_type == "int64"
    assert facts["CodeResp", "List"].go_type == "[]*Code"
    assert facts["CountResp", "Count"].source_ref == "protocol/model_count.go:6"
    assert UPSTREAM_COMMIT == "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"


@pytest.mark.parametrize(
    "ex,market", [(Exchange.SZ, 0), (Exchange.SH, 1), (Exchange.BJ, 2)]
)
def test_exchange_is_explicit_not_inferred_from_code(ex, market):
    route = Route.BJ_REPORT if ex == Exchange.BJ else Route.STANDARD
    decoded = response([native("000001")], request(ex=ex, route=route))
    row = decoded.records[0]
    assert row.exchange == ex and row.market == market
    assert row.code == "000001" and row.symbol == ex.value.lower() + "000001"
    assert row.raw("Code") == "000001"


@pytest.mark.parametrize(
    "ex,code,hints",
    [
        (Exchange.SH, "600001", (AssetClass.A_SHARE,)),
        (Exchange.SZ, "000001", (AssetClass.A_SHARE,)),
        (Exchange.SZ, "301001", (AssetClass.A_SHARE,)),
        (Exchange.BJ, "920001", (AssetClass.A_SHARE,)),
        (Exchange.BJ, "830001", ()),
        (Exchange.SH, "530001", (AssetClass.ETF,)),
        (Exchange.SH, "560001", (AssetClass.ETF,)),
        (Exchange.SH, "580001", (AssetClass.ETF,)),
        (Exchange.SH, "550001", ()),
        (Exchange.SZ, "160001", (AssetClass.ETF,)),
        (Exchange.SH, "880001", (AssetClass.INDEX, AssetClass.SECTOR_INDEX)),
        (Exchange.SH, "881001", (AssetClass.INDEX, AssetClass.SECTOR_INDEX)),
        (Exchange.SZ, "399001", (AssetClass.INDEX,)),
        (Exchange.BJ, "899050", (AssetClass.INDEX,)),
        (Exchange.SH, "999999", (AssetClass.INDEX,)),
        (Exchange.SH, "110001", ()),
    ],
)
def test_upstream_range_hints_include_new_etf_prefixes_and_ambiguity(ex, code, hints):
    assert category_hints(ex, code) == hints


@pytest.mark.parametrize(
    "code,status",
    [
        ("600001", "UPSTREAM_STOCK_INVALID"),
        ("000001", "UPSTREAM_INDEX_YUAN_PREVIOUS_CLOSE_UNQUALIFIED"),
        ("530001", "UNKNOWN"),
        ("110001", "UNKNOWN"),
    ],
)
def test_last_price_retains_float64_and_never_becomes_general_stock_preclose(
    code, status
):
    row = response(
        [native(code, LastPrice=123.456789, Decimal=-128, Multiple=65535)]
    ).records[0]
    assert type(row.raw("LastPrice")) is float
    assert row.raw("LastPrice") == 123.456789
    assert row.last_price_status == status
    assert row.raw("Decimal") == -128 and row.raw("Multiple") == 65535
    assert not hasattr(row, "preclose")


@pytest.mark.parametrize(
    "field,value,classification",
    [
        ("Code", 1, "native_string_required"),
        ("Code", "00001", "six_ascii_digit"),
        ("Code", "０００００１", "six_ascii_digit"),
        ("Code", "sh000001", "six_ascii_digit"),
        ("Multiple", -1, "native_integer"),
        ("Multiple", 65536, "native_integer"),
        ("Multiple", True, "native_integer"),
        ("Decimal", 128, "native_integer"),
        ("Decimal", -129, "native_integer"),
        ("Decimal", 2.0, "native_integer"),
        ("LastPrice", True, "finite_float64"),
        ("LastPrice", "123", "finite_float64"),
        ("LastPrice", float("nan"), "finite_float64"),
        ("LastPrice", float("inf"), "finite_float64"),
        ("LastPrice", 10**400, "finite_float64"),
        ("Name", None, "native_string"),
    ],
)
def test_native_type_faults_fail_without_coercing(field, value, classification):
    with pytest.raises(ContractError, match=classification):
        response([native(**{field: value})])


@pytest.mark.parametrize(
    "payload,classification",
    [
        (b'{"Count":0,"Count":0,"List":[]}', "duplicate_json_field"),
        (b'{"Count":true,"List":[]}', "native_integer"),
        (b'{"Count":-1,"List":[]}', "native_integer"),
        (b'{"Count":65536,"List":[]}', "native_integer"),
        (b'{"Count":0,"List":[null]}', "response_schema"),
        (b'{"count":0,"list":[]}', "response_schema"),
        (b'{"Count":0,"List":[],"unexpected":1}', "response_schema"),
        (b'{"Count":0,"List":{}}', "response_list"),
        (b'{"private-value":', "malformed_json"),
        (b"\xff", "malformed_json"),
    ],
)
def test_malformed_and_unknown_schema_are_classified(payload, classification):
    candidate = material(payload)
    with pytest.raises(ContractError, match=classification) as caught:
        decode(candidate)
    assert "private-value" not in str(caught.value)
    observation = observe(candidate)
    assert observation.classification == "DECODE_REJECTED"
    assert observation.exception_classification == "ContractError"
    assert "private-value" not in repr(observation)


def test_integral_float64_nil_slice_and_missing_field_rules():
    row = response([native(LastPrice=1)]).records[0]
    assert type(row.raw("LastPrice")) is float
    empty = decode(material({"Count": 0, "List": None}))
    assert empty.records == () and not empty.issues
    broken = native()
    del broken["Decimal"]
    with pytest.raises(ContractError, match="response_schema"):
        response([broken])


@pytest.mark.parametrize(
    "op,code,category",
    [
        (Operation.GET_STOCKS, "600001", AssetClass.A_SHARE),
        (Operation.GET_ETFS, "580001", AssetClass.ETF),
        (Operation.GET_INDEXES, "000001", AssetClass.INDEX),
    ],
)
def test_codesbase_native_model_fields_and_local_timestamps(op, code, category):
    model = dict(
        id=2**63 - 1,
        name="synthetic cache",
        code=code,
        exchange="sh",
        multiple=100,
        decimal=3,
        lastPrice=1.001,
        editDate=1234,
        inDate=5678,
    )
    candidate = material([model], request(op, route=Route.CACHE, limit=1))
    decoded = decode(candidate)
    assert decoded.records[0].hints == (category,)
    assert decoded.records[0].raw("editDate") == 1234
    assert decoded.material.known_at == NOW  # cache timestamp is not known_at
    assert decoded.termination == "UPSTREAM_AGGREGATE_UNVERIFIED"
    assert not decoded.issues
    for field, value, classification in [
        ("id", 2**63, "native_integer"),
        ("editDate", False, "native_integer"),
        ("exchange", "SH", "invalid_raw_exchange"),
        ("exchange", "hk", "invalid_raw_exchange"),
        ("exchange", "sz", "row_market_outside_request"),
    ]:
        with pytest.raises(ContractError, match=classification):
            decode(material([model | {field: value}], candidate.request))


@pytest.mark.parametrize(
    "op,codes,category,observed_price_status",
    [
        (
            Operation.GET_STOCKS,
            ("600001", "000001", "920001"),
            AssetClass.A_SHARE,
            "UPSTREAM_STOCK_INVALID",
        ),
        (
            Operation.GET_INDEXES,
            ("000001", "399001", "899050"),
            AssetClass.INDEX,
            "UPSTREAM_INDEX_YUAN_PREVIOUS_CLOSE_UNQUALIFIED",
        ),
    ],
)
@pytest.mark.parametrize("economics", [(0, 0, 0), (100, 2, 123.25)])
def test_cached_bj_economics_need_field_evidence_even_with_native_values(
    op, codes, category, observed_price_status, economics
):
    multiple, decimal, price = economics
    models = [
        dict(
            id=index,
            name="synthetic cache",
            code=code,
            exchange=exchange.value.lower(),
            multiple=multiple,
            decimal=decimal,
            lastPrice=price,
            editDate=1234,
            inDate=5678,
        )
        for index, (exchange, code) in enumerate(zip(Exchange, codes))
    ]
    candidate = replace(
        material(models, request(op, exchanges=tuple(Exchange), route=Route.CACHE)),
        mode=EvidenceMode.RETAINED,
    )
    result = decode(candidate)
    assert not result.issues
    for row in result.records:
        assert row.raw("multiple") == multiple
        assert row.raw("decimal") == decimal
        assert row.raw("lastPrice") == float(price)
        assert type(row.raw("lastPrice")) is float
        assert row.hints == (category,)
    for row in result.records[:2]:
        assert row.metadata_status == "OBSERVED_NATIVE_FIELDS"
        assert row.last_price_status == observed_price_status
    bj = result.records[2]
    assert bj.metadata_status == "UNKNOWN_CACHE_FIELD_PROVENANCE"
    assert bj.last_price_status == "UNKNOWN"
    assert result.material.payload == candidate.payload
    # Identity selection remains possible without promoting economic metadata.
    content = universe(result, (selection(bj, category),))
    assert content.members[0].market == "BJ"


@pytest.mark.parametrize(
    "op,symbol",
    [
        (Operation.GET_STOCK_CODES, "sh600001"),
        (Operation.GET_ETF_CODES, "sh580001"),
        (Operation.GET_INDEX_CODES, "sh000001"),
        (Operation.GET_STOCK_CODE_ALL, "bj920001"),
        (Operation.GET_ETF_CODE_ALL, "sz159001"),
        (Operation.GET_INDEX_CODE_ALL, "sz399001"),
    ],
)
def test_string_operations_keep_unknown_metadata(op, symbol):
    client = op in {
        Operation.GET_STOCK_CODE_ALL,
        Operation.GET_ETF_CODE_ALL,
        Operation.GET_INDEX_CODE_ALL,
    }
    exchanges = (
        (
            (Exchange.SH, Exchange.SZ)
            if op == Operation.GET_ETF_CODE_ALL
            else tuple(Exchange)
        )
        if client
        else (Exchange.SH,)
    )
    req = request(
        op, exchanges=exchanges, route=Route.CLIENT_FILTER if client else Route.CACHE
    )
    symbols = ["bj899050", symbol] if op == Operation.GET_INDEX_CODE_ALL else [symbol]
    result = decode(material(symbols, req))
    assert result.records[-1].symbol == symbol
    assert result.records[-1].raw("Name") is None
    assert result.records[-1].raw("Multiple") is None
    assert result.records[-1].last_price_status == "UNKNOWN"
    assert not result.issues


def test_operation_category_conflict_and_rangeindexes_bug_do_not_leak_into_d1():
    result = decode(
        material(["sh580001"], request(Operation.GET_INDEX_CODES, route=Route.CACHE))
    )
    assert issues(result) == {"operation_category_conflict"}
    assert observe(result.material).classification == "QUALITY_REJECTED"
    with pytest.raises(ContractError, match="directory_validation_issues"):
        universe(result)
    with pytest.raises(ContractError, match="unsupported_operation"):
        request("RangeIndexes", route=Route.CACHE)


def test_getindexcodeall_hardcoded_bj_seed_is_not_membership_evidence():
    req = request(
        Operation.GET_INDEX_CODE_ALL,
        exchanges=tuple(Exchange),
        route=Route.CLIENT_FILTER,
    )
    result = decode(material(["bj899050", "sh000001"], req))
    assert result.records[0].origin == "UPSTREAM_HARDCODED_SEED"
    with pytest.raises(ContractError, match="hardcoded_seed_not_membership_evidence"):
        universe(result, (selection(result.records[0], AssetClass.INDEX),))
    content = universe(result, (selection(result.records[1], AssetClass.INDEX),))
    assert content.members[0].market == "SH"


def test_getindexcodeall_extra_bj_row_is_retained_but_blocks_d1_conversion():
    req = request(
        Operation.GET_INDEX_CODE_ALL,
        exchanges=tuple(Exchange),
        route=Route.CLIENT_FILTER,
    )
    candidate = material(["bj899050", "bj899100"], req)
    result = decode(candidate)
    assert [row.symbol for row in result.records] == ["bj899050", "bj899100"]
    assert result.records[0].origin == "UPSTREAM_HARDCODED_SEED"
    extra = result.records[1]
    assert extra.raw("FullCode") == "bj899100"
    assert extra.hints == (AssetClass.INDEX,)
    assert issues(result) == {"index_producer_scope_conflict"}
    assert result.issues[0].locators == (extra.row_locator,)
    assert result.material.payload == candidate.payload
    observation = observe(candidate)
    assert observation.classification == "QUALITY_REJECTED"
    assert observation.issue_classifications == ("index_producer_scope_conflict",)
    with pytest.raises(ContractError, match="directory_validation_issues"):
        universe(result, (selection(extra, AssetClass.INDEX),))


def test_bj_report_preserves_raw_material_and_missing_metadata_without_defaulting():
    payload = "# synthetic report\r\n44|920001|2|合成证券|1|\r\n".encode("gbk")
    req = request(Operation.PARSE_BJ_REPORT, Exchange.BJ, route=Route.BJ_REPORT)
    result = decode(material(payload, req))
    row = result.records[0]
    assert row.raw("Name") == "合成证券"
    assert row.raw("Multiple") is row.raw("Decimal") is row.raw("LastPrice") is None
    assert row.metadata_status == row.last_price_status == "UNKNOWN"
    assert result.material.payload == payload and result.termination == "REPORT_EOF"
    # The native Go projection contains initialized zeros, not observed metadata.
    projected = response(
        [native("920001", Multiple=0, Decimal=0, LastPrice=0)],
        request(ex=Exchange.BJ, route=Route.BJ_REPORT),
    )
    assert projected.records[0].raw("LastPrice") == 0.0
    assert projected.records[0].metadata_status == "SDK_DEFAULT_NOT_OBSERVED"
    assert projected.records[0].last_price_status == "UNKNOWN"


@pytest.mark.parametrize(
    "text,classification",
    [
        ("broken", "malformed_bj_report_row"),
        ("44||2|synthetic|", "malformed_bj_report_row"),
        ("44|920001|0|synthetic|", "bj_report_market_conflict"),
        ("01|920001|2|synthetic|", "bj_report_market_conflict"),
        ("44|920001|2|synthetic|\n44|920001|2|synthetic|", "duplicate_code"),
    ],
)
def test_bj_skips_and_foreign_market_rows_are_quality_issues(text, classification):
    req = request(Operation.PARSE_BJ_REPORT, Exchange.BJ, route=Route.BJ_REPORT)
    assert classification in issues(decode(material(text.encode("gbk"), req)))


@pytest.mark.parametrize("row_count", [MAX_ROWS, MAX_ROWS + 1])
@pytest.mark.parametrize("valid_position", [None, "first", "last"])
def test_bj_row_budget_includes_malformed_rows(row_count, valid_position):
    req = request(Operation.PARSE_BJ_REPORT, Exchange.BJ, route=Route.BJ_REPORT)
    valid = b"44|920001|2|synthetic|\r\n"
    malformed_count = row_count - (valid_position is not None)
    payload = b"x\r\n" * malformed_count
    if valid_position == "first":
        payload = valid + payload
    elif valid_position == "last":
        payload += valid
    # Blank and comment lines consume no row budget, but retain line positions.
    payload = b"\r\n# comment\r\n" + payload + b"\n# final comment"
    candidate = material(payload, req)
    if row_count > MAX_ROWS:
        with pytest.raises(ContractError, match="^row_budget$"):
            decode(candidate)
        return
    result = decode(candidate)
    assert result.termination == "REPORT_EOF"
    assert len(result.records) == (valid_position is not None)
    assert len(result.issues) == malformed_count
    assert issues(result) == {"malformed_bj_report_row"}
    if valid_position is not None:
        expected_line = 3 if valid_position == "first" else row_count + 2
        assert result.records[0].row_locator == (
            f"{candidate.material_id}:line:{expected_line}"
        )


def test_bj_gbk_decode_fault_is_safe_and_standard_bj_requests_are_forbidden():
    req = request(Operation.PARSE_BJ_REPORT, Exchange.BJ, route=Route.BJ_REPORT)
    with pytest.raises(ContractError, match="malformed_bj_gbk"):
        decode(material(b"\x81", req))
    for op in (Operation.GET_COUNT, Operation.GET_CODE):
        with pytest.raises(ContractError, match="standard_sh_sz_only"):
            request(
                op, Exchange.BJ, **({"start": 0} if op == Operation.GET_CODE else {})
            )
    with pytest.raises(ContractError, match="directory_route_mismatch"):
        request(ex=Exchange.BJ)


def test_duplicates_are_retained_and_conflicts_do_not_overwrite_first_row():
    result = response([native(), native(), native(Name="synthetic rename")])
    assert len(result.records) == 3
    assert issues(result) == {"duplicate_code", "conflicting_duplicate_code"}
    assert len({row.row_locator for row in result.records}) == 3
    assert result.records[-1].raw("Name") == "synthetic rename"
    with pytest.raises(ContractError, match="directory_validation_issues"):
        universe(result)


def test_page_short_and_exact_multiple_need_observed_terminal_page():
    rows = [native(f"{600000 + index}") for index in range(1000)]
    full = page(rows)
    assert full.termination == "FULL_PAGE"
    missing = assemble_pages(count(1000), (full,), count(1000))
    assert issues(missing) == {"termination_not_observed"}
    complete = assemble_pages(count(1000), (full, page([], start=1000)), count(1000))
    assert complete.termination == "PAGE_TERMINATED_COUNT_MATCH"
    assert not complete.issues and len(complete.records) == 1000
    short = assemble_pages(count(2), (page(rows[:2]),), count(2))
    assert not short.issues
    empty = assemble_pages(count(0), (page([]),), count(0))
    assert not empty.issues and not empty.records
    with pytest.raises(ContractError, match="empty_universe|explicit_members_required"):
        snapshot_universe(
            (empty,),
            (),
            links(*empty.responses),
            universe_id="empty",
            mapping_revision="mapping-1",
            known_at=NOW,
        )


def test_page_faults_drift_gaps_duplicates_and_short_termination_are_explicit():
    one, second = page([native()]), page([native("600002")], start=1000)
    cases = [
        (assemble_pages(count(1), (one,), count(2)), "directory_count_drift"),
        (assemble_pages(count(2), (one,), count(2)), "total_count_mismatch"),
        (assemble_pages(count(1), (), count(1)), "termination_not_observed"),
        (assemble_pages(count(2), (one, second), count(2)), "page_after_termination"),
        (
            assemble_pages(count(2), (second, one), count(2)),
            "page_offset_gap_or_reorder",
        ),
        (
            assemble_pages(count(2), (one, page([native()], start=1000)), count(2)),
            "duplicate_code",
        ),
    ]
    for assembly, classification in cases:
        assert classification in issues(assembly)
        assert assembly.termination == "UNKNOWN"
        with pytest.raises(ContractError):
            universe(assembly)
    oversized = page([native(f"{600000 + index}") for index in range(1001)])
    assert "oversized_page" in issues(oversized)
    bad_count = decode(
        material({"Count": 2, "List": [native()]}, request(Operation.GET_CODE, start=0))
    )
    assert "declared_count_mismatch" in issues(bad_count)


def test_page_binding_drift_and_offset_overflow_are_rejected():
    one = page([native()])
    changed = decode(
        replace(
            one.material, request=replace(one.material.request, snapshot_id="other")
        )
    )
    with pytest.raises(ContractError, match="page_binding_drift"):
        assemble_pages(count(1), (changed,), count(1))
    for start in (-1, 65536, True, 1.0):
        with pytest.raises(ContractError, match="native_integer"):
            request(Operation.GET_CODE, start=start)
    assert request(Operation.GET_CODE, start=65535).start == 65535
    with pytest.raises(ContractError, match="bounded_pages"):
        assemble_pages(count(1), (one,) * 67, count(1))


def test_material_identity_integrity_clock_and_capability_gates():
    candidate = material({"Count": 0, "List": []})
    for changes, classification in [
        ({"payload": b"different"}, "digest_mismatch"),
        ({"mode": EvidenceMode.LIVE}, "offline_evidence_only"),
        ({"upstream_commit": "other"}, "upstream_identity_mismatch"),
        ({"implementation_sha256": "bad"}, "implementation_digest_required"),
        ({"known_at": NOW - timedelta(seconds=1)}, "material_clock_order"),
        ({"received_at": NOW - timedelta(seconds=1)}, "material_clock_order"),
        ({"obtained_at": NOW.replace(tzinfo=None)}, "naive_time"),
        ({"capability": Capability.QUOTE_SNAPSHOT}, "directory_capability_required"),
    ]:
        with pytest.raises(ContractError, match=classification):
            replace(candidate, **changes)
    with pytest.raises(ContractError, match="material_byte_budget"):
        replace(candidate, payload=b" " * (16 * 1024 * 1024 + 1))
    assert candidate.material_id == material({"Count": 0, "List": []}).material_id
    assert (
        candidate.material_id
        != replace(
            candidate, request=replace(candidate.request, snapshot_id="other")
        ).material_id
    )


def test_bounded_snapshot_is_d1_content_with_no_historical_or_calendar_rights():
    result = response()
    content = universe(result)
    assert content.mode == MembershipMode.SNAPSHOT
    assert (
        content.effective
        == content.history_window
        == DateWindow(date(2026, 10, 22), date(2026, 10, 23))
    )
    assert content.calendars == ()
    assert all(gates == Qualification() for _, gates in content.qualifications)
    assert content.members[0].lifecycle.state(date(2026, 10, 22)) == "UNKNOWN"
    assert content.members[0].instrument == InstrumentIdentity(
        "synthetic.stable", "stable-1"
    )
    mapping = content.mapping_for(
        content.members[0].instrument, date(2026, 10, 22), SOURCE, BINDING, "mapping-1"
    )
    assert (
        mapping.request_namespace == "tdx.standard.SH"
        and mapping.request_code == "600001"
    )
    assert content.revision == universe(result).revision
    with pytest.raises(ContractError, match="explicit_source_mapping_required"):
        content.mapping_for(
            content.members[0].instrument,
            date(2026, 10, 21),
            SOURCE,
            BINDING,
            "mapping-1",
        )
    with pytest.raises(ContractError, match="snapshot_cannot_claim_history"):
        replace(
            content, history_window=DateWindow(date(2000, 1, 1), date(2026, 10, 23))
        )


def test_unknown_or_ambiguous_category_requires_explicit_evidence_and_identity():
    for code in ("110001", "880001", "160001"):
        result = response([native(code)])
        selected = selection(result.records[0], AssetClass.SECTOR_INDEX)
        with pytest.raises(ContractError, match="explicit_member_evidence"):
            replace(selected, category_evidence=())
        with pytest.raises(ContractError, match="explicit_member_evidence"):
            replace(selected, identity_evidence=())
        assert (
            universe(result, (selected,)).members[0].category == AssetClass.SECTOR_INDEX
        )


def test_same_code_cross_market_is_distinct_and_can_build_one_snapshot():
    sh = response([native("000001")])
    sz = response([native("000001")], request(ex=Exchange.SZ))
    content = snapshot_universe(
        (sh, sz),
        (
            selection(sh.records[0], AssetClass.INDEX, "index-sh"),
            selection(sz.records[0], AssetClass.A_SHARE, "stock-sz"),
        ),
        links(sh, sz),
        universe_id="cross-market",
        mapping_revision="m1",
        known_at=NOW,
    )
    assert len(content.members) == 2
    assert {item.request_namespace for item in content.mappings} == {
        "tdx.standard.SH",
        "tdx.standard.SZ",
    }
    assert len({item.instrument for item in content.members}) == 2


def test_snapshot_requires_exact_d3_material_keys_and_rechecks_input_content():
    result = response()
    selected = (selection(result.records[0]),)
    args = dict(universe_id="test", mapping_revision="m1", known_at=NOW)
    with pytest.raises(ContractError, match="exact_canonical_material_links"):
        snapshot_universe((result,), selected, (), **args)
    with pytest.raises(ContractError, match="decoded_content_mismatch"):
        snapshot_universe(
            (replace(result, issues=(), records=()),), selected, links(result), **args
        )
    with pytest.raises(ContractError, match="known_at_backdated"):
        snapshot_universe(
            (result,),
            selected,
            links(result),
            **(args | {"known_at": NOW - timedelta(seconds=1)}),
        )
    other_day = replace(result.material, obtained_at=NOW - timedelta(days=1))
    with pytest.raises(ContractError, match="snapshot_binding_drift"):
        snapshot_universe(
            (result, decode(other_day)),
            selected,
            links(result, decode(other_day)),
            **args,
        )


def test_code_reuse_requires_nonoverlapping_identity_mapping_windows():
    old = MappingAssertion(
        InstrumentIdentity("synthetic.stable", "old"),
        Exchange.SH,
        "600001",
        DateWindow(date(2020, 1, 1), date(2021, 1, 1)),
        ("synthetic old identity",),
    )
    new = replace(
        old,
        instrument=InstrumentIdentity("synthetic.stable", "new"),
        validity=DateWindow(date(2021, 1, 1), date(2022, 1, 1)),
    )
    args = dict(source=SOURCE, binding=BINDING, revision="m1")
    assert len(source_mappings((new, old), **args)) == 2
    with pytest.raises(ContractError, match="source_code_collision"):
        source_mappings((old, replace(new, validity=old.validity)), **args)
    cross = replace(new, exchange=Exchange.SZ, validity=old.validity)
    assert len(source_mappings((old, cross), **args)) == 2
    with pytest.raises(ContractError, match="ambiguous_source_mapping"):
        source_mappings((old, replace(old, code="600002")), **args)
    # Separate mapping history does not grant historical membership to a snapshot.
    assert universe(response()).mode == MembershipMode.SNAPSHOT


def test_retained_inputs_remain_unqualified_and_metadata_cannot_claim_universe():
    result = response()
    retained = decode(replace(result.material, mode=EvidenceMode.RETAINED))
    assert all(
        gates == Qualification() for _, gates in universe(retained).qualifications
    )
    metadata = decode(replace(result.material, capability=Capability.SECURITY_METADATA))
    with pytest.raises(ContractError, match="universe_capability_required"):
        universe(metadata)


def test_decode_and_d1_adapter_have_no_network_database_subprocess_or_write_effects(
    monkeypatch,
):
    import socket
    import subprocess
    from pathlib import Path

    import psycopg

    def forbidden(*args, **kwargs):
        raise AssertionError("external effect")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(psycopg, "connect", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    result = assemble_pages(count(1), (page([native()]),), count(1))
    content = universe(result)
    assert content.members[0].original_code == "600001"
    assert (
        observe(result.responses[1].material).classification
        == "OFFLINE_DECODED_UNQUALIFIED"
    )


def test_reordered_json_keys_are_equal_native_rows_and_raw_bytes_stay_exact():
    original = native(Name="合成\u0000名称  ")
    reordered = dict(reversed(tuple(original.items())))
    result = response([original, reordered])
    assert issues(result) == {"duplicate_code"}
    assert result.records[0].raw("Name") == "合成\u0000名称  "
    assert (
        result.material.expected_sha256 == sha256(result.material.payload).hexdigest()
    )


def test_index_seed_reordering_and_missing_seed_remain_untrusted():
    req = request(
        Operation.GET_INDEX_CODE_ALL,
        exchanges=tuple(Exchange),
        route=Route.CLIENT_FILTER,
    )
    for symbols in ([], ["sh000001"], ["sh000001", "bj899050"]):
        result = decode(material(symbols, req))
        assert "missing_or_reordered_index_seed" in issues(result)
    assert result.records[-1].origin == "UPSTREAM_HARDCODED_SEED"


def test_observation_excludes_raw_rows_and_native_payload():
    result = response([native(Name="private-value")])
    observation = observe(result.material)
    assert observation.record_count == 1
    assert observation.issue_classifications == ()
    assert "private-value" not in repr(observation)
    assert not hasattr(observation, "response")
    assert not hasattr(observation, "payload")


def test_bj_native_projection_nonzero_metadata_is_not_the_pinned_report_decoder():
    result = response(
        [native("920001")], request(ex=Exchange.BJ, route=Route.BJ_REPORT)
    )
    assert issues(result) == {"bj_report_unexpected_metadata"}
    assert result.records[0].raw("Multiple") == 100
    with pytest.raises(ContractError, match="directory_validation_issues"):
        universe(result)


def test_assemblies_and_snapshot_content_cannot_be_forged_by_dataclass_replace():
    one = page([native()])
    with pytest.raises(ContractError, match="decoded_content_mismatch"):
        assemble_pages(count(1), (replace(one, records=()),), count(1))
    assembly = assemble_pages(count(1), (one,), count(1))
    with pytest.raises(ContractError, match="assembly_content_mismatch"):
        universe(replace(assembly, termination="invented"))


def test_count_bounds_row_budget_and_cache_limit_are_independent():
    assert count(0).declared_count == 0
    assert count(65535).declared_count == 65535
    req = request(Operation.GET_ETF_CODES, route=Route.CACHE, limit=1)
    result = decode(material(["sh580001", "sh580002"], req))
    assert issues(result) == {"cache_limit_exceeded"}
    assert len(result.records) == 2
    with pytest.raises(ContractError, match="row_budget"):
        decode(material(["sh580001"] * 65536, req))


def test_obtained_local_day_and_late_known_at_are_kept_separate():
    result = response()
    obtained = datetime(2026, 10, 21, 17, tzinfo=UTC)  # already Oct 22 in Shanghai
    result = decode(replace(result.material, obtained_at=obtained))
    assert universe(result).effective.start == date(2026, 10, 22)
    later_known = NOW + timedelta(days=1)
    result = decode(replace(result.material, known_at=later_known))
    with pytest.raises(ContractError, match="known_at_backdated"):
        universe(result)
    content = snapshot_universe(
        (result,),
        (selection(result.records[0]),),
        links(result),
        universe_id="late",
        mapping_revision="m1",
        known_at=later_known,
    )
    assert content.known_at == later_known
    assert content.effective.start == date(2026, 10, 22)


@pytest.mark.parametrize(
    "changes,classification",
    [
        ({"exchanges": ()}, "explicit_unique_exchanges"),
        ({"exchanges": (Exchange.SH, Exchange.SH)}, "explicit_unique_exchanges"),
        ({"exchanges": ("SH",)}, "explicit_unique_exchanges"),
        ({"start": 0}, "unexpected_start"),
        ({"limit": 1}, "unexpected_limit"),
        ({"route": Route.CACHE}, "directory_route_mismatch"),
    ],
)
def test_request_never_guesses_market_route_or_operation_parameters(
    changes, classification
):
    with pytest.raises(ContractError, match=classification):
        replace(request(), **changes)
