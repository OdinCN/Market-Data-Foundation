"""Explicit asset-bound release operations; never a GET bootstrap."""

import argparse
import importlib
import json
from pathlib import Path

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE,
    CPI_PROFILE,
    EMPLOYMENT_PROFILE,
    JOLTS_PROFILE,
    PCE_PROFILE,
    PPI_PROFILE,
)
from market_data_foundation.historical.macro_release_records import (
    ReleaseRecords,
    migration_sql,
)

PROFILES = {
    p.key: p
    for p in (
        CPI_PROFILE,
        PPI_PROFILE,
        PCE_PROFILE,
        EMPLOYMENT_PROFILE,
        CONSUMPTION_PROFILE,
        JOLTS_PROFILE,
    )
}


def main(argv=None, *, service_factory=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset", required=True, choices=(*PROFILES, "retail", "m3"))
    parser.add_argument(
        "--service-factory",
        help="Trusted local module:callable; service already capability-bound",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("schema-sql")
    commands.add_parser("prepare")
    material = commands.add_parser("import-material")
    material.add_argument("--file", type=Path, required=True)
    material.add_argument("--source-url", required=True)
    material.add_argument(
        "--material-format",
        required=True,
        choices=(
            "retained_document",
            "original_http_body",
            "official_summary_projection",
            "official_selected_projection",
        ),
    )
    material.add_argument("--supersedes-evidence")
    consensus = commands.add_parser("import-consensus")
    consensus.add_argument("--file", type=Path, required=True)
    bind = commands.add_parser("bind-version")
    bind.add_argument(
        "--file",
        type=Path,
        required=True,
        help="event_id/evidence_id/value_identity/version/proof JSON",
    )
    bind.add_argument("--numeric-response", type=Path, required=True)
    acquire = commands.add_parser("acquire")
    acquire.add_argument(
        "--selection",
        type=Path,
        required=True,
        help="Explicit source_url/reference_month/source_date JSON",
    )
    acquire.add_argument("--state-path", type=Path, required=True)
    acquire.add_argument("--live-provider", action="store_true")
    read = commands.add_parser("read")
    for option in ("event-id", "as-of", "source-version", "reference-month"):
        read.add_argument("--" + option)
    read.add_argument(
        "--read-mode", choices=("event", "source_history"), default="event"
    )
    args = parser.parse_args(argv)
    if args.command == "schema-sql":
        from types import SimpleNamespace

        from market_data_foundation.historical.m3_capital_goods_store import (
            LOCK_ID,
            SCHEMA,
        )

        profile = PROFILES.get(args.asset) or (
            SimpleNamespace(schema=SCHEMA, lock_id=LOCK_ID)
            if args.asset == "m3"
            else SimpleNamespace(
                schema="mdf_retail_timeseries_dev_r1", lock_id=78328741642028105
            )
        )
        print(migration_sql(profile))
        return 0
    service = None
    try:
        if service_factory is None and args.service_factory:
            module, name = args.service_factory.split(":", 1)
            service_factory = getattr(importlib.import_module(module), name)
        if service_factory is None:
            raise ValueError
        service = service_factory()
        repo = ReleaseRecords(service.store)
        if repo.profile.key != args.asset:
            raise ValueError
        if args.command == "prepare":
            repo.prepare()
            result = dict(schema=repo.schema, release_schema_prepared=True)
        elif args.command == "import-material":
            result = repo.import_material(
                args.file.read_bytes(),
                args.source_url,
                material_format=args.material_format,
                supersedes_evidence=args.supersedes_evidence,
            )
        elif args.command == "import-consensus":
            result = repo.freeze_consensus(json.loads(args.file.read_text()))
        elif args.command == "bind-version":
            document = json.loads(args.file.read_text())
            document["proof"]["numeric_response"] = args.numeric_response.read_bytes()
            result = repo.bind_version(**document)
        elif args.command == "acquire":
            if not args.live_provider:
                from market_data_foundation.historical.cpi_timeseries import (
                    CpiTimeSeriesError,
                )

                raise CpiTimeSeriesError("release_live_provider_opt_in_required")
            from market_data_foundation.historical.macro_release_acquisition import (
                MaterialAcquirer,
            )

            result = MaterialAcquirer(args.state_path).capture(
                service.store, json.loads(args.selection.read_text())
            )
        else:
            result = repo.read(
                event_id=args.event_id,
                as_of=args.as_of,
                read_mode=args.read_mode,
                source_version=args.source_version,
                reference_month=args.reference_month,
            )
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(
            json.dumps(
                dict(
                    status="ERROR",
                    classification=getattr(
                        error,
                        "classification",
                        "release_local_binding_or_input_invalid",
                    ),
                )
            )
        )
        return 2
    finally:
        if service is not None:
            if hasattr(service, "close"):
                service.close()
            elif getattr(service.store, "connection", None) is not None:
                service.store.connection.close()


if __name__ == "__main__":
    raise SystemExit(main())
