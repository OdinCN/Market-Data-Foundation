"""Controlled M3 collection/readback and local-first cutoff queries."""

from __future__ import annotations

from datetime import datetime

from market_data_foundation.historical.cpi_timeseries import month_at, month_number
from market_data_foundation.historical.m3_capital_goods import M3Request, check
from market_data_foundation.historical.m3_capital_goods_store import (
    SERIES_IDS,
    M3Batch,
    _rows,
)


class M3TimeSeriesService:
    def __init__(self, store, source=None):
        self.store, self.source = store, source

    def fetch_batch(self, dataset, start, end):
        check(self.source is not None, "source_not_bound")
        return M3Batch(
            tuple(
                self.source.fetch(M3Request(dataset, category, start, end, kind))
                for category, kind in (("MDM", "NO"), ("NXA", "NO"), ("NXA", "VS"))
            )
        )

    def collect_range(self, start, end, *, dataset="m3", confirm_write=False):
        check(confirm_write is True, "explicit_write_opt_in_required")
        batch = self.fetch_batch(dataset, start, end)
        return self.import_batch(batch, confirm_write=True) | {
            "provider_requests": 3,
            "provider_attempts": 3,
        }

    def import_batch(self, batch, *, confirm_write=False):
        check(confirm_write is True, "explicit_write_opt_in_required")
        check(type(batch) is M3Batch, "batch_invalid")
        saved = self.store.save_batch(batch)
        actual = self.store.read_range(
            batch.dataset, batch.request.start_month, batch.request.end_month
        )
        indexed = {(r["series_id"], r["month"]): r for r in actual}
        from market_data_foundation.historical.m3_capital_goods_store import _same

        check(
            all(
                (r["series_id"], r["month"]) in indexed
                and _same(indexed[(r["series_id"], r["month"])], r)
                for r in _rows(batch)
            ),
            "collection_readback_mismatch",
        )
        return saved | {
            "batch_id": batch.identity,
            "readback_points": len(actual),
            "persisted": True,
            "provider_requests": 0,
            "provider_attempts": 0,
        }

    def query_range(
        self,
        start,
        end,
        *,
        dataset="m3",
        as_of=None,
        basis="available",
        allow_upstream=False,
    ):
        req = M3Request(dataset, "MDM", start, start)
        first, last = month_number(start), month_number(end)
        check(0 <= last - first < 2400, "query_range_invalid")
        months = tuple(month_at(n) for n in range(first, last + 1))
        check(type(allow_upstream) is bool, "upstream_flag_invalid")
        check(basis in {"observed", "available"}, "cutoff_basis_invalid")
        if as_of is not None:
            check(not allow_upstream, "history_must_be_local")
            try:
                as_of = (
                    datetime.fromisoformat(as_of.replace("Z", "+00:00"))
                    if isinstance(as_of, str)
                    else as_of
                )
                check(
                    isinstance(as_of, datetime) and as_of.utcoffset() is not None,
                    "cutoff_invalid",
                )
            except TypeError, ValueError:
                check(False, "cutoff_invalid")
        rows = self.store.read_range(dataset, start, end, as_of, basis)
        indexed = {(r["series_id"], r["month"]): dict(r, origin="local") for r in rows}
        expected = {(s, m) for s in SERIES_IDS for m in months}
        requests = 0
        if allow_upstream and any(
            k not in indexed or indexed[k]["value"] is None for k in expected
        ):
            check(len(months) <= 2, "upstream_window_requires_batching")
            batch = self.fetch_batch(dataset, start, end)
            requests = 3
            for row in _rows(batch):
                key = row["series_id"], row["month"]
                if key not in indexed or indexed[key]["value"] is None:
                    indexed[key] = dict(
                        row,
                        origin="upstream",
                        revision=None,
                        available_at=row["observed_at"],
                    )
        points = []
        for series, month in sorted(expected, key=lambda k: (k[1], k[0]), reverse=True):
            row = indexed.get((series, month))
            points.append(
                row
                if row is not None
                else {
                    "dataset": dataset,
                    "series_id": series,
                    "month": month,
                    "value": None,
                    "status": "missing",
                    "reason": "local_record_missing",
                    "origin": "local",
                    "observed_at": None,
                    "available_at": None,
                    "revision": None,
                    "source": {
                        "unit": "million_current_usd",
                        "seasonal_adjustment": "SA",
                        "frequency": "monthly",
                        "annualized": False,
                        "report_stage": req.report_stage,
                        "official_release_event": None,
                        "source_version_date": None,
                    },
                }
            )
        return {
            "points": points,
            "dataset": dataset,
            "report_stage": req.report_stage,
            "query_mode": "history" if as_of is not None else "current",
            "as_of": None if as_of is None else as_of.isoformat(),
            "as_of_basis": basis,
            "provider_requests": requests,
            "provider_attempts": requests,
            "persisted": False,
            "coverage": {
                "expected": len(expected),
                "available": sum(p["value"] is not None for p in points),
            },
        }


def build_service(
    *,
    runtime_path,
    secret_path,
    package_directory,
    request_control=None,
    allow_live=False,
):
    """Exact existing DEV PG/Garage binding; construction does no DDL or HTTP."""
    import tomllib
    from pathlib import Path

    from market_data_foundation.historical.bls_cpi_material_store import (
        BUCKET,
        GARAGE_ENDPOINT,
        GarageCredentials,
    )
    from market_data_foundation.historical.m3_capital_goods_source import (
        M3CandidateSource,
    )
    from market_data_foundation.historical.m3_capital_goods_store import (
        GarageM3RawStore,
        PostgresM3Store,
    )
    from market_data_foundation.storage.market_dataset_postgres import (
        connect_dev,
        preflight,
    )
    from market_data_foundation.web.package_catalog import PackageCatalog
    from market_data_foundation.web.package_store import LocalPackageStore

    runtime = tomllib.loads(Path(runtime_path).read_text())
    secret = tomllib.loads(Path(secret_path).read_text())
    s3 = runtime["s3"]
    check(
        runtime["storage"]["backend"] == "s3"
        and s3["endpoint_url"] == GARAGE_ENDPOINT
        and s3["bucket"] == BUCKET
        and s3["region"] == "garage"
        and s3["key_prefix"] == ""
        and s3["addressing_style"] == "path",
        "storage_binding_invalid",
    )
    check(not allow_live or request_control is not None, "request_control_required")
    packages = LocalPackageStore(Path(package_directory), PackageCatalog())

    def credential():
        record = packages.read("mdf-dev", "macro")
        check(
            record is not None and "Census" in record.credential_refs,
            "saved_credential_required",
        )
        return packages.resolve_credential(record, "Census")

    connection = connect_dev(
        secret["postgres"]["password"], runtime_path=Path(runtime_path)
    )
    try:
        preflight(connection)
        raw = GarageM3RawStore(
            GarageCredentials(
                secret["s3"]["access_key_id"], secret["s3"]["secret_access_key"]
            )
        )
        return M3TimeSeriesService(
            PostgresM3Store(connection, raw),
            M3CandidateSource(
                credential_loader=credential,
                request_control=request_control,
                enabled=allow_live,
            ),
        )
    except Exception:
        connection.close()
        raise
