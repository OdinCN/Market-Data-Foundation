"""Rebuild the public DTO contract without a listener or storage capability."""

import json
from pathlib import Path

from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.macro_query_contract import contract_app
from market_data_foundation.web.management import create_management_app
from market_data_foundation.web.retail_timeseries import create_app as retail_app


def main():
    target = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
    target.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n"
    )

    management_target = target.parent / "src" / "management.openapi.json"
    management_target.write_text(
        json.dumps(create_management_app().openapi(), ensure_ascii=False, indent=2)
        + "\n"
    )

    macro_target = target.parent / "src" / "macro-timeseries.openapi.json"
    macro_target.write_text(
        json.dumps(contract_app().openapi(), ensure_ascii=False, indent=2) + "\n"
    )
    retail_target = target.parent / "src" / "retail.openapi.json"
    retail_target.write_text(
        json.dumps(retail_app().openapi(), ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
