import type { ReactNode } from "react";
import { DateField } from "./DateField";
import { Tabs } from "./WorkflowTable";
import { formatBeijingTime, beijingInputToIso } from "./beijingTime";

export function rangeWithDefaults(
  draft: { start: string; end: string },
  defaults: { start: string; end: string },
  edited: { start: boolean; end: boolean },
) {
  return {
    start: edited.start ? draft.start : defaults.start,
    end: edited.end ? draft.end : defaults.end,
  };
}

export function dailyRangeError(
  range: { start: string; end: string },
  maxDays: number,
  latestEnd: string,
) {
  const a = beijingInputToIso(range.start + "T00:00:00"),
    b = beijingInputToIso(range.end + "T00:00:00");
  if (!a || !b) return "请填写合法的采集开始和结束日期。";
  if (range.start > range.end) return "采集开始日期不得晚于结束日期。";
  if ((Date.parse(b) - Date.parse(a)) / 86400000 + 1 > maxDays)
    return `采集范围不得超过 ${maxDays} 个日历日。`;
  if (range.end > latestEnd)
    return `采集结束日期不得晚于 ${latestEnd}（北京时间当日边界）。`;
  return "";
}

export function DailyDateRange({
  start,
  end,
  onStart,
  onEnd,
  collection = false,
  disabled = false,
  max,
  min,
  singleDay = false,
}: {
  start: string;
  end: string;
  onStart: (value: string) => void;
  onEnd: (value: string) => void;
  collection?: boolean;
  disabled?: boolean;
  max?: string;
  min?: string;
  singleDay?: boolean;
}) {
  return (
    <>
      {!singleDay && (
        <DateField
          label={collection ? "采集开始日期" : "开始日期"}
          type="date"
          value={start}
          onChange={onStart}
          required
          disabled={disabled}
          min={min}
          max={max}
        />
      )}
      <DateField
        label={singleDay ? "交易日" : collection ? "采集结束日期" : "结束日期"}
        type="date"
        value={end}
        onChange={onEnd}
        required
        disabled={disabled}
        min={min}
        max={max}
      />
    </>
  );
}
export function HistoryCutoffFields({
  enabled,
  value,
  onEnabled,
  onChange,
}: {
  enabled: boolean;
  value: string;
  onEnabled: (value: boolean) => void;
  onChange: (value: string) => void;
}) {
  return (
    <>
      <label className="cpi-history-toggle">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => onEnabled(e.target.checked)}
        />
        <span>历史截止查询</span>
      </label>
      {enabled && (
        <div className="cpi-history-fields">
          <p className="small">
            按 MDF
            确认可用时间查看截止时已保存的数据；时间基准为北京时间，支持小数秒。
          </p>
          <DateField
            label="截止时间（北京时间）"
            type="datetime-local"
            value={value}
            onChange={onChange}
            required
            step="any"
          />
        </div>
      )}
    </>
  );
}
export function QueryResultTabs({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
}) {
  return (
    <Tabs
      label={`${label} 结果视图`}
      values={[
        ["table", "表格"],
        ["raw", "原始响应"],
      ]}
      value={value}
      onChange={onChange}
    />
  );
}
export function LocalQuerySummary({
  title = "本地市场汇总",
  children,
}: {
  title?: ReactNode;
  children: ReactNode;
}) {
  return (
    <details className="local-query-summary">
      <summary>{title}</summary>
      {children}
    </details>
  );
}
export function QueryPagination({
  label,
  offset,
  total,
  pageSize,
  nextOffset,
  pending,
  onPage,
}: {
  label: string;
  offset: number;
  total: number;
  pageSize: number;
  nextOffset: number | null;
  pending: boolean;
  onPage: (offset: number) => void;
}) {
  return (
    <div className="actions" aria-label={`${label} 结果分页`}>
      <button
        disabled={pending || offset === 0}
        onClick={() => onPage(Math.max(0, offset - pageSize))}
      >
        上一页
      </button>
      <span>
        第 {Math.floor(offset / pageSize) + 1} /{" "}
        {Math.max(1, Math.ceil(total / pageSize))} 页，每页最多 {pageSize} 条
      </span>
      <button
        disabled={pending || nextOffset === null}
        onClick={() => nextOffset !== null && onPage(nextOffset)}
      >
        下一页
      </button>
    </div>
  );
}
export function CollectionColumns({
  children,
  briefs,
}: {
  children: ReactNode;
  briefs: ReactNode;
}) {
  return (
    <div className="workbench cpi-collection-workbench">
      <div className="collection-input-pane">{children}</div>
      {briefs}
    </div>
  );
}
export function ExecutionBriefCard({
  id,
  title,
  createdAt,
  start,
  end,
  result,
  current,
  scope,
  onSelect,
  disabled,
}: {
  id: string;
  title: string;
  createdAt: string;
  start?: string;
  end?: string;
  result: ReactNode;
  current?: boolean;
  scope?: string;
  onSelect: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      className="execution-brief-card"
      disabled={disabled}
      aria-current={current ? "true" : undefined}
      onClick={onSelect}
    >
      <strong>{title}</strong>
      <dl>
        <div>
          <dt>请求编号</dt>
          <dd className="request-id">{id}</dd>
        </div>
        <div>
          <dt>创建时间</dt>
          <dd>{formatBeijingTime(createdAt).replace(" 北京时间", "")}</dd>
        </div>
        <div>
          <dt>采集范围</dt>
          <dd>{scope ?? `${start ?? "未记录"} 至 ${end ?? "未记录"}`}</dd>
        </div>
        <div>
          <dt>结果</dt>
          <dd>{result}</dd>
        </div>
      </dl>
    </button>
  );
}
