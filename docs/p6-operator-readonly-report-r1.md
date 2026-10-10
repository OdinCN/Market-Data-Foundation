# P6 offline Operator report candidate

`canonical.operator_report.render_operator_report(status)` returns a complete,
self-contained HTML string for an already authorized Operator. It accepts the
completed in-process `OperatorStatus`; it never inspects storage, reads content,
opens files, fetches assets, starts a service or exposes a public consumer mode.
Use the status only **after successful consistent scope exit**, as before:

```python
with ledger.operator_snapshot() as scope:
    status = inspect_operator_status(scope, definition, as_of=instant)
# A failed context exit must prevent rendering/saving the candidate.
html = render_operator_report(status)
# The caller may save html as UTF-8 under ignored artifacts/ and open it locally.
```

For a wholly synthetic offline preview, a stable `InMemoryLedger` with either
admitted Definition can supply the existing inspection. The renderer does not
certify authentication, successful scope exit, snapshot consistency or physical
backend resources; callers own those prerequisites and local artifact access.
Do not publish or share reports beyond their authorized Operator audience.

The report groups stored bindings into stored-only, referenced with no eligible
Publication, and Publication eligible. Counts, expandable binding cards and
internal Version links make exact revision associations browsable. Revisions of
one identity remain separate. Version cards show exact binding or its absence,
frozen time, selection state, effective Publication, and expandable Publication
history with decision/usable/effective clocks and pending clocks at the requested
instant. Times use UTC, with no render-time clock. Empty groups, no Version
records, no Publication history, unbound Versions and ambiguous eligible sets
remain explicit. No Version/provider/revision is selected. Eligibility is solely
Publication metadata; even one candidate leaves actual content readability and
availability unknown. All eleven runtime states remain `unobservable`.

Visibility is deliberately narrow: exact Definition/Binding/Source/Version/
Publication Keys, associations, fixed state tokens and timestamps. No dataclass
dump, JSON embedding, provider prose, compatibility/limitation prose, mapping
claims, scope principals, finite coverage members, raw content, receipts or
controlled locators are emitted. A recorded `completeness="unknown"` is shown as
unknown; other free-text completeness claims are explicitly withheld. Withheld
claims are not converted into observed unknown/healthy states. Operators needing
full metadata can use the original in-process inspection separately.

Dynamic Key identity/revision text is HTML escaped, including quotes and
ampersands. It never enters an attribute, URL, CSS or script context. All links
are generated numeric/section fragments. Locator-shaped Keys (URI schemes or
absolute path forms), control characters, surrogate characters and directional
override/isolate characters fail closed rather than being silently rewritten.
Locator checks scan each bounded Key once, preserving the same URI and path
rejection rules at the 16,384-character limit.
This is not a secret detector: callers must ensure opaque record identifiers
themselves are appropriate for the Operator audience. The explicit field whitelist
prevents free-text private claims from reaching the artifact.

No script, external asset, form or network action is emitted. A restrictive CSP
disables resources by default and permits only the static embedded style; safety
does not depend on CSP support. The fixed output limit is **262,144 UTF-8 bytes**,
including markup, escaped entities and multibyte characters. Each visible text
field is at most 16,384 characters, with bounded escaping chunks. Fixed report
row bounds are 128 bindings, 128 Versions and 512 total Publications. There are
no caller budget overrides, pagination or truncated successes. An otherwise valid
inspection may exceed the tighter presentation budget and require a separately
authorized future presentation design.

Malformed presentation structure, inconsistent associations/cardinality/pending
clocks, changed runtime states or unsafe identifiers raise internal
`OperatorReportError("operator_report_invalid_status")`. Excess bounds raise
`OperatorReportError("operator_report_budget_exceeded")`. Both contain only the
classification, no raw exception context/cause and no partial HTML. Validation
compares the existing ordered, unique Publication history and decision
transitions with its last record whose three clocks have reached the requested
instant, its exact effective Key, terminal/eligible state and binding summaries.
Pending later records leave the prior effective record in force. No storage or
lineage is resolved. Exceptions raised by metadata getters, including report
error subclasses, are classified without reading their properties or invoking
their string conversion. Budget provenance uses the identity of a fresh signal
owned by that render invocation; supplied exact-type signals, earlier signals
and getter-initiated nested overflows are invalid status. The final classified
error is caught, its context/cause cleared and then bare re-raised, so a caller's
active private exception is not retained in those links. Python 3.14.8 probes
and regressions cover active handlers and finally blocks; `from None` alone
does not clear context. These checks do not reconstruct or authenticate a
canonical graph; genuine inspection remains
mandatory. Existing inspections, canonical model/schema, dependency lock and
`mdf.consumer.read.v1` are unchanged.

Offline tests exercise real synthetic inspections, SQL/snapshot doubles, exact
revision groups, reversed insertion order and stable output, empty/unbound and
unknown cases, ambiguity, clock boundaries, withdrawal/restoration/supersession,
HTML/JSON injection, field exclusion, locator/control rejection, invalid joins,
encoded byte boundaries and aggregate overflow. They make no real PG/S3/provider
requests. This is an uncommitted source/tests/docs candidate. Next gates are owner
host QA (including visual local-file review and the existing sandbox ptrace
limitation), fresh independent revision-bound professional review, then separately
authorized normal Git delivery and exact-head CI. No deployment, source activation,
MBO adoption or whole-P6 exit is authorized.
**B-END-TO-END-READ-CAPABILITY remains STOP / NOT PROVEN.**
