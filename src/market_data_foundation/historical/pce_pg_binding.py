"""Exact bounded DEV PCE identity; never activates the T04 SourceBinding."""

from dataclasses import asdict, dataclass

from market_data_foundation.canonical.model import digest, require
from market_data_foundation.historical.bea_pce import (
    PCE_BINDING_CANDIDATE,
    PCE_DEFINITION,
    PCE_SOURCE,
    REFERENCE,
)

SCHEMA = "mdf_macro_bea_pce_vertical_r1"
DOMAIN = "mdf-b3-bea-pce-202607-r1"
BACKFILL_SCOPE = DOMAIN + "-backfill-r2"
OPERATOR = "ao-dev-bea-pce"


@dataclass(frozen=True)
class PcePgTarget:
    host: str = "192.168.10.4"
    port: int = 5432
    database: str = "market_data_foundation_dev"
    schema: str = SCHEMA
    domain: str = DOMAIN

    def __post_init__(self):
        require(
            all(
                getattr(self, n) == f.default
                for n, f in self.__dataclass_fields__.items()
            )
            and type(self.port) is int,
            "pce_pg_target_not_admitted",
        )

    def check_connection(self, connection):
        info = connection.info
        require(
            (info.host, info.hostaddr, info.port, info.dbname)
            == (self.host, self.host, self.port, self.database),
            "pce_pg_target_not_admitted",
        )


@dataclass(frozen=True)
class PcePgBinding:
    operator: str = OPERATOR
    target: PcePgTarget = PcePgTarget()
    definition: str = PCE_DEFINITION.identity
    definition_revision: str = PCE_DEFINITION.revision
    source: str = PCE_SOURCE.identity
    source_revision: str = PCE_SOURCE.contract_revision
    operation: str = PCE_SOURCE.operation
    binding: str = PCE_BINDING_CANDIDATE.identity
    binding_revision: str = PCE_BINDING_CANDIDATE.revision
    reference: str = REFERENCE
    candidate_only: bool = True
    source_qualified: bool = False

    def __post_init__(self):
        require(
            type(self.target) is PcePgTarget
            and self.candidate_only is True
            and self.source_qualified is False
            and all(
                getattr(self, n) == f.default
                for n, f in self.__dataclass_fields__.items()
            ),
            "pce_pg_binding_not_admitted",
        )

    def document(self):
        return asdict(self)

    def check_spec(self, document, scope=None):
        spec = candidate_spec()
        require(document == spec.document(), "pce_pg_binding_not_admitted")
        if scope is not None:
            require(scope == spec.scope, "pce_pg_scope_mismatch")


BINDING = PcePgBinding()


def candidate_spec():
    from market_data_foundation.historical.pce_local_job import PceJobSpec

    return PceJobSpec(
        mode="acquire", effect_domain="owner_nonproduction", max_provider_requests=2
    )


def job_identity():
    return "pce-init-" + digest(DOMAIN.encode())[:40]


def backfill_identity():
    """One immutable successor slot; never derived from a request nonce."""
    return (
        "pce-backfill-"
        + digest(
            (DOMAIN + "\0failed-initial-successor-r2\0" + job_identity()).encode()
        )[:40]
    )


def check_job(document, identity):
    require(
        identity in {job_identity(), backfill_identity()},
        "pce_pg_job_identity_mismatch",
    )
    require(document["job_id"] == identity, "pce_pg_job_identity_mismatch")
    if identity == backfill_identity() and document["state"] == "succeeded":
        reconciliation = document.get("reconciliation") or {}
        evidence = reconciliation.get("evidence") or {}
        require(
            reconciliation.get("outcome") == "confirmed_published"
            and evidence.get("version_identity") == identity + ":version"
            and evidence.get("publication_identity") == identity + ":publication",
            "pce_backfill_success_evidence_mismatch",
        )
    BINDING.check_spec(document["spec"])
    require(document["binding"] == BINDING.document(), "pce_pg_binding_mismatch")
    require(
        document.get("parent_job_id")
        == (job_identity() if identity == backfill_identity() else None),
        "pce_pg_parent_mismatch",
    )


def check_backfill_parent(document):
    check_job(document, job_identity())
    require(document["state"] == "failed", "pce_backfill_parent_not_failed")
    require(
        document["classification"] == "credential_echo_rejected",
        "pce_backfill_original_failure_mismatch",
    )
    require(document["attempts"] == 1, "pce_backfill_failed_attempt_required")
    require(
        not document["effects"].get("canonical_possible")
        and not document["effects"].get("raw_creates_possible"),
        "pce_backfill_parent_effects_unresolved",
    )
