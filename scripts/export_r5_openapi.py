"""Rebuild the public DTO contract without a listener or storage capability."""

import json
from pathlib import Path

from market_data_foundation.web.cpi_api import create_app


def main():
    target = Path(__file__).resolve().parents[1] / "web" / "openapi.json"
    target.write_text(
        json.dumps(create_app().openapi(), ensure_ascii=False, indent=2) + "\n"
    )


if __name__ == "__main__":
    main()
