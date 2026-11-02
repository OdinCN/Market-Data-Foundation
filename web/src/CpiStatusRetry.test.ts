import { afterEach, expect, it, vi } from "vitest";
import { readCpiStatus } from "./cpiTimeSeriesApi";
import { PPI_PROFILE } from "./macroTimeSeriesProfile";
const busy = () =>
  new Response(JSON.stringify({ classification: "cpi_timeseries_busy" }), {
    status: 503,
  });
afterEach(() => vi.useRealTimers());
it("recovers an explicit read lock without retrying writes", async () => {
  vi.useFakeTimers();
  const transport = vi
    .fn()
    .mockImplementationOnce(busy)
    .mockResolvedValueOnce(new Response('{"status":"OK"}'));
  const result = readCpiStatus(
    transport,
    new AbortController().signal,
    PPI_PROFILE,
  );
  await vi.advanceTimersByTimeAsync(200);
  expect(await result).toEqual({ status: "OK" });
  expect(transport).toHaveBeenCalledTimes(2);
  expect(
    transport.mock.calls.every(
      ([path, init]) =>
        path === PPI_PROFILE.basePath + "/status" && init.method === "GET",
    ),
  ).toBe(true);
});
it("stops after three retries and reports the actual failure", async () => {
  vi.useFakeTimers();
  const transport = vi.fn().mockImplementation(busy);
  const result = readCpiStatus(transport, new AbortController().signal).catch(
    (e) => e,
  );
  await vi.runAllTimersAsync();
  expect((await result).classification).toBe("cpi_timeseries_busy");
  expect(transport).toHaveBeenCalledTimes(4);
});
it.each([
  [503, "cpi_timeseries_not_enabled"],
  [403, "local_read_not_authorized"],
  [503, "cpi_jobs_busy"],
])("does not retry %s %s", async (status, classification) => {
  const transport = vi
    .fn()
    .mockImplementation(
      () => new Response(JSON.stringify({ classification }), { status }),
    );
  await expect(
    readCpiStatus(transport, new AbortController().signal),
  ).rejects.toMatchObject({ status, classification });
  expect(transport).toHaveBeenCalledTimes(1);
});
it("cancels the pending retry when the consumer leaves", async () => {
  vi.useFakeTimers();
  const transport = vi.fn().mockImplementation(busy);
  const controller = new AbortController();
  const result = readCpiStatus(transport, controller.signal).catch((e) => e);
  await vi.advanceTimersByTimeAsync(100);
  controller.abort();
  await vi.runAllTimersAsync();
  expect((await result).name).toBe("AbortError");
  expect(transport).toHaveBeenCalledTimes(1);
});
