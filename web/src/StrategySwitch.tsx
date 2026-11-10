export function StrategySwitch({
  asset,
  enabled,
  saving,
  uncertain,
  disabled,
  onToggle,
}: {
  asset: string;
  enabled: boolean | null;
  saving: boolean;
  uncertain: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  const note = saving
    ? "正在协调自动采集，等待实际回读"
    : uncertain
      ? "保存结果未知，等待独立回读"
      : enabled === null
        ? "策略状态尚未核实"
        : "自动采集意图；实际运行情况另列";
  return (
    <button
      type="button"
      role="switch"
      aria-label={`${asset}自动采集策略`}
      aria-checked={enabled === true}
      aria-busy={saving || uncertain}
      aria-description={note}
      title={note}
      className="strategy-switch"
      data-unconfirmed={enabled === null || uncertain}
      disabled={disabled}
      onClick={(event) => {
        event.stopPropagation();
        onToggle();
      }}
    >
      <span className="strategy-switch-thumb" aria-hidden="true" />
      <span className="strategy-switch-label sr-only">
        {saving
          ? "正在开启或暂停"
          : uncertain
            ? "待核实"
            : enabled === null
              ? "未核实"
              : enabled
                ? "启用"
                : "暂停"}
      </span>
    </button>
  );
}
