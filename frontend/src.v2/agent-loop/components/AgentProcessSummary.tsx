import { CheckCircle2, ChevronDown, ChevronRight, CircleAlert } from "lucide-react";
import type { AgentTurnStatus } from "../projection/project-turn";

type AgentProcessSummaryProps = {
  status: AgentTurnStatus;
  processExpanded: boolean;
  hasTimelineItems: boolean;
  durationMs: number | null;
  failureMessage?: string;
  canCollapse?: boolean;
  onToggle: () => void;
};

export function AgentProcessSummary({
  status,
  processExpanded,
  hasTimelineItems,
  durationMs,
  failureMessage,
  canCollapse = status === "completed",
  onToggle,
}: AgentProcessSummaryProps) {
  const running = status === "running";
  const statusLabel = running
    ? "开始处理"
    : status === "failed"
      ? "出错"
      : status === "partial"
        ? "部分完成"
        : status === "stopped"
          ? "已停止"
          : "已处理";
  const durationLabel = running ? "" : formatElapsedSeconds(durationMs);
  const displayLabel = durationLabel ? `${statusLabel} ${durationLabel}` : statusLabel;
  const normalizedFailure = status === "failed" ? failureMessage?.trim() : "";
  const summaryFailure = normalizedFailure && !processExpanded ? normalizedFailure : "";
  const accessibleStatusLabel = summaryFailure
    ? `${displayLabel} · ${summaryFailure}`
    : displayLabel || "处理完成";
  const content = (
    <>
      {!running && (
        <span className="agent-loop-process-summary-icon" aria-hidden="true">
          {status === "failed" || status === "partial" || status === "stopped" ? (
            <CircleAlert size={14} className="agent-loop-failed-icon" />
          ) : (
            <CheckCircle2 size={14} className="agent-loop-done-icon" />
          )}
        </span>
      )}
      <span className="agent-loop-process-summary-body">
        <span className="chat-turn-process-summary-text">
          <span
            className="agent-loop-process-summary-status"
            data-running={running ? "true" : undefined}
          >
            <span className="agent-loop-process-summary-status-label">
              {displayLabel}
            </span>
            {summaryFailure && (
              <span className="agent-loop-process-summary-failure">
                {summaryFailure}
              </span>
            )}
          </span>
        </span>
      </span>
    </>
  );

  if (!hasTimelineItems || !canCollapse) {
    return (
      <div className="chat-turn-process-summary-wrap agent-loop-process-summary-wrap">
        <div
          className="chat-turn-process-summary agent-loop-process-summary agent-loop-process-summary-static"
          aria-label={accessibleStatusLabel}
          role="status"
        >
          {content}
        </div>
      </div>
    );
  }

  return (
    <div className="chat-turn-process-summary-wrap agent-loop-process-summary-wrap">
      <button
        type="button"
        className="chat-turn-process-summary agent-loop-process-summary"
        aria-label={processExpanded ? "收起处理步骤" : "展开处理步骤"}
        aria-expanded={processExpanded}
        onClick={onToggle}
      >
        {content}
        {processExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
    </div>
  );
}

function formatElapsedSeconds(durationMs: number | null): string {
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs < 0) return "";
  if (durationMs < 1_000) return "<1 秒";
  const seconds = durationMs / 1_000;
  if (seconds >= 60) {
    const roundedSeconds = Math.round(seconds);
    const minutes = Math.floor(roundedSeconds / 60);
    const remainder = roundedSeconds % 60;
    return remainder > 0 ? `${minutes} 分钟 ${remainder} 秒` : `${minutes} 分钟`;
  }
  const value = seconds < 10
    ? seconds.toFixed(1).replace(/\.0$/, "")
    : String(Math.round(seconds));
  return `${value} 秒`;
}
