import { afterEach, expect, it, vi } from "vitest";
import { cleanup, renderHook, waitFor } from "@testing-library/react";
import type { Transport } from "./api";
import {
  buildCpiQueryRequest,
  cpiRequest,
  CpiServiceFailure,
  queryCpiRange,
  readCpiJobs,
  readCpiStrategy,
  useCpiAvailability,
  type CpiHttpExchange,
  type CpiResult,
  type CpiScope,
} from "./cpiTimeSeriesApi";

// All HTTP responses are synthetic; no sockets, providers or stores.
const scope: CpiScope = {
  start: "2026-09",
  end: "2026-09",
  basket: "headline",
  adjustment: "sa",
  measure: "index",
};
const signal = () => new AbortController().signal;
function result(selected: CpiScope = scope): CpiResult {
  const history = selected.query_mode === "history" || !!selected.as_of;
  return {
    start_month: selected.start,
    end_month: selected.end,
    series_ids: ["CUSR0000SA0"],
    points: [
      {
        series_id: "CUSR0000SA0",
        month: selected.start,
        index_value: "320.125",
        mom_pct: "0.24",
        yoy_pct: "2.82",
        change_1m_ratio: "0.0024",
        change_3m_ratio: "0.0083",
        change_6m_ratio: "0.0145",
        change_12m_ratio: "0.0282",
      },
    ],
    missing: [],
    calculation_missing: [],
    complete: true,
    coverage_complete: true,
    persisted: false,
    provider_requests: 0,
    provider_attempts: 0,
    vintage_mode: history ? "available" : "observed",
    query_mode: history ? "history" : "current",
    as_of_basis: history ? "available" : "observed",
    as_of: selected.as_of ?? null,
    measure: selected.measure,
  };
}
const transportFor = (bodyText: string, status = 200) =>
  vi.fn<Transport>(async () => new Response(bodyText, { status }));

it.each(["0E-9", "-1.25e+3", "1E-1000", "12345678901234567890.123456789"])(
  "preserves shared query Decimal wire value %s",
  async (value) => {
    const body = result();
    body.points[0].index_value = value;
    body.points[0].change_1m_ratio = value;
    const raw = JSON.stringify(body);
    const observer = vi.fn();
    await expect(
      queryCpiRange(transportFor(raw), scope, signal(), observer),
    ).resolves.toEqual(body);
    expect(observer.mock.calls[0][0].bodyText).toBe(raw);
  },
);

it.each(["NaN", "Infinity", "-Infinity", "1e", "1e2junk", "", "0x10", "1\n"])(
  "rejects invalid shared query decimal %j",
  async (value) => {
    const body = result();
    body.points[0].index_value = value;
    await expect(
      queryCpiRange(transportFor(JSON.stringify(body)), scope, signal()),
    ).rejects.toThrow("cpi_result_scope_unknown");
  },
);

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("uses the public preview builder verbatim for the actual GET", async () => {
  const request = buildCpiQueryRequest(scope);
  const transport = transportFor(JSON.stringify(result()));
  const observer = vi.fn();
  await expect(
    queryCpiRange(transport, scope, signal(), observer),
  ).resolves.toEqual(result());
  expect(request.method).toBe("GET");
  expect(request.path).toBe(
    "/api/r5/cpi/timeseries/query?start=2026-09&end=2026-09&basket=headline&adjustment=sa&measure=index&query_mode=current&local_only=false",
  );
  expect(transport).toHaveBeenCalledWith(
    request.path,
    expect.objectContaining({
      method: request.method,
      cache: "no-store",
      credentials: "same-origin",
    }),
  );
  const exchange: CpiHttpExchange = observer.mock.calls[0][0];
  expect(exchange).toEqual({
    method: "GET",
    url: new URL(request.path, location.origin).href,
    status: 200,
    bodyText: JSON.stringify(result()),
    elapsedMs: expect.any(Number),
    completedAt: expect.any(String),
  });
  expect(exchange.elapsedMs).toBeGreaterThanOrEqual(0);
  expect(Number.isFinite(Date.parse(exchange.completedAt))).toBe(true);
});

it.each([
  { query_mode: "history" as const },
  { query_mode: "current" as const, as_of: "2026-10-29T22:15:00+08:00" },
  { query_mode: "history" as const, as_of: "2026-10-29T22:15:00+08:00" },
])(
  "forces local available history for %j and encodes the timezone",
  async (mode) => {
    const selected: CpiScope = { ...scope, vintage_mode: "observed", ...mode };
    const request = buildCpiQueryRequest(selected);
    const params = new URL(request.path, location.origin).searchParams;
    expect(Object.fromEntries(params)).toEqual({
      ...scope,
      query_mode: "history",
      local_only: "true",
      vintage_mode: "available",
      as_of_basis: "available",
      ...(mode.as_of ? { as_of: mode.as_of } : {}),
    });
    if (mode.as_of) expect(request.path).toContain("%2B08%3A00");
    const transport = transportFor(JSON.stringify(result(selected)));
    await queryCpiRange(transport, selected, signal());
    expect(transport.mock.calls[0][0]).toBe(request.path);
  },
);

it("preserves decimal digits, large integer digits and whitespace from HTTP", async () => {
  const bodyText = `  ${JSON.stringify(result()).replace('"320.125"', "320.123456789012345678901234567890").replace('"points":[{', '"points":[{"revision":900719925474099312345,')}\n`;
  const observer = vi.fn();
  await queryCpiRange(transportFor(bodyText), scope, signal(), observer);
  expect(observer).toHaveBeenCalledOnce();
  expect(observer.mock.calls[0][0].bodyText).toBe(bodyText);
});

it.each([
  [
    503,
    '{ "classification": "storage_unavailable", "detail": 900719925474099312345 }',
    "storage_unavailable",
    503,
  ],
  [400, '{"classification":"unsafe error text"}', "cpi_response_unknown", 400],
  [502, "<html>upstream unavailable</html>", "cpi_response_unknown", undefined],
  [200, "not JSON", "cpi_response_unknown", undefined],
  [200, '{"points":[]}', "cpi_result_scope_unknown", undefined],
  [404, "legacy host without JSON", "cpi_host_not_found", 404],
])(
  "reports raw HTTP %i before rejecting (%s)",
  async (status, bodyText, classification, errorStatus) => {
    const observer = vi.fn();
    const error = await queryCpiRange(
      transportFor(bodyText, status),
      scope,
      signal(),
      observer,
    ).catch((failure: unknown) => failure);
    expect(observer).toHaveBeenCalledOnce();
    expect(observer.mock.calls[0][0]).toMatchObject({ status, bodyText });
    expect(error).toBeInstanceOf(CpiServiceFailure);
    expect(error).toMatchObject({ classification, status: errorStatus });
  },
);

it.each([
  new TypeError("synthetic network failure"),
  new DOMException("synthetic timeout", "TimeoutError"),
  new DOMException("synthetic cancellation", "AbortError"),
])("does not invent an exchange without HTTP (%s)", async (failure) => {
  const observer = vi.fn();
  const transport = vi.fn<Transport>(async () => {
    throw failure;
  });
  await expect(
    queryCpiRange(transport, scope, signal(), observer),
  ).rejects.toMatchObject({
    classification: "cpi_response_unknown",
    status: undefined,
  });
  expect(observer).not.toHaveBeenCalled();
});

it("suppresses stale exchanges when cancellation happens while reading the body", async () => {
  const controller = new AbortController();
  const response = new Response(JSON.stringify(result()));
  const readText = response.text.bind(response);
  vi.spyOn(response, "text").mockImplementation(async () => {
    controller.abort();
    return readText();
  });
  const observer = vi.fn();
  await queryCpiRange(async () => response, scope, controller.signal, observer);
  expect(observer).not.toHaveBeenCalled();
});

it.each(["sync", "async"])(
  "isolates %s observer failures from success and HTTP errors",
  async (mode) => {
    const observer = () => {
      if (mode === "sync") throw new Error("synthetic observer failure");
      return Promise.reject(new Error("synthetic observer rejection"));
    };
    await expect(
      queryCpiRange(
        transportFor(JSON.stringify(result())),
        scope,
        signal(),
        observer,
      ),
    ).resolves.toEqual(result());
    await expect(
      queryCpiRange(
        transportFor('{"classification":"storage_unavailable"}', 503),
        scope,
        signal(),
        observer,
      ),
    ).rejects.toMatchObject({
      classification: "storage_unavailable",
      status: 503,
    });
  },
);

it("keeps a safe relative URL outside a browser", async () => {
  vi.stubGlobal("window", undefined);
  const observer = vi.fn();
  await queryCpiRange(
    transportFor(JSON.stringify(result())),
    scope,
    signal(),
    observer,
  );
  expect(observer.mock.calls[0][0].url).toBe(buildCpiQueryRequest(scope).path);
});

it("keeps the original three-argument query caller and JSON read behavior", async () => {
  const response = new Response(JSON.stringify(result()));
  const json = vi.spyOn(response, "json");
  const text = vi.spyOn(response, "text");
  await expect(
    queryCpiRange(async () => response, scope, signal()),
  ).resolves.toEqual(result());
  expect(json).toHaveBeenCalledOnce();
  expect(text).not.toHaveBeenCalled();
});

it("preserves existing status/jobs/strategy requests and request options", async () => {
  const jobs = { jobs: [] };
  await expect(
    readCpiJobs(transportFor(JSON.stringify(jobs)), signal()),
  ).resolves.toEqual(jobs);
  const strategy = {
    exists: false,
    enabled: false,
    history_start: "2006-01",
    worker: { state: "stopped", last_heartbeat_at: null },
    next_due: null,
    policy: {},
  };
  await expect(
    readCpiStrategy(transportFor(JSON.stringify(strategy)), signal()),
  ).resolves.toEqual(strategy);
  const transport = transportFor('{"ok":true}');
  const requestSignal = signal();
  await expect(
    cpiRequest(transport, "/strategy", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: '{"enabled":false}',
      signal: requestSignal,
    }),
  ).resolves.toEqual({ ok: true });
  expect(transport).toHaveBeenCalledWith(
    "/api/r5/cpi/timeseries/strategy",
    expect.objectContaining({
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: '{"enabled":false}',
      cache: "no-store",
      credentials: "same-origin",
      signal: expect.any(AbortSignal),
    }),
  );
  await expect(
    cpiRequest(
      transportFor('{"classification":"storage_unavailable"}', 503),
      "/status",
    ),
  ).rejects.toMatchObject({
    classification: "storage_unavailable",
    status: 503,
  });
});

it("still treats a non-JSON status 404 as legacy availability", async () => {
  const transport = transportFor("legacy host", 404);
  const { result: availability } = renderHook(() =>
    useCpiAvailability(transport, 0, true, false),
  );
  await waitFor(() => expect(availability.current.legacy).toBe(true));
  expect(availability.current.failure).toBeNull();
  expect(availability.current.data).toBeNull();
});
