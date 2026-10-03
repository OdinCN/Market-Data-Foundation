"""A final matrix must match the report and cannot imply a primary binding."""

import json
from pathlib import Path, PurePosixPath

from market_data_foundation.qualification import Capability

ROOT = Path(__file__).resolve().parents[1]


def test_report_and_matrix_agree_without_local_live_artifacts():
    matrix = json.loads((ROOT / "provider_lab/tdx_r1.matrix.json").read_text())
    report = (ROOT / "docs/qualification/tdx-r1.md").read_text()
    symbols = {
        "QUALIFIED_CANDIDATE": "Q",
        "QUALIFIED_WITH_LIMITATIONS": "L",
        "NOT_SUPPORTED": "N",
        "REJECTED": "R",
        "UNRESOLVED": "U",
    }
    versions = {"tdx-python": "0.1.6", "pytdxdata": "0.6.1", "mootdx-plus": "0.12.0"}
    assert matrix["versions"] == versions
    assert matrix["python"] == "3.14.8"
    assert matrix["binding_authorized"] is False
    assert matrix["recommended_implementation"] == "tdx-python"
    assert matrix["recommended_configuration"] == {
        "explicit_endpoint": True,
        "redial": False,
        "exclude_bj_aggregate": True,
    }
    rows = {}
    for line in report.splitlines():
        columns = [s.strip() for s in line.strip("|").split("|")]
        if columns[0] in {c.value for c in Capability}:
            rows[columns[0]] = [column[0] for column in columns[1:]]
    assert set(rows) == {c.value for c in Capability}
    for index, (name, version) in enumerate(versions.items()):
        cells = matrix["implementations"][name]
        assert set(cells) == set(rows)
        assert name + " " + version in report
        for capability, cell in cells.items():
            assert symbols[cell["disposition"]] == rows[capability][index]
            assert cell["basis"]
            for path in cell["evidence"]:
                evidence = PurePosixPath(path)
                assert evidence.parts[:2] == ("artifacts", "tdx-r1")
                assert ".." not in evidence.parts
        assert cells["financial.statements"]["disposition"] == "UNRESOLVED"
        assert cells["security.universe"]["disposition"] == "UNRESOLVED"
