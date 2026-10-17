# T07 Stage-1: bounded FOMC statement candidate

This lane covers only `FOMC:STATEMENT:2026-09-16`, the September 15–16,
2026 meeting. Stage-1 supplies an offline parser, deterministic candidate and
an explicitly opted-in HTTPS edge. It does not establish real qualification,
canonical storage/publication, local exact/as-of Read or product completion.

`qualify_statement` defaults off and accepts injected request, store, clock and
sleep capabilities. `execute_live_candidate` also defaults off. Imports perform
no IO. A successful execution has exactly two logical reads: the official
`https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm` calendar, then
its discovered Statement HTML link. Each read uses the shared `request_source`
policy, independently: at most three transport attempts, 1s/2s backoff, transient
failures only. No release-window polling or alternate provider is present.

Discovery requires exactly one 2026 FOMC Meetings panel, one September 15–16
meeting row and one Statement HTML anchor. PDF siblings are not selected.
Missing, duplicate, foreign, wrong-document or ambiguous targets fail closed.
The target is resolved from that anchor; it is never synthesized. Only the exact
`https://www.federalreserve.gov` origin and the September 16 monetary-policy
press-release HTML route are admitted. Minutes, SEP, implementation notes,
PDFs, credentials, ports, queries and fragments are rejected.

The statement requires the exact title `Federal Reserve issues FOMC statement`,
`September 16, 2026` article date and `For release at 2:00 p.m. EDT` metadata.
Only that mechanically observed combination binds provider publication to
`2026-09-16T18:00:00Z`. Request/acquisition timestamps come from the MDF clock;
they do not inherit publication time. No MDF availability is inferred.

Extraction revision `t07-fomc-text-r1` uses the unique `#article` body column
`div.col-md-8`. Its first paragraph must start with `Economic activity` or
`Recent indicators`. Body paragraphs are entity-decoded, whitespace-normalized
and joined with two newlines. A voting paragraph is mandatory. Extraction stops
before a paragraph beginning `For media inquiries`, `Media Contacts` or
`Implementation Note`; a missing stop anchor fails closed. Navigation outside
the body, media/footer and linked implementation material are excluded. This
is deliberately conservative: changed source structure requires review rather
than speculative extraction. Last Update (`#lastUpdate`) is optional metadata;
no provider revision sequence is invented.

Both original bodies have independent SHA-256 and byte sizes. Extracted UTF-8
text has its own SHA-256 and remains a projection of the statement HTML.
`StatementCandidate` carries two `RetainedRead` objects, `CalendarDiscovery`,
`StatementDocument` and safe classified observations. Identical HTML replays
identically; wrapper changes can change raw identity without changing text.

Each HTTP200 body and request metadata is retained atomically before parsing.
`AtomicArtifactStore` publishes a directory containing `raw.html` and
`request.json` by a same-filesystem rename under ignored `artifacts/`.
Parser, semantic and artifact failures never retry or advance to another read.
Failure observations contain classifications, never raw exception messages.
The store retains request URL, attempts, own times, protocol metadata and raw
identity. This is local Stage-1 retention, not a durable canonical publication.

The concrete client sends User-Agent
`Market-Data-Foundation/0.1 FOMCOfficialDocument`, Accept `text/html` and
Accept-Encoding `identity`. TLS uses Python's verified HTTPS connection with a
20-second timeout. Redirects are never followed. Duplicate/malformed headers,
unapproved content type/encoding/framing, length mismatch and bodies above
5 MiB fail closed. Only bounded response protocol headers are retained.

Offline checks:

```sh
uv run pytest tests/test_fomc_statement.py tests/test_fomc_statement_live.py
uv run ruff check
uv run ruff format --check
```

All fixtures are synthetic, including their statement prose. Tests replace
transport and never qualify actual official-source availability or structure.
Any future real qualification requires explicit opt-in and the repository's
capability/fixture/observation contract; it is separate from deterministic CI
and must be marked `live_provider`.


## Stage-2 local-first

`fomc_statement.build_live_plan` constructs the static candidate definition
`mdf.macro.fomc-statement-document` / `t07-r1`, with exactly the single finite
member `FOMC:STATEMENT:2026-09-16`. `source_qualified=False` remains a static
candidate flag. The official federalreserve.gov binding has no fallback.
A budget of two preserves calendar logical read 1 and discovered statement
logical read 2, including each URL, attempts and own requested/obtained times.
Two ORIGINAL materials and one PROJECTED text/metadata carrier form the Version.

Content embeds both raw bodies as base64 with independent size/SHA-256,
request metadata, discovery, extracted text and text SHA-256, official publication
metadata and extraction revision. Replay parses both HTML bodies again and
compares the entire canonical carrier; a new content digest cannot authorize
invented document semantics. Official publication may precede acquisition;
it never supplies MDF obtained, decision or usable times.

`fomc_statement_materialization` acquires a writer ticket per raw, creates if
absent and confirms exact readback before any canonical handoff/publication.
Raw outcome unknown can proceed only with exact present bytes. Authoritative
commit/publication unknown retains both tickets until independent fresh ledger
reconciliation. Release proof survives journal/readback-exit failure. A restarted
synthetic writer without release proof fails closed; owner_nonproduction retains
the old writer backend PID gate and exact host/schema checks.

`fomc_statement_local_job` uses a local SQLite journal and fixed acquire/retained
specifications: provider logical budgets 2/0, raw creates 2, ledger handoff 1,
publication 1. Acquire calls the unchanged Stage-1 `qualify_statement`, including
its transport retry policy. Journal/local failures never rerun Source in that
attempt. Each journal raw entry records exact identity, locator and observed_at.
Reconciliation observes the same identities and never automatically retries;
repair requires confirmed absence of publication, both verified raws and an
explicit retained successor with zero Provider budget.

Fresh readback checks both exact raw bodies under protection and exact/as-of
Local Read using an independent ledger (read-only connection for PostgreSQL).
`fomc_statement_local_read` needs neither Source nor storage-write capability;
it rebuilds the candidate and all semantic lineage from Version content.
Exact reads fail closed on lineage or acquisition/freeze/publication inversions;
as-of selects latest completed acquisition and rejects equal-time ambiguity.
The isolated development target is exact schema `mdf_macro_fomc_statement_r1`
at `192.168.10.4`; this implementation/validation uses no external service IO.

Deterministic Stage-1 and Stage-2 checks:

```sh
uv run --offline pytest tests/test_fomc_statement.py tests/test_fomc_statement_live.py tests/test_fomc_statement_local.py
uv run --offline ruff check
uv run --offline ruff format --check
uv run --offline python scripts/check_repository_hygiene.py
```

All Stage-2 fixtures remain synthetic. Local canonical publication establishes
only the tested local lineage and availability, not real Source qualification.
