"""Pinned v1.0.0 field facts; prefix predicates are hints, never admission."""

from dataclasses import dataclass
from enum import StrEnum

from market_data_foundation.domain_contracts.market_universe import AssetClass

UPSTREAM_COMMIT = "2a9000681f5a9896ea98e4de8bc4904f1dce78ba"
SDK_VERSION = "v1.0.0"
DECODE_REVISION = "tdx-security-directory-json-v1"
MAX_MATERIAL_BYTES = 16 * 1024 * 1024
MAX_ROWS = 65535
PAGE_SIZE = 1000


class Exchange(StrEnum):
    SH = "SH"
    SZ = "SZ"
    BJ = "BJ"

    @property
    def market(self) -> int:
        return {Exchange.SZ: 0, Exchange.SH: 1, Exchange.BJ: 2}[self]

    @property
    def request_namespace(self) -> str:
        return f"tdx.standard.{self.value}"


class Operation(StrEnum):
    GET_COUNT = "GetCount"
    GET_CODE = "GetCode"
    GET_CODE_ALL = "GetCodeAll"
    GET_STOCK_CODE_ALL = "GetStockCodeAll"
    GET_ETF_CODE_ALL = "GetETFCodeAll"
    GET_INDEX_CODE_ALL = "GetIndexCodeAll"
    GET_STOCKS = "GetStocks"
    GET_STOCK_CODES = "GetStockCodes"
    GET_ETFS = "GetETFs"
    GET_ETF_CODES = "GetETFCodes"
    GET_INDEXES = "GetIndexes"
    GET_INDEX_CODES = "GetIndexCodes"
    PARSE_BJ_REPORT = "ParseTdxBjMore"


class Route(StrEnum):
    STANDARD = "standard_getcode"
    BJ_REPORT = "zhb.zip/tdxbjmore.cfg"
    CACHE = "explicit_retained_codesbase"
    CLIENT_FILTER = "client_filtered_directories"


MODEL_OPERATIONS = frozenset(
    {Operation.GET_STOCKS, Operation.GET_ETFS, Operation.GET_INDEXES}
)
STRING_OPERATIONS = frozenset(
    {
        Operation.GET_STOCK_CODES,
        Operation.GET_ETF_CODES,
        Operation.GET_INDEX_CODES,
        Operation.GET_STOCK_CODE_ALL,
        Operation.GET_ETF_CODE_ALL,
        Operation.GET_INDEX_CODE_ALL,
    }
)
CLIENT_OPERATIONS = frozenset(
    {
        Operation.GET_STOCK_CODE_ALL,
        Operation.GET_ETF_CODE_ALL,
        Operation.GET_INDEX_CODE_ALL,
    }
)


@dataclass(frozen=True)
class FieldFact:
    response_struct: str
    source_field_name: str
    go_type: str
    source_ref: str
    sdk_native_unit: str = "UNKNOWN"
    semantic_status: str = "UPSTREAM_DECLARED"
    clock_semantics: str = "UNKNOWN"
    decoded_unit: str = "UNKNOWN"
    transform_ref: str = "native Go JSON projection; no economic conversion"
    market_scope: tuple[str, ...] = ("SH", "SZ", "BJ")
    nullable_unknown_rule: str = (
        "required native field; BJ report defaults and string-only metadata UNKNOWN"
    )
    upstream_commit: str = UPSTREAM_COMMIT


# Exact AST fields, including CodeModel's local database fields, not Provider facts.
FIELD_FACTS = (
    FieldFact("CountResp", "Count", "uint16", "protocol/model_count.go:6"),
    FieldFact("CodeResp", "Count", "uint16", "protocol/model_code.go:9"),
    FieldFact("CodeResp", "List", "[]*Code", "protocol/model_code.go:10"),
    FieldFact("Code", "Name", "string", "protocol/model_code.go:14"),
    FieldFact("Code", "Code", "string", "protocol/model_code.go:15"),
    FieldFact("Code", "Multiple", "uint16", "protocol/model_code.go:16"),
    FieldFact("Code", "Decimal", "int8", "protocol/model_code.go:17"),
    FieldFact(
        "Code",
        "LastPrice",
        "float64",
        "protocol/model_code.go:18",
        "yuan: index only; stock invalid; others UNKNOWN",
        "UPSTREAM_DOCUMENTED_CONDITIONAL",
    ),
    *(
        FieldFact("CodeModel", name, go_type, f"codes.go:{line}")
        for line, name, go_type in (
            (284, "ID", "int64"),
            (285, "Name", "string"),
            (286, "Code", "string"),
            (287, "Exchange", "string"),
            (288, "Multiple", "uint16"),
            (289, "Decimal", "int8"),
            (290, "LastPrice", "float64"),
            (291, "EditDate", "int64"),
            (292, "InDate", "int64"),
        )
    ),
)


def category_hints(exchange: Exchange, code: str) -> tuple[AssetClass, ...]:
    """Exact protocol/unit.go predicates, with block/index ambiguity exposed.

    These are SDK classifications of code ranges, not security-master evidence.
    In particular SZ 16xxxx is called ETF upstream; economics remain unverified.
    """
    if (
        not isinstance(exchange, Exchange)
        or type(code) is not str
        or len(code) != 6
        or not code.isascii()
        or not code.isdigit()
    ):
        return ()
    stock = (
        (exchange == Exchange.SH and code.startswith("6"))
        or (exchange == Exchange.SZ and code.startswith(("0", "30")))
        or (exchange == Exchange.BJ and code.startswith("92"))
    )
    etf = (
        exchange == Exchange.SH and code[:2] in {"50", "51", "52", "53", "56", "58"}
    ) or (exchange == Exchange.SZ and code[:2] in {"15", "16"})
    block = exchange == Exchange.SH and code.startswith(("880", "881"))
    index = (
        (exchange == Exchange.SH and (code.startswith("000") or code == "999999"))
        or (exchange == Exchange.SZ and code.startswith("399"))
        or (exchange == Exchange.BJ and code.startswith("899"))
        or block
    )
    return tuple(
        category
        for category, matches in (
            (AssetClass.A_SHARE, stock),
            (AssetClass.ETF, etf),
            (AssetClass.INDEX, index),
            (AssetClass.SECTOR_INDEX, block),
        )
        if matches
    )


def operation_category(operation: Operation) -> AssetClass | None:
    if operation in {
        Operation.GET_STOCKS,
        Operation.GET_STOCK_CODES,
        Operation.GET_STOCK_CODE_ALL,
    }:
        return AssetClass.A_SHARE
    if operation in {
        Operation.GET_ETFS,
        Operation.GET_ETF_CODES,
        Operation.GET_ETF_CODE_ALL,
    }:
        return AssetClass.ETF
    if operation in {
        Operation.GET_INDEXES,
        Operation.GET_INDEX_CODES,
        Operation.GET_INDEX_CODE_ALL,
    }:
        return AssetClass.INDEX
    return None
