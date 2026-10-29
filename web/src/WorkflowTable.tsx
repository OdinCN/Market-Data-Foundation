import { useEffect, useRef, useState, type ReactNode } from "react";
export type Row = { id: string; cells: ReactNode[]; search: string };
export function WorkflowTable({
  label,
  columns,
  rows,
  selected,
  onSelect,
  children,
  empty = "暂无可用条目",
  tools,
  pageSize,
  locateId,
  className,
}: {
  label: string;
  columns: string[];
  rows: Row[];
  selected?: string | null;
  onSelect: (id: string) => void;
  children?: ReactNode;
  empty?: string;
  tools?: ReactNode;
  pageSize?: number;
  locateId?: string | null;
  className?: string;
}) {
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  const located = useRef<string | null>(null);
  useEffect(() => {
    if (!locateId) {
      located.current = null;
      return;
    }
    if (located.current === locateId) return;
    setSearch("");
    const index = rows.findIndex((row) => row.id === locateId);
    setPage(pageSize && index >= 0 ? Math.floor(index / pageSize) : 0);
    if (index >= 0) located.current = locateId;
  }, [locateId, rows, pageSize]);
  const visible = rows.filter((r) =>
    r.search.toLowerCase().includes(search.trim().toLowerCase()),
  );
  const lastPage = pageSize
    ? Math.max(0, Math.ceil(visible.length / pageSize) - 1)
    : 0;
  const currentPage = Math.min(page, lastPage);
  const shown = pageSize
    ? visible.slice(currentPage * pageSize, (currentPage + 1) * pageSize)
    : visible;
  return (
    <div className={`workbench${className ? ` ${className}` : ""}`}>
      <section aria-label={label} className="list-pane">
        <div className="searchbar">
          <input
            ref={input}
            aria-label={`搜索${label}`}
            placeholder={`搜索${label}`}
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              setPage(0);
            }}
          />
          <button
            onClick={() => {
              setSearch("");
              input.current?.focus();
            }}
          >
            清除
          </button>
          {tools}
        </div>
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                {columns.map((c) => (
                  <th key={c}>{c}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shown.map((row) => (
                <tr
                  key={row.id}
                  className={row.id === selected ? "selected" : ""}
                  aria-selected={row.id === selected}
                  onClick={() => onSelect(row.id)}
                >
                  {row.cells.map((cell, i) => (
                    <td key={i}>
                      {i === 0 ? (
                        <button
                          className="row-link"
                          onClick={(e) => {
                            e.stopPropagation();
                            onSelect(row.id);
                          }}
                          aria-label={`选择 ${row.search}`}
                        >
                          {cell}
                        </button>
                      ) : (
                        cell
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
          {!visible.length && (
            <p className="empty">
              {search ? "无匹配结果，请调整搜索条件。" : empty}
            </p>
          )}
        </div>
        {pageSize && visible.length > pageSize && (
          <div className="actions">
            <button
              disabled={currentPage === 0}
              onClick={() => setPage(currentPage - 1)}
            >
              前一页记录
            </button>
            <span>第 {currentPage + 1} 页</span>
            <button
              disabled={currentPage >= lastPage}
              onClick={() => setPage(currentPage + 1)}
            >
              后一页记录
            </button>
          </div>
        )}
      </section>
      <aside className="detail-pane" aria-label={`${label}详情`}>
        {children ?? (
          <div className="empty detail-empty">
            <span aria-hidden="true">▦</span>
            <h3>选择一项查看详情</h3>
            <p>详细信息与关联操作将在此显示。</p>
          </div>
        )}
      </aside>
    </div>
  );
}
export function Fact({
  name,
  children,
}: {
  name: string;
  children: ReactNode;
}) {
  return (
    <div className="fact">
      <dt>{name}</dt>
      <dd>{children}</dd>
    </div>
  );
}
export function Technical({
  children,
  open = false,
}: {
  children: ReactNode;
  open?: boolean;
}) {
  return (
    <details className="technical" open={open}>
      <summary>技术详情</summary>
      {children}
    </details>
  );
}
export function Tabs<T extends string>({
  label,
  values,
  value,
  onChange,
}: {
  label: string;
  values: readonly (readonly [T, string])[];
  value: string;
  onChange: (value: T) => void;
}) {
  return (
    <div role="tablist" aria-label={label} className="tabs">
      {values.map(([id, text]) => (
        <button
          key={id}
          role="tab"
          aria-selected={value === id}
          onClick={() => onChange(id)}
        >
          {text}
        </button>
      ))}
    </div>
  );
}
