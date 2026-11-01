// Finite Decimal wire values, including Python's scale-preserving "0E-9".
// Validate the entire string without rounding or rewriting its coefficient.
export const isDecimalString = (value: unknown): value is string =>
  typeof value === "string" &&
  value.trim() === value &&
  /^[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?$/.test(value);

// Zero exponents carry source scale, but are clearer as zero in table cells.
// Do not coerce nonzero Decimal strings (including underflow-sized values).
export function decimalDisplayText(value: string | number): string {
  const text = String(value);
  const [coefficient, exponent] = text.split(/[eE]/);
  return exponent !== undefined &&
    isDecimalString(text) &&
    /^[+-]?(?:0+(?:\.0*)?|\.0+)$/.test(coefficient)
    ? "0"
    : text;
}

// Retail levels and official percent are already in their display units.
// Work on digits only: Number would round high-precision source values.
export function retailDecimalText(value: string | number): string {
  const text = decimalDisplayText(value);
  if (!isDecimalString(text) || /[eE]/.test(text)) return text;
  const [integer, fraction = ""] = text.split(".");
  const tail = fraction.replace(/0+$/, "");
  if (/^[+-]?0*$/.test(integer) && !tail) return "0";
  return `${(integer || "0").replace(/\B(?=(\d{3})+(?!\d))/g, ",")}${tail ? `.${tail}` : ""}`;
}
