import { CheckCircle2, ChevronDown, ChevronRight, CircleAlert, LoaderCircle } from "lucide-react";
import type { AgentTurnStatus } from "../projection/project-turn";

export function AgentProcessSummary({
  status,
  processExpanded,
  durationLabel,
  hasTimelineItems,
  onToggle,
}: {
  status: AgentTurnStatus;
  processExpanded: boolean;
  durationLabel: string;
  hasTimelineItems: boolean;
  onToggle: () => void;
}) {
  const completedLabel =
    status === "failed"
      ? "出错"
      : status === "partial"
        ? "部分完成"
      : status === "stopped"
        ? "已停止"
        : "完成";

  const running = status === "running";
  const label = running ? "正在处理" : completedLabel;

  if (running && !hasTimelineItems) {
    return (
      <div className="agent-loop-running-summary agent-loop-thinking-indicator" aria-label="Agent is processing" role="status">
        <LoaderCircle size={14} className="agent-loop-thinking-spinner" aria-hidden="true" />
        <span className="agent-loop-thinking-shimmer" data-text={label}>{label}</span>
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
          {running ? (
            <LoaderCircle size={14} className="agent-loop-thinking-spinner" />
          ) : status === "failed" || status === "partial" ? (
            <CircleAlert size={14} className="agent-loop-failed-icon" />
          ) : (
            <CheckCircle2 size={14} className="agent-loop-done-icon" />
          )}
        </span>
        <span className="chat-turn-process-summary-text">
          {label}
          {!running && durationLabel && (
            <span className="chat-turn-process-summary-duration">
              {durationLabel}
            </span>
          )}
        </span>
        {processExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
    </div>
  );
}
