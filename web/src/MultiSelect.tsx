import { useEffect, useId, useRef, useState } from "react";

export type MultiSelectOption = { value: string; label: string };

export function MultiSelect({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly MultiSelectOption[];
  value: readonly string[];
  onChange: (value: string[]) => void;
}) {
  const id = useId();
  const root = useRef<HTMLDivElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const [open, setOpen] = useState(false);
  const summary =
    options
      .filter((option) => value.includes(option.value))
      .map((option) => option.label)
      .join("、") || "未选择";
  useEffect(() => {
    if (!open) return;
    const close = (event: PointerEvent) => {
      if (!root.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", close);
    return () => document.removeEventListener("pointerdown", close);
  }, [open]);
  return (
    <div
      className="cpi-multiselect"
      ref={root}
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}
      onKeyDown={(event) => {
        if (event.key === "Escape") {
          event.preventDefault();
          setOpen(false);
          trigger.current?.focus();
        }
      }}
    >
      <span id={`${id}-label`} className="cpi-multiselect-label">
        {label}
      </span>
      <button
        type="button"
        ref={trigger}
        aria-labelledby={`${id}-label`}
        aria-describedby={`${id}-summary`}
        aria-expanded={open}
        aria-controls={`${id}-options`}
        onClick={() => setOpen(!open)}
        onKeyDown={(event) => {
          if (event.key === "ArrowDown") {
            event.preventDefault();
            setOpen(true);
            requestAnimationFrame(() =>
              root.current
                ?.querySelector<HTMLInputElement>('input[type="checkbox"]')
                ?.focus(),
            );
          }
        }}
      >
        <span id={`${id}-summary`}>{summary}</span>
        <span aria-hidden="true"> ▾</span>
      </button>
      {open && (
        <div
          id={`${id}-options`}
          className="cpi-multiselect-options"
          role="group"
          aria-label={`${label}选项`}
          onKeyDown={(event) => {
            if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key))
              return;
            const inputs = Array.from(
              event.currentTarget.querySelectorAll<HTMLInputElement>(
                'input[type="checkbox"]',
              ),
            );
            const index = inputs.indexOf(
              document.activeElement as HTMLInputElement,
            );
            if (index < 0) return;
            event.preventDefault();
            const next =
              event.key === "Home"
                ? 0
                : event.key === "End"
                  ? inputs.length - 1
                  : (index +
                      (event.key === "ArrowDown" ? 1 : -1) +
                      inputs.length) %
                    inputs.length;
            inputs[next]?.focus();
          }}
        >
          <div className="actions">
            <button
              type="button"
              onClick={() => onChange(options.map((option) => option.value))}
            >
              全选
            </button>
            <button type="button" onClick={() => onChange([])}>
              清空
            </button>
          </div>
          {options.map((option) => (
            <label key={option.value}>
              <input
                type="checkbox"
                checked={value.includes(option.value)}
                onChange={(event) =>
                  onChange(
                    options
                      .filter((item) =>
                        item.value === option.value
                          ? event.target.checked
                          : value.includes(item.value),
                      )
                      .map((item) => item.value),
                  )
                }
              />
              <span>{option.label}</span>
            </label>
          ))}
          <p className="small">
            已选 {value.length} / {options.length} 项
          </p>
        </div>
      )}
    </div>
  );
}
