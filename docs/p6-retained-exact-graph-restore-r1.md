# Internal retained exact graph restore candidate

`RetainedExactGraph` imports one retained Version, its complete canonical lineage,
whole content bytes and its original first committed Publication. It is bounded
to 16 records and 1 MiB of content. Decision chains, input Version graphs and
general migration are outside this candidate.

The trusted caller must independently establish the legal source proof window
and approve `RestoreBinding` before import. Its three digests bind the complete
graph, canonical Publication and retained PG readback together. A newly generated
binding or a caller-written readback is **not** evidence of historical commitment.
This is an internal control boundary, not a public consumer or upload API.
Control must verify the historical frozen indexes, canonical nodes/content and
independent committed readback before approving the binding; missing proof stops
the affected path. Neither hashes nor this API authenticate arbitrary history.

Validation runs off-target and checks complete exact lineage, content, evidence
claims, original identity/occurrence/decision/usable times, first-decision state,
and acquisition/run/freeze/decision chronology. Before digest or lineage checks,
canonical field types are checked recursively, including actual enums, bools,
frozen containers and embedded contracts. Input/producer dependencies must be
acyclic; assessment targets and other canonical backrefs remain permitted.
Unknown source timestamps stay
unknown. Effective time retains the existing contract, including permitted
retroactive effect; usability cannot precede decision. Opaque canonical reference
fields are preserved; no receipt or locator is resolved, fetched or logged.

`restore_in_memory()` stages before replacing the reference ledger state.
`PostgresLedger.restore_exact_graph()` requires an idle connection and owns one
transaction, locking records, contents and publications before conflict checks.
It inserts the original Publication without calling `publish()` or sampling a
clock, and returns only after commit. Repeated identical imports are idempotent;
different identities, content, occurrence mappings or Version decision histories
fail closed. Existing unrelated records remain intact. A commit transport error
returns no Publication: reconcile by retrying the same bound graph on a fresh
connection. Ordinary `put()` still rejects Publication, and `publish()` retains
its server-timed new-decision behavior.

No schema layout, admitted Definition, P3 model, `mdf.consumer.read.v1` or existing
read error changes. Restore does not assert present provider/storage availability,
freshness, durability, replayability, source activation or admission.

This candidate has synthetic transaction tests and local retained-byte replay.
It has no live PG proof. Product Control must independently review the exact
candidate and preflight the isolated development namespace before any future PG
writer-close/fresh-observer/repeated-read/cleanup proof. There is no CLI, automatic
connection, adoption, production namespace or delivery in this candidate.
