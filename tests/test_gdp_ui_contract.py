"""Pin native quarterly HTTP responses consumed by TypeScript adapter tests."""

import json
from pathlib import Path

from tests.gdp_ui_wire import responses


def test_synthetic_http_fixtures_match_quarterly_host(tmp_path, monkeypatch):
    expected = json.loads(Path("tests/fixtures/gdp_ui_http.json").read_text())
    assert responses(tmp_path, monkeypatch) == expected
