"""Qualified BEA NIPA monthly consumption/income identities.

UNIT_MULT controls native scale; descriptive table labels do not override it.
The retained 2026 qualification establishes mappings, not native history starts.
"""

DESCRIPTORS = (
    (
        "DPCERC",
        "nominal_pce",
        "名义个人消费支出",
        "T20600",
        "29",
        "Current Dollars",
        "6",
        "million_USD",
        None,
    ),
    (
        "DPCERX",
        "real_pce",
        "实际个人消费支出",
        "T20806",
        "1",
        "Chained Dollars",
        "6",
        "million_chained_2017_USD",
        "2017",
    ),
    (
        "DGDSRX",
        "real_goods",
        "实际商品消费",
        "T20806",
        "2",
        "Chained Dollars",
        "6",
        "million_chained_2017_USD",
        "2017",
    ),
    (
        "DSERRX",
        "real_services",
        "实际服务消费",
        "T20806",
        "13",
        "Chained Dollars",
        "6",
        "million_chained_2017_USD",
        "2017",
    ),
    (
        "A067RC",
        "nominal_dpi",
        "名义可支配个人收入",
        "T20600",
        "27",
        "Current Dollars",
        "6",
        "million_USD",
        None,
    ),
    (
        "A067RX",
        "real_dpi",
        "实际可支配个人收入",
        "T20600",
        "37",
        "Chained Dollars",
        "6",
        "million_chained_2017_USD",
        "2017",
    ),
    (
        "A072RC",
        "saving_rate",
        "个人储蓄率",
        "T20600",
        "35",
        "Ratio",
        "0",
        "percent",
        None,
    ),
)
METADATA = tuple(
    (
        sid,
        tuple(
            dict(
                indicator=indicator,
                name=name,
                provider="BEA",
                dataset="NIPA",
                table_name=table,
                line_number=line,
                metric_name=metric,
                unit_mult=scale,
                value_unit=unit,
                seasonal_adjustment="SA",
                annual_rate=sid != "A072RC",
                annual_rate_semantics="SAAR_level_not_annualized_growth"
                if sid != "A072RC"
                else "not_annualized",
                chained_base=base,
                additive=False if base else None,
                start_month=None,
                coverage_basis="retained_2026_mapping_qualification_not_native_start",
                provenance=(
                    "BEA NIPA API Results; UNIT_MULT authoritative; PIO methodology"
                ),
            ).items()
        ),
    )
    for sid, indicator, name, table, line, metric, scale, unit, base in DESCRIPTORS
)
