"""Exact sampled directory association, without requiring or claiming full coverage."""

from dataclasses import dataclass, field, replace

from market_data_foundation.candidates.tdx_exhq_directory import (
    OfflineReceipt,
    decode_instruments,
    decode_markets,
)
from market_data_foundation.candidates.tdx_exhq_directory.schema import (
    Operation as DirectoryOperation,
)
from market_data_foundation.candidates.tdx_intraday_v1.model import (
    Material,
    Request,
    canonical,
    require,
    sha,
)


@dataclass(frozen=True)
class SampleAssociation:
    source: str
    binding: str
    mode: str
    producer_sha256: str | None
    market: int
    category: int
    code: str
    name: str
    market_name: str
    market_receipt_identity: str
    instrument_receipt_identity: str
    market_row_index: int
    instrument_row_index: int
    receipts: tuple = field(repr=False)
    qualification: str = "EXACT_SAMPLED_ASSOCIATION_ONLY"
    economics: str = "UNKNOWN"
    historical_membership: str = "UNKNOWN"

    @property
    def identity(self) -> str:
        from dataclasses import asdict

        values = asdict(self)
        del values["receipts"]
        return sha(canonical(values))


def associate_sample(
    markets: OfflineReceipt, instruments: OfflineReceipt, request: Request
) -> SampleAssociation:
    require(
        type(markets) is OfflineReceipt and type(instruments) is OfflineReceipt,
        "directory_receipts_required",
    )
    require(
        markets.operation == DirectoryOperation.MARKETS
        and instruments.operation == DirectoryOperation.INSTRUMENTS,
        "directory_operations_mismatch",
    )
    require(
        markets.context == instruments.context, "directory_capture_context_mismatch"
    )
    require(request.operation.value.startswith("Ex"), "ex_request_required")
    market_rows = decode_markets(markets.payload).rows
    instrument_rows = decode_instruments(instruments.payload, instruments.request).rows
    matched = [
        row
        for row in instrument_rows
        if row.key == (request.market, request.category, request.code)
    ]
    matching_markets = [
        row for row in market_rows if row.key == (request.market, request.category)
    ]
    require(
        len(matched) == len(matching_markets) == 1,
        "exact_market_category_code_not_observed",
    )
    row, market = matched[0], matching_markets[0]
    context = markets.context
    return SampleAssociation(
        context.source,
        context.binding,
        context.mode.value,
        context.producer_derived_sha256,
        row.market,
        row.category,
        row.code,
        row.name,
        market.name,
        markets.material.identity,
        instruments.material.identity,
        market.row_index,
        row.row_index,
        (markets, instruments),
    )


def attach_association(material: Material, association: SampleAssociation) -> Material:
    req = material.request
    require(
        association == associate_sample(*association.receipts, req),
        "association_receipt_mismatch",
    )
    require(
        (req.market, req.category, req.code)
        == (association.market, association.category, association.code),
        "association_instrument_mismatch",
    )
    require(
        (material.source, material.binding, material.mode, material.producer_sha256)
        == (
            association.source,
            association.binding,
            association.mode,
            association.producer_sha256,
        ),
        "association_source_binding_mismatch",
    )
    require(
        material.directory_identity in (None, association.identity),
        "association_digest_conflict",
    )
    return replace(
        material,
        directory_identity=association.identity,
        directory_receipts=association.receipts,
    )


def verified_association(material: Material) -> bool:
    if not material.directory_receipts:
        return False
    association = associate_sample(*material.directory_receipts, material.request)
    attach_association(material, association)
    require(
        material.directory_identity == association.identity,
        "association_digest_conflict",
    )
    return True
