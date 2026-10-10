"""Bounded, offline HTML for an already authorized, inspected OperatorStatus.

This module has no storage, file, clock, content or network access. Render only
after the caller's consistent read-only scope has exited successfully.
"""

from datetime import UTC, datetime
from html import escape

from .model import Decision, Key, Kind
from .operator_status import OperatorStatus

MAX_REPORT_BYTES = 262144
_MAX_TEXT_CHARACTERS = 16384
_UNKNOWN_FIELDS = (
    "binding_activation",
    "executable_binding",
    "source_health",
    "current_freshness",
    "external_admission",
    "finite_coverage",
    "quality",
    "content_integrity",
    "durability",
    "replayability",
    "current_availability",
)
_STATES = {
    "no_publication",
    "no_effective_publication",
    "version_withdrawn",
    "version_superseded",
    "publication_eligible",
}
_OUTCOMES = {
    "as_of_no_version": "No Publication candidate is eligible at this instant.",
    "one_publication_candidate": (
        "One Publication candidate; content readability and availability are unknown."
    ),
    "as_of_ambiguous": "Ambiguous: multiple eligible candidates; no Version selected.",
}
_HEAD = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta http-equiv="Content-Security-Policy" content="default-src 'none';
style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>MDF offline Operator report</title>
<style>
body{font:16px system-ui,sans-serif;max-width:72rem;margin:2rem auto;padding:0 1rem;
color:#18202a;background:#fff}a{color:#164d9c}code{white-space:pre-wrap;
overflow-wrap:anywhere}article,details{border:1px solid #9ba9b8;padding:1rem;
margin:1rem 0}summary{cursor:pointer}dt{font-weight:bold}dd{margin-bottom:.5rem}
.notice{border-left:4px solid #95610a;padding:1rem;background:#fff5df}
table{border-collapse:collapse}th,td{padding:.4rem;border:1px solid #9ba9b8;
text-align:left}h2{margin-top:2rem}
</style></head><body><main>
<h1>MDF offline Operator report</h1>
<p class="notice">Metadata at the requested instant. Stored and Publication eligible
do not establish activation, quality, content readability or current availability.
Authorization and successful consistent scope exit are caller prerequisites.</p>
<nav aria-label="Report sections"><a href="#bindings">Stored bindings</a> |
<a href="#versions">Versions and Publication clocks</a> |
<a href="#unbound">Versions without binding record</a> |
<a href="#unknown">Unobservable runtime states</a></nav>
"""


class OperatorReportError(ValueError):
    """Internal report failure; classification only, no input or partial HTML."""


class _BudgetExceeded(Exception):
    """Signal whose per-render instance identity establishes budget provenance."""


def _locator(value: str) -> bool:
    # A scheme may begin at any ASCII letter within a scheme-character run.
    # Track that run once; an unanchored greedy scheme regex retries each suffix.
    scheme_letter = False
    for offset, character in enumerate(value):
        letter = "a" <= character <= "z" or "A" <= character <= "Z"
        if character in "/\\" and (offset == 0 or value[offset - 1].isspace()):
            return True
        if letter and value[offset + 1 : offset + 3] in (":/", ":\\"):
            return True
        if character == ":" and scheme_letter and value.startswith("://", offset):
            return True
        if letter:
            scheme_letter = True
        elif not ("0" <= character <= "9" or character in "+.-"):
            scheme_letter = False
    return False


def _require(condition: bool) -> None:
    if not condition:
        raise OperatorReportError("operator_report_invalid_status")


class _HTML:
    def __init__(self, budget: _BudgetExceeded | None = None):
        self.budget = _BudgetExceeded() if budget is None else budget
        self.parts: list[str] = []
        self.bytes_used = 0

    def add(self, markup: str) -> None:
        size = len(markup.encode("utf-8"))
        if size > MAX_REPORT_BYTES - self.bytes_used:
            raise self.budget
        self.bytes_used += size
        self.parts.append(markup)

    def text(self, value: str) -> None:
        _require(type(value) is str)
        if len(value) > _MAX_TEXT_CHARACTERS:
            raise self.budget
        # Reject invisible direction/control characters and invalid UTF-8 rather
        # than silently altering an exact Key. Escape bounded chunks before any
        # output accounting; entities and multibyte text count at encoded size.
        _require(
            all(
                ord(c) >= 32
                and not 127 <= ord(c) <= 159
                and not 0xD800 <= ord(c) <= 0xDFFF
                and not 0x202A <= ord(c) <= 0x202E
                and not 0x2066 <= ord(c) <= 0x2069
                for c in value
            )
        )
        for start in range(0, len(value), 1024):
            self.add(escape(value[start : start + 1024], quote=True))

    def key(self, key: Key, kind: Kind) -> None:
        _require(type(key) is Key and key.kind == kind)
        for value in (key.identity, key.revision):
            _require(type(value) is str)
            if len(value) > _MAX_TEXT_CHARACTERS:
                raise self.budget
            _require(not _locator(value))
        _require(bool(key.identity))
        self.add("<code>")
        self.text(kind.value)
        self.add(" | identity: ")
        self.text(key.identity)
        self.add(" | revision: ")
        self.text(key.revision)
        if not key.revision:
            self.add("(none)")
        self.add("</code>")

    def time(self, value: datetime | None) -> None:
        if value is None:
            self.add("not recorded")
        else:
            _require(type(value) is datetime and value.utcoffset() is not None)
            self.text(value.astimezone(UTC).isoformat())


def _items(values: tuple, maximum: int, budget: _BudgetExceeded) -> tuple:
    _require(type(values) is tuple)
    if len(values) > maximum:
        raise budget
    return values


def _publication_state(
    version, at: datetime, seen: set[Key], maximum: int, budget: _BudgetExceeded
) -> int:
    """Check the existing diagnostic history, without resolving canonical records."""
    history = _items(version.publications, maximum, budget)
    probe = _HTML(budget)
    effective = None
    previous = None
    allowed = {
        Decision.PUBLISH: {Decision.WITHDRAW, Decision.SUPERSEDE},
        Decision.RESTORE: {Decision.WITHDRAW, Decision.SUPERSEDE},
        Decision.WITHDRAW: {Decision.RESTORE, Decision.SUPERSEDE},
        Decision.SUPERSEDE: set(),
    }
    for publication in history:
        probe.key(publication.publication, Kind.PUBLICATION)
        _require(publication.publication not in seen)
        seen.add(publication.publication)
        _require(type(publication.decision) is Decision)
        clocks = (
            ("decision_at", publication.decision_at),
            ("usable_at", publication.usable_at),
            ("effective_at", publication.effective_at),
        )
        for name, clock in clocks:
            _require(
                (name == "effective_at" and clock is None)
                or (type(clock) is datetime and clock.utcoffset() is not None)
            )
        _require(publication.usable_at >= publication.decision_at)
        if previous is None:
            _require(publication.decision == Decision.PUBLISH)
        else:
            _require(publication.decision_at > previous.decision_at)
            _require(publication.decision in allowed[previous.decision])
        previous = publication
        pending = _items(publication.pending_clocks, 3, budget)
        _require(
            pending
            == tuple(name for name, clock in clocks if clock is not None and clock > at)
        )
        if not pending:
            effective = publication
    expected_key = None if effective is None else effective.publication
    if version.effective_publication is not None:
        probe.key(version.effective_publication, Kind.PUBLICATION)
    _require(version.effective_publication == expected_key)
    expected_state = (
        "no_publication"
        if not history
        else "no_effective_publication"
        if effective is None
        else "version_withdrawn"
        if effective.decision == Decision.WITHDRAW
        else "version_superseded"
        if effective.decision == Decision.SUPERSEDE
        else "publication_eligible"
    )
    _require(version.selection_state == expected_state)
    return len(history)


def _render(status: OperatorStatus, budget: _BudgetExceeded) -> str:
    _require(type(status) is OperatorStatus)
    html = _HTML(budget)
    html.add(_HEAD)
    html.add("<h2>Definition / as-of</h2><p>")
    html.key(status.definition.key, Kind.DEFINITION)
    _require(status.as_of.definition == status.definition.key)
    _require(type(status.as_of.as_of) is datetime)
    html.add("</p><p>Requested instant (UTC): ")
    html.time(status.as_of.as_of)
    versions = _items(status.as_of.candidates, 128, budget)
    bindings = _items(status.stored_bindings, 128, budget)
    # Keys are admitted through bounded escaped text before hashing/indexing.
    html.add("</p><p>Selection outcome: ")
    _require(status.as_of.selection_outcome in _OUTCOMES)
    html.text(status.as_of.selection_outcome)
    html.add('</p><p class="notice">')
    html.text(_OUTCOMES[status.as_of.selection_outcome])
    html.add("</p>")
    index = {}
    publications_seen = set()
    checked_publications = 0
    for number, version in enumerate(versions):
        probe = _HTML(budget)
        probe.key(version.version, Kind.VERSION)
        _require(version.version not in index and version.selection_state in _STATES)
        checked_publications += _publication_state(
            version,
            status.as_of.as_of,
            publications_seen,
            512 - checked_publications,
            budget,
        )
        index[version.version] = number
    eligible_count = sum(v.selection_state == "publication_eligible" for v in versions)
    expected = (
        "as_of_no_version"
        if eligible_count == 0
        else "one_publication_candidate"
        if eligible_count == 1
        else "as_of_ambiguous"
    )
    _require(status.as_of.selection_outcome == expected)
    html.add(
        f"<p>Stored bindings: {len(bindings)} | Version records: {len(versions)} | "
        f"Publication eligible Versions: {eligible_count}</p>"
    )

    def references(keys: tuple) -> None:
        html.add("<ul>")
        if not keys:
            html.add("<li>None recorded</li>")
        for key in _items(keys, 128, budget):
            _require(key in index)
            html.add(f'<li><a href="#version-{index[key]}">')
            html.key(key, Kind.VERSION)
            html.add("</a></li>")
        html.add("</ul>")

    html.add('<section id="bindings"><h2>Stored bindings</h2>')
    seen = set()
    for group in (
        "Stored-only",
        "Referenced, no eligible Publication",
        "Publication eligible",
    ):
        rows = tuple(
            row
            for row in bindings
            if (
                "Publication eligible"
                if row.publication_candidates
                else "Referenced, no eligible Publication"
                if row.referencing_versions
                else "Stored-only"
            )
            == group
        )
        html.add("<h3>")
        html.text(group)
        html.add(f" ({len(rows)})</h3>")
        if not rows:
            html.add("<p>No stored bindings in this group.</p>")
        for row in rows:
            html.add("<details><summary>")
            binding = row.candidate.binding
            html.key(binding, Kind.BINDING)
            _require(binding not in seen and row.record_state == "stored")
            seen.add(binding)
            _items(row.referencing_versions, 128, budget)
            _items(row.publication_candidates, 128, budget)
            _require(
                row.referencing_versions
                == tuple(v.version for v in versions if v.binding_record == binding)
            )
            _require(
                row.publication_candidates
                == tuple(
                    v.version
                    for v in versions
                    if v.binding_record == binding
                    and v.selection_state == "publication_eligible"
                )
            )
            html.add("</summary><p>Record state: stored</p><p>Source record: ")
            html.key(row.candidate.source.key, Kind.SOURCE)
            html.add("</p><h4>Referencing Versions</h4>")
            references(row.referencing_versions)
            html.add("<h4>Publication eligible Versions at requested instant</h4>")
            references(row.publication_candidates)
            html.add("</details>")
    html.add("</section>")
    _require(
        all(v.binding_record is None or v.binding_record in seen for v in versions)
    )
    html.add('<section id="versions"><h2>Versions and Publication clocks</h2>')
    if not versions:
        html.add("<p>No Version records for this Definition.</p>")
    publication_count = 0
    for number, version in enumerate(versions):
        html.add(f'<article id="version-{number}"><h3>')
        html.key(version.version, Kind.VERSION)
        html.add("</h3><p>Publication selection state: ")
        html.text(version.selection_state)
        html.add("</p><p>Frozen at (UTC): ")
        _require(type(version.frozen_at) is datetime)
        html.time(version.frozen_at)
        html.add("</p><p>Exact binding record: ")
        if version.binding_record is None:
            html.add("none recorded")
        else:
            html.key(version.binding_record, Kind.BINDING)
        html.add("</p><p>Completeness claim: ")
        html.add(
            "unknown (recorded)"
            if version.completeness == "unknown"
            else "recorded value withheld; no completeness observation"
        )
        html.add("</p><p>Effective Publication: ")
        if version.effective_publication is None:
            html.add("none at requested instant")
        else:
            html.key(version.effective_publication, Kind.PUBLICATION)
        html.add(
            "</p><details><summary>Publication history and pending clocks</summary>"
        )
        history = _items(version.publications, 512 - publication_count, budget)
        publication_count += len(history)
        _require(
            version.effective_publication is None
            or any(p.publication == version.effective_publication for p in history)
        )
        if not history:
            html.add("<p>No Publication records.</p>")
        for publication in history:
            _require(type(publication.decision_at) is datetime)
            _require(type(publication.usable_at) is datetime)
            html.add("<dl><dt>Publication</dt><dd>")
            html.key(publication.publication, Kind.PUBLICATION)
            _require(type(publication.decision) is Decision)
            html.add("</dd><dt>Decision</dt><dd>")
            html.text(publication.decision.value)
            for label, at in (
                ("decision_at", publication.decision_at),
                ("usable_at", publication.usable_at),
                ("effective_at", publication.effective_at),
            ):
                html.add("</dd><dt>")
                html.text(label)
                html.add(" (UTC)</dt><dd>")
                html.time(at)
            pending = _items(publication.pending_clocks, 3, budget)
            _require(
                pending
                == tuple(
                    name
                    for name, at in (
                        ("decision_at", publication.decision_at),
                        ("usable_at", publication.usable_at),
                        ("effective_at", publication.effective_at),
                    )
                    if at is not None and at > status.as_of.as_of
                )
            )
            html.add("</dd><dt>Pending at requested instant</dt><dd>")
            html.text(", ".join(pending) if pending else "none")
            html.add("</dd></dl>")
        html.add("</details></article>")
    html.add('</section><section id="unbound"><h2>Versions without binding record</h2>')
    _require(
        status.versions_without_binding_record
        == tuple(v.version for v in versions if v.binding_record is None)
    )
    references(status.versions_without_binding_record)
    html.add('</section><section id="unknown"><h2>Unobservable runtime states</h2>')
    html.add("<p>Unknown from this metadata report; no runtime observation.</p><dl>")
    for name in _UNKNOWN_FIELDS:
        _require(getattr(status, name) == "unobservable")
        html.add("<dt>")
        html.text(name)
        html.add("</dt><dd>unobservable</dd>")
    html.add(
        "</dl></section><p>Visibility: exact record Keys, associations and clocks only."
    )
    html.add(
        " Free-text contracts, locators, scope principals, coverage members, receipts"
    )
    html.add(" and content are withheld. This file is for its authorized Operator.</p>")
    html.add("</main></body></html>\n")
    return "".join(html.parts)


def render_operator_report(status: OperatorStatus) -> str:
    """Return complete deterministic HTML or a classified error, never partial.

    Not an authentication/inspection boundary: the caller supplies a genuine
    completed status and owns identifier visibility and local artifact handling.
    This white list deliberately excludes all free-text provider/claim fields.
    """
    budget = _BudgetExceeded()
    try:
        return _render(status, budget)
    except Exception as error:
        # Never inspect args/properties or stringify an input exception, including
        # OperatorReportError subclasses. A type match proves no provenance;
        # only the signal created for this invocation represents its overflow.
        failure = (
            "operator_report_budget_exceeded"
            if error is budget
            else "operator_report_invalid_status"
        )
    # Raising outside our handler still inherits an active caller exception.
    # Clear the new, trusted error after raising it. Bare re-raise preserves the
    # cleared links on Python 3.14.8; "from None" alone only hides their display.
    try:
        raise OperatorReportError(failure) from None
    except OperatorReportError as clean:
        clean.__context__ = None
        clean.__cause__ = None
        raise
