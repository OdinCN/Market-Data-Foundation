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
    ? "正在保存策略，等待结果"
    : uncertain
      ? "保存结果未知，等待独立回读"
      : enabled === null
        ? "策略状态尚未核实"
        : "仅控制策略启用，执行器状态独立显示";
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
      <span className="strategy-switch-label">
        {saving
          ? "保存中"
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
