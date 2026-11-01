import { expect, it } from "vitest";
import { decimalDisplayText, isDecimalString } from "./decimalValue";

it.each([
  "0",
  "-0",
  "0E-9",
  "-0E+9",
  "1.25E-7",
  "-1.25e+30",
  "+1E+3",
  ".5",
  "1.",
  "12345678901234567890.123456789",
  "1E+1000",
])("accepts finite Decimal string %s without numeric coercion", (value) => {
  expect(isDecimalString(value)).toBe(true);
});

it.each([
  null,
  undefined,
  1,
  NaN,
  Infinity,
  "",
  " ",
  "NaN",
  "sNaN",
  "Infinity",
  "-Infinity",
  "inf",
  "1E",
  "1e+",
  "1.2.3",
  "1_000",
  "0x10",
  "1foo",
  "1\n",
  " 1",
  "1 ",
  "--1",
  "1e2junk",
])("rejects non-decimal wire value %s", (value) => {
  expect(isDecimalString(value)).toBe(false);
});

it.each(["0E-9", "-0E+9", "+0.000e-9", ".0e+2"])(
  "displays scientific zero %s as zero",
  (value) => {
    expect(decimalDisplayText(value)).toBe("0");
  },
);
it.each([
  "1E-1000",
  "-1.25E-7",
  "1.00000000000000000001E+30",
  "123456.123456789",
])("preserves nonzero decimal text %s without coercion", (value) => {
  expect(decimalDisplayText(value)).toBe(value);
});
