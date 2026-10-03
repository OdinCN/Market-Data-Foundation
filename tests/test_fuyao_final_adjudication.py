"""Final structural adjudication counterexamples; synthetic, no live artifacts."""

import pytest

from provider_lab.fuyao_r1_adjudication import (
    STATEMENT_VALUES,
    statements_check,
    taxonomy_check,
)


def statement(kind):
    return {
        "timestamp": 1767110400000,
        "item": [
            {
                "thscode": "600000.SH",
                "ticker": "600000",
                "period": "annual",
                "fiscal_year": 2025,
                "fiscal_period": "FY",
                "currency": "CNY",
                "period_end_ms": 1767110400000,
                "report_date_ms": 1774886400000,
                **dict.fromkeys(STATEMENT_VALUES[kind], -1),
            }
        ],
    }


@pytest.mark.parametrize("kind", STATEMENT_VALUES)
@pytest.mark.parametrize(
    "damage",
    [
        None,
        "identity",
        "empty",
        "null",
        "nan",
        "date",
        "duplicate",
        "period",
        "currency",
    ],
)
def test_each_statement_requires_its_own_structure(kind, damage):
    data = statement(kind)
    row = data["item"][0]
    if damage == "identity":
        row["thscode"] = "000001.SZ"
    elif damage == "empty":
        data["item"] = []
    elif damage == "null":
        row.update(dict.fromkeys(STATEMENT_VALUES[kind]))
    elif damage == "nan":
        row[STATEMENT_VALUES[kind][0]] = float("nan")
    elif damage == "date":
        row["report_date_ms"] = "1774886400000"
    elif damage == "duplicate":
        data["item"].append(dict(row))
    elif damage == "period":
        row["period_end_ms"] = 1735574400000
    elif damage == "currency":
        row["currency"] = None
    result = statements_check(kind, {"thscode": "600000.SH", "period": "annual"}, data)
    assert result["structural_sample_valid"] is (damage is None)
    assert result["publication_date_established"] is False
    assert result["update_date_established"] is False
    assert result["units"] == {}
    assert result["complete"] is None


def test_statements_preserve_null_and_dates_without_inventing_units():
    data = statement("balance-sheet")
    data["item"][0]["cash"] = None
    result = statements_check(
        "balance-sheet", {"thscode": "600000.SH", "period": "annual"}, data
    )
    assert result["structural_sample_valid"] is True
    assert result["null_counts"]["cash"] == 1
    assert result["rows"][0]["cash"] is None
    assert result["dates_asia_shanghai"][0]["report_date_ms"] == "2026-03-31"
    assert "publication_date" not in result["rows"][0]


@pytest.mark.parametrize("tag", ["industry", "cn_concept"])
@pytest.mark.parametrize(
    "damage", [None, "missing_group", "ticker", "duplicate", "empty", "tag"]
)
def test_ths_catalog_selection_and_member_identity(tag, damage):
    catalog = {"item": [{"thscode": "881101.TI", "name": "sample"}]}
    members = {"item": [{"thscode": "600000.SH", "ticker": "600000", "name": "sample"}]}
    group = "881101.TI"
    if damage == "missing_group":
        group = "885311.TI"
    elif damage == "ticker":
        members["item"][0]["ticker"] = "000001"
    elif damage == "duplicate":
        members["item"].append(dict(members["item"][0]))
    elif damage == "empty":
        members["item"] = []
    elif damage == "tag":
        tag = "concept"
    result = taxonomy_check(tag, catalog, group, members)
    assert result["structural_sample_valid"] is (damage is None)
    assert result["requested_tag"] == tag
    assert result["response_tag_present"] is False
    assert result["response_group_identity_present"] is False
    assert result["historical_membership_established"] is False
    assert result["tdx_mapping_established"] is False
    assert result["hierarchy_established"] is False
    assert result["complete"] is None
