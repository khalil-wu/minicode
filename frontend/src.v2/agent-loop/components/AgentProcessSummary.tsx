import { ChevronDown, ChevronRight, CircleAlert } from "lucide-react";
import type { AgentTurnStatus } from "../projection/project-turn";
import { useSharedSecondTick } from "../../lib/shared-tick";
import { useI18n, type Translate } from "../../i18n";

type AgentProcessSummaryProps = {
  status: AgentTurnStatus;
  processExpanded: boolean;
  hasTimelineItems: boolean;
  durationMs: number | null;
  startedAt?: number;
  failureMessage?: string;
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
  position = "top",
  onToggle,
}: AgentProcessSummaryProps) {
  const { t } = useI18n();
  const running = status === "running";
  const now = useSharedSecondTick(running && startedAt !== undefined);
  const statusLabel = t(running ? "处理中" : status === "stopped" ? "已停止" : "已处理");
  const durationLabel = formatElapsedSeconds(running && startedAt !== undefined ? Math.max(0, now - startedAt) : durationMs, t);
  const displayLabel = durationLabel
    ? status !== "stopped" ? t("已处理 {duration}", { duration: durationLabel }) : `${statusLabel} · ${durationLabel}`
    : statusLabel;
  const normalizedFailure = status === "failed" ? failureMessage?.trim() : "";
  const summaryFailure = normalizedFailure && !processExpanded ? normalizedFailure : "";
  const accessibleStatusLabel = summaryFailure
    ? `${displayLabel} · ${summaryFailure}`
    : displayLabel || t("处理完成");
  const content = (
    <>
      {status === "stopped" && (
        <span className="agent-loop-process-summary-icon" aria-hidden="true">
          <CircleAlert size={14} />
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

  if (!hasTimelineItems) {
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
        aria-label={t(processExpanded ? "收起处理步骤" : "展开处理步骤")}
        aria-expanded={processExpanded}
        onClick={onToggle}
      >
        {content}
        {processExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>
    </div>
  );
}

function formatElapsedSeconds(durationMs: number | null, t: Translate): string {
  if (durationMs == null || !Number.isFinite(durationMs) || durationMs < 1_000) return "";
  const seconds = Math.floor(durationMs / 1_000);
  if (seconds >= 60) {
    const minutes = Math.floor(seconds / 60);
    const remainder = seconds % 60;
    if (minutes >= 60) {
      const hours = Math.floor(minutes / 60);
      const remainingMinutes = minutes % 60;
      return `${t("{hours}小时", { hours })}${remainingMinutes ? t("{minutes}分钟", { minutes: remainingMinutes }) : ""}${remainder ? t("{seconds}秒", { seconds: remainder }) : ""}`;
    }
    return `${t("{minutes}分钟", { minutes })}${remainder > 0 ? t("{seconds}秒", { seconds: remainder }) : ""}`;
  }
  return t("{seconds}秒", { seconds });
}
