"""Per-security directory receipts, separate from exchange lifecycle evidence."""

import json
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from market_data_foundation.canonical.model import instant, require
from market_data_foundation.historical.tdx_p0_roster import a_share

SOURCE_BINDING = "mdf.binding.tdx-v100.stock.roster"
REVISION = "directory-members-r1"


def receipt_time(value):
    parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
    instant(parsed)
    return parsed


def verify_step(step, identity, *, cutoff=None):
    """Replay every retained page and match the step's actual security records."""
    market = step["market"]
    require(
        market in {0, 1, 2} and step["state"] == "COMPLETE", "tdx_directory_incomplete"
    )
    rows, offset, count, times = {}, 0, None, []
    for proof in step["evidence"]:
        raw = Path(proof["path"]).read_bytes()
        require(sha256(raw).hexdigest() == proof["sha256"], "tdx_directory_digest")
        value = json.loads(raw)
        request, data = value["request"], value["source_projection"]
        require(request["market"] == market, "tdx_directory_market")
        observed = value["received_at"]
        parsed = receipt_time(observed)
        require(cutoff is None or parsed <= cutoff, "tdx_directory_after_cutoff")
        if request["operation"] == "GetCount":
            require(market < 2 and count is None, "tdx_directory_count")
            count = data["Count"]
            continue
        require(
            request["operation"] == ("GetBJDirectory" if market == 2 else "GetCode")
            and request["start"] == offset,
            "tdx_directory_page_scope",
        )
        items = data["List"] or []
        require(data["Count"] == len(items), "tdx_directory_count")
        times.append(observed)
        for item in items:
            code = item["Code"]
            if not a_share(market, code):
                continue
            require((market, code) not in rows, "tdx_directory_duplicate")
            rows[market, code] = dict(
                state="PRESENT",
                market=market,
                instrument=code,
                observed_at=observed,
                request_id=identity,
                source_binding=SOURCE_BINDING,
                name=item["Name"],
                path=proof["path"],
                sha256=proof["sha256"],
            )
        offset += len(items)
    require(bool(times) and step["offset"] == offset, "tdx_directory_page_scope")
    require(
        market == 2 or count == step["count"] == offset, "tdx_directory_total_drift"
    )
    retained = {
        (m["market"], m["instrument"]): (
            m["name"],
            m["evidence"]["path"],
            m["evidence"]["sha256"],
        )
        for m in step["members"]
    }
    require(len(retained) == len(step["members"]), "tdx_directory_duplicate")
    require(
        retained == {k: (v["name"], v["path"], v["sha256"]) for k, v in rows.items()},
        "tdx_directory_members_drift",
    )
    return rows, dict(
        market=market,
        observed_at=max(times, key=receipt_time),
        request_id=identity,
        source_binding=SOURCE_BINDING,
        state="COMPLETE",
        directory_count=offset,
        members=len(rows),
        evidence=step["evidence"],
    )


def verify_steps(record, *, cutoff=None):
    require(record["command"]["action"] == "universe", "tdx_directory_job_action")
    require(set(record["command"]["markets"]) == {0, 1, 2}, "tdx_directory_incomplete")
    require(
        len(record["steps"]) == 3
        and {s["market"] for s in record["steps"]} == {0, 1, 2},
        "tdx_directory_incomplete",
    )
    rows, scopes = {}, {}
    for step in record["steps"]:
        members, scope = verify_step(step, record["request_id"], cutoff=cutoff)
        rows.update(members)
        scopes[step["market"]] = scope
    return rows, scopes


def absence(scope, instrument=None):
    return {
        k: scope[k] for k in ("observed_at", "request_id", "source_binding", "market")
    } | dict(state="ABSENT", instrument=instrument)


def publication_verification(snapshot, jobs, cutoff):
    """Read stored receipts, or qualify an old snapshot through its original job."""
    from market_data_foundation.historical.tdx_p0_source import TdxError, classify

    details = (snapshot or {}).get("details", {})
    producer = details.get("producer", {})
    if not isinstance(producer, dict):
        producer = {}
    rows, scopes = {}, {}
    try:
        require(snapshot is not None, "tdx_directory_unavailable")
        cutoff = min(cutoff, receipt_time(snapshot["available_at"]))
        if (
            "producer" not in details
            and details.get("directory_verification", {}).get("revision") != REVISION
        ):
            require(jobs is not None, "tdx_directory_job_unavailable")
            # Old publications did not persist producer. Associate only the
            # exact immutable output, never the newest refresh or a dated slot.
            errors = []
            candidates = [
                row
                for row in jobs.summaries(read_errors=errors, action="universe")
                if row.get("snapshot_id") == snapshot["snapshot_id"]
                and row.get("state") == "COMPLETE"
            ]
            require(len(candidates) < 2, "tdx_directory_job_ambiguous")
            require(
                bool(candidates),
                "tdx_directory_job_unreadable"
                if errors
                else "tdx_directory_job_unbound",
            )
            producer = dict(
                source_binding=SOURCE_BINDING, request_id=candidates[0]["request_id"]
            )
        require(
            producer.get("source_binding") == SOURCE_BINDING
            and bool(producer.get("request_id")),
            "tdx_directory_job_unbound",
        )
        if details.get("directory_verification", {}).get("revision") == REVISION:
            require(
                len(details["directory_verification"]["markets"]) == 3,
                "tdx_directory_incomplete",
            )
            for scope in details["directory_verification"]["markets"]:
                require(
                    scope["state"] == "COMPLETE"
                    and scope["request_id"] == producer["request_id"]
                    and scope["source_binding"] == SOURCE_BINDING,
                    "tdx_directory_receipt_identity",
                )
                require(
                    receipt_time(scope["observed_at"]) <= cutoff,
                    "tdx_directory_after_cutoff",
                )
                scopes[scope["market"]] = scope
            require(set(scopes) == {0, 1, 2}, "tdx_directory_incomplete")
            for member in [
                *snapshot["members"],
                *details.get("directory_only_candidates", []),
                *details.get("directory_missing_candidates", []),
            ]:
                proof = member.get("evidence", {}).get("directory_verification")
                require(isinstance(proof, dict), "tdx_directory_receipt_missing")
                if proof is not None:
                    require(
                        proof["request_id"] == producer["request_id"]
                        and proof["source_binding"] == SOURCE_BINDING
                        and proof["state"] in {"PRESENT", "ABSENT"},
                        "tdx_directory_receipt_identity",
                    )
                    require(
                        proof["market"] == member["market"]
                        and proof["instrument"] == member["instrument"],
                        "tdx_directory_receipt_identity",
                    )
                    if proof["state"] == "PRESENT":
                        require(
                            all(
                                isinstance(proof[k], str) and proof[k]
                                for k in ("name", "path", "sha256")
                            ),
                            "tdx_directory_receipt_missing",
                        )
                    require(
                        receipt_time(proof["observed_at"]) <= cutoff,
                        "tdx_directory_after_cutoff",
                    )
                    rows[member["market"], member["instrument"]] = proof
            require(
                all(
                    sum(
                        v["state"] == "PRESENT"
                        for (market, _), v in rows.items()
                        if market == m
                    )
                    == scope["members"]
                    for m, scope in scopes.items()
                ),
                "tdx_directory_members_drift",
            )
            method = "published_receipts"
        else:
            require(jobs is not None, "tdx_directory_job_unavailable")
            record = jobs.read(producer["request_id"])
            require(
                record["request_id"] == producer["request_id"]
                and record["snapshot_id"] == snapshot["snapshot_id"]
                and record["state"] == "COMPLETE",
                "tdx_directory_job_identity",
            )
            require(
                not producer.get("origin")
                or record.get("origin") == producer["origin"],
                "tdx_directory_job_identity",
            )
            require(
                not producer.get("day") or producer["day"] == record["command"]["end"],
                "tdx_directory_job_identity",
            )
            rows, scopes = verify_steps(record, cutoff=cutoff)
            method = "retained_job_steps"
        return (
            rows,
            scopes,
            dict(state="VERIFIED", method=method, request_id=producer["request_id"]),
        )
    except (
        OSError,
        ValueError,
        KeyError,
        TypeError,
        AttributeError,
        TdxError,
    ) as error:
        # Keep original published rows readable; missing/corrupt evidence cannot
        # turn a former successful refresh into a newly verified membership.
        return (
            {},
            {},
            dict(
                state="UNVERIFIED",
                request_id=producer.get("request_id"),
                classification="tdx_directory_verification_unavailable",
                cause_classification=classify(error),
            ),
        )
