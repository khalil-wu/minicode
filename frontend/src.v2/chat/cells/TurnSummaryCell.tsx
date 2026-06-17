import type React from "react";
import { CheckCircle2, ChevronRight, Loader2, TriangleAlert } from "lucide-react";
import type { TurnSummaryCellState } from "./cellTypes";
import "./cells.css";

export function TurnSummaryCell({ cell }: { cell: TurnSummaryCellState }) {
  if (cell.items.length === 0) return null;
  const label = summaryLabel(cell);
  const meta = summaryMeta(cell);
  const details = cell.items
    .map((item) => [item.label, item.detail].filter(Boolean).join(" "))
    .filter(Boolean)
    .join(" · ");
  const icon =
    cell.status === "running" ? (
      <Loader2 size={13} className="spinner" style={{ color: "var(--accent-primary)" }} />
    ) : cell.status === "failed" || cell.status === "interrupted" ? (
      <TriangleAlert size={13} style={{ color: "var(--state-danger)" }} />
    ) : (
      <CheckCircle2 size={13} style={{ color: "var(--text-muted)" }} />
    );

  return (
    <div className="turn-summary-cell" aria-label="Turn activity summary" title={details}>
      <span className="turn-summary-icon">{icon}</span>
      <span className="turn-summary-label">{label}</span>
      {meta && <span className="turn-summary-detail">{meta}</span>}
      <ChevronRight size={12} className="turn-summary-chevron" aria-hidden="true" />
    </div>
  );
}

function summaryLabel(cell: TurnSummaryCellState): string {
  if (cell.status === "running") return "正在处理";
  if (cell.status === "failed" || cell.status === "interrupted") return "需要处理";
  return "已处理";
}

function summaryMeta(cell: TurnSummaryCellState): string {
  const total = cell.items.length;
  if (total <= 0) return "";
  const failed = cell.items.some((item) => item.tone === "danger");
  if (failed) return `${total} 项有异常`;
  return `${total} 项`;
}
