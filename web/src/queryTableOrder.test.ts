import { expect, it } from "vitest";
import { newestMonthFirst } from "./queryTableOrder";

it("orders across years, preserves same-month measures and leaves response/state untouched", () => {
  const rows = Object.freeze([
    Object.freeze({ month: "2025-12", metric: "first" }),
    Object.freeze({ month: "2026-01", metric: "second" }),
    Object.freeze({ month: "2025-12", metric: "third" }),
    Object.freeze({ month: "2026-01", metric: "fourth" }),
  ]);
  const body = JSON.stringify({ points: rows });
  expect(newestMonthFirst(rows, (r) => r.month).map((r) => r.metric)).toEqual([
    "second",
    "fourth",
    "first",
    "third",
  ]);
  expect(JSON.stringify({ points: rows })).toBe(body);
  expect(newestMonthFirst(rows, (r) => r.month)).not.toBe(rows);
});
