"""Unwired A1 PG registration adapter; caller owns connection and transaction.

Never creates a connection, runs DDL, commits, retries or reads credentials.
Rows reference the pinned catalog operation identity (including Source revision).
This is management metadata, not a canonical asset/version/publication store.
"""

from market_data_foundation.web.management import InstanceRecord
from market_data_foundation.web.source_catalog import DirectoryQuery, directory

TABLE = "mdf_r3_a1.instance_registration"
COLUMNS = "identity, catalog_identity, ownership, endpoint_ref"


class ManagementStorageError(Exception):
    """Bounded classification only, including ambiguous write failures."""


def _validate(record: InstanceRecord) -> None:
    known = {entry.identity: entry for entry in directory(DirectoryQuery()).entries}
    if (
        record.catalog_identity not in known
        or (
            known[record.catalog_identity].domain == "KD"
            and record.ownership == "MDF_MANAGED"
        )
        or record.observed_at is not None
        or record.expires_at is not None
        or record.state != "UNKNOWN"
    ):
        raise ManagementStorageError("registration_contract_invalid")


class PgManagementRecords:
    def __init__(self, connection):
        self.connection = connection

    def register(self, record: InstanceRecord) -> None:
        """Exact replay is idempotent; changed identity content is a conflict.

        Caller MUST roll back on error. An I/O/commit outcome is never retried
        here; resolve it with the same identity in a later authorized read.
        """
        _validate(record)
        values = (
            record.identity,
            record.catalog_identity,
            record.ownership,
            record.endpoint_ref,
        )
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f"INSERT INTO {TABLE} ({COLUMNS}) VALUES (%s, %s, %s, %s) "
                    "ON CONFLICT (identity) DO NOTHING",
                    values,
                )
                cursor.execute(
                    f"SELECT {COLUMNS} FROM {TABLE} WHERE identity = %s",
                    (record.identity,),
                )
                if cursor.fetchone() != values:
                    raise ManagementStorageError("registration_identity_conflict")
        except ManagementStorageError:
            raise
        except Exception:
            raise ManagementStorageError("storage_outcome_unknown") from None

    def read(self) -> tuple[InstanceRecord, ...]:
        try:
            with self.connection.cursor() as cursor:
                cursor.execute(
                    f"SELECT {COLUMNS} FROM {TABLE} ORDER BY identity LIMIT 65"
                )
                rows = cursor.fetchall()
            if len(rows) > 64:
                raise ManagementStorageError("management_limit_exceeded")
            records = tuple(
                InstanceRecord(**dict(zip(COLUMNS.split(", "), row, strict=True)))
                for row in rows
            )
            for record in records:
                _validate(record)
            return records
        except ManagementStorageError:
            raise
        except Exception:
            raise ManagementStorageError("management_read_unavailable") from None
