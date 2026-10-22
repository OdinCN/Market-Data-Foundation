"""API × field × request-context evidence. No cross-market normalization."""

import json
from importlib.resources import files

from .model import CATALOG, Request


def field_facts(request: Request) -> tuple[dict, ...]:
    response = CATALOG[request.operation][0]
    structs = {response}
    if response == "MinuteResp":
        structs.add("PriceNumber")
    if response == "TradeResp":
        structs.add("Trade")
    if response == "KlineResp":
        structs.add("Kline")
    structs = {f"github.com/injoyai/tdx/protocol.{name}" for name in structs}
    document = json.loads(files(__package__).joinpath("field_facts.json").read_bytes())
    result = []
    for raw in document["records"]:
        if raw["qualified_response_struct"] not in structs:
            continue
        fact = dict(raw)
        fact.update(
            sdk_commit=document["sdk_commit"],
            producing_method=request.operation,
            market=request.market,
            category=request.category,
            code=request.code,
            period=request.period,
            kind=request.kind,
            transform_ref=CATALOG[request.operation][3],
        )
        name = fact["source_field_name"]
        if fact["go_type"] == "Price":
            fact.update(
                sdk_native_unit="SDK_INTEGER_MILLI_YUAN",
                decoded_unit="SDK_INTEGER_MILLI_YUAN",
                semantic_status="UPSTREAM_DOCUMENTED_NOT_MDF_QUALIFIED",
            )
        if response == "TradeResp" and name == "Volume":
            fact["sdk_native_unit"] = "UPSTREAM_DOCUMENTED_HAND"
        if (
            response == "TradeResp"
            and name == "Number"
            and request.operation == "GetHistoryTrade"
        ):
            fact["semantic_status"] = "ABSENT_ON_WIRE_SDK_ZERO_NOT_OBSERVED"
            fact["projection"] = {
                "kind": "UNKNOWN_NULL",
                "target": "row.native.Number",
                "rule": "ABSENT_ON_HISTORY_WIRE",
            }
        if response == "ExKline" and name == "Amount":
            fact["semantic_status"] = (
                "POSITION_BYTES_FLOAT_REINTERPRETATION_NOT_INDEPENDENT_AMOUNT"
            )
        if response == "ExTradeTick" and name == "Price":
            fact["semantic_status"] = "RAW_UINT32_NO_QUOTE_FLOAT_SCALE"
        if response == "ExMinuteTick" and name == "OpenInterest":
            fact["semantic_status"] = "ECONOMIC_MEANING_UNKNOWN_DECODER_LOCAL_NAME_AMT"
        result.append(fact)
    return tuple(result)
