import type React from "react";
import { CheckCircle2, CircleDot, Loader2, TerminalSquare, TriangleAlert } from "lucide-react";
import type { TurnSummaryCellState } from "./cellTypes";
import "./cells.css";

export function TurnSummaryCell({ cell }: { cell: TurnSummaryCellState }) {
  if (cell.items.length === 0) return null;
  const icon =
    cell.status === "running" ? (
      <Loader2 size={13} className="spinner" style={{ color: "var(--accent-primary)" }} />
    ) : cell.status === "failed" || cell.status === "interrupted" ? (
      <TriangleAlert size={13} style={{ color: "var(--state-danger)" }} />
    ) : (
      <CheckCircle2 size={13} style={{ color: "var(--text-muted)" }} />
    );

  return (
    <div className="turn-summary-cell" aria-label="Turn activity summary">
      <span className="turn-summary-icon">{icon}</span>
      {cell.items.map((item, index) => (
        <span key={`${item.kind}-${item.label}-${index}`} className={`turn-summary-item turn-summary-item-${item.tone}`}>
          {item.kind === "command" ? <TerminalSquare size={12} /> : <CircleDot size={9} />}
          <span className="turn-summary-label">{item.label}</span>
          {item.detail ? <span className="turn-summary-detail">{item.detail}</span> : null}
        </span>
      ))}
    </div>
  );
}
