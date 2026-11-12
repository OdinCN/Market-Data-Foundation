import {
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type KeyboardEvent,
} from "react";
import { createPortal } from "react-dom";
import {
  beijingInputToIso,
  beijingToday,
  nowBeijingIso,
  toBeijingInput,
} from "./beijingTime";

type Precision = "month" | "date" | "datetime-local";
export type DateFieldProps = {
  label: string;
  type: Precision;
  value: string;
  onChange: (value: string) => void;
  id?: string;
  disabled?: boolean;
  required?: boolean;
  min?: string;
  max?: string;
  step?: string | number;
  className?: string;
};
const pad = (value: number) => String(value).padStart(2, "0");
const yearText = (value: number) => String(value).padStart(4, "0");
const weekdays = ["一", "二", "三", "四", "五", "六", "日"];

function normalize(value: string, type: Precision): string {
  if (type === "month") {
    return /^\d{4}-\d{2}$/.test(value) &&
      beijingInputToIso(`${value}-01T00:00:00`)
      ? value
      : "";
  }
  if (type === "date") {
    return /^\d{4}-\d{2}-\d{2}$/.test(value) &&
      beijingInputToIso(`${value}T00:00:00`)
      ? value
      : "";
  }
  return beijingInputToIso(value).replace(/\+08:00$/, "");
}

// UTC is used only for calendar arithmetic; statistical dates never shift zones.
function calendarDate(year: number, month: number, day: number): Date {
  const result = new Date(0);
  result.setUTCFullYear(year, month - 1, day);
  result.setUTCHours(0, 0, 0, 0);
  return result;
}
function scalar(value: string, type: Precision): number {
  if (type === "month")
    return Number(value.slice(0, 4)) * 12 + Number(value.slice(5, 7)) - 1;
  return (
    Date.parse(type === "date" ? `${value}T00:00:00Z` : `${value}Z`) /
    (type === "date" ? 86400000 : 1000)
  );
}

export function DateField({
  label,
  type,
  value,
  onChange,
  id,
  disabled = false,
  required = false,
  min,
  max,
  step,
  className,
}: DateFieldProps) {
  const uniqueId = useId();
  const inputId = id ?? `datefield-${uniqueId}`;
  const dialogId = `${inputId}-dialog`;
  const inputRef = useRef<HTMLInputElement>(null);
  const anchorRef = useRef<HTMLDivElement>(null);
  const panelRef = useRef<HTMLDivElement>(null);
  const returnFocus = useRef<HTMLElement | null>(null);
  const focusCalendar = useRef(false);
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState(value);
  const [view, setView] = useState(() => beijingToday().slice(0, 7));
  const [position, setPosition] = useState({
    left: 8,
    top: 8,
    width: 320,
    maxHeight: 600,
  });
  const canonical = normalize(draft, type);
  const lower = min ? normalize(min, type) : "";
  const upper = max ? normalize(max, type) : "";
  const increment = step === "any" ? 0 : Number(step ?? 1);
  const validStep = Number.isFinite(increment) && increment > 0 ? increment : 0;
  const base = scalar(
    lower ||
      (type === "month"
        ? "1970-01"
        : type === "date"
          ? "1970-01-01"
          : "1970-01-01T00:00:00"),
    type,
  );
  const placeholder =
    type === "month"
      ? "YYYY-MM"
      : type === "date"
        ? "YYYY-MM-DD"
        : "YYYY-MM-DDTHH:mm:ss";

  function errorFor(candidate: string): string {
    if (!candidate) return required ? "此项必填" : "";
    const normalized = normalize(candidate, type);
    if (!normalized) return `请输入有效的 ${placeholder}`;
    if (lower && normalized < lower) return `不得早于 ${lower}`;
    if (upper && normalized > upper) return `不得晚于 ${upper}`;
    if (validStep) {
      const difference = scalar(normalized, type) - base;
      const nearest = Math.round(difference / validStep) * validStep;
      const tolerance = Math.max(
        0.000001,
        Math.abs(difference) * Number.EPSILON * 8,
      );
      if (Math.abs(difference - nearest) > tolerance)
        return `请选择符合步长 ${validStep}${type === "datetime-local" ? " 秒" : type === "date" ? " 天" : " 个月"}的值`;
    }
    return "";
  }
  const error = errorFor(open ? draft : value);
  const dirty = open && normalize(draft, type) !== normalize(value, type);

  function close(restoreFocus = true) {
    setOpen(false);
    setDraft(value);
    if (restoreFocus) returnFocus.current?.focus();
  }
  function begin(fromCalendar: boolean) {
    if (disabled) return;
    returnFocus.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : inputRef.current;
    if (!open) {
      setDraft(value);
      const initial = normalize(value, type) || beijingToday();
      setView(initial.slice(0, 7));
    }
    focusCalendar.current = fromCalendar;
    setOpen(true);
  }
  function focusPanel(preferConfirm = false) {
    const panel = panelRef.current;
    const target =
      (preferConfirm
        ? panel?.querySelector<HTMLElement>(".datefield-confirm:not(:disabled)")
        : null) ??
      panel?.querySelector<HTMLElement>(
        '[data-selected="true"]:not(:disabled)',
      ) ??
      panel?.querySelector<HTMLElement>("[data-date]:not(:disabled)") ??
      panel?.querySelector<HTMLElement>("button:not(:disabled)") ??
      panel;
    target?.focus({ preventScroll: true });
  }
  function confirm() {
    if (disabled || errorFor(draft)) return;
    const next = draft ? normalize(draft, type) : "";
    // An unchanged display must not truncate a parent instant with fractional seconds.
    if (next !== normalize(value, type) || (!next && Boolean(value)))
      onChange(next);
    close();
  }

  useEffect(() => {
    inputRef.current?.setCustomValidity(
      error || (dirty ? "请确定或取消日期修改" : ""),
    );
  }, [error, dirty]);
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);
  useEffect(() => {
    if (!open) return;
    const outside = (event: PointerEvent) => {
      const target = event.target as Node;
      if (
        !anchorRef.current?.contains(target) &&
        !panelRef.current?.contains(target)
      )
        close(false);
    };
    document.addEventListener("pointerdown", outside);
    return () => document.removeEventListener("pointerdown", outside);
  });
  useLayoutEffect(() => {
    if (!open) return;
    const reposition = () => {
      const anchor = anchorRef.current?.getBoundingClientRect();
      const panel = panelRef.current;
      if (!anchor || !panel) return;
      const viewport = window.visualViewport;
      const viewportWidth = viewport?.width ?? window.innerWidth;
      const viewportHeight = viewport?.height ?? window.innerHeight;
      const viewportLeft = viewport?.offsetLeft ?? 0;
      const viewportTop = viewport?.offsetTop ?? 0;
      const width = Math.min(320, Math.max(0, viewportWidth - 16));
      const maxHeight = Math.max(0, viewportHeight - 16);
      const height = Math.min(
        panel.scrollHeight || panel.getBoundingClientRect().height,
        maxHeight,
      );
      const below = anchor.bottom + 6;
      const above = anchor.top - height - 6;
      const preferred =
        below + height <= viewportTop + viewportHeight - 8 ? below : above;
      setPosition({
        width,
        maxHeight,
        left: Math.max(
          viewportLeft + 8,
          Math.min(anchor.left, viewportLeft + viewportWidth - width - 8),
        ),
        top: Math.max(
          viewportTop + 8,
          Math.min(preferred, viewportTop + viewportHeight - height - 8),
        ),
      });
    };
    reposition();
    if (focusCalendar.current) {
      focusPanel();
      focusCalendar.current = false;
    }
    window.addEventListener("resize", reposition);
    window.addEventListener("scroll", reposition, true);
    window.visualViewport?.addEventListener("resize", reposition);
    window.visualViewport?.addEventListener("scroll", reposition);
    const observer =
      typeof ResizeObserver === "undefined"
        ? null
        : new ResizeObserver(reposition);
    if (panelRef.current) observer?.observe(panelRef.current);
    return () => {
      window.removeEventListener("resize", reposition);
      window.removeEventListener("scroll", reposition, true);
      window.visualViewport?.removeEventListener("resize", reposition);
      window.visualViewport?.removeEventListener("scroll", reposition);
      observer?.disconnect();
    };
  }, [open, view, type]);

  function handleKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && open) {
      event.preventDefault();
      event.stopPropagation();
      close();
    } else if (event.key === "Enter") {
      // Suppress implicit form submission, including events bubbling through the portal.
      event.preventDefault();
      event.stopPropagation();
      if (event.target instanceof HTMLButtonElement) event.target.click();
      else if (!open) begin(true);
      else focusPanel(true);
    } else if (event.key === "ArrowDown" && event.altKey) {
      event.preventDefault();
      event.stopPropagation();
      if (open) focusPanel();
      else begin(true);
    } else if (
      event.key === "Tab" &&
      open &&
      !event.shiftKey &&
      !panelRef.current?.contains(event.target as Node)
    ) {
      event.preventDefault();
      event.stopPropagation();
      focusPanel(event.target === inputRef.current);
    }
  }
  function panelKey(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab") return;
    const controls = Array.from(
      panelRef.current?.querySelectorAll<HTMLElement>(
        "button:not(:disabled), input:not(:disabled)",
      ) ?? [],
    );
    const first = controls[0];
    const last = controls.at(-1);
    if (
      event.shiftKey &&
      (event.target === first || event.target === panelRef.current)
    ) {
      event.preventDefault();
      last?.focus();
    } else if (!event.shiftKey && event.target === last) {
      event.preventDefault();
      first?.focus();
    }
    event.stopPropagation();
  }

  const year = Number(view.slice(0, 4));
  const month = Number(view.slice(5, 7));
  function navigate(months: number) {
    const next = calendarDate(year, month + months, 1);
    if (next.getUTCFullYear() < 1 || next.getUTCFullYear() > 9999) return;
    setView(
      `${yearText(next.getUTCFullYear())}-${pad(next.getUTCMonth() + 1)}`,
    );
  }
  function dayCandidate(
    day: string,
    clock = canonical ? canonical.slice(11) : "00:00:00",
  ): string {
    if (type !== "datetime-local") return day;
    const start = `${day}T00:00:00`;
    const end = `${day}T23:59:59${step === "any" ? ".999999999" : ""}`;
    const earliest = lower && lower > start ? lower : start;
    const latest = upper && upper < end ? upper : end;
    if (earliest > latest) return "";
    let candidate = `${day}T${clock}`;
    if (candidate < earliest) candidate = earliest;
    if (candidate > latest) candidate = latest;
    if (validStep) {
      const current = scalar(candidate, type);
      let aligned = base + Math.ceil((current - base) / validStep) * validStep;
      if (aligned > scalar(latest, type)) {
        aligned =
          base +
          Math.floor((scalar(latest, type) - base) / validStep) * validStep;
      }
      if (
        !Number.isFinite(aligned) ||
        aligned < scalar(earliest, type) ||
        aligned > scalar(latest, type)
      )
        return "";
      candidate = new Date(aligned * 1000).toISOString().slice(0, 19);
    }
    return candidate >= earliest && candidate <= latest && !errorFor(candidate)
      ? candidate
      : "";
  }
  function gridKey(event: KeyboardEvent<HTMLButtonElement>, date: string) {
    const movement: Record<string, number> = {
      ArrowLeft: -1,
      ArrowRight: 1,
      ArrowUp: type === "month" ? -3 : -7,
      ArrowDown: type === "month" ? 3 : 7,
    };
    const shift = movement[event.key];
    if (
      shift === undefined &&
      !["Home", "End", "PageUp", "PageDown"].includes(event.key)
    )
      return;
    event.preventDefault();
    event.stopPropagation();
    const current = calendarDate(
      Number(date.slice(0, 4)),
      Number(date.slice(5, 7)),
      type === "month" ? 1 : Number(date.slice(8, 10)),
    );
    if (event.key === "PageUp" || event.key === "PageDown") {
      const direction = event.key === "PageUp" ? -1 : 1;
      const targetMonth =
        current.getUTCMonth() +
        direction * (type === "month" || event.shiftKey ? 12 : 1);
      const day = current.getUTCDate();
      current.setUTCDate(1);
      current.setUTCMonth(targetMonth);
      current.setUTCDate(
        Math.min(
          day,
          calendarDate(
            current.getUTCFullYear(),
            current.getUTCMonth() + 2,
            0,
          ).getUTCDate(),
        ),
      );
    } else if (type === "month") {
      current.setUTCMonth(
        current.getUTCMonth() +
          (shift ??
            (event.key === "Home"
              ? -current.getUTCMonth()
              : 11 - current.getUTCMonth())),
      );
    } else {
      const weekday = (current.getUTCDay() + 6) % 7;
      current.setUTCDate(
        current.getUTCDate() +
          (shift ?? (event.key === "Home" ? -weekday : 6 - weekday)),
      );
    }
    if (current.getUTCFullYear() < 1 || current.getUTCFullYear() > 9999) return;
    const target = current.toISOString().slice(0, type === "month" ? 7 : 10);
    setView(target.slice(0, 7));
    // Focus follows navigation, while only an explicit cell activation changes the draft.
    requestAnimationFrame(() =>
      panelRef.current
        ?.querySelector<HTMLElement>(`[data-date="${target}"]:not(:disabled)`)
        ?.focus(),
    );
  }
  const currentValue = () =>
    type === "month"
      ? beijingToday().slice(0, 7)
      : type === "date"
        ? beijingToday()
        : toBeijingInput(nowBeijingIso());
  const today = currentValue();
  const quickValue =
    type === "datetime-local"
      ? dayCandidate(today.slice(0, 10), today.slice(11))
      : today;
  const daysInMonth = calendarDate(year, month + 1, 0).getUTCDate();
  const leading = (calendarDate(year, month, 1).getUTCDay() + 6) % 7;
  const cells = type === "month" ? 12 : 42;
  const columns = type === "month" ? 3 : 7;
  const timeParts = (draft.split("T")[1] ?? "00:00:00").split(":");

  return (
    <div
      className={`datefield${className ? ` ${className}` : ""}`}
      onKeyDown={handleKey}
      onClick={(event) => event.stopPropagation()}
    >
      <label className="datefield-label" htmlFor={inputId}>
        {label}
      </label>
      <div className="datefield-control" ref={anchorRef}>
        <input
          ref={inputRef}
          id={inputId}
          type="text"
          value={open ? draft : value}
          placeholder={placeholder}
          disabled={disabled}
          required={required}
          autoComplete="off"
          spellCheck={false}
          aria-invalid={Boolean(error)}
          aria-describedby={error ? `${inputId}-error` : `${inputId}-hint`}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? dialogId : undefined}
          onChange={(event) => {
            if (!open) begin(false);
            const raw = event.target.value;
            setDraft(raw);
            const next = normalize(raw, type);
            if (next) setView(next.slice(0, 7));
            // Keep invalid manual text in parent state so optional cutoffs
            // cannot silently reuse the prior instant or become an empty query.
            const output = raw && !errorFor(raw) ? next : raw;
            if (output !== value && (!next || next !== normalize(value, type)))
              onChange(output);
          }}
        />
        <button
          type="button"
          className="datefield-trigger"
          aria-label={`${label}：打开日历`}
          aria-haspopup="dialog"
          aria-expanded={open}
          aria-controls={open ? dialogId : undefined}
          disabled={disabled}
          onClick={(event) => {
            returnFocus.current = event.currentTarget;
            if (open) close();
            else {
              begin(true);
              returnFocus.current = event.currentTarget;
            }
          }}
        >
          <svg viewBox="0 0 24 24" width="18" height="18" aria-hidden="true">
            <rect x="3" y="5" width="18" height="16" rx="2" />
            <path d="M16 3v4M8 3v4M3 11h18M7 15h2M12 15h2M7 18h2" />
          </svg>
        </button>
      </div>
      <span id={`${inputId}-hint`} className="datefield-hint">
        {type === "datetime-local" ? "北京时间（UTC+08:00）" : placeholder}
      </span>
      {error && !open && (
        <span className="datefield-error" id={`${inputId}-error`} role="alert">
          {error}
        </span>
      )}
      {open &&
        !disabled &&
        createPortal(
          <div
            ref={panelRef}
            id={dialogId}
            role="dialog"
            aria-label={`${label}选择器`}
            tabIndex={-1}
            className="datefield-popover"
            style={position}
            onKeyDown={panelKey}
            onClick={(event) => event.stopPropagation()}
          >
            <div className="datefield-heading">
              {label}
              {type === "datetime-local" ? " · 北京时间" : ""}
            </div>
            <div className="datefield-navigation">
              <button
                type="button"
                aria-label="上一年"
                disabled={year <= 1}
                onClick={() => navigate(-12)}
              >
                «
              </button>
              {type !== "month" && (
                <button
                  type="button"
                  aria-label="上个月"
                  disabled={year === 1 && month === 1}
                  onClick={() => navigate(-1)}
                >
                  ‹
                </button>
              )}
              <span aria-live="polite">
                {yearText(year)} 年{type !== "month" ? ` ${month} 月` : ""}
              </span>
              {type !== "month" && (
                <button
                  type="button"
                  aria-label="下个月"
                  disabled={year === 9999 && month === 12}
                  onClick={() => navigate(1)}
                >
                  ›
                </button>
              )}
              <button
                type="button"
                aria-label="下一年"
                disabled={year >= 9999}
                onClick={() => navigate(12)}
              >
                »
              </button>
            </div>
            <div
              role="grid"
              aria-label={type === "month" ? "月份" : "日期"}
              className={`datefield-grid${type === "month" ? " datefield-months" : ""}`}
            >
              {type !== "month" && (
                <div role="row" className="datefield-row">
                  {weekdays.map((day) => (
                    <span
                      key={day}
                      role="columnheader"
                      className="datefield-weekday"
                    >
                      {day}
                    </span>
                  ))}
                </div>
              )}
              {Array.from({ length: cells / columns }, (_, row) => (
                <div key={row} role="row" className="datefield-row">
                  {Array.from({ length: columns }, (_, col) => {
                    const index = row * columns + col;
                    const day = index - leading + 1;
                    const empty =
                      type !== "month" && (day < 1 || day > daysInMonth);
                    const date =
                      type === "month"
                        ? `${yearText(year)}-${pad(index + 1)}`
                        : `${view}-${pad(day)}`;
                    const candidate = empty ? "" : dayCandidate(date);
                    const selected = Boolean(
                      canonical && canonical.slice(0, date.length) === date,
                    );
                    return (
                      <div key={col} role="gridcell" aria-selected={selected}>
                        {!empty && (
                          <button
                            type="button"
                            data-date={date}
                            data-selected={selected}
                            aria-label={date}
                            aria-pressed={selected}
                            aria-current={
                              date === beijingToday().slice(0, date.length)
                                ? "date"
                                : undefined
                            }
                            disabled={
                              !candidate || Boolean(errorFor(candidate))
                            }
                            onKeyDown={(event) => gridKey(event, date)}
                            onClick={() => setDraft(candidate)}
                          >
                            {type === "month" ? `${index + 1}月` : day}
                          </button>
                        )}
                      </div>
                    );
                  })}
                </div>
              ))}
            </div>
            {type === "datetime-local" && (
              <div
                className="datefield-time"
                role="group"
                aria-label="北京时间（时分秒）"
              >
                {["时", "分", "秒"].map((unit, index) => (
                  <label key={unit} htmlFor={`${inputId}-time-${index}`}>
                    <span>{unit}</span>
                    <input
                      id={`${inputId}-time-${index}`}
                      type="number"
                      inputMode="numeric"
                      min={0}
                      max={
                        index === 0
                          ? 23
                          : index === 2 && step === "any"
                            ? 59.999999999
                            : 59
                      }
                      step={index === 2 && step === "any" ? "any" : 1}
                      value={timeParts[index] ?? "00"}
                      onChange={(event) => {
                        const parts = [...timeParts];
                        parts[index] =
                          event.target.value === ""
                            ? ""
                            : event.target.value.padStart(2, "0");
                        setDraft(
                          `${normalize(draft.slice(0, 10), "date") || beijingToday()}T${parts.join(":")}`,
                        );
                      }}
                    />
                  </label>
                ))}
              </div>
            )}
            <div className="datefield-draft" aria-live="polite">
              {draft || "未选择"}
            </div>
            {error && (
              <div
                className="datefield-error"
                id={`${inputId}-error`}
                role="alert"
              >
                {error}
              </div>
            )}
            <div className="datefield-footer">
              <button
                type="button"
                disabled={required}
                onClick={() => setDraft("")}
              >
                清除
              </button>
              <button
                type="button"
                disabled={!quickValue || Boolean(errorFor(quickValue))}
                onClick={() => {
                  let next = currentValue();
                  if (type === "datetime-local")
                    next = dayCandidate(next.slice(0, 10), next.slice(11));
                  if (!next || errorFor(next)) return;
                  setDraft(next);
                  setView(next.slice(0, 7));
                }}
              >
                {type === "datetime-local" ? "现在" : "今天"}
              </button>
              <button type="button" onClick={() => close()}>
                取消
              </button>
              <button
                type="button"
                className="datefield-confirm"
                disabled={Boolean(error)}
                onClick={confirm}
              >
                确定
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
