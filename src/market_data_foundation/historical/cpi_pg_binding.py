"""Inert, exact B1 candidate identity. This is not SourceBinding activation."""

from dataclasses import asdict, dataclass

from market_data_foundation.canonical import codec
from market_data_foundation.canonical.model import digest, require
from market_data_foundation.historical.bls_cpi_direct import (
    SEPTEMBER_REFERENCE_CONTRACT,
    SERIES_IDS,
)
from market_data_foundation.historical.macro_job_journal import valid_identity

SCHEMA = "mdf_b3_cpi_05_dev_r1"
DOMAIN = "mdf-b3-cpi-05-dev-r1"


@dataclass(frozen=True)
class CpiPgTarget:
    host: str = "192.168.10.4"
    port: int = 5432
    database: str = "market_data_foundation_dev"
    schema: str = SCHEMA
    domain: str = DOMAIN

    def __post_init__(self):
        require(
            (self.host, self.port, self.database, self.schema, self.domain)
            == ("192.168.10.4", 5432, "market_data_foundation_dev", SCHEMA, DOMAIN)
            and type(self.port) is int,
            "cpi_pg_target_not_admitted",
        )

    def check_connection(self, connection):
        info = connection.info
        require(
            (info.host, info.hostaddr, info.port, info.dbname)
            == (self.host, self.host, self.port, self.database),
            "cpi_pg_target_not_admitted",
        )


@dataclass(frozen=True)
class CpiPgBinding:
    # Host-authenticated non-secret subject, never a browser supplied role.
    operator: str
    target: CpiPgTarget = CpiPgTarget()
    definition: str = "mdf.macro.bls.cpi.headlines"
    definition_revision: str = "p3-r1"
    source: str = "mdf.source-candidate.bls-v2.cpi.release-2026-09"
    source_revision: str = "p4-release-reference-2026-09-r1"
    operation: str = "timeseries.data:CPI"
    binding: str = "mdf.binding-candidate.bls-v2.cpi.release-2026-09"
    binding_revision: str = "p4-release-reference-2026-09-r1"
    reference: str = "2026-09"
    release_slot: str = "CPI/2026-09/2026-10-14T12:30:00Z"
    provider_revision: str = "not_declared"

    def __post_init__(self):
        valid_identity(self.operator)
        require(type(self.target) is CpiPgTarget, "cpi_pg_target_not_admitted")
        for name in self.__dataclass_fields__:
            if name not in {"operator", "target"}:
                require(
                    getattr(self, name) == self.__dataclass_fields__[name].default,
                    "cpi_pg_binding_not_admitted",
                )

    def document(self):
        return asdict(self)

    def check_spec(self, spec: dict, scope: str | None = None):
        # Lazy import avoids the engine -> binding -> engine import cycle.
        # Full budget/shape checks apply even to direct journal callers.
        from market_data_foundation.historical.cpi_local_job import CpiJobSpec

        valid = False
        try:
            valid = CpiJobSpec.from_document(spec).document() == spec
        except ValueError, TypeError, KeyError:
            pass
        require(valid, "cpi_pg_binding_not_admitted")
        expected = {
            "definition": self.definition,
            "definition_revision": self.definition_revision,
            "source_identity": self.source,
            "source_revision": self.source_revision,
            "reference": self.reference,
            "release_slot": self.release_slot,
            "provider_revision": self.provider_revision,
            "series": list(SERIES_IDS),
            "quality": ["original", "projected"],
            "completeness": SEPTEMBER_REFERENCE_CONTRACT.completeness,
            "effect_domain": "owner_nonproduction",
            "mode": "acquire",
        }
        require(
            all(spec.get(k) == v for k, v in expected.items()),
            "cpi_pg_binding_not_admitted",
        )
        if scope is not None:
            require(
                scope
                == digest(
                    codec.dumps(
                        (
                            self.definition,
                            self.definition_revision,
                            SERIES_IDS,
                            self.reference,
                            self.source,
                            self.source_revision,
                            self.provider_revision,
                        )
                    ).encode()
                ),
                "cpi_pg_scope_mismatch",
            )


# One Owner-verified failed INIT; no nonce, arbitrary month or successor chain.
INITIAL_JOB = "cpi-init-fb8e51bc93a2918b6854c5d9e743c7a38f16de53"
BACKFILL_SCOPE = DOMAIN + "-failed-init-successor-r8"
BACKFILL_JOB = (
    "cpi-backfill-" + digest((BACKFILL_SCOPE + "\0" + INITIAL_JOB).encode())[:40]
)


def check_backfill_parent(document):
    require(document["job_id"] == INITIAL_JOB, "cpi_backfill_parent_mismatch")
    require(document["state"] == "failed", "cpi_backfill_parent_not_failed")
    require(
        document["classification"] == "bls_live_http_failure",
        "cpi_backfill_original_failure_mismatch",
    )
    require(
        document["attempts"] == 1 and document["attempt_id"] == INITIAL_JOB + "-a1",
        "cpi_backfill_failed_attempt_required",
    )
    require(
        not document.get("parent_job_id")
        and document.get("plan") is None
        and document.get("raw") is None
        and document.get("last_success") is None
        and document.get("reconciliation") is None
        and not any(
            document["effects"].get(k)
            for k in (
                "canonical_possible",
                "raw_create_calls_possible",
                "ledger_commit_possible",
                "publication_possible",
            )
        ),
        "cpi_backfill_parent_effects_unresolved",
    )


def backfill_constraint():
    # Exact pg_get_constraintdef representation of the candidate CHECK. Fail
    # closed if the Host's PG renders a different expression; never substring-match.
    import json

    expression = (
        "jsonb_build_array(scope, identity, (document ->> 'parent_job_id'::text))"
    )
    parent = json.dumps([DOMAIN, INITIAL_JOB, None])
    child = json.dumps([BACKFILL_SCOPE, BACKFILL_JOB, INITIAL_JOB])
    return (
        f"CHECK ((({expression} = '{parent}'::jsonb) "
        f"OR ({expression} = '{child}'::jsonb)))"
    )
