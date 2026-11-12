# Registering a family in the shared workbench

`web/src/runtimeFamilies.ts` is the default entry for a non-Macro data family.
A `RuntimeFamily` supplies its exact asset identity/revision, title, local query,
API help, collection parameters, execution detail, and read adapters for execution
and periodic facts. Register the adapter in `RUNTIME_FAMILIES`; do not add an
asset-specific page branch in `App` or create another task table.

The normal source directory must expose the asset's binding, and the package
catalog remains responsible for runtime visibility and permissions. Source deep
links must match the exact asset/revision; neither registration nor an empty
local read authorizes a provider fallback.

`RuntimeAssets` owns catalog search, package filtering, selection and expansion.
`FamilyAssetReader` owns the normal query/API tabs. `CollectionWorkbench` owns
creation target selection, task tabs, independent asset filters, execution
ordering, pagination, search and the two-pane execution layout.
`CpiStrategyWorkbench` accepts an array of `PeriodicEntry` supplements alongside
its existing Macro and financial entries, retaining the existing strategy
switch, desktop detail pane and narrow-screen drawer interaction.

`useFamilyRecords` reads only registrations matching the active global filter.
Execution row identities include the asset revision and request identity, so a
shared request ID across families cannot select another family's detail. The
route owns the selection; reloading or returning restores the named object.
Ordinary row selection keeps the search and filters. Initial deep links and
submission receipts can locate their row across table pages.

Adapters must describe their own domain facts. TDX uses native price basis,
market membership, calendar/suspension evidence, durable collection shards and
provider request counts. It must not use Macro month/series coverage metrics or
present scheduler heartbeat as a latest data period. TDX query, row pagination
and independent missing-item pagination use local GET reads. Time instants are
validated with a zone and displayed in Beijing time. Mutations are explicit;
a lost submission receipt is recalled by its original request identity without
automatic resubmission.

`RegisteredFamilyPaths.test.tsx` runs the same user path for TDX and a synthetic
future registration through `App`: package to catalog, API help, standard target
and form, submission, global execution filters/search/details, reload, and global
periodic details. It also checks exact source deep links and conflicting revisions.
The synthetic registration uses the same shared renderers without an App branch.
A restoration of the old whole-page bypass fails these path assertions. Keep
provider execution in separate opt-in tests; these fixtures never use a network
listener, LAN database or live collection.

Daily query and collection forms use `DailyFormControls`: `DailyDateRange`
(implemented with the product `DateField`), `HistoryCutoffFields`,
`QueryResultTabs`, `CollectionColumns`, `ExecutionBriefCard` and range validation.
Financial daily and TDX render these actual controls. The shared workbench always
owns the collection target, including for registrations with a minimal leaf.
A family must retain its domain defaults and limits: TDX's fourteen-day lookback
is independent from the financial family's fourteen inclusive calendar days.
Initialization and update drafts remain independent; the submitted range is the
range shown in the form. Recent execution cards come from persisted jobs and
navigate to the global named execution detail.

Historical cutoffs accept fractional Beijing wall-clock seconds without passing
through a millisecond-only `Date` conversion. The preview and cURL reflect the
current validated request. Edits cancel in-flight queries and clear their results;
controls never trigger an implicit query. Row and missing-item pages remain
separate. Pending summaries do not gate the form or appear as empty holdings.

`tdxJobOrigin` presents the origin returned by the jobs API. The API records new
manual/periodic intents explicitly and qualifies old automatic slots by the
resident strategy's exact UUIDv5 namespace/name contract. Other legacy origins
remain unverified rather than being labeled manual from an action or UUID
version; the frontend does not duplicate that provenance derivation.
`DailyFamilyForms.test.tsx` exercises the same actual query/collection interactions
for financial daily and TDX, including precision, previews, cancellation, result
views, independent drafts, exact submitted ranges and recent-job navigation.

DEV 18766 may compose the existing Macro capabilities using the explicit
`--allow-unified-dev-macro-operations` flag together with `--tdx-state-directory`
and `--tdx-dev-port 18766`. Other listeners reject this opt-in before constructing
runtimes. The socket adapter and ASGI routes use the same server-owned policy;
they validate the actual private peer, Host, Origin, framing and existing exact
path/method allowlists. No request authority is rewritten or forwarded. Existing
per-capability collection, strategy, source and package flags remain mandatory.
Use the existing Macro job/schedule directories and worker window configuration;
this flag starts no Macro worker and changes no strategy intent.

`QueryPagination` is the actual shared financial/TDX result pager. Each displayed
page corresponds to one API cursor; there is no additional client pager inside
the TDX result table. Previous/next navigation remains available at terminal pages.
`LocalQuerySummary` retains holding details without pushing the query form below
a long metric list. Both components live in `DailyFormControls`.

For read-only deployed acceptance, run `web/scripts/check-live-workbench.mjs`
with the origin and an evidence directory. Set `MDF_PLAYWRIGHT_MODULE` to an
installed Playwright module and `MDF_BROWSER_EXECUTABLE` to Chromium. The default
cases exercise financial daily, CPI and (on 18766) TDX through the actual catalog,
query/results/raw response, API help, collection target, real recent jobs,
global execution deep links and periodic details. Every mutation is blocked.
Query 4xx, absent result rows and missing-only results fail. The checker respects
CPI's `points` schema and daily families' `rows` schema. A JSON case array supplied
by `MDF_WORKBENCH_CASES` can register another family's row selector, exact asset,
API prefix, row/value properties and form parameters without another page script.

`rangeWithDefaults` merges daily collection defaults per untouched field. An
operator may edit a start date while status is loading, switch collection modes,
and receive the remaining end-date default without losing either draft. Pending
financial receipts pin both submitted fields. TDX's immediate fallback follows
the same Beijing 15:05 boundary as its backend; server defaults still qualify the
untouched fields. Daily row reads and coverage reads have independent loading
and cancellation states: returned rows remain pageable during reconciliation,
and paging rows does not duplicate an in-flight coverage request.
