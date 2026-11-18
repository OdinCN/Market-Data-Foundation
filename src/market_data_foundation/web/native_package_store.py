"""Additive TDX configuration without invalidating the older shared reader.

18761 knows only daily_raw and rejects unknown IDs anywhere in packages.json.
Keep that file authoritative for legacy capabilities and credentials. Native
capabilities use a separately atomic snapshot under the same configuration lock.
The public CAS revision binds both snapshots, including changes by the old host.
"""

import json
from hashlib import sha256

from market_data_foundation.web.package_catalog import PackageError
from market_data_foundation.web.package_store import (
    ConfigRecord,
    LocalPackageStore,
    Snapshot,
)

NATIVE_IDS = frozenset({"daily_native", "stock_roster"})


class NativePackageStore(LocalPackageStore):
    configuration_readonly_capabilities = {"tdx": ["daily_raw"]}

    def _extension_store(self):
        store = LocalPackageStore(self.root, self.catalog)
        store.path = self.root / "tdx-capabilities.json"
        return store

    def _extensions(self):
        store = self._extension_store()
        if not store.path.exists():
            return Snapshot(records=[])
        snapshot = store._load()
        if any(
            r.package_id != "tdx"
            or not set(r.enabled_capability_ids) <= NATIVE_IDS
            or r.credential_refs
            for r in snapshot.records
        ):
            raise PackageError("config_storage_corrupt")
        return snapshot

    @staticmethod
    def _combined(legacy, extension):
        if extension is None:
            return legacy
        payload = json.dumps(
            [r.model_dump(mode="json") if r else None for r in (legacy, extension)],
            sort_keys=True,
        ).encode()
        # r remains an opaque CAS token within the existing DTO's 32-char bound.
        revision = "r" + str(1 + int.from_bytes(sha256(payload).digest()[:12]))
        return extension.model_copy(
            update={
                "revision": revision,
                "enabled_capability_ids": sorted(
                    set(extension.enabled_capability_ids)
                    | set(legacy.enabled_capability_ids if legacy else [])
                ),
            }
        )

    def snapshot(self):
        with self._lock():
            base, extra = self._load(), self._extensions()
            records = [r for r in base.records if r.package_id != "tdx"]
            environments = {
                r.environment_id
                for r in [*base.records, *extra.records]
                if r.package_id == "tdx"
            }
            for environment in sorted(environments):
                records.append(
                    self._combined(
                        base.record(environment, "tdx"),
                        extra.record(environment, "tdx"),
                    )
                )
            return Snapshot(records=records)

    def save(self, environment_id, package_id, command, *, allow_replace=False):
        if package_id != "tdx":
            return super().save(
                environment_id, package_id, command, allow_replace=allow_replace
            )
        self.catalog.validate(package_id, command.enabled_capability_ids)
        # The browser echoes a keep intent for the host-managed TDX connection.
        if command.source_changes and not (
            len(command.source_changes) == 1
            and command.source_changes[0].source_id == "TDX"
            and command.source_changes[0].credential_action == "keep"
        ):
            raise PackageError("source_credentials_not_applicable", 422, "REJECTED")
        with self._lock(exclusive=True):
            base, extra = self._load(), self._extensions()
            legacy = base.record(environment_id, package_id)
            extension = extra.record(environment_id, package_id)
            current = self._combined(legacy, extension)
            if command.expected_revision != (current.revision if current else None):
                raise PackageError("config_revision_conflict", 409, "CONFLICT")
            selected = set(command.enabled_capability_ids)
            if selected - NATIVE_IDS != set(
                legacy.enabled_capability_ids if legacy else []
            ):
                raise PackageError(
                    "legacy_capability_managed_by_reference_host", 422, "REJECTED"
                )
            if current is None and not selected:
                raise PackageError("first_enable_required", 422, "REJECTED")
            record = ConfigRecord(
                environment_id=environment_id,
                package_id=package_id,
                revision=f"r{int(extension.revision[1:]) + 1}" if extension else "r1",
                enabled_capability_ids=sorted(selected & NATIVE_IDS),
            )
            self._extension_store()._commit(
                Snapshot(
                    records=[r for r in extra.records if r != extension] + [record]
                )
            )
            return self._combined(legacy, record)
