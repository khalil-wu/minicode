import { ChevronDown, ChevronRight, CircleAlert } from "lucide-react";
import type { AgentTurnStatus } from "../projection/project-turn";
import { useSharedSecondTick } from "../../lib/shared-tick";

type AgentProcessSummaryProps = {
  status: AgentTurnStatus;
  processExpanded: boolean;
  hasTimelineItems: boolean;
  durationMs: number | null;
  startedAt?: number;
  failureMessage?: string;
  canCollapse?: boolean;
  canExpand?: boolean;
  position?: "top" | "bottom";
  onToggle: () => void;
};

export function AgentProcessSummary({
  status,
  processExpanded,
  hasTimelineItems,
  durationMs,
  startedAt,
  failureMessage,
  canCollapse = status === "completed",
  canExpand = false,
  position = "top",
  onToggle,
}: AgentProcessSummaryProps) {
  const running = status === "running";
  const now = useSharedSecondTick(running && startedAt !== undefined);
  const statusLabel = running
    ? "处理中"
    : status === "failed"
      ? "处理失败"
      : status === "partial"
        ? "部分完成"
        : status === "stopped"
          ? "已停止"
          : "已处理";
  const durationLabel = formatElapsedSeconds(running && startedAt !== undefined ? Math.max(0, now - startedAt) : durationMs);
  const displayLabel = durationLabel
    ? running || status === "completed" ? `已处理 ${durationLabel}` : `${statusLabel} · ${durationLabel}`
    : statusLabel;
  const normalizedFailure = status === "failed" ? failureMessage?.trim() : "";
  const summaryFailure = normalizedFailure && !processExpanded ? normalizedFailure : "";
  const accessibleStatusLabel = summaryFailure
    ? `${displayLabel} · ${summaryFailure}`
    : displayLabel || "处理完成";
  const content = (
    <>
      {!running && status !== "completed" && (
        <span className="agent-loop-process-summary-icon" aria-hidden="true">
          <CircleAlert size={14} className={status === "failed" ? "agent-loop-failed-icon" : undefined} />
        </span>
      )}
      <span className="agent-loop-process-summary-body">
        <span className="chat-turn-process-summary-text">
          <span
            className="agent-loop-process-summary-status"
            data-running={running && !durationLabel ? "true" : undefined}
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

  if (!hasTimelineItems || (!canCollapse && !canExpand)) {
    return (
      <div
        className="chat-turn-process-summary-wrap agent-loop-process-summary-wrap"
        data-position={position}
      >
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
    <div
      className="chat-turn-process-summary-wrap agent-loop-process-summary-wrap"
      data-position={position}
    >
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
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs < 1_000) return "";
  const seconds = Math.floor(durationMs / 1_000);
  if (seconds >= 60) {
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      return `${hours}小时${remainingMinutes ? `${remainingMinutes}分钟` : ""}${remainder ? `${remainder}秒` : ""}`;
    }
    return remainder > 0 ? `${minutes}分钟${remainder}秒` : `${minutes}分钟`;
  }
  return `${seconds}秒`;
}
