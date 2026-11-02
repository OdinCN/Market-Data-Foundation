import { useId, useRef } from "react";

export const quarterValid = (value: unknown): value is string =>
  typeof value === "string" && /^[0-9]{4}-Q[1-4]$/.test(value);
export function shiftQuarter(value: string, offset: number) {
  const index =
    Number(value.slice(0, 4)) * 4 + Number(value.slice(6)) - 1 + offset;
  return `${Math.floor(index / 4)}-Q${(((index % 4) + 4) % 4) + 1}`;
}
export const quarterCount = (start: string, end: string) =>
  (Number(end.slice(0, 4)) - Number(start.slice(0, 4))) * 4 +
  Number(end.slice(6)) -
  Number(start.slice(6)) +
  1;
export const quarterRangeValid = (start: string, end: string) =>
  quarterValid(start) &&
  quarterValid(end) &&
  start >= "1901-Q1" &&
  start <= end &&
  quarterCount(start, end) <= 800;
export function previousCompleteQuarter(now = new Date()) {
  return shiftQuarter(
    `${now.getUTCFullYear()}-Q${Math.floor(now.getUTCMonth() / 3) + 1}`,
    -1,
  );
}
export function QuarterField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  const choices = useId();
  const input = useRef<HTMLInputElement>(null);
  const inputId = `${choices}-input`;
  const latest = previousCompleteQuarter();
  return (
    <div className="datefield">
      <label className="datefield-label" htmlFor={inputId}>
        {label}
      </label>
      <div className="datefield-control">
        <input
          id={inputId}
          ref={input}
          aria-label={label}
          type="text"
          placeholder="YYYY-Qn"
          pattern="[0-9]{4}-Q[1-4]"
          maxLength={7}
          list={choices}
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
        <button
          type="button"
          className="datefield-trigger"
          aria-label={`${label}：选择季度`}
          onClick={() => {
            input.current?.focus();
            input.current?.showPicker?.();
          }}
        >
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <rect x="3" y="5" width="18" height="16" rx="2" />
            <path d="M16 3v4M8 3v4M3 11h18M7 15h2M12 15h2M7 18h2" />
          </svg>
        </button>
      </div>
      <span className="datefield-hint">YYYY-Qn · 可输入或选择季度</span>
      <datalist id={choices}>
        {Array.from({ length: 40 }, (_, offset) =>
          shiftQuarter(latest, -offset),
        ).map((quarter) => (
          <option key={quarter} value={quarter} />
        ))}
      </datalist>
    </div>
  );
}
