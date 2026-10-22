"""Reconcile and remove only one receipted DEV probe; default is a local plan."""

import argparse
import json
import os
import re
import signal
from pathlib import Path

from mdf_i3_storage_probe import ENV, classification

from market_data_foundation.canonical.market_material import byte_digest
from market_data_foundation.canonical.model import require
from market_data_foundation.historical.bls_cpi_material_store import GarageCredentials
from market_data_foundation.storage.market_dataset_garage import (
    BUCKET,
    ENDPOINT,
    RUN,
    GarageMarketMaterials,
)
from market_data_foundation.storage.market_dataset_postgres import (
    DDL_PATH,
    SCHEMA,
    PostgresMarketDataset,
    connect_dev,
    preflight,
)


def cleanup(original, output, apply):
    root = Path("artifacts").resolve()
    require(
        original.resolve().is_relative_to(root)
        and output.resolve().is_relative_to(root),
        "receipt_must_be_under_artifacts",
    )
    value = json.loads(original.read_text())
    require(
        value.get("schema_revision") == "i3.host.probe.receipt.r1",
        "cleanup_receipt_revision",
    )
    run_id = value["run_id"]
    require(RUN.fullmatch(run_id) is not None, "invalid_probe_run")
    require(
        value["ddl_sha256"] == byte_digest(DDL_PATH.read_bytes()),
        "ddl_revision_mismatch",
    )
    require(
        value["target"]["pg_schema"] == SCHEMA
        and value["target"]["bucket"] == BUCKET
        and value["target"]["garage_endpoint"] == ENDPOINT,
        "cleanup_target_mismatch",
    )
    prefix = f"market-datasets/v1/probes/{run_id}/"
    keys = sorted(
        {
            effect["key"]
            for effect in value.get("garage_effects", ())
            if effect["method"] == "PUT"
        }
    )
    require(
        len(keys) <= 16 and all(k.startswith(prefix) for k in keys), "cleanup_key_scope"
    )
    for key in keys:
        tail = key[len(prefix) :]
        require(
            tail == "conditional-sentinel"
            or re.fullmatch(
                r"(?:raw|source-projection|status|metadata)/(?:[a-f0-9]{64}/){3}"
                r"sha256/[a-f0-9]{64}",
                tail,
            )
            is not None,
            "cleanup_key_scope",
        )
    # The input must be a trusted, unedited Host receipt. Record its exact bytes.
    result = {
        "status": "PLAN",
        "run_id": run_id,
        "keys": keys,
        "input_receipt_sha256": byte_digest(original.read_bytes()),
        "pg_schema": SCHEMA,
        "pg_effect": "NOT_ATTEMPTED",
        "garage_effects": [],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as handle:
        json.dump(result, handle, indent=2)

    def save():
        pending = output.with_suffix(".pending")
        pending.write_text(json.dumps(result, indent=2) + "\n")
        pending.replace(output)

    if not apply:
        return result
    store, connection = None, None

    def alarm(*_):
        raise TimeoutError

    signal.signal(signal.SIGALRM, alarm)
    signal.alarm(90)
    try:
        require(all(os.environ.get(name) for name in ENV), "credentials_not_injected")
        connection = connect_dev(os.environ[ENV[0]])
        intake = preflight(connection)
        if intake["namespace_exists"]:
            ledger = PostgresMarketDataset(connection, None, owner_run=run_id)
            result["pg_effect"] = "UNKNOWN"
            save()
            ledger.cleanup_probe_schema()
        require(not preflight(connection)["namespace_exists"], "pg_cleanup_unconfirmed")
        result["pg_effect"] = "ABSENT_VERIFIED"
        save()
        # Garage cleanup follows confirmed PG absence: never delete referenced bytes.
        store = GarageMarketMaterials(
            GarageCredentials(os.environ[ENV[1]], os.environ[ENV[2]]),
            run_id,
            max_requests=32,
            wall_seconds=60,
        )
        for key in keys:
            if key != prefix + "conditional-sentinel":
                store._key(f"s3://{BUCKET}/{key}")
        store._owned.update(keys)
        result["garage_effects"] = store.effects
        store.on_effect = save
        store.cleanup_owned()
        result["status"] = "OWNED_OBJECTS_ABSENT_VERIFIED"
    except Exception as exc:
        result.update(status="STOP", exception_classification=classification(exc))
    finally:
        signal.alarm(0)
        if connection is not None:
            connection.close()
        save()
    return result


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--original-receipt", type=Path, required=True)
    parser.add_argument("--cleanup-receipt", type=Path, required=True)
    parser.add_argument("--apply-owned-cleanup", action="store_true")
    options = parser.parse_args()
    try:
        result = cleanup(
            options.original_receipt,
            options.cleanup_receipt,
            options.apply_owned_cleanup,
        )
        print(
            json.dumps(
                {"status": result["status"], "receipt": str(options.cleanup_receipt)}
            )
        )
        return 2 if result["status"] == "STOP" else 0
    except Exception as exc:
        print(
            json.dumps(
                {"status": "STOP", "exception_classification": classification(exc)}
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
