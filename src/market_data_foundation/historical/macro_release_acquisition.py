"""One selected official material per execution, separately from numeric success.

Only an explicit collection execution calls this module. GET does not. Durable
reservations/dedup/cooldown survive process restarts; there is no archive backlog.
"""

import fcntl
import http.client
import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

from market_data_foundation.historical.bls_web_transport import fetch_bls_page
from market_data_foundation.historical.cpi_timeseries import CpiTimeSeriesError
from market_data_foundation.historical.macro_release_material import (
    MONTHS,
    official_url,
    parse_material,
)
from market_data_foundation.historical.macro_release_records import (
    ReleaseRecords,
    instant,
)


def fetch_official_material(url, family):
    official_url(family, url)
    if family == "jolts":
        from market_data_foundation.historical.jolts_release_schedule import (
            fetch_official_page,
        )

        return fetch_official_page(url)
    if family in {"cpi", "ppi", "employment", "jolts"}:
        return fetch_bls_page(url, allowed_path=urlsplit(url).path, timeout=5)
    deadline = time.monotonic() + 20
    original = urlsplit(url)
    for hop in range(4):
        parts = official_url(family, url)
        if parts.path != original.path:
            raise CpiTimeSeriesError("release_redirect_rejected")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise CpiTimeSeriesError("release_transport_timeout")
        connection = http.client.HTTPSConnection(
            parts.hostname, timeout=min(5, remaining)
        )
        try:
            connection.request(
                "GET",
                parts.path,
                headers={"Accept-Encoding": "identity", "Connection": "close"},
            )
            response = connection.getresponse()
            if response.status in {301, 302, 303, 307, 308}:
                target = response.getheader("Location")
                if not target or hop == 3:
                    raise CpiTimeSeriesError("release_redirect_rejected")
                url = urljoin(url, target)
                official_url(family, url)
                continue
            if response.status != 200:
                raise CpiTimeSeriesError(f"release_http_{response.status}")
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise CpiTimeSeriesError("release_material_size_invalid")
            return raw
        finally:
            connection.close()
    raise CpiTimeSeriesError("release_redirect_rejected")


def select_material(family, snapshot, start, end, now):
    """Latest due, explicitly mapped calendar event inside the numeric target range."""
    targets = snapshot.get("targets", [])
    if isinstance(targets, dict):
        targets = [dict(v, reference_month=k) for k, v in targets.items()]
    calendar = snapshot.get("calendar", {})
    events = calendar.get("events", {})
    events = list(events.values()) if isinstance(events, dict) else events
    by_uid = {e["uid"]: e for e in events if e.get("uid")}

    def blocked(row):
        return (
            str(row.get("status", "")).lower() == "cancelled"
            or row.get("cancelled")
            or row.get("calendar_execution_blocked")
            or row.get("absent_from_latest_calendar")
            or row.get("publication_status") == "cancelled"
            or row.get("state") == "calendar_removed"
        )

    def references(row):
        return row.get("reference_months") or (
            [row["reference_month"]] if row.get("reference_month") else []
        )

    # Execution targets are current decisions, including removals/retimings.
    # They suppress older history for the same reference even when blocked.
    target_refs = {ref for t in targets for ref in references(t)}
    rows = list(targets)
    event_refs = {ref for e in events for ref in references(e)}
    for event in events:
        for ref in references(event):
            if ref in target_refs:
                continue
            mapping = event.get("reference_month_evidence") or {
                "source": event.get("source") or calendar.get("source"),
                "reference_month": ref,
            }
            if not mapping.get("source"):
                continue
            rows.append(
                dict(event, reference_month=ref, reference_month_evidence=mapping)
            )
    for history in calendar.get("display_history", []):
        ref = history.get("reference_month")
        if ref in target_refs or ref in event_refs:
            continue
        uid = (
            history.get("uid") or history.get("ics_uid") or history.get("calendar_uid")
        )
        current = by_uid.get(uid)
        if not (history.get("reference_month_evidence") or {}).get("source"):
            continue
        if not uid and any(
            e.get("release_at") == history.get("release_at") and blocked(e)
            for e in events
        ):
            continue
        if current and (
            blocked(current) or current.get("release_at") != history.get("release_at")
        ):
            continue
        rows.append(history)
    candidates = []
    for target in rows:
        reference = target.get("reference_month")
        at = target.get("release_at")
        if not reference or not start <= reference <= end or not at or blocked(target):
            continue
        uid = target.get("uid") or target.get("ics_uid") or target.get("calendar_uid")
        current = by_uid.get(uid)
        if current and (blocked(current) or current.get("release_at") != at):
            continue
        if instant(at) > now:
            continue
        mapping = target.get("reference_month_evidence") or {}
        source = mapping.get("source")
        if source:
            origin = urlsplit(source)
            expected_host = (
                "www.bea.gov"
                if family in {"pce", "consumption"}
                else "www.census.gov"
                if family == "retail"
                else "www.bls.gov"
            )
            if (
                origin.scheme != "https"
                or origin.netloc != expected_host
                or origin.query
                or origin.fragment
            ):
                continue
            if mapping.get("reference_month") not in {None, reference}:
                continue
            if mapping.get("release_at") and instant(mapping["release_at"]) != instant(
                at
            ):
                continue
        elif not target.get("uid"):
            continue
        day = instant(at).astimezone(ZoneInfo("America/New_York")).date()
        if family in {"cpi", "ppi", "employment", "jolts"}:
            stem = {"employment": "empsit"}.get(family, family)
            url = f"https://www.bls.gov/news.release/archives/{stem}_{day:%m%d%Y}.htm"
        elif family in {"pce", "consumption"}:
            title = target.get("summary") or (current or {}).get("summary")
            name = MONTHS[int(reference[5:]) - 1].lower()
            slug = (
                title.lower().replace(",", "").replace(" ", "-")
                if title
                else f"personal-income-and-outlays-{name}-{reference[:4]}"
            )
            url = (
                target.get("source_url")
                or f"https://www.bea.gov/news/{day.year}/{slug}"
            )
        else:
            url = "https://www.census.gov/retail/sales.html"
        official_url(family, url)
        candidates.append(
            dict(
                source_url=url,
                reference_month=reference,
                reference_months=references(target),
                calendar_uid=uid,
                source_date=day.isoformat(),
                selection_basis="latest_due_explicit_official_calendar_mapping",
            )
        )
    return max(
        candidates, key=lambda t: (t["source_date"], t["reference_month"]), default=None
    )


class MaterialAcquirer:
    def __init__(self, state_path, *, fetcher=None):
        self.path = Path(state_path)
        self.fetcher = fetcher or fetch_official_material

    def _save(self, state):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, "w") as output:
                json.dump(state, output, sort_keys=True)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def record_limitation(
        self,
        profile,
        selection,
        *,
        classification,
        observed_at,
        cooldown_until,
        evidence_locator,
    ):
        """Operator-only admission of an already qualified transport limitation.

        It performs no provider request. Existing successful material is retained.
        This lets an operator honor a no-reprobe qualification window after restart.
        """
        official_url(profile.key, selection["source_url"])
        if (
            not classification
            or not evidence_locator
            or instant(cooldown_until) <= instant(observed_at)
        ):
            raise CpiTimeSeriesError("release_limitation_evidence_invalid")
        key = "|".join(
            (
                profile.schema,
                profile.key,
                selection["source_date"],
                selection["source_url"],
            )
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state = json.loads(self.path.read_text()) if self.path.exists() else {}
            if state.get(key, {}).get("state") == "retained":
                return state[key]
            state[key] = dict(
                selection,
                state="partial",
                attempts=max(1, state.get(key, {}).get("attempts", 0)),
                reason=classification,
                attempted_at=instant(observed_at).isoformat(),
                next_attempt_at=instant(cooldown_until).isoformat(),
                qualification_evidence_locator=evidence_locator,
                basis="operator_imported_prior_transport_observation",
            )
            self._save(state)
            return state[key]

    def capture(self, store, selection, *, batch_ids=()):
        repo = ReleaseRecords(store)
        now = instant(repo.clock())
        base = dict(
            state="partial",
            material_outcomes=[],
            event_ids=[],
            evidence_ids=[],
            numeric_batch_ids=list(batch_ids),
            provider_requests=0,
            transport_calls=0,
        )
        if selection is None:
            return dict(base, reasons=["no_due_official_material_selected"])
        family = repo.profile.key
        official_url(family, selection["source_url"])
        prefix = {
            "employment": "bls:employment-situation",
            "cpi": "bls:cpi",
            "ppi": "bls:ppi",
            "jolts": "bls:jolts",
            "pce": "bea:personal-income-and-outlays",
            "consumption": "bea:personal-income-and-outlays",
            "retail": "census:advance-retail",
        }[family]
        identity = prefix + ":" + selection["source_date"]
        try:
            retained = [
                r
                for r in repo._rows(now)
                if r["event_id"] == identity
                and r["source_url"] == selection["source_url"]
            ]
        except CpiTimeSeriesError as error:
            if error.classification != "release_schema_not_prepared":
                raise
            retained = []
        if retained:
            base.update(
                event_ids=[identity],
                evidence_ids=[r["evidence_id"] for r in retained],
                evidence_origin="preexisting_local_material_not_this_transport_delivery",
            )
        acquired = [
            r
            for r in retained
            if r.get("retention", {}).get("acquisition_method")
            in {"bounded_official_transport", "shared_original_material_cache"}
        ]
        if acquired:
            return dict(
                base,
                state="retained",
                event_ids=[identity],
                evidence_ids=[r["evidence_id"] for r in acquired],
                evidence_origin=(
                    "previous_shared_original_material_cache"
                    if all(
                        r.get("retention", {}).get("acquisition_method")
                        == "shared_original_material_cache"
                        for r in acquired
                    )
                    else "previous_bounded_official_transport"
                ),
                material_outcomes=[
                    dict(
                        state="retained",
                        event_id=identity,
                        evidence_ids=[r["evidence_id"] for r in acquired],
                        deduplicated=True,
                        basis="previous_asset_scoped_material_acquisition",
                    )
                ],
                reasons=list(
                    dict.fromkeys(reason for r in acquired for reason in r["missing"])
                ),
            )
        key = "|".join(
            (repo.schema, family, selection["source_date"], selection["source_url"])
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.with_suffix(self.path.suffix + ".lock").open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return dict(base, reasons=["release_material_acquisition_busy"])
            try:
                state = json.loads(self.path.read_text()) if self.path.exists() else {}
            except ValueError, OSError:
                return dict(base, reasons=["release_material_state_unknown"])
            old = state.get(key, {})
            if old.get("state") == "retained":
                return dict(
                    base,
                    state="retained",
                    event_ids=[old["event_id"]],
                    evidence_ids=[old["evidence_id"]],
                    evidence_origin="previous_bounded_official_transport",
                    material_outcomes=[dict(old, deduplicated=True)],
                    reasons=old.get("missing", []),
                )
            if (
                old.get("attempts", 0) >= 3
                or old.get("next_attempt_at")
                and instant(old["next_attempt_at"]) > now
            ):
                return dict(
                    base,
                    reasons=[old.get("reason", "release_material_cooldown")],
                    material_outcomes=[old],
                )
            # Reserve before network. Interrupted attempts consume the budget.
            attempt = dict(
                selection,
                state="running",
                attempts=old.get("attempts", 0) + 1,
                attempted_at=now.isoformat(),
                next_attempt_at=(now + timedelta(hours=1)).isoformat(),
            )
            state[key] = attempt
            self._save(state)
            base.update(provider_requests=1, transport_calls=1)
            try:
                if family in {"pce", "consumption"}:
                    from market_data_foundation.historical.bea_shared_material import (
                        cached_material,
                    )

                    base.update(provider_requests=0, transport_calls=0)

                    def acquire_raw(url, asset):
                        base.update(provider_requests=1, transport_calls=1)
                        return self.fetcher(url, asset)

                    raw, fetched = cached_material(
                        self.path.parent / "bea-pio-original-material",
                        selection,
                        family,
                        acquire_raw,
                    )
                    base.update(
                        provider_requests=int(fetched), transport_calls=int(fetched)
                    )
                    attempt["raw_material_origin"] = (
                        "bounded_transport"
                        if fetched
                        else "shared_original_bytes_cache"
                    )
                else:
                    raw = self.fetcher(selection["source_url"], family)
                doc = parse_material(raw, selection["source_url"], family)
                if (
                    selection["reference_month"] not in doc["reference_months"]
                    or doc["source_version"]["value"] != selection["source_date"]
                ):
                    raise CpiTimeSeriesError(
                        "release_material_calendar_identity_mismatch"
                    )
                saved = repo.import_material(
                    raw,
                    selection["source_url"],
                    material_format="original_http_body",
                    acquisition_method=(
                        "shared_original_material_cache"
                        if family in {"pce", "consumption"} and not fetched
                        else "bounded_official_transport"
                    ),
                )
                attempt.update(
                    state="retained",
                    event_id=saved["event_id"],
                    evidence_id=saved["evidence_id"],
                    next_attempt_at=None,
                    reason=None,
                    missing=saved["missing"],
                )
                base.update(
                    state="retained",
                    event_ids=[saved["event_id"]],
                    evidence_ids=[saved["evidence_id"]],
                    evidence_origin=(
                        "current_shared_original_material_cache"
                        if family in {"pce", "consumption"} and not fetched
                        else "current_bounded_official_transport"
                    ),
                    reasons=saved["missing"],
                )
            except Exception as error:
                reason = getattr(
                    error,
                    "classification",
                    "release_material_transport_or_storage_failed",
                )
                attempt.update(
                    state="partial",
                    reason=reason,
                    next_attempt_at=(
                        now + timedelta(days=1)
                        if "403" in reason
                        else now + timedelta(hours=1)
                    ).isoformat(),
                )
                base["reasons"] = [reason]
            state[key] = attempt
            try:
                self._save(state)
            except OSError:
                base.update(
                    state="unknown",
                    reasons=["release_material_state_write_outcome_unknown"],
                )
            base["material_outcomes"] = [attempt]
            return base


def capture_for_execution(
    service,
    *,
    snapshot,
    start,
    end,
    state_path,
    numeric_result,
    fetcher=None,
):
    """Material failure preserves successfully persisted numeric data."""
    try:
        repo = ReleaseRecords(service.store)
        selection = select_material(
            repo.profile.key, snapshot, start, end, instant(repo.clock())
        )
        ids = sorted(
            {
                p["batch_id"]
                for p in numeric_result.get("points", [])
                if p.get("batch_id")
            }
        )
        if numeric_result.get("collection_id"):
            ids.append(numeric_result["collection_id"])
        return MaterialAcquirer(state_path, fetcher=fetcher).capture(
            service.store, selection, batch_ids=ids
        )
    except Exception as error:
        return dict(
            state="partial",
            reasons=[
                getattr(error, "classification", "release_material_acquisition_failed")
            ],
            event_ids=[],
            evidence_ids=[],
            material_outcomes=[],
            provider_requests=None,
            transport_calls=None,
        )
