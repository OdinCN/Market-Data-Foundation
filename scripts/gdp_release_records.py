"""Explicit GDP release evidence migration/import and local cutoff reads."""

import argparse
import json
from pathlib import Path

from market_data_foundation.historical.gdp_release_records import GdpReleaseRecords
from market_data_foundation.historical.gdp_store import GdpStorageError
from market_data_foundation.historical.gdp_timeseries import (
    GdpTimeSeriesError,
    build_service,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "import", "read"))
    parser.add_argument("--confirm-dev-write", action="store_true")
    parser.add_argument("--material")
    parser.add_argument("--source-url")
    parser.add_argument(
        "--material-format",
        choices=("retained_document", "original_http_body"),
        default="retained_document",
    )
    parser.add_argument("--event-id")
    parser.add_argument("--reference-quarter")
    parser.add_argument("--source-version")
    parser.add_argument("--as-of")
    parser.add_argument(
        "--read-mode", choices=("event", "source_history"), default="event"
    )
    args = parser.parse_args(argv)
    if args.command != "read" and not args.confirm_dev_write:
        parser.error("--confirm-dev-write is required")
    if args.command == "import" and (not args.material or not args.source_url):
        parser.error("--material and --source-url are required")
    service = None
    try:
        raw = None
        if args.command == "import":
            with Path(args.material).open("rb") as stream:
                raw = stream.read(2_000_001)
            if not 0 < len(raw) <= 2_000_000:
                raise GdpTimeSeriesError("gdp_release_material_size_invalid")
        service = build_service()
        records = GdpReleaseRecords(service.store)
        if args.command == "prepare":
            records.prepare()
            result = {"prepared": True, "schema": service.store.schema}
        elif args.command == "import":
            result = records.import_material(
                raw, args.source_url, material_format=args.material_format
            )
        else:
            result = records.read(
                event_id=args.event_id,
                reference_quarter=args.reference_quarter,
                source_version=args.source_version,
                as_of=args.as_of,
                read_mode=args.read_mode,
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (GdpTimeSeriesError, GdpStorageError) as error:
        print(json.dumps({"error": error.classification}))
        return 1
    except OSError:
        print(json.dumps({"error": "gdp_release_material_file_unavailable"}))
        return 1
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
