# T06 Stage-1: FRED/ALFRED vintage candidate

Stage-1 builds an in-memory qualification candidate using an explicitly injected
request source and artifact store. It does not materialize data or create a local
job. No alternate provider is available. Execution requires `live_opt_in=True`;
the request source owns authentication and performs one attempt per invocation.
The default is no execution. The test suite uses fakes exclusively and requires
neither credentials nor external services.

The fixed logical read sequence is:

1. GDP `series/vintagedates`: `sort_order=desc`, `limit=2`, `file_type=json`.
2. GDP `series/observations`: `output_type=2`, the two explicit selected
   `vintage_dates`, `observation_start=2025-01-01`,
   `observation_end=2026-12-31`, `units=lin`, `file_type=json`.
3. PAYEMS discovery with the same discovery parameters.
4. PAYEMS observations with the same observation parameters and its own selection.

All reads include the corresponding `series_id`. Discovery selects the first two
distinct valid ISO calendar date strings in response order. Invalid entries and
repeated dates do not qualify; fewer than two distinct valid dates is terminal.
No additional discovery read is made. Vintages remain date strings.

Each logical read independently permits at most three calls to the same shared
request source. Transport exceptions, HTTP 429 and HTTP 5xx consume this budget;
other non-200 statuses are terminal. There is no retry after HTTP 200. Before
parsing any HTTP 200 body, the store atomically retains the raw bytes and a
secret-safe request artifact. The filesystem implementation writes both files
into a staging directory under ignored `artifacts/` and publishes the directory
with a single same-filesystem rename. This provides atomic visibility, not a
power-loss durability guarantee. A retention or parsing error stops the sequence
immediately, with no requery and no later logical request. Previously retained
responses remain available after a failure.

Request artifacts contain only logical read number, fixed endpoint, series,
allowlisted parameters, HTTP status and attempt count. Authentication stays in
the source. Qualification observations contain fixed exception classifications,
never credential values or raw exception messages. Raw provider bodies remain
unaltered and belong only in ignored `artifacts/`, never Git. Source adapters must
not echo credentials into response bodies or encode hidden internal retries.

Observations accept only the real `output_type=2` wide response contract. Top
metadata must exactly match the older/newer selected realtime bounds, requested
observation window, `units=lin`, integer `output_type=2`, `file_type=json`,
`order_by=observation_date` and `sort_order=asc`. Optional `count` must be an
integer equal to the row count, `offset` integer zero, and `limit` a positive
integer; booleans are rejected. The observations list must be nonempty.

Each row has exactly `date`, `SERIES_YYYYMMDD` for the newer vintage, and
`SERIES_YYYYMMDD` for the older vintage. Dates must be valid, within the window
and strictly ascending. Every row expands into two observations whose vintage
comes from the exact column suffix. Internal `realtime_end` retains the top-level
provider realtime window end. Duplicate `(series, vintage, date)` cells cannot
qualify. A `.` cell is retained with `raw_value='.'`, `value=None`, and
`unavailable=True`. Other cells must be strings validating as finite `Decimal`
numbers; their original strings, including precision, are preserved. The earlier
assumed row-wise `realtime_start` / `value` shape is rejected.

GDP metadata is quarterly, billions USD, SAAR, underlying BEA via FRED. PAYEMS
metadata is monthly, thousands persons, SA, underlying BLS via FRED. Both identify
the provider as Federal Reserve Bank of St. Louis FRED/ALFRED aggregator and are
explicitly not first-party releases.

A successful candidate carries exactly four retained raw bodies and their raw /
request paths, request attempt counts, both per-series selections, safe execution
observations, and a semantic bundle containing metadata and normalized rows.
Deterministic tests cover the read contract, metadata, retry isolation, terminal
failures, retention-before-parse ordering, secret safety and absence of fallback.
No live-provider tests are executed by this Stage-1 suite.

## Stage-2 multi-raw local-first

Stage-2 consumes the successful Stage-1 `VintageCandidate` without changing its
Source, transport retry policy or parsers. `build_live_plan` requires the exact
GDP discovery, GDP observations, PAYEMS discovery, PAYEMS observations order.
The candidate Definition is `mdf.macro.fred-alfred.vintage-bundle`, revision
`t06-r1`, with `source_qualified=False`; materialization does not activate or
qualify the provider. One COLLECT run has logical budget four, four exact
acquisitions and five evidence materials. The single canonical bundle mapping
retains discovery/observations explicitly in each acquisition's public operation
parameter and stable request identity. All observation scope members, including
unavailable cells, are `SERIES|VINTAGE|OBSERVATION_DATE` strings.

The projected content embeds four base64 raw carriers, individual SHA-256/size,
public requests, attempts, original receipts and exact acquisition times. Metadata,
selected vintage date strings and normalized observations are reconstructed from
these carriers, with parser and contract revisions pinned. Local Read compares
the entire rebuilt graph and content, including Source, Binding, Definition,
run, four acquisitions, five materials, Version and Publication request. It never
calls a Source or writes storage. Exact Read uses a version identity; as-of Read
selects the usable version with the latest **maximum acquisition obtained_at**.
Equal acquisition completion times are rejected as ambiguous; publication time
is an availability gate, not a tie-breaker.

`vintage_materialization` acquires four writer tickets in logical read order,
creates each raw only if absent and reads every raw back exactly. An unknown PUT
can continue only when exact bytes are independently observed; its disposition
remains unknown and its individual verification records that reconciliation.
Only after all four raw carriers are confirmed does one durable plan replace all
raw locators, persist the full graph/content, hand off one authoritative commit
and request one publication. Partial Garage success never establishes canonical
success. Pre-authoritative failures release every acquired ticket. Commit or
publication ambiguity retains all four tickets until reconciliation on an
independent ledger. Release callbacks run only after every release completes.

`VintageLocalJob` directly follows Retail's one-attempt journal lifecycle. Its
fixed target is both series and the 2025–2026 window: acquire budget four logical
reads (up to three transport attempts each), retained budget zero, four raw
creates, one ledger handoff and one publication. The journal raw field is an
ordered list of four SHA-256/size/locator/observed-at entries. Successful readback
checks all four Garage materials and exact/as-of local Reads on a fresh ledger.
An unknown terminal remains unknown after reconciliation; no implicit retry or
new Source request occurs. Explicit repair reserves a new job, requires
`confirmed_not_published` plus all four raw verifications, and accepts only the
same retained plan and four original raw bodies with Provider budget zero.

Synthetic reconciliation after restart requires a durable lock-release proof;
a missing proof remains unresolved. Owner-nonproduction bindings retain the
Retail host/schema, owned protection connection and old-writer backend PID gates,
using only the added exact schema `mdf_macro_fred_alfred_vintage_r1`. Tests inject
all Source, material store, protection and ledger behavior; no real Provider,
PostgreSQL or Garage service is required or exercised by this focused suite.

## R1 retained real-response evidence

The correction is bound to input HEAD
`70899197f243979300527a2a4bf199e5d0dc1c20`. R1 GDP discovery succeeded and
selected newer `2026-09-30`, older `2026-08-26`. GDP `output_type=2` returned
HTTP 200 and was retained before parsing. Its real wide shape, six date rows
with exactly `date`, `GDP_20260930`, `GDP_20260826`, drives this parser correction.
The original row-wise parser assumption did not match that response.

Retained files under
`/srv/market-data-foundation/artifacts/t06-fred-alfred-vintage-real-r1/source/`:

| Read | Raw path | Bytes | SHA-256 |
| --- | --- | ---: | --- |
| GDP discovery | `7cc3c65c7cfd44cf951f590db7913cd9/raw.json` | 182 | `0b25b5184fe982b61ffd25a3b077971ba3b3a3952ffaef509bd0d951aadbad84` |
| GDP observations | `ec270d7726304e5fb57991e476364b7a/raw.json` | 729 | `82f980542736e0a6852cc467f425fea7de586c73018c69e89afe164cf7bb91dc` |

Local read-only replay parses discovery and observations and constructs the GDP
metadata/semantic projection for the first two logical reads: six wide rows,
12 cells, both selected vintages represented on every date. No GDP numeric
values are emitted or copied into Git. This replay makes no Provider, PostgreSQL
or Garage calls. Source qualification is **not COMPLETE**: PAYEMS was not
requested, and this partial GDP evidence is not a four-read qualified candidate
or a materialized canonical publication.
