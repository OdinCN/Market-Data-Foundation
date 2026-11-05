"""Explicit daily migration, content-addressed raw evidence and cutoff snapshots."""

import re
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import psycopg
from psycopg.types.json import Jsonb

from market_data_foundation.historical.financial_daily import (
    DATASET_ID,
    DEFINITION_REVISION,
    DailyBatch,
    FinancialError,
    dates,
    day,
    digest,
    instant,
    require_source_permission,
    series_ids,
)
from market_data_foundation.historical.financial_daily_source import (
    parse_metadata,
    parse_observations,
    validate_batch,
)
from market_data_foundation.historical.verified_garage_raw import VerifiedGarageRawStore

SCHEMA = "mdf_financial_conditions_daily_dev_r1"
REVISION = "financial.daily.physical.r3"
SECOND_REVISION = "financial.daily.physical.r2"
PREVIOUS_REVISION = "financial.daily.physical.r1"
LOCK_ID = 7312026110503
DDL = Path(__file__).resolve().parents[3] / "db/financial_daily/001_daily.sql"
UPGRADE = DDL.with_name("002_origin.sql")


def schema_name(value):
    if type(value) is not str or not (
        value == SCHEMA or re.fullmatch(r"test_financial_daily_[a-z0-9_]{1,35}", value)
    ):
        raise FinancialError("financial_schema_invalid")
    return value


class GarageDailyRawStore(VerifiedGarageRawStore):
    def __init__(self, credentials, **options):
        super().__init__(
            credentials,
            raw_prefix="macro/financial-conditions-daily/r1/",
            error_factory=FinancialError,
            error_prefix="financial",
            **options,
        )

    def read_verified(self, sha, length):
        status, raw = self._send("GET", sha, maximum=length)
        if status != 200 or len(raw) != length or digest(raw) != sha:
            raise FinancialError("financial_raw_readback_failed")
        return raw


class PostgresDailyStore:
    def __init__(
        self,
        connection,
        raw_store=None,
        *,
        schema=SCHEMA,
        clock=lambda: datetime.now(UTC),
    ):
        if not connection.autocommit:
            raise FinancialError("financial_autocommit_required")
        self.connection, self.raw_store, self.schema, self.clock = (
            connection,
            raw_store,
            schema_name(schema),
            clock,
        )

    def _idle(self):
        if self.connection.info.transaction_status != psycopg.pq.TransactionStatus.IDLE:
            raise FinancialError("financial_top_level_transaction_required")

    @contextmanager
    def read_snapshot(self):
        self._idle()
        with self.connection.transaction():
            self.connection.execute(
                "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
            )
            yield

    def migrate(self):
        self._idle()
        s = self.schema
        with self.connection.transaction():
            self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
            exists = self.connection.execute(
                "SELECT pg_get_userbyid(nspowner) FROM pg_namespace WHERE nspname=%s",
                (s,),
            ).fetchone()
            if exists:
                if (
                    exists[0]
                    != self.connection.execute("SELECT current_user").fetchone()[0]
                ):
                    raise FinancialError("financial_schema_owner_mismatch")
                if (
                    self.connection.execute(
                        "SELECT to_regclass(%s)", (s + ".schema_metadata",)
                    ).fetchone()[0]
                    is None
                ):
                    raise FinancialError("financial_existing_namespace_unrecognized")
                versions = self.connection.execute(
                    f"""
                    SELECT schema_revision, dataset_id, definition_revision FROM
                    {s}.schema_metadata
                    """
                ).fetchall()
                if versions in (
                    [(PREVIOUS_REVISION, DATASET_ID, DEFINITION_REVISION)],
                    [(SECOND_REVISION, DATASET_ID, DEFINITION_REVISION)],
                ):
                    self._upgrade(versions[0][0])
                    return dict(
                        state="upgraded",
                        schema=s,
                        schema_revision=REVISION,
                        previous_revision=versions[0][0],
                    )
                if versions != [(REVISION, DATASET_ID, DEFINITION_REVISION)]:
                    raise FinancialError("financial_schema_revision_mismatch")
                return dict(
                    state="already_prepared", schema=s, schema_revision=REVISION
                )
            self.connection.execute(f"CREATE SCHEMA {s}")
            self.connection.execute(f"REVOKE ALL ON SCHEMA {s} FROM PUBLIC")
            for sql in DDL.read_text().split(";"):
                if sql.strip():
                    self.connection.execute(sql.replace("{s}", s))
            self._upgrade()
            self.connection.execute(
                f"INSERT INTO {s}.schema_metadata VALUES(true,%s,%s,%s)",
                (REVISION, DATASET_ID, DEFINITION_REVISION),
            )
            self.connection.execute(
                f"REVOKE ALL ON ALL TABLES IN SCHEMA {s} FROM PUBLIC"
            )
            if self.connection.execute(
                "SELECT 1 FROM pg_roles WHERE rolname='odin_dba'"
            ).fetchone():
                self.connection.execute(f"GRANT USAGE ON SCHEMA {s} TO odin_dba")
                self.connection.execute(
                    f"GRANT SELECT ON ALL TABLES IN SCHEMA {s} TO odin_dba"
                )
        return dict(state="prepared", schema=s, schema_revision=REVISION)

    def _upgrade(self, previous=PREVIOUS_REVISION):
        scripts = ([UPGRADE] if previous == PREVIOUS_REVISION else []) + [
            DDL.with_name("003_credit.sql")
        ]
        for script in scripts:
            for sql in script.read_text().split(";"):
                if sql.strip():
                    self.connection.execute(sql.replace("{s}", self.schema))
        self.connection.execute(
            f"UPDATE {self.schema}.schema_metadata SET schema_revision=%s",
            (REVISION,),
        )

    def _safe_batch(self, batch):
        validate_batch(batch)
        if batch.observed_at > instant(self.clock()):
            raise FinancialError("financial_future_capture_rejected")
        if batch.origin == "controlled_fixture" and not self.schema.startswith(
            "test_financial_daily_"
        ):
            raise FinancialError("financial_fixture_cannot_enter_product_dataset")
        if batch.origin == "fred":
            require_source_permission(batch.series_id)
        if batch.origin == "mdf_derived":
            for point in batch.points:
                for leg in point.provider_extra["inputs"]:
                    row = self.connection.execute(
                        f"SELECT r.value,r.batch_id,r.available_at,b.origin "
                        f"FROM {self.schema}.revisions r "
                        f"JOIN {self.schema}.batches b USING(batch_id) "
                        "WHERE r.series_id=%s "
                        "AND r.observation_date=%s AND r.revision=%s",
                        (
                            leg["series_id"],
                            day(point.observation_date),
                            leg["revision"],
                        ),
                    ).fetchone()
                    if (
                        not row
                        or row[0] is None
                        or str(row[0]) != leg["value"]
                        or row[1] != leg["batch_id"]
                        or row[2] is None
                        or row[3] != "us_treasury"
                        or row[2] != datetime.fromisoformat(leg["available_at"])
                        or row[2]
                        > datetime.fromisoformat(point.provider_extra["cutoff"])
                    ):
                        raise FinancialError("financial_derived_persisted_leg_mismatch")
        if self.raw_store is None:
            raise FinancialError("financial_raw_store_required")

    def _confirm(self, bid):
        with self.connection.transaction():
            self.connection.execute(
                f"""
                UPDATE {self.schema}.batches SET available_at=clock_timestamp() WHERE
                batch_id=%s AND available_at IS NULL
                """,
                (bid,),
            )
            self.connection.execute(
                f"""
                UPDATE {self.schema}.revisions SET available_at=clock_timestamp()
                WHERE
                batch_id=%s AND available_at IS NULL
                """,
                (bid,),
            )

    def save(self, batch, *, before_confirm=None):
        self._idle()
        self._safe_batch(batch)
        raw_locator = self.raw_store.put_verified(batch.raw)
        metadata_locator = self.raw_store.put_verified(batch.metadata_raw)
        bid, inserted = batch.batch_id, []
        try:
            with self.connection.transaction():
                self.connection.execute("SELECT pg_advisory_xact_lock(%s)", (LOCK_ID,))
                old = self.connection.execute(
                    f"""
                    SELECT observed_at, origin FROM {self.schema}.batches WHERE
                    batch_id=%s
                    """,
                    (bid,),
                ).fetchone()
                if old is None:
                    self.connection.execute(
                        f"""
                        INSERT INTO {self.schema}.batches
                        (batch_id,series_id,start_date,
                        end_date,raw_sha256,raw_locator,raw_bytes,metadata_sha256,
                        metadata_locator,metadata_bytes,metadata,metadata_observed_at,
                        observed_at,available_at,request,origin,provider_attempts,
                        is_copyrighted) VALUES(%s, %s, %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s, NULL, %s, %s, %s, %s)
                        """,
                        (
                            bid,
                            batch.series_id,
                            day(batch.start),
                            day(batch.end),
                            digest(batch.raw),
                            raw_locator,
                            len(batch.raw),
                            digest(batch.metadata_raw),
                            metadata_locator,
                            len(batch.metadata_raw),
                            Jsonb(batch.metadata),
                            batch.metadata_observed_at,
                            batch.observed_at,
                            Jsonb(batch.request),
                            batch.origin,
                            batch.attempts,
                            batch.metadata.get("is_copyrighted"),
                        ),
                    )
                    prior_rows = self.connection.execute(
                        f"""SELECT DISTINCT ON(observation_date) observation_date,
                        revision,value,observed_at,provider_extra
                        FROM {self.schema}.revisions WHERE series_id=%s
                        AND observation_date BETWEEN %s AND %s
                        ORDER BY observation_date,revision DESC""",
                        (batch.series_id, day(batch.start), day(batch.end)),
                    ).fetchall()
                    prior_by_date = {r[0]: r[1:] for r in prior_rows}
                    pending_rows = []
                    for point in batch.points:
                        prior = prior_by_date.get(day(point.observation_date))
                        if (
                            prior
                            and prior[1] == point.value
                            and prior[3].get("semantic_identity")
                            == point.provider_extra.get("semantic_identity")
                        ):
                            continue
                        if prior and batch.observed_at <= prior[2]:
                            raise FinancialError("financial_observation_order_conflict")
                        version = prior[0] + 1 if prior else 1
                        pending_rows.append(
                            (
                                point.series_id,
                                day(point.observation_date),
                                version,
                                point.value,
                                Jsonb(point.provider_extra),
                                bid,
                                batch.observed_at,
                            )
                        )
                        inserted.append(version)
                    if pending_rows:
                        with self.connection.cursor() as cursor:
                            cursor.executemany(
                                f"INSERT INTO {self.schema}.revisions "
                                "VALUES(%s,%s,%s,%s,%s,%s,%s,NULL)",
                                pending_rows,
                            )
            # Confirmation follows the returned data commit. An interrupted
            # confirmation is recovered from the same retained batch, not FRED.
            if before_confirm:
                before_confirm()
            self._confirm(bid)
        except psycopg.Error:
            raise FinancialError("financial_storage_outcome_unknown") from None
        return dict(
            batch_id=bid,
            dataset_id=DATASET_ID,
            raw_locator=raw_locator,
            metadata_locator=metadata_locator,
            saved_points=len(inserted),
            saved_revisions=sum(v > 1 for v in inserted),
        )

    def recover(self, bid):
        if type(bid) is not str or not re.fullmatch(r"[0-9a-f]{64}", bid):
            raise FinancialError("financial_batch_id_invalid")
        row = self.connection.execute(
            f"""
            SELECT series_id, start_date, end_date, raw_sha256, raw_bytes,
            metadata_sha256, metadata_bytes, metadata, metadata_observed_at,
            observed_at, request, origin, provider_attempts FROM
            {self.schema}.batches WHERE batch_id=%s
            """,
            (bid,),
        ).fetchone()
        if row is None:
            raise FinancialError("financial_batch_not_found")
        (
            sid,
            start,
            end,
            sha,
            length,
            msha,
            mlength,
            meta,
            mat,
            at,
            request,
            origin,
            attempts,
        ) = row
        raw = self.raw_store.read_verified(sha, length)
        metadata_raw = self.raw_store.read_verified(msha, mlength)
        if origin == "credit_archive":
            from market_data_foundation.historical.financial_daily_credit import (
                archive_points,
            )

            parsed = archive_points(raw, sid, start.isoformat(), end.isoformat())
            parsed_meta = __import__("json").loads(metadata_raw)
        elif origin not in {"fred", "controlled_fixture"}:
            from market_data_foundation.historical.financial_daily_origin import (
                derived_points,
                points,
            )

            parsed = (
                derived_points(raw, start.isoformat(), end.isoformat())
                if origin == "mdf_derived"
                else points(
                    raw,
                    sid,
                    start.isoformat(),
                    end.isoformat(),
                    period="year"
                    if "field_tdr_date_value" in request.get("parameters", {})
                    else "month",
                )
            )
            parsed_meta = __import__("json").loads(metadata_raw)
        else:
            parsed = parse_observations(raw, sid, start.isoformat(), end.isoformat())
            parsed_meta = parse_metadata(metadata_raw, sid)
        batch = DailyBatch(
            sid,
            start.isoformat(),
            end.isoformat(),
            parsed,
            raw,
            metadata_raw,
            parsed_meta,
            mat,
            at,
            request,
            origin,
            attempts,
        )
        if batch.batch_id != bid or batch.metadata != meta:
            raise FinancialError("financial_batch_identity_mismatch")
        result = self.save(batch)
        self.record_attempt(sid, batch.start, batch.end, "complete", bid=bid)
        return result

    def record_attempt(self, sid, start, end, state, classification=None, bid=None):
        series_ids((sid,))
        dates(start, end)
        if state not in {"complete", "failed", "unknown"}:
            raise FinancialError("financial_attempt_state_invalid")
        self._idle()
        with self.connection.transaction():
            self.connection.execute(
                f"""
                INSERT INTO {self.schema}.attempts(attempt_id, series_id, start_date,
                end_date, state, classification, batch_id) VALUES(%s, %s, %s, %s, %s,
                %s, %s)
                """,
                (uuid4(), sid, day(start), day(end), state, classification, bid),
            )

    def read(self, selected, start, end, *, as_of=None, basis="available"):
        series_ids(selected)
        dates(start, end)
        if basis not in {"available", "observed"}:
            raise FinancialError("financial_cutoff_basis_invalid")
        cutoff = instant(as_of or self.clock())
        s = self.schema
        with self.read_snapshot():
            values = self.connection.execute(
                f"""
                SELECT DISTINCT ON(r.series_id, r.observation_date) r.series_id,
                r.observation_date, r.revision, r.value, r.provider_extra, r.batch_id,
                r.observed_at, r.available_at, b.raw_locator, b.metadata_locator,
                b.origin, b.request, b.is_copyrighted FROM {s}.revisions r
                JOIN {s}.batches b USING(batch_id) WHERE
                r.series_id=ANY(%s) AND r.observation_date BETWEEN %s AND %s AND
                r.available_at IS NOT NULL AND r.{basis}_at<=%s ORDER BY r.series_id,
                r.observation_date, r.revision DESC
                """,
                (list(selected), day(start), day(end), cutoff),
            ).fetchall()
            batches = self.connection.execute(
                f"""
                SELECT series_id, batch_id, metadata,
                observed_at, available_at, start_date, end_date, origin FROM
                {s}.batches
                WHERE series_id=ANY(%s) AND available_at IS NOT NULL AND
                {basis}_at<=%s AND start_date<=%s AND end_date>=%s
                ORDER BY series_id, observed_at DESC, batch_id
                """,
                (list(selected), cutoff, day(end), day(start)),
            ).fetchall()
            attempts = self.connection.execute(
                f"""
                SELECT series_id, state, classification,
                observed_at, start_date, end_date FROM {s}.attempts WHERE
                series_id=ANY(%s) AND observed_at<=%s AND start_date<=%s AND
                end_date>=%s ORDER BY series_id, observed_at DESC, attempt_id
                """,
                (list(selected), cutoff, day(end), day(start)),
            ).fetchall()
            ranges = self.connection.execute(
                f"""SELECT series_id,start_date,end_date FROM {s}.batches
                WHERE series_id=ANY(%s) AND available_at IS NOT NULL
                AND {basis}_at<=%s AND start_date<=%s AND end_date>=%s""",
                (list(selected), cutoff, day(end), day(start)),
            ).fetchall()
            pending = self.connection.execute(
                f"""
                SELECT series_id, observation_date FROM {s}.revisions WHERE
                series_id=ANY(%s) AND observation_date BETWEEN %s AND %s AND
                available_at IS NULL AND observed_at<=%s
                """,
                (list(selected), day(start), day(end), cutoff),
            ).fetchall()
        return dict(
            values=values,
            batches=batches,
            attempts=attempts,
            ranges=ranges,
            pending=pending,
            cutoff=cutoff,
            basis=basis,
        )

    def close(self):
        self.connection.close()
