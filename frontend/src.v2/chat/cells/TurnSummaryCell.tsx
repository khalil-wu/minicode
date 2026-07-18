import type React from "react";
import { CheckCircle2, ChevronRight, TriangleAlert } from "lucide-react";
import type { TurnSummaryCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import "./cells.css";

const CJK_RE = /[㐀-䶿一-鿿豈-﫿]/;

function useTurnSummaryLang(): "zh" | "en" {
  const messages = useAppStore((s) => s.messages);
  // Detect from the most recent user message
  for (let i = messages.length - 1; i >= 0; i--) {
    if (messages[i]?.role === "user") {
      return messages[i].content && CJK_RE.test(messages[i].content) ? "zh" : "en";
    }
  }
  return "zh";
}

export function TurnSummaryCell({ cell }: { cell: TurnSummaryCellState }) {
  if (cell.items.length === 0) return null;
  const lang = useTurnSummaryLang();
  const label = summaryLabel(cell, lang);
  const meta = summaryMeta(cell, lang);
  const isRunning = cell.status === "running";
  const details = cell.items
    .map((item) => [item.label, item.detail].filter(Boolean).join(" "))
    .filter(Boolean)
    .join(" · ");
  const icon =
    cell.status === "failed" || cell.status === "interrupted" ? (
      <TriangleAlert size={13} style={{ color: "var(--state-danger)" }} />
    ) : (
      <CheckCircle2 size={13} style={{ color: "var(--text-muted)" }} />
    );

  return (
    <div className="turn-summary-cell" aria-label="Turn activity summary" title={details}>
      {!isRunning && <span className="turn-summary-icon">{icon}</span>}
      <span
        className={isRunning ? "turn-summary-label agent-loop-thinking-shimmer" : "turn-summary-label"}
        data-text={isRunning ? label : undefined}
      >
        {label}
      </span>
      {meta && <span className="turn-summary-detail">{meta}</span>}
      <ChevronRight size={12} className="turn-summary-chevron" aria-hidden="true" />
    </div>
  );
}

function summaryLabel(cell: TurnSummaryCellState, lang: "zh" | "en"): string {
  if (cell.status === "running") return lang === "zh" ? "正在思考" : "Thinking";
  return lang === "zh" ? "已处理" : "Processed";
}

function summaryMeta(cell: TurnSummaryCellState, lang: "zh" | "en"): string {
  const total = cell.items.length;
  if (total <= 0) return "";
  return lang === "zh" ? `${total} 项` : `${total} item${total === 1 ? "" : "s"}`;
}
