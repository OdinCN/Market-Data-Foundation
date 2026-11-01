"""Explicit DEV numeric/evidence preparation, bounded collection and local reads."""

import argparse
import sys
from pathlib import Path

from market_data_foundation.historical.bls_timeseries_profile import (
    CONSUMPTION_PROFILE as PROFILE,
)
from market_data_foundation.historical.consumption_timeseries import build_service
from market_data_foundation.historical.cpi_timeseries_cli import (
    emit,
    error_document,
    execute,
    package_credential_loader,
)
from market_data_foundation.historical.cpi_timeseries_store import (
    _latest_view_sql,
    _rates_view_sql,
    _revision_ddl,
)
from market_data_foundation.historical.macro_release_records import (
    ReleaseRecords,
    migration_sql,
)


def grants_sql():
    schema = PROFILE.schema
    return f"""REVOKE ALL ON SCHEMA {schema} FROM PUBLIC;
REVOKE ALL ON SCHEMA {schema} FROM odin_dba;
GRANT USAGE ON SCHEMA {schema} TO odin_dba;
REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM PUBLIC;
REVOKE ALL ON ALL TABLES IN SCHEMA {schema} FROM odin_dba;
GRANT SELECT ON ALL TABLES IN SCHEMA {schema} TO odin_dba;
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM odin_dba;
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA {schema} GRANT SELECT ON TABLES TO odin_dba;"""


def schema_sql():
    schema = PROFILE.schema
    sql = "\n".join(
        [
            f"CREATE SCHEMA IF NOT EXISTS {schema};",
            f"CREATE TABLE IF NOT EXISTS {schema}.batches ("
            "batch_id text PRIMARY KEY, raw_sha256 text NOT NULL, "
            "observed_at timestamptz NOT NULL, request jsonb NOT NULL, "
            "provider_attempts integer NOT NULL CHECK(provider_attempts >= 0));",
            _revision_ddl(profile=PROFILE) + ";",
            _latest_view_sql(event_columns=True, profile=PROFILE) + ";",
            _rates_view_sql(profile=PROFILE) + ";",
            migration_sql(PROFILE),
            grants_sql(),
        ]
    )
    return "\n".join(line.rstrip() for line in sql.splitlines())


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command",
        choices=(
            "schema-sql",
            "prepare-schema",
            "init",
            "update",
            "query",
            "status",
            "import-material",
            "releases",
        ),
    )
    parser.add_argument("--runtime", type=Path)
    parser.add_argument("--secret", type=Path)
    parser.add_argument("--package-store-directory", type=Path)
    parser.add_argument("--environment-id")
    parser.add_argument("--bea-request-control-path", type=Path)
    parser.add_argument("--start")
    parser.add_argument("--end")
    parser.add_argument("--series")
    parser.add_argument("--columns")
    parser.add_argument("--as-of")
    parser.add_argument("--query-mode", choices=("current", "history"))
    parser.add_argument("--as-of-basis", choices=("observed", "available"))
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--file", type=Path)
    parser.add_argument("--source-url")
    parser.add_argument(
        "--material-format",
        choices=(
            "original_http_body",
            "official_selected_projection",
            "official_summary_projection",
            "retained_document",
        ),
    )
    parser.add_argument("--event-id")
    parser.add_argument("--reference-month")
    parser.add_argument("--source-version")
    parser.add_argument(
        "--read-mode", choices=("event", "source_history"), default="event"
    )
    return parser


def main(argv=None, *, service_factory=None):
    service = None
    try:
        args = build_parser().parse_args(argv)
        if sys.version_info[:3] != (3, 14, 8):
            raise ValueError("python_baseline_mismatch")
        if args.command == "schema-sql":
            print(schema_sql())
            return 0
        if not args.runtime or not args.secret:
            raise ValueError("explicit_dev_config_required")

        def credential():
            if not args.package_store_directory or not args.environment_id:
                raise ValueError("explicit_consumption_package_binding_required")
            return package_credential_loader(
                args.package_store_directory,
                args.environment_id,
                secret_path=args.secret,
                profile=PROFILE,
            )()

        service = (service_factory or build_service)(
            runtime_path=args.runtime,
            secret_path=args.secret,
            credential_loader=credential,
            request_control_path=args.bea_request_control_path,
        )
        if service.profile is not PROFILE:
            raise ValueError("consumption_profile_mismatch")
        if args.command == "prepare-schema":
            service.store.ensure_schema()
            ReleaseRecords(service.store).prepare()
            with service.store.connection.transaction():
                service.store.connection.execute(grants_sql())
            emit({"status": "prepared", "schema": PROFILE.schema})
            return 0
        if args.command == "import-material":
            if not args.file or not args.source_url or not args.material_format:
                raise ValueError("explicit_material_origin_required")
            emit(
                ReleaseRecords(service.store).import_material(
                    args.file.read_bytes(),
                    args.source_url,
                    material_format=args.material_format,
                )
            )
            return 0
        if args.command == "releases":
            emit(
                ReleaseRecords(service.store).read(
                    event_id=args.event_id,
                    reference_month=args.reference_month,
                    source_version=args.source_version,
                    read_mode=args.read_mode,
                    as_of=args.as_of,
                )
            )
            return 0
        params = {
            k: v
            for k, v in vars(args).items()
            if k
            in {
                "start",
                "end",
                "series",
                "columns",
                "as_of",
                "query_mode",
                "as_of_basis",
            }
            and v is not None
        }
        if args.command == "query":
            params["local_only"] = args.local_only
        emit(execute(service, args.command, params))
        return 0
    except Exception as error:
        emit(error_document(error), stream=sys.stderr)
        return 2
    finally:
        if service is not None:
            service.close()


if __name__ == "__main__":
    raise SystemExit(main())
