import { memo, useEffect, useState } from "react";
import { Bot, ChevronDown, ChevronRight } from "lucide-react";
import type { CollaborationCellState } from "./cellTypes";
import "./cells.css";

export const CollaborationCell = memo(function CollaborationCell({
  cell,
}: {
  cell: CollaborationCellState;
}) {
  const [expanded, setExpanded] = useState(!cell.collapsed);

  useEffect(() => {
    setExpanded(!cell.collapsed);
  }, [cell.collapsed, cell.id]);

  const agentCount = new Set(cell.entries.map((entry) => entry.agentId)).size;
  const actionLabel = cell.action === "closed"
    ? cell.status === "running" ? "正在关闭" : cell.status === "success" ? "已关闭" : "关闭失败"
    : cell.status === "running" ? "正在发送" : cell.status === "success" ? "已发送消息" : "发送失败";
  const summary = `${actionLabel} ${agentCount} 个智能体`;
  const canExpand = cell.entries.length > 0;

  return (
    <div className="collaboration-cell" data-action={cell.action} data-status={cell.status}>
      <button
        type="button"
        className="collaboration-cell-summary"
        aria-label={canExpand ? `${expanded ? "收起" : "展开"}${summary}详情` : summary}
        aria-expanded={canExpand ? expanded : undefined}
        disabled={!canExpand}
        onClick={() => { if (canExpand) setExpanded((value) => !value); }}
      >
        <Bot size={15} strokeWidth={1.8} aria-hidden="true" />
        <span>{summary}</span>
        {canExpand && (
          <span className="collaboration-cell-chevron" aria-hidden="true">
            {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
          </span>
        )}
      </button>

      {expanded && (
        <div className="collaboration-cell-details">
          {cell.entries.map((entry, index) => (
            <div
              key={`${entry.agentId}-${index}-${entry.content || cell.action}`}
              className="collaboration-cell-detail"
            >
              <span className="collaboration-cell-detail-action">{actionLabel}</span>
              <strong>{entry.agentLabel}</strong>
              {entry.content && (
                <>
                  <span aria-hidden="true">：</span>
                  <span className="collaboration-cell-message" title={entry.content}>{entry.content}</span>
                </>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
});
