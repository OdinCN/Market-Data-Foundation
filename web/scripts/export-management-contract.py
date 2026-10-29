"""Offline R3 subapp schema export/check; no listener or host binding."""

import json
import sys
from pathlib import Path

from market_data_foundation.web.management import create_management_app

target = Path(__file__).resolve().parents[1] / "src" / "management.openapi.json"
schema = create_management_app().openapi()
content = json.dumps(schema, ensure_ascii=False, indent=2) + "\n"
if "--write" in sys.argv:
    target.write_text(content)
elif json.loads(target.read_text()) != schema:
    raise SystemExit("R3 management OpenAPI is stale")
