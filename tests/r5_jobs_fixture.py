"""Synthetic trusted host; no real authentication, provider or storage binding."""

from contextlib import contextmanager

from market_data_foundation.web.cpi_api import create_app
from market_data_foundation.web.cpi_jobs import CpiJobsHost, CpiManualWorker, Operator
from tests.r5_fixture import ReadOnlySnapshot
from tests.test_cpi_local_job import NOW, Rig, spec


def jobs_host(rig, *, operator=Operator("synthetic-operator", True), target=None):
    return CpiJobsHost(
        rig.journal,
        target or spec(),
        lambda: NOW,
        # Server fixture owns this identity; request role/header/body is ignored.
        lambda _request: operator,
        "OFFLINE_FIXTURE",
    )


def jobs_app(rig, **kwargs):
    @contextmanager
    def scope():
        yield ReadOnlySnapshot(rig.ledger)

    return create_app(
        scope, evidence_mode="OFFLINE_FIXTURE", jobs_host=jobs_host(rig, **kwargs)
    )


def worker(rig, *, preflight=lambda _spec: None):
    return CpiManualWorker(
        "synthetic-cpi-worker", rig.engine, spec(), preflight, rig.acquirer
    )


def intent(job="manual-first", **changes):
    target = spec()
    return {
        "job_identity": job,
        "asset_identity": target.definition,
        "asset_revision": target.definition_revision,
        "reference": target.reference,
        "release_slot": target.release_slot,
        "mode": "manual_once",
        **changes,
    }


__all__ = ["Rig", "intent", "jobs_app", "jobs_host", "worker"]
