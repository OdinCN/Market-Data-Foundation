"""Explicit synthetic records only; no Host reads, provider, environment or I/O."""

from datetime import UTC, datetime

from market_data_foundation.web.observability import (
    DiagnosticDTO,
    DiskDTO,
    InstanceDTO,
    LoadDTO,
    MemoryDTO,
    NetworkDTO,
    ObjectRef,
    ObservabilityHost,
    Observation,
    ResourcesDTO,
    StorageDTO,
    TrustedEvidence,
)
from market_data_foundation.web.source_catalog import DirectoryQuery, directory


def fixture_host(at: datetime | None = None):
    instant = datetime.now(UTC) if at is None else at
    stamp = Observation(
        status="OK",
        classification="observed",
        sampled_at=instant.isoformat(),
        source="OFFLINE_FIXTURE",
    )
    entry = next(
        e
        for e in directory(DirectoryQuery()).entries
        if e.asset and e.asset.identity == "mdf.macro.bls.cpi.headlines"
    )
    related = ObjectRef(
        operation_identity=entry.identity,
        asset_identity=entry.asset.identity,
        asset_revision=entry.asset.revision,
        version_identity="fixture.version.1",
        publication_identity="fixture.publication.1",
        job_identity="fixture.job.1",
        attempt_identity="fixture.attempt.1",
    )
    resources = ResourcesDTO(
        host_identity="offline-fixture-host",
        cpu_load=LoadDTO(observation=stamp, values=[0.25, 0.5, 0.75]),
        memory=MemoryDTO(observation=stamp, total=1048576, available=524288),
        disks=[DiskDTO(observation=stamp, total=8388608, available=4194304)],
        networks=[
            NetworkDTO(
                interface="fixture0",
                observation=stamp,
                received=123456,
                transmitted=654321,
            )
        ],
        sample_duration_seconds=0.01,
    )
    evidence = TrustedEvidence(
        observation=stamp,
        instances=[
            InstanceDTO(
                component_identity=entry.component_identity,
                instance_identity="offline-fixture-reader",
                observation=stamp,
                state="DEGRADED",
                started_at=stamp.sampled_at,
                heartbeat_at=stamp.sampled_at,
                related=related,
            )
        ],
        storage=[
            StorageDTO(
                identity="postgresql",
                instance_identity="fixture.pg.1",
                qualification_identity="fixture.qualification.pg.1",
                observation=stamp,
                state="UNAVAILABLE",
                qualification="QUALIFIED_READ_ONLY",
            )
        ],
        diagnostics=[
            DiagnosticDTO(
                identity="fixture.diagnostic.1",
                observation=stamp,
                severity="ERROR",
                module="job",
                classification="job_failed",
                related=related,
            ),
            DiagnosticDTO(
                identity="fixture.diagnostic.2",
                observation=stamp,
                severity="INFO",
                module="service",
                classification="recovered",
            ),
        ],
    )
    return ObservabilityHost(
        sampler=lambda: resources,
        evidence=lambda: evidence,
        evidence_mode="OFFLINE_FIXTURE",
    )
