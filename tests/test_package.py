"""Deterministic smoke check of the installed package."""

from importlib import import_module
from importlib.metadata import distribution


def test_installed_package_import() -> None:
    package = import_module("market_data_foundation")
    assert package.__name__ == "market_data_foundation"
    assert distribution("market-data-foundation").metadata["Name"] == (
        "market-data-foundation"
    )
