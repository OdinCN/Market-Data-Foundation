/** Beijing wall time uses the product's fixed modern UTC+08:00 offset. */
const BEIJING_OFFSET_MS = 8 * 60 * 60 * 1000;
const WALL_CLOCK = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2}))?$/;
const INSTANT =
  /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.(\d+))?)?(Z|[+-]\d{2}:\d{2})$/;

function legalCalendar(parts: RegExpMatchArray): boolean {
  const [, y, m, d, h, minute, second = "0"] = parts;
  const year = Number(y);
  const month = Number(m);
  const leap = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  return (
    year >= 1 &&
    year <= 9999 &&
    month >= 1 &&
    month <= 12 &&
    Number(d) >= 1 &&
    Number(d) <= days[month - 1] &&
    Number(h) <= 23 &&
    Number(minute) <= 59 &&
    Number(second) <= 59
  );
}

function sourceInstant(value: string | null | undefined): number | null {
  if (typeof value !== "string") return null;
  const parts = value.match(INSTANT);
  if (!parts || parts[0] !== value || !legalCalendar(parts)) return null;
  const offset = parts[8];
  if (
    offset !== "Z" &&
    (Number(offset.slice(1, 3)) > 23 || Number(offset.slice(4)) > 59)
  )
    return null;
  // Validate the source calendar above, before Date.parse can normalize February 30.
  const time = Date.parse(value);
  return Number.isFinite(time) ? time : null;
}

function beijingWallClock(time: number): string {
  const shifted = new Date(time + BEIJING_OFFSET_MS);
  const year = shifted.getUTCFullYear();
  if (year < 1 || year > 9999) return "";
  return shifted.toISOString().slice(0, 19);
}

export function formatBeijingTime(
  value: string | null | undefined,
  fallback = "未提供",
): string {
  const time = sourceInstant(value);
  const wall = time === null ? "" : beijingWallClock(time);
  return wall ? `${wall.replace("T", " ")} 北京时间` : fallback;
}

export function toBeijingInput(value: string): string {
  const time = sourceInstant(value);
  return time === null ? "" : beijingWallClock(time);
}

/** Invalid wall clocks return an empty string; no guessed or normalized dates. */
export function beijingInputToIso(value: string): string {
  const parts = value.match(WALL_CLOCK);
  if (!parts || parts[0] !== value || !legalCalendar(parts)) return "";
  return `${parts[6] === undefined ? `${value}:00` : value}+08:00`;
}

/** Normalize an instant without losing the original fractional-second precision. */
export function toBeijingIso(value: string): string {
  const time = sourceInstant(value);
  if (time === null) return "";
  const wall = beijingWallClock(time);
  if (!wall) return "";
  const fraction = value.match(INSTANT)?.[7];
  return `${wall}${fraction === undefined ? "" : `.${fraction}`}+08:00`;
}

export function nowBeijingIso(): string {
  return toBeijingIso(new Date().toISOString());
}

export function beijingToday(): string {
  return beijingWallClock(Date.now()).slice(0, 10);
}

/** Compare instants without collapsing sub-millisecond historical cutoffs. */
export function beijingInstantKey(value: string | null | undefined): string {
  return toBeijingIso(value ?? "").replace(
    /\.(\d+)\+08:00$/,
    (_match, fraction: string) => {
      const significant = fraction.replace(/0+$/, "");
      return `${significant ? `.${significant}` : ""}+08:00`;
    },
  );
}
