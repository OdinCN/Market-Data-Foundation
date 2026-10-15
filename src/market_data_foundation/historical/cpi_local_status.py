"""Read-only CPI catalog/status and a journal-only CLI without network clients."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path

from market_data_foundation.canonical.consumer_read import (
    ReadContractError,
    read_exact_version,
)
from market_data_foundation.canonical.model import instant
from market_data_foundation.canonical.repository import Ledger
from market_data_foundation.domain_contracts.macro import BLS_CPI_HEADLINES
from market_data_foundation.historical.bls_cpi_direct import SERIES_IDS
from market_data_foundation.historical.bls_cpi_release_runner import (
    CALENDAR_SOURCE,
    RELEASE,
    SLOT_IDENTITY,
)
from market_data_foundation.historical.cpi_job_journal import FileJobJournal
from market_data_foundation.historical.cpi_local_read import (
    CpiReadRejected,
    read_local_cpi,
)


def _job(document: dict) -> dict:
    # Allowlist: never render plan/content, arbitrary error strings or raw bytes.
    result = {
        key: document[key]
        for key in (
            "job_id",
            "state",
            "stage",
            "attempt_id",
            "attempts",
            "created_at",
            "started_at",
            "finished_at",
            "updated_at",
            "cancel_requested",
            "classification",
            "failure_stage",
            "effects",
            "raw",
            "reconciliation",
            "last_success",
        )
    }
    result["effect_domain"] = document["spec"]["effect_domain"]
    result["target"] = {
        key: document["spec"][key]
        for key in (
            "reference",
            "source_identity",
            "source_revision",
            "provider_revision",
            "mode",
        )
    }
    return result


def local_cpi_status(
    journal: FileJobJournal, *, at: datetime, ledger: Ledger | None = None
) -> dict:
    """Use a caller-owned local read-only snapshot; never mutate job/source.

    Exact publications are listed independently. The as-of selector can refuse
    ambiguity; this function never invents 'latest wins' to override that rule.
    Raw states are dated journal observations, not fresh S3 availability checks.
    """
    instant(at)
    jobs = journal.list_jobs()
    successes = sorted(
        (item for item in jobs if item["last_success"] is not None),
        key=lambda item: datetime.fromisoformat(item["last_success"]["observed_at"]),
    )
    result = {
        "product": "BLS CPI",
        "definition": BLS_CPI_HEADLINES.identity,
        "definition_revision": BLS_CPI_HEADLINES.revision,
        "journal_backend": "single_host_file_not_pg_job_state",
        "observed_at": at.isoformat(),
        "catalog": {
            "series": list(SERIES_IDS),
            "references": ["2026-08", "2026-09"],
            "executable_reference": "2026-09",
            "measures": ["headline_mom", "headline_yoy", "core_mom", "core_yoy"],
            "values": "BLS provider calculations; no MDF inferred vintage",
        },
        "freshness": {
            "official_release_slot": {
                "identity": SLOT_IDENTITY,
                "release": RELEASE.isoformat(),
                "evidence": CALENDAR_SOURCE,
                "basis": "existing_prebound_contract",
            },
            "provider_revision": "not_declared",
            "provider_actual_released_at": None,
            "next_proven_official_slot": None,
            "latest_successful_job": _job(successes[-1]) if successes else None,
            "latest_successful_observed_at": successes[-1]["last_success"][
                "observed_at"
            ]
            if successes
            else None,
            "latest_successful_retrieval": successes[-1]["last_success"]["evidence"][
                "known_at"
            ]
            if successes
            else None,
            "ttl": None,
        },
        "jobs": [_job(item) for item in jobs],
        "latest_job": _job(jobs[-1]) if jobs else None,
        "raw_state_basis": "journal_observation_only_no_object_store_probe",
        "availability": "not_checked",
        "publications": [],
        "coverage": [],
        "quality": [],
        "selected_publication": None,
    }
    if ledger is None:
        return result
    try:
        for version in ledger.versions_for_definition(BLS_CPI_HEADLINES.key):
            try:
                exact = read_exact_version(
                    ledger, BLS_CPI_HEADLINES.key, version.key, as_of=at
                )
            except ReadContractError:
                continue
            references = sorted(
                {member.split(":", 1)[0] for member in version.finite_scope.members}
            )
            try:
                read_local_cpi(
                    ledger,
                    reference=references[0] if len(references) == 1 else "",
                    version_identity=version.identity,
                    as_of=at,
                )
                cpi_classification = "available"
            except CpiReadRejected as exc:
                cpi_classification = exc.classification
            except ReadContractError as exc:
                cpi_classification = exc.classification.value
            result["publications"].append(
                {
                    "version_identity": version.identity,
                    "storage_state": "canonical_readable",
                    "cpi_read_classification": cpi_classification,
                    "coverage": list(version.finite_scope.members),
                    "completeness": version.completeness,
                    "quality": sorted(
                        grade.value
                        for grade in exact.publication.request.claimed_grades
                    ),
                    "usable_at": exact.publication.usable_at.isoformat(),
                    "decision_at": exact.publication.decision_at.isoformat(),
                    "known_at": [
                        item.obtained_at.isoformat() if item.obtained_at else None
                        for item in exact.acquisitions
                    ],
                    "reference": references,
                    "publication_identity": exact.publication.request.identity,
                }
            )
        selected = read_local_cpi(ledger, reference="2026-09", as_of=at)
        result["availability"] = "available"
        result["selected_publication"] = selected.publication.request.identity
        result["coverage"] = list(selected.version.finite_scope.members)
        result["quality"] = sorted(
            grade.value for grade in selected.publication.request.claimed_grades
        )
    except ReadContractError as exc:
        result["availability"] = exc.classification.value
    except CpiReadRejected as exc:
        result["availability"] = exc.classification
    except Exception:
        result["availability"] = "storage_unavailable"
        result["publications"] = []
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--journal", required=True, type=Path)
    args = parser.parse_args()
    try:
        result = local_cpi_status(
            FileJobJournal(args.journal, read_only=True), at=datetime.now(UTC)
        )
    except Exception:
        result = {"classification": "journal_unavailable"}
    print(json.dumps(result, sort_keys=True, ensure_ascii=False))


if __name__ == "__main__":
    main()
