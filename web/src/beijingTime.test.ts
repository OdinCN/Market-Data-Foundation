import { beijingInstantKey } from "./beijingTime";
import { afterEach, describe, expect, it, vi } from "vitest";
import {
  beijingInputToIso,
  beijingToday,
  formatBeijingTime,
  nowBeijingIso,
  toBeijingInput,
  toBeijingIso,
} from "./beijingTime";

afterEach(() => vi.useRealTimers());

describe("Beijing instant formatting", () => {
  it("crosses the UTC date boundary and preserves seconds", () => {
    expect(formatBeijingTime("2024-02-28T16:02:03Z")).toBe(
      "2024-02-29 00:02:03 北京时间",
    );
    expect(toBeijingInput("2024-02-28T16:02:03Z")).toBe("2024-02-29T00:02:03");
  });
  it("roundtrips the same instant with an explicit Beijing offset", () => {
    const input = "2024-03-01T02:03:04-05:30";
    const wall = toBeijingInput(input);
    expect(wall).toBe("2024-03-01T15:33:04");
    expect(beijingInputToIso(wall)).toBe("2024-03-01T15:33:04+08:00");
    expect(Date.parse(beijingInputToIso(wall))).toBe(Date.parse(input));
    expect(toBeijingInput("2024-03-01T15:33:04+08:00")).toBe(wall);
  });
  it("accepts standard fractions without rounding into a different second", () => {
    expect(toBeijingInput("2024-12-31T15:59:59.999999Z")).toBe(
      "2024-12-31T23:59:59",
    );
    expect(toBeijingInput("2024-12-31T16:00+00:00")).toBe(
      "2025-01-01T00:00:00",
    );
  });
  it.each([
    "",
    "garbage",
    "2024-01-01",
    "2024-01-01T12:00",
    "2024-01-01T12:00:00",
    "2024-01-01 12:00:00Z",
    "2024-01-01T12:00:00+0800",
    "2024-01-01T12:00:00+24:00",
    "2024-01-01T12:00:00+08:60",
    "2023-02-29T12:00:00Z",
    "2024-02-30T12:00:00Z",
    "2024-04-31T12:00:00Z",
    "2024-13-01T12:00:00Z",
    "2024-00-01T12:00:00Z",
    "2024-01-00T12:00:00Z",
    "2024-01-01T24:00:00Z",
    "2024-01-01T12:60:00Z",
    "2024-01-01T12:00:60Z",
    "0000-01-01T00:00:00Z",
    "1900-02-29T00:00:00Z",
    "2024-01-01T12:00.5Z",
    "2024-01-01T12:00:00.Z",
    " 2024-01-01T12:00:00Z",
    "2024-01-01T12:00:00Z\n",
  ])("rejects invalid or offsetless source %s", (source) => {
    expect(toBeijingInput(source)).toBe("");
    expect(formatBeijingTime(source)).toBe("未提供");
    expect(formatBeijingTime(source, "—")).toBe("—");
  });
  it("handles missing source values and Gregorian century leap rules", () => {
    expect(formatBeijingTime(null)).toBe("未提供");
    expect(formatBeijingTime(undefined, "空")).toBe("空");
    expect(toBeijingInput("2000-02-29T00:00:00Z")).toBe("2000-02-29T08:00:00");
    expect(toBeijingInput("0001-01-01T00:00:00Z")).toBe("0001-01-01T08:00:00");
  });
  it("is independent of non-Beijing process time zones", () => {
    const original = process.env.TZ;
    try {
      for (const zone of [
        "America/Los_Angeles",
        "Europe/Berlin",
        "Pacific/Honolulu",
      ]) {
        process.env.TZ = zone;
        expect(toBeijingInput("2024-03-10T09:59:59Z")).toBe(
          "2024-03-10T17:59:59",
        );
        expect(beijingInputToIso("2024-03-10T17:59:59")).toBe(
          "2024-03-10T17:59:59+08:00",
        );
      }
    } finally {
      if (original === undefined) delete process.env.TZ;
      else process.env.TZ = original;
    }
  });
});

describe("Beijing wall clocks", () => {
  it("validates real calendar dates and accepts integration minute precision", () => {
    expect(beijingInputToIso("2024-02-29T23:59:59")).toBe(
      "2024-02-29T23:59:59+08:00",
    );
    expect(beijingInputToIso("2024-02-29T23:59")).toBe(
      "2024-02-29T23:59:00+08:00",
    );
    expect(beijingInputToIso("0099-01-01T00:00:00")).toBe(
      "0099-01-01T00:00:00+08:00",
    );
  });
  it.each([
    "2023-02-29T00:00:00",
    "1900-02-29T00:00:00",
    "2024-04-31T00:00:00",
    "2024-00-01T00:00:00",
    "2024-13-01T00:00:00",
    "2024-01-00T00:00:00",
    "2024-01-01T24:00:00",
    "2024-01-01T00:60:00",
    "2024-01-01T00:00:60",
    "2024-1-01T00:00:00",
    "2024-01-01T0:00:00",
    "2024-01-01T00:00:00.1",
    "2024-01-01T00:00:00Z",
    "2024-01-01T00:00:00+08:00",
    "0000-01-01T00:00:00",
    "2024-01-01T00:00:00\n",
    "2024-01-01T00:00\r\n",
    "",
  ])("rejects invalid wall clock %s", (wall) =>
    expect(beijingInputToIso(wall)).toBe(""),
  );
  it("gets Beijing now and today without the host timezone", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2025-12-31T16:00:01.234Z"));
    expect(nowBeijingIso()).toBe("2026-01-01T00:00:01.234+08:00");
    expect(beijingToday()).toBe("2026-01-01");
  });
});

describe("fraction-preserving Beijing normalization", () => {
  it.each(["1", "123", "123456", "000000001"])(
    "preserves every fractional digit %s",
    (fraction) => {
      const source = `2024-02-29T16:00:00.${fraction}Z`;
      const result = toBeijingIso(source);
      expect(result).toBe(`2024-03-01T00:00:00.${fraction}+08:00`);
      expect(Date.parse(result)).toBe(Date.parse(source));
      expect(toBeijingIso(result)).toBe(result);
    },
  );
  it.each(["", "2024-02-30T00:00:00Z", "2024-02-29T00:00:00"])(
    "rejects invalid or offsetless source %s",
    (value) => expect(toBeijingIso(value)).toBe(""),
  );
});

it("matches offset representations without dropping sub-millisecond precision", () => {
  expect(beijingInstantKey("2026-10-28T20:00:00.123456Z")).toBe(
    beijingInstantKey("2026-10-29T04:00:00.123456+08:00"),
  );
  expect(beijingInstantKey("2026-10-28T20:00:00.123456Z")).not.toBe(
    beijingInstantKey("2026-10-28T20:00:00.123457Z"),
  );
  expect(beijingInstantKey("2026-10-28T20:00:00.123000Z")).toBe(
    beijingInstantKey("2026-10-29T04:00:00.123+08:00"),
  );
  expect(beijingInstantKey("2026-10-28T20:00:00.000Z")).toBe(
    beijingInstantKey("2026-10-29T04:00:00+08:00"),
  );
});
