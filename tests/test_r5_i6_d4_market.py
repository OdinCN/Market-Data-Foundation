"""D4 offline product tests: exact identities, completeness, costs, cursor faults."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest

from market_data_foundation.canonical.market_consumer_read import (
    Binding,
    CursorCodec,
    Item,
    Limits,
    MarketReader,
    Query,
    ReadError,
)
from market_data_foundation.canonical.market_consumer_read.contracts import (
    WIRE,
    Budget,
    Publication,
    Slot,
    Version,
    byte_digest,
    digest,
    encode,
    public,
    query_subjects,
    strict_json,
    version_subjects,
)
from market_data_foundation.canonical.market_consumer_read.cursor import b64, unb64
from market_data_foundation.canonical.market_consumer_read.synthetic import (
    SyntheticProjection,
)
from market_data_foundation.canonical.market_consumer_wire import parse_body, read_body
from market_data_foundation.canonical.model import Key, Kind
from market_data_foundation.domain_contracts.market_universe import InstrumentIdentity
from market_data_foundation.web.market_api import PATH, create_app

NOW = datetime(2026, 10, 22, 8, tzinfo=UTC)
DAY = date(2026, 9, 1)
DEF = Key(Kind.DEFINITION, "synthetic.daily", "r1")
INST = InstrumentIdentity("synthetic:SH", "000001")
BINDING = Binding("synthetic-ledger", "d4-port-r1", "d4-selection-r1", "g1")
SEMANTICS = (("economic_units", "UNKNOWN"), ("mode", "raw_native"))


def event(
    key, n=0, decision="PUBLISH", at=None, previous=None, usable=None, effective=None
):
    at = at or NOW - timedelta(days=2) + timedelta(seconds=n)
    return Publication(
        Key(Kind.PUBLICATION, f"{key.identity}-p{n}"),
        key,
        f"o{n}",
        previous,
        decision,
        at,
        usable or at,
        effective,
        "synthetic-rule-r1",
        ("synthetic-evidence",),
        digest((key, n)),
    )


def seed(p, name="v1", instrument=INST, start=DAY, days=5, published=True, rows=None):
    k = Key(Kind.VERSION, name)
    source = Key(Kind.SOURCE, "synthetic-source", "r1")
    binding = Key(Kind.BINDING, "synthetic-binding", "r1")
    acquisition = Key(Kind.ACQUISITION, f"a-{name}")
    material = Key(Kind.MATERIAL, f"m-{name}")
    refs = (source, binding, acquisition, material)
    lineage = encode(
        [
            {"key": public(ref), "record_digest": digest(ref), "references": []}
            for ref in refs
        ]
    )
    if rows is None:
        rows = [
            {
                "day": (start + timedelta(days=n)).isoformat(),
                "classification": "MARKET_CLOSED" if n == 2 else "OBSERVED_VALID",
                "raw": {} if n == 2 else {"Volume": n + 100, "Code": "000001"},
                "evidence": ["synthetic-not-calendar-authority"],
            }
            for n in range(days)
        ]
    content = encode(rows)
    v = Version(
        k,
        DEF,
        instrument,
        start,
        start + timedelta(days=days),
        byte_digest(content),
        len(content),
        refs,
        byte_digest(lineage),
        len(lineage),
        source,
        binding,
        "synthetic-mapping-r1",
        "synthetic-calendar-r1",
        None,
        SEMANTICS,
        digest((name, "canonical")),
        digest((DEF, instrument, start, start + timedelta(days=days))),
    )
    p.seed_version(v, content, lineage, (event(k),) if published else ())
    return v


def query(items=None, **kwargs):
    return Query(
        DEF,
        items or (Item("a", INST, DAY, DAY + timedelta(days=5)),),
        page_size=2,
        semantics=SEMANTICS,
        **kwargs,
    )


def setup(q=None, limits=Limits()):
    q = q or query()
    p = SyntheticProjection(NOW, BINDING)
    v = seed(p)
    p.seed_admission(q)
    codec = CursorCodec(
        "synthetic-audience", "k1", {"k1": b"synthetic-only-value-32-bytes-long"}
    )
    reader = MarketReader(p, BINDING, codec, lambda: p.now, limits)
    return reader, p, v, q


def error(code, call):
    with pytest.raises(ReadError) as exc:
        call()
    assert exc.value.code == code
    assert str(exc.value) == code
    return exc.value


def wire(q):
    return {
        "query": {
            "wire_revision": WIRE,
            "definition": {
                "identity": q.definition.identity,
                "revision": q.definition.revision,
            },
            "items": [
                {
                    "item_id": i.item_id,
                    "instrument": public(i.instrument),
                    "start_date": i.start_date.isoformat(),
                    "end_date_exclusive": i.end_date_exclusive.isoformat(),
                    **(
                        {
                            "exact_versions": [
                                {"identity": k.identity} for k in i.exact_versions
                            ]
                        }
                        if q.mode == "exact_versions"
                        else {}
                    ),
                }
                for i in q.items
            ],
            "selection": {"mode": q.mode, **({"at": q.at.isoformat()} if q.at else {})},
            "page_size": q.page_size,
            "semantics": dict(q.semantics),
        }
    }


def all_pages(reader, q):
    pages, cursor = [], None
    while True:
        page = reader.read(q, cursor)
        pages.append(page)
        cursor = page["next_cursor"]
        if cursor is None:
            return pages


def test_natural_grid_paging_replay_and_single_batch_equivalence():
    r, p, v, q = setup()
    pages = all_pages(r, q)
    assert [page["range"] for page in pages] == [[0, 2], [2, 4], [4, 5]]
    units = [u for page in pages for u in page["units"]]
    assert [u["local_date"] for u in units] == [
        d.isoformat() for d in q.items[0].days()
    ]
    assert units[2]["classification"] == "MARKET_CLOSED"
    assert units[2]["raw"] == {}
    assert units[0]["raw"] == {"Code": "000001", "Volume": 100}
    assert all(u["version"] == public(v.key) for u in units)
    replay = r.read(q, pages[0]["next_cursor"])
    assert replay == pages[1]
    assert len(pages[0]["next_cursor"]) < 2048
    p.now += timedelta(seconds=1)
    assert r.read(q, pages[0]["next_cursor"]) == pages[1]
    batch = replace(
        q, items=(q.items[0], replace(q.items[0], item_id="b")), page_size=1000
    )
    full = r.read(batch)
    assert full["units"][:5] == units
    assert [u["raw"] for u in full["units"][5:]] == [u["raw"] for u in units]
    assert p.views_opened == p.views_closed


def test_asof_ambiguity_unpublished_candidate_and_exact_independence():
    r, p, v, q = setup()
    other = seed(p, "v2", published=False)
    p.seed_admission(q)
    assert r.read(q)["items"][0]["status"] == "VERIFIED"
    p.seed_history(other.key, (event(other.key),))
    response = r.read(q)
    assert response["status"] == "PARTIAL_FAILURE"
    assert response["items"][0]["status"] == "AMBIGUOUS"
    assert response["units"] == []
    for selected in (v, other):
        exact = replace(
            q,
            mode="exact_versions",
            items=(replace(q.items[0], exact_versions=(selected.key,)),),
        )
        assert r.read(exact)["units"][0]["version"] == public(selected.key)


def test_namespace_and_item_order_are_identity_not_bare_code():
    r, p, _, q = setup()
    inst2 = InstrumentIdentity("synthetic:SZ", "000001")
    v2 = seed(p, "sz", instrument=inst2)
    q = replace(
        q,
        items=(
            Item("z", inst2, DAY, DAY + timedelta(days=1)),
            Item("a", INST, DAY, DAY + timedelta(days=1)),
        ),
    )
    p.seed_admission(q)
    response = r.read(q)
    assert [u["item_id"] for u in response["units"]] == ["z", "a"]
    assert response["units"][0]["version"] == public(v2.key)


def test_uncovered_is_not_empty_and_explicit_empty_month_has_daily_unknown():
    r, p, _, q = setup()
    october = replace(
        q.items[0], start_date=date(2026, 10, 1), end_date_exclusive=date(2026, 10, 4)
    )
    q = replace(q, items=(october,))
    error("INDEX_NOT_COVERED", lambda: r.read(q))
    p.seed_month(DEF, INST, date(2026, 10, 1))
    response = r.read(q)
    assert response["items"][0]["coverage"] == "UNKNOWN"
    assert response["total_units"] == 3
    assert all(u["version"] is None for u in response["units"])


@pytest.mark.parametrize(
    "fault",
    [
        "count",
        "digest",
        "missing_day",
        "missing_candidate",
        "duplicate",
        "canonical",
        "history",
        "history_digest",
        "reverse",
    ],
)
def test_incomplete_or_inconsistent_projection_is_request_failure(fault):
    r, p, v, q = setup()
    month_key = (DEF, INST, DAY)
    directory = p.month_index[month_key]
    if fault in {"count", "digest"}:
        first = directory.days[0]
        summary = replace(
            first[1], **({"count": 0} if fault == "count" else {"digest": "0" * 64})
        )
        p.month_index[month_key] = replace(
            directory, days=((DAY, summary),) + directory.days[1:]
        )
    elif fault == "missing_day":
        p.month_index[month_key] = replace(directory, days=directory.days[:-1])
    elif fault in {"missing_candidate", "duplicate"}:
        p.slot_index[Slot(DEF, INST, DAY)] = (
            () if fault == "missing_candidate" else (v.key, v.key)
        )
    elif fault == "canonical":
        p.canonical_version_index[v.key] = replace(v, canonical_digest="bad")
    elif fault == "history":
        p.history_index[v.key] = ()
    elif fault == "history_digest":
        p.guard_index[v.key] = replace(p.guard_index[v.key], history_digest="bad")
    else:
        q = replace(
            q,
            mode="exact_versions",
            items=(replace(q.items[0], exact_versions=(v.key,)),),
        )
        p.reverse_index[v.key] = p.reverse_index[v.key][:-1]
    error("INDEX_INCONSISTENT", lambda: r.read(q))
    assert p.views_opened == p.views_closed


@pytest.mark.parametrize(
    "fault",
    ["predecessor", "transition", "time", "occurrence", "canonical_publication"],
)
def test_publication_history_faults_are_never_consumed(fault):
    r, p, v, q = setup()
    first = p.history_index[v.key][0]
    second = event(v.key, 1, "WITHDRAW", previous=first.key)
    if fault == "predecessor":
        second = replace(second, predecessor=None)
    elif fault == "transition":
        second = replace(second, decision="PUBLISH")
    elif fault == "time":
        second = replace(second, usable_at=first.decision_at)
    elif fault == "occurrence":
        second = replace(second, occurrence=first.occurrence)
    p.seed_history(v.key, (first, second))
    if fault == "canonical_publication":
        p.canonical_publication_index.pop(second.key)
        error("INDEX_INCONSISTENT", lambda: r.read(q))
    else:
        response = r.read(q)
        assert response["items"][0]["status"] == "PUBLICATION_STATE_INVALID"
        assert response["units"] == []


def test_publication_decision_usable_effective_and_current_gate():
    r, p, v, q = setup()
    first = p.history_index[v.key][0]
    # Known decision, not yet usable at A.
    first = replace(first, usable_at=NOW - timedelta(hours=1))
    p.seed_history(v.key, (first,))
    past = replace(q, at=NOW - timedelta(hours=2))
    assert r.read(past)["units"][0]["publication"] == "UNPUBLISHED"
    withdrawal = event(
        v.key,
        1,
        "WITHDRAW",
        at=NOW - timedelta(minutes=30),
        previous=first.key,
        effective=NOW + timedelta(minutes=1),
    )
    p.seed_history(v.key, (first, withdrawal))
    historical = replace(q, at=NOW - timedelta(minutes=45))
    first_page = r.read(historical)
    assert first_page["units"]
    p.now += timedelta(minutes=2)
    error(
        "CURSOR_QUALIFICATION_CHANGED",
        lambda: r.read(historical, first_page["next_cursor"]),
    )
    fresh = r.read(historical)
    assert fresh["items"][0]["status"] == "CURRENT_PUBLICATION_UNAVAILABLE"
    assert fresh["units"] == []


@pytest.mark.parametrize(
    "mutation",
    [
        "candidate_after_a",
        "publication_after_a",
        "withdraw_restore",
        "qualification",
        "policy_restore",
        "generation",
        "content",
        "late_restore",
    ],
)
def test_related_mutation_invalidates_entire_page(mutation):
    r, p, v, q = setup()
    first = r.read(q)
    if mutation == "candidate_after_a":
        seed(p, "later", published=False)
        p.seed_admission(q)
    elif mutation in {"publication_after_a", "withdraw_restore", "late_restore"}:
        original = p.history_index[v.key][0]
        at = (
            NOW + timedelta(seconds=1)
            if mutation == "publication_after_a"
            else NOW - timedelta(hours=1)
        )
        withdrawn = event(v.key, 1, "WITHDRAW", at=at, previous=original.key)
        history = [original, withdrawn]
        if mutation != "publication_after_a":
            history.append(
                event(
                    v.key,
                    2,
                    "RESTORE",
                    at=at + timedelta(seconds=1),
                    previous=withdrawn.key,
                )
            )
        p.seed_history(v.key, history)
    elif mutation in {"qualification", "policy_restore"}:
        s = version_subjects(v)[-1]
        old = p.admission_index[s]
        p.admission_index[s] = replace(
            old,
            status="UNQUALIFIED" if mutation == "qualification" else "QUALIFIED",
            change_no=old.change_no + 2,
            revision="r2",
        )
    elif mutation == "generation":
        p.binding = replace(p.binding, generation="g2")
    else:
        p.content_index[v.key] = p.content_index[v.key].replace(b"100", b"999")
    with pytest.raises(ReadError) as exc:
        r.read(q, first["next_cursor"])
    assert exc.value.code in {
        "CURSOR_SELECTION_CHANGED",
        "CURSOR_QUALIFICATION_CHANGED",
        "CURSOR_GENERATION_CHANGED",
    }
    assert p.views_opened == p.views_closed


def test_unrelated_version_and_publication_do_not_invalidate_cursor():
    r, p, _, q = setup()
    first = r.read(q)
    expected = r.read(q, first["next_cursor"])
    seed(p, "unrelated", instrument=InstrumentIdentity("elsewhere", "000001"))
    p.seed_admission(q)
    assert r.read(q, first["next_cursor"]) == expected


@pytest.mark.parametrize(
    "axis",
    [
        "definition",
        "units",
        "version",
        "source",
        "binding",
        "mapping",
        "calendar",
        "retention",
    ],
)
def test_each_current_admission_axis_fails_closed(axis):
    r, p, _, q = setup()
    target = next(s for s in p.admission_index if s.startswith(axis + ":"))
    p.admission_index[target] = replace(p.admission_index[target], status="UNKNOWN")
    result = r.read(q)
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["items"][0]["status"] == "QUALIFICATION_FAILED"
    assert result["items"][0]["qualification"] == "UNKNOWN"
    assert result["units"] == []


@pytest.mark.parametrize(
    "fault",
    [
        "digest",
        "missing_day",
        "duplicate",
        "raw_on_closed",
        "no_evidence",
        "unknown_field",
        "private_locator",
        "lineage_digest",
        "lineage_reference",
    ],
)
def test_content_and_lineage_faults_cannot_return_bars(fault):
    r, p, v, q = setup()
    rows = strict_json(p.content_index[v.key])
    lineage = strict_json(p.lineage_index[v.key])
    if fault == "digest":
        p.content_index[v.key] = b"bad"
    elif fault == "lineage_digest":
        p.lineage_index[v.key] = b"bad"
    elif fault in {"private_locator", "lineage_reference"}:
        if fault == "private_locator":
            lineage[0]["locator"] = "private-never-return"
        else:
            lineage[0]["references"] = [public(Key(Kind.MATERIAL, "missing"))]
        raw = encode(lineage)
        p.lineage_index[v.key] = raw
        v = replace(v, lineage_digest=byte_digest(raw), lineage_length=len(raw))
    else:
        if fault == "missing_day":
            rows.pop()
        elif fault == "duplicate":
            rows[1] = rows[0]
        elif fault == "raw_on_closed":
            rows[2]["raw"] = {"Volume": 1}
        elif fault == "no_evidence":
            rows[0]["evidence"] = []
        else:
            rows[0]["economic_invention"] = "not allowed"
        raw = encode(rows)
        p.content_index[v.key] = raw
        v = replace(v, content_digest=byte_digest(raw), content_length=len(raw))
    p.version_index[v.key] = p.canonical_version_index[v.key] = v
    p.seed_month(DEF, INST, DAY)
    result = r.read(q)
    assert result["status"] == "PARTIAL_FAILURE"
    assert result["units"] == []
    assert "private-never-return" not in encode(result).decode()


@pytest.mark.parametrize("field", ["classification", "day"])
@pytest.mark.parametrize("mode", ["as_of", "exact_versions"])
def test_unhashable_content_field_preserves_healthy_batch_item(field, mode):
    r, p, healthy, q = setup()
    q = replace(q, page_size=1000)
    expected = r.read(q)["units"]
    malformed = seed(p, "malformed", instrument=InstrumentIdentity("bad", "000001"))
    rows = strict_json(p.content_index[malformed.key])
    rows[0][field] = []
    raw = encode(rows)
    malformed = replace(
        malformed, content_digest=byte_digest(raw), content_length=len(raw)
    )
    p.content_index[malformed.key] = raw
    p.version_index[malformed.key] = malformed
    p.canonical_version_index[malformed.key] = malformed
    p.seed_month(DEF, malformed.instrument, DAY)
    q = replace(
        q,
        mode=mode,
        items=(
            replace(
                q.items[0],
                exact_versions=(healthy.key,) if mode == "exact_versions" else (),
            ),
            replace(
                q.items[0],
                item_id="malformed",
                instrument=malformed.instrument,
                exact_versions=(malformed.key,) if mode == "exact_versions" else (),
            ),
        ),
    )
    p.seed_admission(q)
    head, result = asgi_request(create_app(r), encode(wire(q)))
    assert head["status"] == 200
    assert result["status"] == "PARTIAL_FAILURE"
    assert [item["status"] for item in result["items"]] == [
        "VERIFIED",
        "INTEGRITY_FAILURE",
    ]
    assert result["units"] == expected
    assert result["total_units"] == len(expected)
    assert result["next_cursor"] is None
    assert p.views_opened == p.views_closed


def test_bad_item_does_not_hide_good_item_and_exact_overlap_gap_scope():
    r, p, v, q = setup()
    other = seed(p, "other", instrument=InstrumentIdentity("elsewhere", "000001"))
    q = replace(
        q,
        mode="exact_versions",
        items=(
            replace(q.items[0], exact_versions=(v.key,)),
            replace(q.items[0], item_id="wrong", exact_versions=(other.key,)),
        ),
    )
    p.seed_admission(q)
    result = r.read(q)
    assert result["status"] == "PARTIAL_FAILURE"
    assert [s["status"] for s in result["items"]] == [
        "VERIFIED",
        "VERSION_SCOPE_MISMATCH",
    ]
    assert all(u["item_id"] == "a" for u in result["units"])
    overlapping = seed(p, "overlap")
    p.seed_admission(q)
    overlap = replace(
        q, items=(replace(q.items[0], exact_versions=(v.key, overlapping.key)),)
    )
    assert r.read(overlap)["items"][0]["status"] == "EXACT_OVERLAP"
    gap = replace(
        q, items=(replace(q.items[0], end_date_exclusive=DAY + timedelta(days=6)),)
    )
    result = r.read(gap)
    assert result["total_units"] == 6
    assert result["items"][0]["coverage"] == "UNKNOWN"


@pytest.mark.parametrize(
    "fault",
    [
        "signature",
        "unknown_kid",
        "audience",
        "query",
        "expired",
        "position",
        "last_key",
        "unknown_field",
        "oversize",
        "noncanonical",
        "duplicate_key",
    ],
)
def test_cursor_validation_and_position_faults(fault):
    r, p, _, q = setup()
    token = r.read(q)["next_cursor"]
    opened = p.views_opened
    payload = strict_json(unb64(token.split(".")[0]))
    expected = "INVALID_CURSOR"
    if fault == "signature":
        token = token[:-3] + "abc"
    elif fault == "unknown_kid":
        r.cursor.keys.clear()
        r.cursor.keys["k1"] = b"another-synthetic-key-of-32-bytes-long"
    elif fault == "audience":
        r.cursor = replace(r.cursor, audience="other")
    elif fault == "query":
        q = replace(q, page_size=3)
    elif fault == "expired":
        p.now += timedelta(minutes=16)
        expected = "CURSOR_EXPIRED"
    elif fault in {"position", "last_key", "unknown_field"}:
        if fault == "position":
            payload["pos"] = 4
        elif fault == "last_key":
            payload["last"] = [99, "2026-09-02"]
        else:
            payload["extra"] = True
        token = r.cursor.sign(payload, r.limits)
        if fault != "unknown_field":
            expected = "INVALID_CURSOR_POSITION"
    elif fault == "oversize":
        token = "a" * 4097
        expected = "CURSOR_TOO_LARGE"
    else:
        raw = unb64(token.split(".")[0])
        raw = b" " + raw if fault == "noncanonical" else raw[:-1] + b',"v":1}'
        import hmac

        token = b64(raw) + "." + b64(hmac.digest(r.cursor.keys["k1"], raw, "sha256"))
    error(expected, lambda: r.read(q, token))
    if fault not in {"position", "last_key"}:
        assert p.views_opened == opened


def test_key_rotation_retains_verification_and_expiry_without_sliding():
    r, p, _, q = setup()
    token = r.read(q)["next_cursor"]
    first = strict_json(unb64(token.split(".")[0]))
    r.cursor.keys["k2"] = b"second-synthetic-key-32-bytes-long"
    r.cursor = replace(r.cursor, issuer="k2")
    p.now += timedelta(minutes=4)
    page = r.read(q, token)
    next_payload = strict_json(unb64(page["next_cursor"].split(".")[0]))
    assert next_payload["kid"] == "k2"
    assert next_payload["exp"] == first["exp"]
    del r.cursor.keys["k1"]
    error("INVALID_CURSOR", lambda: r.read(q, token))


@pytest.mark.parametrize(
    "dimension,limits",
    [
        ("items", Limits(items=1)),
        ("days", Limits(days=4)),
        ("logical_slots", Limits(slots=4)),
        ("candidate_versions", Limits(versions=1)),
        ("candidate_references", Limits(candidate_refs=4)),
        ("events", Limits(events=1)),
        ("content_bytes", Limits(content=10)),
        ("metadata", Limits(metadata=100)),
        ("version_content", Limits(version_content=10)),
        ("version_lineage", Limits(lineage_per_version=3)),
        ("lineage_references", Limits(lineage=3)),
        ("logical_unit", Limits(unit=100)),
        ("fixed_metadata", Limits(fixed=100)),
        ("logical_result", Limits(logical=100)),
        ("page_bytes", Limits(page=100)),
        ("statements", Limits(calls=3)),
    ],
)
def test_cumulative_budget_dimensions(dimension, limits):
    r, p, v, q = setup(limits=limits)
    if dimension == "items":
        q = replace(q, items=(q.items[0], replace(q.items[0], item_id="b")))
    elif dimension == "candidate_versions":
        seed(p, "second", published=False)
        p.seed_admission(q)
    elif dimension == "events":
        first = p.history_index[v.key][0]
        p.seed_history(v.key, (first, event(v.key, 1, "WITHDRAW", previous=first.key)))
    exc = error("READ_CAPACITY_EXCEEDED", lambda: r.read(q))
    assert exc.dimension == dimension
    assert exc.status == 422


@pytest.mark.parametrize(
    "phase,limits,elapsed,dimension",
    [
        ("directories", Limits(), 1.1, "statement_time"),
        ("contents", Limits(), 1.1, "statement_time"),
        ("statement", Limits(transaction_seconds=0.5), 0.6, "transaction_time"),
        ("page", Limits(transaction_seconds=0.5), 0.6, "transaction_time"),
        ("directories", Limits(request_seconds=0.5), 0.6, "request_time"),
        ("view_exit", Limits(), 8.1, "request_time"),
    ],
)
def test_elapsed_timeout_is_503_without_page(
    monkeypatch, phase, limits, elapsed, dimension
):
    from contextlib import contextmanager

    r, p, _, q = setup(limits=limits)
    ticks = [0.0]
    r.timer = lambda: ticks[0]
    if phase == "view_exit":
        original_view = p.read_view

        @contextmanager
        def delayed_exit(budget):
            with original_view(budget) as view:
                yield view
            ticks[0] += elapsed

        monkeypatch.setattr(p, "read_view", delayed_exit)
    else:
        target, name = (r, "_page") if phase == "page" else (p, phase)
        if phase == "statement":
            target, name = p, "directories"
        original = getattr(target, name)

        def delayed(*args):
            if phase == "statement":
                ticks[0] += elapsed
            result = original(*args)
            if phase != "statement":
                ticks[0] += elapsed
            return result

        monkeypatch.setattr(target, name, delayed)
    head, response = asgi_request(create_app(r), encode(wire(q)))
    assert head["status"] == 503
    assert response == {
        "wire_revision": WIRE,
        "status": "ERROR",
        "error": "READ_TIMEOUT",
        "dimension": dimension,
    }
    assert p.views_opened == p.views_closed == 1


def test_overflow_asof_rejected_before_candidates_but_exact_can_read():
    r, p, v, q = setup()
    for n in range(64):
        seed(p, f"overflow-{n}", published=False)
    p.seed_admission(q)
    p.lookups.clear()
    error("READ_CAPACITY_EXCEEDED", lambda: r.read(q))
    assert not any(name == "candidates" for name, _ in p.lookups)
    q = replace(
        q, mode="exact_versions", items=(replace(q.items[0], exact_versions=(v.key,)),)
    )
    assert r.read(q)["units"]
    assert not any(name == "candidates" for name, _ in p.lookups)


def test_history_capacity_rejected_before_history_fetch():
    r, p, v, q = setup()
    p.guard_index[v.key] = replace(p.guard_index[v.key], history_count=65)
    p.lookups.clear()
    exc = error("READ_CAPACITY_EXCEEDED", lambda: r.read(q))
    assert exc.dimension == "version_history"
    assert not any(name == "histories" for name, _ in p.lookups)


def test_page_bytes_select_maximal_prefix_and_always_make_progress():
    r, _, _, q = setup()
    q = replace(q, page_size=5)
    original = r.read(q)
    r.limits = replace(Limits(), page=8000)
    pages = all_pages(r, q)
    assert [u for p in pages for u in p["units"]] == original["units"]
    assert all(len(encode(p)) <= 8000 and p["range"][1] > p["range"][0] for p in pages)


@pytest.mark.parametrize(
    "mutation",
    [
        "unknown",
        "duplicate",
        "naive",
        "nonutc",
        "null_at",
        "reversed",
        "duplicate_item",
        "float_page",
        "extra_exact",
        "nan",
        "nested",
        "oversize",
    ],
)
def test_wire_negative_cases(mutation):
    r, p, _, q = setup()
    body = wire(q)
    query_body = body["query"]
    if mutation == "unknown":
        query_body["fallback_provider"] = "forbidden"
    elif mutation in {"naive", "nonutc", "null_at"}:
        query_body["selection"]["at"] = {
            "naive": "2026-01-01T00:00:00",
            "nonutc": "2026-01-01T00:00:00+08:00",
            "null_at": None,
        }[mutation]
    elif mutation == "reversed":
        query_body["items"][0]["end_date_exclusive"] = "2026-08-01"
    elif mutation == "duplicate_item":
        query_body["items"].append(query_body["items"][0])
    elif mutation == "float_page":
        query_body["page_size"] = 2.0
    elif mutation == "extra_exact":
        query_body["items"][0]["exact_versions"] = []
    raw = encode(body)
    if mutation == "duplicate":
        raw = raw[:-1] + b',"query":{}}'
    elif mutation == "nan":
        raw = b'{"query":NaN}'
    elif mutation == "nested":
        raw = b"[" * 2000 + b"]" * 2000
    elif mutation == "oversize":
        raw = b" " * (72 * 1024 + 1)
    status, result = read_body(r, raw)
    assert status in {400, 413}
    assert strict_json(result)["status"] == "ERROR"
    assert "units" not in strict_json(result)
    assert p.views_opened == 0


def asgi_request(
    app,
    body=b"",
    method="POST",
    content_type=b"application/json",
    query_string=b"",
    extra_headers=(),
):
    import asyncio

    messages = []
    chunks = [body[:10], body[10:]]

    async def receive():
        part = chunks.pop(0)
        return {"type": "http.request", "body": part, "more_body": bool(chunks)}

    async def send(message):
        messages.append(message)

    asyncio.run(
        app(
            {
                "type": "http",
                "path": PATH,
                "method": method,
                "query_string": query_string,
                "headers": [(b"content-type", content_type), *extra_headers],
            },
            receive,
            send,
        )
    )
    return messages[0], strict_json(messages[1]["body"])


def test_api_factory_body_cursor_only_and_stable_sanitized_errors(monkeypatch):
    r, p, _, q = setup()
    app = create_app(r)
    head, first = asgi_request(app, encode(wire(q)))
    assert head["status"] == 200
    assert (b"cache-control", b"no-store") in head["headers"]
    body = wire(q)
    body["cursor"] = first["next_cursor"]
    head, second = asgi_request(app, encode(body))
    assert head["status"] == 200 and second["range"] == [2, 4]
    assert asgi_request(app, method="GET")[0]["status"] == 405
    assert asgi_request(app, b"{}", content_type=b"text/plain")[0]["status"] == 415
    assert asgi_request(app, b"x" * (72 * 1024 + 1))[0]["status"] == 413
    assert asgi_request(app, query_string=b"cursor=bad")[0]["status"] == 400
    assert asgi_request(app, extra_headers=((b"cursor", b"bad"),))[0]["status"] == 400

    def broken(*args):
        raise RuntimeError("private-credential-must-never-cross")

    monkeypatch.setattr(p, "directories", broken)
    head, response = asgi_request(app, encode(wire(q)))
    assert head["status"] == 503
    assert response["error"] == "STORAGE_UNAVAILABLE"
    assert "private-credential" not in encode(response).decode()
    assert p.views_opened == p.views_closed


def test_contract_wire_roundtrip_and_no_reader_writes_or_legacy_access(monkeypatch):
    r, p, _, q = setup()
    parsed, cursor = parse_body(encode(wire(q)))
    assert parsed == q and cursor is None
    from market_data_foundation.canonical.repository import Ledger

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy full enumeration forbidden")

    monkeypatch.setattr(Ledger, "versions_for_definition", forbidden)
    from pathlib import Path

    from market_data_foundation.canonical import market_consumer_read

    code = "\n".join(
        f.read_text()
        for f in Path(market_consumer_read.__file__).parent.glob("*.py")
        if f.name != "synthetic.py"
    )
    for forbidden_text in (
        "versions_for_definition",
        "subprocess",
        "psycopg",
        "provider_lab",
    ):
        assert forbidden_text not in code
    indexes = (
        p.version_index,
        p.slot_index,
        p.history_index,
        p.month_index,
        p.content_index,
        p.lineage_index,
        p.guard_index,
        p.admission_index,
    )
    before = digest(indexes)
    all_pages(r, q)
    error("INVALID_CURSOR", lambda: r.read(q, "invalid"))
    assert digest(indexes) == before


def test_request_clock_and_binding_guards():
    r, p, _, q = setup()
    error("INVALID_AS_OF", lambda: r.read(replace(q, at=NOW + timedelta(seconds=1))))
    r.clock = lambda: NOW + timedelta(seconds=31)
    error("CLOCK_UNAVAILABLE", lambda: r.read(q))
    r.clock = lambda: NOW
    p.binding = replace(p.binding, ledger="wrong-target")
    error("PROJECTION_BINDING_MISMATCH", lambda: r.read(q))
    p.binding = replace(BINDING, ready=False)
    error("INDEX_NOT_READY", lambda: r.read(q))


def test_budget_shared_across_batches_and_elapsed_time():
    ticks = [0.0]
    b = Budget(Limits(calls=2), lambda: ticks[0])
    b.statement()
    b.statement()
    error("READ_CAPACITY_EXCEEDED", b.statement)
    ticks[0] = 6.0
    exc = error("READ_TIMEOUT", b.check_time)
    assert exc.status == 503 and exc.dimension == "transaction_time"


def test_multi_month_exact_manifest_and_month_directory_budget():
    r, p, v, q = setup()
    october = seed(p, "october", start=date(2026, 10, 1), days=2)
    q = replace(
        q,
        mode="exact_versions",
        page_size=1000,
        items=(
            replace(
                q.items[0],
                end_date_exclusive=date(2026, 10, 3),
                exact_versions=(v.key, october.key),
            ),
        ),
    )
    p.seed_admission(q)
    result = r.read(q)
    assert result["total_units"] == 32
    assert result["units"][-1]["version"] == public(october.key)
    assert result["units"][10]["version"] is None
    assert result["items"][0]["coverage"] == "UNKNOWN"
    assert not any(name == "candidates" for name, _ in p.lookups)
    r.limits = replace(Limits(), months=1)
    exc = error("READ_CAPACITY_EXCEEDED", lambda: r.read(q))
    assert exc.dimension == "months"


def test_many_slots_are_selective_batched_and_share_statement_budget():
    r, p, _, q = setup()
    start = date(2025, 1, 1)
    q = replace(
        q,
        items=tuple(
            Item(
                str(n),
                InstrumentIdentity("synthetic", str(n)),
                start,
                start + timedelta(days=366),
            )
            for n in range(3)
        ),
    )
    for item in q.items:
        for day in item.days():
            month = day.replace(day=1)
            if (DEF, item.instrument, month) not in p.month_index:
                p.seed_month(DEF, item.instrument, month)
    p.seed_admission(q)
    result = r.read(q)
    assert result["total_units"] == 1098
    assert [size for name, size in p.lookups if name == "candidates"] == [512, 512, 74]
    r.limits = replace(Limits(), calls=10)
    exc = error("READ_CAPACITY_EXCEEDED", lambda: r.read(q))
    assert exc.dimension == "statements"


@pytest.mark.parametrize(
    "failure", ["missing_guard", "universe", "semantics", "universe_version"]
)
def test_explicit_universe_and_semantics_are_not_inferred(failure):
    r, p, v, q = setup()
    q = replace(q, universe_revision="synthetic-universe-r1")
    p.seed_admission(q)
    if failure == "missing_guard":
        del p.admission_index[query_subjects(q)[0]]
        error("ADMISSION_INCONSISTENT", lambda: r.read(q))
        return
    if failure == "universe":
        p.members = ()
        expected = "UNIVERSE_MEMBERSHIP_MISMATCH"
    elif failure == "semantics":
        p.semantics = ()
        expected = "SEMANTICS_MISMATCH"
    else:
        expected = "UNIVERSE_REVISION_MISMATCH"
    result = r.read(q)
    assert result["items"][0]["status"] == expected
    assert not result["units"]


def test_small_exact_request_missing_unpublished_and_withdrawn_versions():
    r, p, v, q = setup()
    q = replace(
        q, mode="exact_versions", items=(replace(q.items[0], exact_versions=(v.key,)),)
    )
    p.seed_history(v.key, ())
    assert r.read(q)["items"][0]["status"] == "EXACT_PUBLICATION_UNAVAILABLE"
    first = event(v.key)
    p.seed_history(v.key, (first, event(v.key, 1, "WITHDRAW", previous=first.key)))
    assert r.read(q)["items"][0]["status"] == "VERSION_WITHDRAWN"
    assert r.read(q)["items"][0]["publication"] == "WITHDRAWN"
    missing = replace(
        q, items=(replace(q.items[0], exact_versions=(Key(Kind.VERSION, "missing"),)),)
    )
    assert r.read(missing)["items"][0]["status"] == "VERSION_NOT_FOUND"


def test_signing_configuration_and_untrusted_clock_are_classified():
    r, p, _, q = setup()
    r.cursor.keys["k1"] = b"short"
    error("SIGNING_UNAVAILABLE", lambda: r.read(q))
    assert p.views_opened == 0
    r.cursor.keys["k1"] = b"synthetic-only-key-at-least-32-bytes"

    def bad_clock():
        raise RuntimeError("private-clock-data")

    r.clock = bad_clock
    error("CLOCK_UNAVAILABLE", lambda: r.read(q))
    assert p.views_opened == 0


def test_cursor_payload_type_confusion_is_rejected_without_view():
    r, p, _, q = setup()
    first = r.read(q)
    payload = strict_json(unb64(first["next_cursor"].split(".")[0]))
    for invalid in (None, [], [True, "2026-09-02"], [0, "not-a-date"], {}, 1):
        token = r.cursor.sign({**payload, "last": invalid}, r.limits)
        views = p.views_opened
        error("INVALID_CURSOR", lambda: r.read(q, token))
        assert p.views_opened == views


def test_standalone_api_without_optional_web_dependencies(monkeypatch):
    import builtins
    import importlib

    from market_data_foundation.web import market_api

    r, p, _, q = setup()
    original_import = builtins.__import__
    attempted = []

    def without_web(name, *args, **kwargs):
        if name.split(".")[0] in {"fastapi", "starlette"}:
            attempted.append(name)
            raise ModuleNotFoundError(name, name=name)
        return original_import(name, *args, **kwargs)

    with monkeypatch.context() as blocked:
        blocked.setattr(builtins, "__import__", without_web)
        # Re-execute the module so this also detects eager optional imports.
        importlib.reload(market_api)
        app = market_api.create_app(r)
        head, first = asgi_request(app, encode(wire(q)))
        body = {**wire(q), "cursor": first["next_cursor"]}
        next_head, second = asgi_request(app, encode(body))
        assert head["status"] == next_head["status"] == 200
        assert first["range"] == [0, 2] and second["range"] == [2, 4]
        assert p.views_opened == p.views_closed == 2
        assert attempted == []
        # Requesting the optional integration must report its missing dependency.
        with pytest.raises(ModuleNotFoundError) as exc:
            market_api.create_router(r)
        assert exc.value.name == "fastapi"
        assert attempted == ["fastapi"]


def test_optional_fastapi_router_with_locked_host_dependencies(monkeypatch):
    # Exercise actual registered FastAPI route without a socket or test server.
    import asyncio
    from importlib.util import find_spec

    if find_spec("fastapi") is None or find_spec("starlette") is None:
        pytest.skip("Optional router requires the host's locked FastAPI/Starlette")

    from starlette.requests import Request

    from market_data_foundation.web.market_api import create_router

    async def inline(fn, *args):
        return fn(*args)

    monkeypatch.setattr("starlette.concurrency.run_in_threadpool", inline)
    r, _, _, q = setup()
    router = create_router(r)
    route = router.routes[0]
    assert route.path == PATH and route.methods == {"POST"}

    async def receive():
        return {"type": "http.request", "body": encode(wire(q)), "more_body": False}

    request = Request(
        {
            "type": "http",
            "path": PATH,
            "method": "POST",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
        },
        receive,
    )
    response = asyncio.run(route.endpoint(request))
    assert response.status_code == 200
    assert strict_json(response.body)["range"] == [0, 2]
