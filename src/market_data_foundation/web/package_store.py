"""CAS publishes capabilities and private immutable credential references together.

Credential bytes are prepared and fsynced before the single packages.json
replacement. Only references in that snapshot are effective. Failed preparation
can leave an unused private file, never a partially enabled configuration. Old
credential files are retained; this store does not delete or rotate live keys.
"""

import fcntl
import json
import os
import re
import stat
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, ValidationError, model_validator

from market_data_foundation.web.dto import DTO
from market_data_foundation.web.package_catalog import (
    PackageCatalog,
    PackageError,
    ReplaceChange,
)

KEY_SOURCES = frozenset({"BLS", "BEA", "Census"})


class ConfigRecord(DTO):
    environment_id: str = Field(pattern=r"^[a-z0-9_-]{1,64}$")
    package_id: str
    revision: str = Field(pattern=r"^r[1-9][0-9]*$", max_length=32)
    enabled_capability_ids: list[str] = Field(max_length=32)
    # Host-only identities. Public PackageView never includes these references.
    credential_refs: dict[str, str] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def valid_credential_refs(self):
        if not set(self.credential_refs) <= KEY_SOURCES or any(
            re.fullmatch(r"[0-9a-f]{32}", ref) is None
            for ref in self.credential_refs.values()
        ):
            raise ValueError("invalid_credential_reference")
        return self


class Snapshot(DTO):
    schema_revision: Literal["d01-local-v1"] = "d01-local-v1"
    records: list[ConfigRecord]

    def record(self, environment_id, package_id):
        return next(
            (
                r
                for r in self.records
                if (r.environment_id, r.package_id) == (environment_id, package_id)
            ),
            None,
        )

    @model_validator(mode="after")
    def unique_records(self):
        identities = [(r.environment_id, r.package_id) for r in self.records]
        if len(identities) != len(set(identities)):
            raise ValueError("duplicate_package_record")
        return self


class LocalPackageStore:
    """Single-host POSIX configuration and private immutable credential storage.

    Cooperating instances must share this directory and flock-capable filesystem.
    Not a distributed/multi-host store. Host initialization is explicit; opening
    a missing, corrupt or inaccessible store never creates an empty snapshot.
    """

    def __init__(self, root: Path, catalog: PackageCatalog):
        self.root = Path(root)
        self.catalog = catalog
        self.path = self.root / "packages.json"
        self.lock_path = self.root / "packages.lock"

    @classmethod
    def initialize(cls, root: Path, catalog: PackageCatalog):
        # Host provisioning only: fail if the dedicated directory already exists.
        root.mkdir(mode=0o700)
        store = cls(root, catalog)
        store.lock_path.touch(mode=0o600, exist_ok=False)
        store._commit(Snapshot(records=[]))
        return store

    @contextmanager
    def _lock(self, exclusive=False):
        entered = False
        try:
            with self.lock_path.open("r+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
                try:
                    entered = True
                    yield
                finally:
                    fcntl.flock(lock, fcntl.LOCK_UN)
        except OSError:
            # An unlock/close failure after an exclusive operation cannot promise
            # that a completed replacement had no effect.
            unknown = exclusive and entered
            raise PackageError(
                "config_commit_outcome_unknown"
                if unknown
                else "config_storage_unavailable",
                status="UNKNOWN" if unknown else "UNAVAILABLE",
                effect="UNKNOWN" if unknown else "NONE",
            ) from None

    def _load(self):
        def unique_pairs(items):
            if len(dict(items)) != len(items):
                raise ValueError("duplicate_json_member")
            return dict(items)

        try:
            raw = self.path.read_bytes()
            parsed = json.loads(raw, object_pairs_hook=unique_pairs)
            if (
                not isinstance(parsed, dict)
                or parsed.get("schema_revision") != "d01-local-v1"
            ):
                raise ValueError("config_schema_revision_missing_or_invalid")
            snapshot = Snapshot.model_validate(parsed)
            for r in snapshot.records:
                self.catalog.validate(r.package_id, r.enabled_capability_ids)
                if not set(r.credential_refs) <= self._sources(r.package_id):
                    raise ValueError("credential_reference_outside_package")
            return snapshot
        except OSError:
            raise PackageError("config_storage_unavailable") from None
        except ValueError, ValidationError, PackageError:
            raise PackageError("config_storage_corrupt") from None

    def _commit(self, snapshot):
        temp_path = None
        replaced = False
        try:
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as f:
                temp_path = Path(f.name)
                f.write(snapshot.model_dump_json().encode())
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, self.path)
            replaced = True
            fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError:
            # Replacement can succeed before durability confirmation fails.
            raise PackageError(
                "config_commit_outcome_unknown" if replaced else "config_save_failed",
                status="UNKNOWN" if replaced else "UNAVAILABLE",
                effect="UNKNOWN" if replaced else "NONE",
            ) from None
        finally:
            if temp_path is not None:
                try:
                    temp_path.unlink(missing_ok=True)
                except OSError:
                    pass  # Cleanup cannot overwrite the commit classification.

    def snapshot(self):
        with self._lock():
            return self._load()

    def read(self, environment_id, package_id):
        self.catalog.validate(package_id, [])
        return self.snapshot().record(environment_id, package_id)

    def _sources(self, package_id, enabled=None):
        return {
            s
            for c in self.catalog.package(package_id).capabilities
            if enabled is None or c.capability_id in enabled
            for s in c.required_sources
        }

    @contextmanager
    def _credential_directory(self, *, create=False):
        root_fd = directory_fd = None
        try:
            root_fd = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            root_stat = os.fstat(root_fd)
            if root_stat.st_uid != os.getuid() or root_stat.st_mode & 0o077:
                raise OSError("private_directory_required")
            if create:
                try:
                    os.mkdir("credentials", mode=0o700, dir_fd=root_fd)
                except FileExistsError:
                    pass
            directory_fd = os.open(
                "credentials",
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            directory_stat = os.fstat(directory_fd)
            if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o077:
                raise OSError("private_directory_required")
            if create:
                # Persist the directory entry before a snapshot can reference it.
                os.fsync(root_fd)
            yield directory_fd
        finally:
            if directory_fd is not None:
                os.close(directory_fd)
            if root_fd is not None:
                os.close(root_fd)

    def _stage_credential(self, secret):
        reference = uuid4().hex
        try:
            with self._credential_directory(create=True) as directory_fd:
                fd = os.open(
                    reference,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "wb") as stream:
                    stream.write(secret.get_secret_value().encode("utf-8"))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.fsync(directory_fd)
            return reference
        except OSError:
            raise PackageError("credential_prepare_failed") from None

    def resolve_credential(self, record, source_id):
        """Trusted Host/Worker resolver for one already selected snapshot.

        No browser path, environment override or fallback is accepted. Callers
        retain the same record for capability decisions and credential loading.
        A corrupt current reference never falls back to an older credential.
        """
        if source_id not in self._sources(record.package_id):
            raise PackageError("illegal_source_change", 422, "REJECTED")
        reference = record.credential_refs.get(source_id)
        if reference is None:
            return None
        try:
            with self._credential_directory() as directory_fd:
                fd = os.open(
                    reference,
                    os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                    dir_fd=directory_fd,
                )
                with os.fdopen(fd, "rb") as stream:
                    info = os.fstat(stream.fileno())
                    if (
                        not stat.S_ISREG(info.st_mode)
                        or info.st_uid != os.getuid()
                        or info.st_mode & 0o077
                        or not 1 <= info.st_size <= 16384
                    ):
                        raise OSError("private_credential_file_required")
                    raw = stream.read(16385).decode("utf-8")
                    if (
                        not raw.strip()
                        or len(raw) > 4096
                        or any(ord(c) < 32 or ord(c) == 127 for c in raw)
                    ):
                        raise ValueError()
            return raw
        except OSError, ValueError:
            raise PackageError("credential_storage_unavailable") from None

    def save(self, environment_id, package_id, command, *, allow_replace=False):
        # Validate the trusted environment before touching persistent state.
        ConfigRecord(
            environment_id=environment_id,
            package_id=package_id,
            revision="r1",
            enabled_capability_ids=[],
        )
        self.catalog.validate(package_id, command.enabled_capability_ids)
        sources = self._sources(package_id)
        changes = [c.source_id for c in command.source_changes]
        if len(changes) != len(set(changes)) or not set(changes) <= sources:
            raise PackageError("illegal_source_change", 422, "REJECTED")
        replacements = [
            c for c in command.source_changes if isinstance(c, ReplaceChange)
        ]
        if replacements and not allow_replace:
            raise PackageError("credential_replace_not_supported", 422, "REJECTED")
        if any(
            c.source_id not in KEY_SOURCES
            or c.source_id
            not in self._sources(package_id, command.enabled_capability_ids)
            for c in replacements
        ):
            raise PackageError("illegal_credential_replacement", 422, "REJECTED")
        with self._lock(exclusive=True):
            snapshot = self._load()
            current = next(
                (
                    r
                    for r in snapshot.records
                    if (r.environment_id, r.package_id) == (environment_id, package_id)
                ),
                None,
            )
            if command.expected_revision != (current.revision if current else None):
                raise PackageError("config_revision_conflict", 409, "CONFLICT")
            if current is None and not command.enabled_capability_ids:
                raise PackageError("first_enable_required", 422, "REJECTED")
            references = dict(current.credential_refs) if current else {}
            for change in replacements:
                references[change.source_id] = self._stage_credential(
                    change.credential_value
                )
            record = ConfigRecord(
                environment_id=environment_id,
                package_id=package_id,
                revision=f"r{int(current.revision[1:]) + 1}" if current else "r1",
                enabled_capability_ids=sorted(command.enabled_capability_ids),
                credential_refs=references,
            )
            self._commit(
                Snapshot(
                    records=[r for r in snapshot.records if r != current] + [record]
                )
            )
            return record
