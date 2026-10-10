# P6 in-process Operator Web entry candidate

`canonical.operator_web.OperatorWebEntry.handle(request, operator_context=...)`
accepts an internal HTTP-shaped `OperatorWebRequest` and returns one complete
immutable `OperatorWebResponse`. This adds request admission, independent host
authorization, owned snapshot execution and failure responses to the existing
offline report. Repeated requests can inspect either accepted Definition at
different exact instants. It creates no HTTP parser/server, listener, public wire
revision, user database, authentication scheme, connection or deployment.

```python
from market_data_foundation.canonical.operator_web import (
    OperatorWebEntry,
    OperatorWebRequest,
)

request = OperatorWebRequest(
    b"GET",
    b"/operator/report?definition=bls-cpi-headlines&as_of=2026-10-06T00:00:20Z",
)
response = OperatorWebEntry().handle(request)
assert response.status == 403  # No trusted host authorization configured.
```

The only selectors are `bls-cpi-headlines` and `raw-daily-bars`, fixed to
`mdf.macro.bls.cpi.headlines@p3-r1` and `mdf.market.bars.daily.raw@p3-r1`.
Both `definition` and `as_of` are required exactly once, in either order. No
additional parameter is accepted. The raw ASCII target uses UTC
`YYYY-MM-DDTHH:MM:SSZ` or exactly six fractional digits before `Z`, within Python's
supported calendar years 0001–9999. Calendar validation and exact canonical
round-trip reject normalization such as `24:00:00`. No render-time clock, latest
selection, timezone offset, percent decoding, implicit default or pagination
exists. This syntax belongs solely to this internal adapter; it changes neither
the accepted consumer wire parser nor its input semantics/classifications.

The host must configure `authorize(context, definition_key, as_of)` and
`snapshot()`. Context is a separate host-only call argument; headers, URL and body
are never passed to the authorizer or used to construct that context. The
authorizer must independently establish Operator permission for the exact
selection and return the literal boolean `True`. Missing context/callback,
denial, other return types and authorization exceptions return 403 without
opening a scope. Client identity/role assertions cannot establish permission.
No real host authorization capability or access policy is supplied by this
candidate. A host that derives context from unverified headers or configures an
unconditional grant violates this prerequisite. The adapter is not a Python
object sandbox, concurrency coordinator or authentication verifier.

After authorization, the host's scope factory must exclusively supply a
consistent read-only context. PostgreSQL wiring, if separately authorized in the
future, must use existing `ledger.operator_snapshot()` and its connection
ownership/discard contract. No ordinary ledger fallback exists. The adapter calls
the real `inspect_operator_status`, waits for **successful context exit**, then
calls the unchanged `render_operator_report`. Missing scope, factory/setup/read/
exit failures and a custom scope suppressing a failed inspection return 503.
Cleanup uncertainty remains the host's discard obligation and never releases
status or HTML. Rendering occurs outside the scope and must complete before a
response is returned. There is no streaming, partial success or retry.

Request limits are fixed: method 1–16 uppercase ASCII bytes; target 1–512 bytes;
empty bytes body; at most 16 distinct case-insensitive headers, names 1–64 ASCII
token bytes, values 0–1024 printable ASCII bytes, and 4096 aggregate name/value
bytes. Exact built-in types are required, including tuple header pairs. These
are post-allocation adapter bounds, not a transport or physical resource SLA.
Identity/role headers are inert data within those bounds.

| Status | Fixed plain-text classification |
| --- | --- |
| 400 | `invalid_request`, `definition_not_admitted`, `invalid_as_of` |
| 404 | `route_not_found` |
| 405 | `method_not_allowed` |
| 403 | `operator_forbidden` |
| 503 | `operator_status_unavailable`, `operator_report_unavailable` |

Ingress validation precedes authorization; its failures reveal only this fixed
syntax. The adapter deliberately coarsens internal status/report exceptions
without inspecting their properties or stringifying them. Even synchronous
interrupts are classified after snapshot cleanup. Existing in-process errors
retain their prior semantics. Error bodies are at most 64 bytes; successful HTML
is at most 262144 UTF-8 bytes, with an additional adapter byte check. Every reply
has an exact Content-Length, no-store, nosniff and a restrictive CSP. Success uses
the reviewed renderer's field whitelist, HTML escaping, identifier rejection,
no-script/no-external-assets behavior and eleven `unobservable` runtime states.

`tests/test_p6_operator_web.py` drives both Definitions through the real
inspection, real PostgreSQL snapshot helper and real renderer using synthetic
canonical records, an opaque synthetic host capability and SQL/transaction
doubles. It checks ordering, repeat scopes, exact as-of changes, spoofed roles,
missing/denied/failing authorization, malformed requests, real lineage failures,
snapshot cleanup failures, render limits, unsafe identifiers and unchanged
consumer behavior. The host fixture is test-only evidence of boundary wiring;
it does not qualify real authentication, PostgreSQL or operational access.

This is an uncommitted source/tests/minimal-docs candidate. Next gates are owner
host QA (including full deterministic rerun for sandbox ptrace EPERM), fresh
independent revision-bound professional review, and separately authorized Git/CI
delivery. Real authentication/permission policy, connection injection and any
listener/deployment require their own gate. No provider, private material,
credential, PG/S3/Garage, source activation, MBO admission or control-root write
is part of this candidate. **B-END-TO-END-READ-CAPABILITY remains STOP / NOT PROVEN.**
