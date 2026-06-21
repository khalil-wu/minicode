import { CheckCircle2, ChevronDown, ChevronRight, CircleAlert } from "lucide-react";
import type { AgentLoopSummaryItem } from "../projection/project-turn";
import type { AgentTurnStatus } from "../types";

export function AgentProcessSummary({
  status,
  shouldCollapseProcess,
  processExpanded,
  durationLabel,
  summaryItems,
  onToggle,
}: {
  status: AgentTurnStatus;
  shouldCollapseProcess: boolean;
  processExpanded: boolean;
  durationLabel: string;
  summaryItems: AgentLoopSummaryItem[];
  onToggle: () => void;
}) {
  const completedSummaryItems = summaryItems.filter((item) => item.kind !== "command");

  if (!shouldCollapseProcess) {
    if (status !== "running") return null;
    return (
      <div className="agent-loop-running-summary" aria-label="Agent is processing">
        <span className="agent-loop-running-dot" aria-hidden="true" />
        <span>正在处理</span>
        {summaryItems.length > 0 && (
          <span className="agent-loop-running-meta">
            {summaryItems.map((item) => item.label).join(" · ")}
          </span>
        )}
      </div>
    );
  }

  return (
    <div className="chat-turn-process-summary-wrap agent-loop-process-summary-wrap">
      <button
        type="button"
        className="chat-turn-process-summary agent-loop-process-summary"
        aria-label={processExpanded ? "Collapse processed steps" : "Expand processed steps"}
        aria-expanded={processExpanded}
        onClick={onToggle}
      >
        <span className="agent-loop-process-summary-icon" aria-hidden="true">
          {status === "failed" ? (
            <CircleAlert size={13} className="agent-loop-failed-icon" />
          ) : (
            <CheckCircle2 size={13} className="agent-loop-done-icon" />
          )}
        </span>
        <span className="chat-turn-process-summary-text">
          已处理
          {durationLabel && (
            <span className="chat-turn-process-summary-duration">
              {durationLabel}
            </span>
          )}
        </span>
        {completedSummaryItems.length > 0 && (
          <span className="agent-loop-process-summary-meta">
            {completedSummaryItems.map((item) => item.label).join(" · ")}
          </span>
        )}
        {processExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
    </div>
  );
}
