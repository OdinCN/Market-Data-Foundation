"""Public HTTP help and actual query semantics, using only synthetic providers."""

import pytest

from tests.test_cpi_timeseries_web import no_network  # noqa: F401
from tests.test_employment_web import setup
from tests.test_pce_timeseries_web import fixture as pce_fixture  # noqa: F401
from tests.test_ppi_timeseries_web import client, dual  # noqa: F401


def check_contract(app, host, *, local_only_default):
    with client(app) as http:
        help_doc = http.get(host.prefix + "/api-help").json()
        assert help_doc["query_defaults"]["local_only"] is local_only_default
        assert help_doc["query_defaults"]["as_of_basis"] == "observed"
        assert help_doc["query_defaults"]["history_as_of_basis"] == "available"
        assert help_doc["availability_is_query_parameter"] is False
        assert "availability_default" not in help_doc
        query = host.prefix + "/query?start=2026-09&end=2026-09"
        default = http.get(query).json()
        assert default["provider_requests"] == (0 if local_only_default else 1)
        assert default["as_of_basis"] == default["vintage_mode"] == "observed"
        assert default["persisted"] is False
        # Explicit current has no historical cutoff: the service normalizes to
        # observed; adapters must report the basis that was actually executed.
        current = http.get(
            query + "&local_only=true&query_mode=current&as_of_basis=available"
        ).json()
        assert current["as_of_basis"] == current["vintage_mode"] == "observed"
        assert current["provider_requests"] == 0
        history = http.get(query + "&query_mode=history&local_only=false").json()
        assert history["as_of_basis"] == history["vintage_mode"] == "available"
        assert history["provider_requests"] == 0


@pytest.mark.parametrize("key", ["cpi", "ppi"])
def test_bls_help_and_executed_basis_agree(dual, key):  # noqa: F811
    app, hosts, _, _ = dual
    check_contract(app, hosts[key], local_only_default=key == "cpi")


def test_bea_help_and_executed_basis_agree(pce_fixture):  # noqa: F811
    app, host, _, _, _ = pce_fixture
    check_contract(app, host, local_only_default=False)


def test_employment_help_and_executed_basis_agree(tmp_path, monkeypatch):
    app, runtime, _, _, _ = setup(tmp_path, monkeypatch)
    check_contract(app, runtime.employment_timeseries, local_only_default=False)
