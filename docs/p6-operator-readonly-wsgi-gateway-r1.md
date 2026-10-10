# P6 in-process Operator WSGI gateway candidate

`canonical.operator_wsgi.OperatorWSGIGateway` is a synchronous PEP 3333 callable
for the delivered `OperatorWebEntry`. Standard-library `wsgiref.validate.validator`
and `wsgiref.handlers.SimpleHandler` can invoke it entirely with memory streams.
No server, listener, socket, authentication policy or connection is created.

```python
from wsgiref.util import setup_testing_defaults
from wsgiref.validate import validator
from market_data_foundation.canonical.operator_wsgi import OperatorWSGIGateway

environ = {}
setup_testing_defaults(environ)
environ.update(
    PATH_INFO="/operator/report",
    QUERY_STRING="definition=bls-cpi-headlines&as_of=2026-10-06T00:00:20Z",
)


def start_response(status, headers, exc_info=None):
    assert status == "403 Forbidden"
    return lambda data: None


result = validator(OperatorWSGIGateway())(environ, start_response)
try:
    assert b"".join(result) == b"operator_forbidden\n"
finally:
    result.close()
```

A trusted host supplies `OperatorWSGIGateway(entry, context_factory=...)`, where
`entry` is the existing `OperatorWebEntry` with its independent exact-selection
authorizer and exclusive consistent read-only scope factory. `context_factory()`
receives **no request or environ arguments**, runs after existing admission, and
must obtain a genuine Operator capability independently of request data. It runs
fresh for each admitted request; no context is cached or retained on the gateway.
Missing injection, `None`, injection failure (including interrupts), failed or
missing authorization returns the existing 403. The factory is a prerequisite,
not an authentication implementation: a host deriving it from Header, Cookie,
URL, `REMOTE_USER` or any environ extension violates the contract. Such fields
never supply context here, even if they contain the host's exact capability.
This does not isolate malicious Python code or coordinate concurrent scopes.

WSGI native strings are converted through Latin-1, with bounds checked before
encoding. Method, path and query are never coerced, repaired or decoded again.
`PATH_INFO` is the WSGI **already-decoded**, application-relative path;
`SCRIPT_NAME` belongs to host mounting and is not prepended. Query/fragment
delimiters inside PATH_INFO are rejected to prevent cross-field interpretation.
`QUERY_STRING` is joined unchanged, including percent escapes and parameter
order. Only the existing selectors, canonical UTC instants and request syntax
are admitted. No alternate request handler or public wire parser is introduced.
The gateway reuses the existing internal admission/error helpers; entry.handle
retains its own checks. Existing operator_web/consumer/parser files are unchanged.

CGI `HTTP_*` names must be uppercase, map underscores to hyphens, and their
Latin-1 values pass the existing printable-ASCII checks. `CONTENT_TYPE` and
nonempty `CONTENT_LENGTH` join the same 16-header/4096-byte aggregate budget;
names and values retain the existing 64/1024-byte bounds. Mapped duplicate
names, `HTTP_CONTENT_TYPE`, `HTTP_CONTENT_LENGTH` and Transfer-Encoding are
rejected. Missing/empty CONTENT_LENGTH means zero; a bounded nonempty string
of ASCII zero digits is accepted and preserved. All positive, malformed,
signed or whitespace length values fail 400. EOF-delimited `wsgi.input_terminated`
extensions are rejected unless explicitly False. No input stream is read,
probed, drained or closed; reading beyond a declared zero can block and violates
WSGI framing. The host owns disposal of rejected request input.

The host must supply a valid PEP 3333 environ, faithful CGI framing and a body
of exactly the declared length (zero here). Undeclared bytes or a falsely zero
length cannot be detected safely by a zero-body application. WSGI cannot recover
raw URI spellings or duplicate headers already decoded/combined by a host.
Encoded spellings that the host turns into the canonical PATH_INFO therefore
have that decoded route meaning; raw REQUEST_URI/RAW_URI hints are ignored.
If raw-path spelling rejection is required, the host must enforce it before
decoding. It must also reject raw duplicate headers, hyphen/underscore CGI-name
collisions, conflicting length/transfer framing and hidden transfer encodings
before generating environ. The gateway makes no claim to validate raw HTTP or
to reconstruct lost budgets. These are conditions of host qualification.

The existing entry owns authorization, scope, inspection and rendering. No
response starts until the entire chain and successful scope exit finish. The
gateway calls `start_response` once with a native status/reason string and a
fresh list of native string header tuples, then returns a finite iterable with
one bytes element. It never uses `write()`, yields partial HTML, retries or streams
scope work. The iterable needs no close method and owns no open resource.
Every HEAD response, including 400 and 405, suppresses the body and omits
Content-Length: the entry's error-body length is not a corresponding GET length.
A single empty block in an iterator without a length sends headers without
letting bare SimpleHandler infer a fabricated Content-Length of zero. Other
methods retain the entry's exact Content-Length and body in a one-element tuple.
Every 405 response carries exactly one `Allow: GET`; HEAD remains unsupported.
All responses retain no-store, nosniff and CSP from the entry, with 262144-byte
success and fixed small error bodies for non-HEAD requests. Existing 400/403/404/405/503
classifications remain unchanged. start_response/host transport exceptions
propagate after cleanup; a broken host cannot be promised a replacement error
response and is never retried. The gateway does not log raw exceptions.

Offline tests use real synthetic inspections/report rendering and the existing
SQL/snapshot doubles. They cover both Definitions, repeated/mounted requests,
standard validator/handler interoperation (with and without validator), HEAD
body/Content-Length suppression, 405 Allow, spoofed context, zero-body framing,
header limits, request ambiguity, missing authority/scope, failed cleanup and
discard, inspection/render interrupts, exact error lengths and host callback
failure. They prove no real authentication, PostgreSQL isolation or transport
resource bounds. **B-END-TO-END-READ-CAPABILITY remains STOP / NOT PROVEN.**

This is an uncommitted source/tests/minimal-docs candidate. Next gates are owner
host QA (including truthful reporting/rerun of sandbox TDX ptrace EPERM), fresh
revision-bound independent professional review, then conditionally authorized
normal non-force Git delivery and exact-head CI. No network, provider, PG/S3,
credentials/private material, user permissions system, deployment/adoption,
SourceBinding activation, MBO admission, whole-P6 exit or control-root mutation
is part of this task.
