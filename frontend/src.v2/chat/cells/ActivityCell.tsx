import { memo, useEffect, useMemo, useState, useCallback } from "react";
import { ChevronDown, ChevronRight, Copy, RefreshCw, Eye, EyeOff } from "lucide-react";
import type { ActivityCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import {
  type ActivityDetail,
  readableTimelineTitle,
  describeRecordDetails,
  hasOutputPreview,
  getOutputPreview,
  isLongRunning,
  getLongRunningExplanation,
  isHttpUrl,
  fileLabel,
  formatDuration,
} from "./activityCellHelpers";
import { subscribeSecondTick } from "../../lib/shared-tick";
import "./cells.css";

/** Real-time elapsed timer — ticks every second while tool is running.
 * Subscribes to the shared 1s tick so N running cells share one interval. */
function useElapsedTime(startedAt: number | undefined, isRunning: boolean): string {
  const [elapsed, setElapsed] = useState("");

  useEffect(() => {
    if (!startedAt || !isRunning) return;
    const update = (now: number) => setElapsed(formatDuration(now - startedAt));
    update(Date.now());
    return subscribeSecondTick(update);
  }, [startedAt, isRunning]);

  return elapsed;
}

/**
 * ActivityCell — Claude Code style compact tool-call display.
 *
 * ● tool_name (details) 1.2s          ← running: blinking dot + bold name + elapsed
 * ● tool_name (details)              ← done: static dot + dim name
 * ● tool_name (details)              ← failed: red dot + red name
 *
 * Expandable on click to show detailed records.
 */
export const ActivityCell = memo(function ActivityCell({
  cell,
  isActive = false,
  forceExpanded = false,
}: {
  cell: ActivityCellState;
  isActive?: boolean;
  forceExpanded?: boolean;
}) {
  const developerMode = useAppStore((s) => s.viewMode === "verbose");
  const shouldAutoExpand = forceExpanded || !cell.collapsed;
  const [isExpanded, setIsExpanded] = useState(shouldAutoExpand);
  const [showErrorDetail, setShowErrorDetail] = useState(false);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    setIsExpanded(shouldAutoExpand);
  }, [cell.id, cell.status, cell.collapsed, shouldAutoExpand]);

  const copyOutput = useCallback(() => {
    const records = cell.toolCallRecords ?? [];
    const outputs = records
      .map((r) => {
        const output = r.outputPreview || r.contentPreview || (/^read_artifact$/i.test(r.name) ? r.summary : "") || r.errorInfo?.user_summary || "";
        return output ? `${r.name}:\n${output}` : "";
      })
      .filter(Boolean)
      .join("\n\n");

    if (!outputs) return;

    navigator.clipboard.writeText(outputs).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  }, [cell.toolCallRecords]);

  const hasRecords = Boolean(cell.toolCallRecords?.length);
  const canToggle = !isActive && hasRecords;
  const isRunning = isActive || cell.status === "running";
  const isFailed = cell.status === "failed" || cell.status === "interrupted";
  const elapsed = useElapsedTime(cell.startedAt, isRunning);
  const recordDetails = useMemo(
    () => hasRecords ? describeRecordDetails(cell.toolCallRecords!, developerMode) : [],
    [cell.toolCallRecords, developerMode, hasRecords],
  );

  const name = readableTimelineTitle(cell);
  const detail = cell.subtitle;

  const cellStateClass = isRunning
    ? "activity-cell-running"
    : isFailed
      ? "activity-cell-failed"
      : "activity-cell-completed";

  return (
    <div className={`activity-cell ${cellStateClass}`}>
      <div className="activity-cell-line">
        <button
          type="button"
          aria-label={canToggle ? (isExpanded ? "Collapse activity details" : "Expand activity details") : undefined}
          aria-expanded={canToggle ? isExpanded : undefined}
          disabled={!canToggle}
          data-clickable={canToggle}
          className="activity-cell-main-button"
          onClick={() => { if (canToggle) setIsExpanded((v) => !v); }}
        >
          <span
            className="activity-cell-dot"
            data-running={isRunning}
            data-failed={isFailed}
            data-completed={!isRunning && !isFailed}
          >
            ●
          </span>

          <span className="activity-cell-name" data-failed={isFailed}>
            {name}
          </span>

          {detail && <span className="activity-cell-detail">{detail}</span>}

          {isRunning && cell.progress?.text && (
            <span className="activity-cell-progress">{cell.progress.text}</span>
          )}

          {elapsed && <span className="activity-cell-elapsed">{elapsed}</span>}

          {canToggle && (
            <span className="activity-cell-toggle">
              {isExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            </span>
          )}

          {isRunning && isLongRunning(cell.startedAt) && (
            <>
              <span className="activity-cell-long-running">仍在运行</span>
              <span
                className="activity-cell-info-icon"
                title={getLongRunningExplanation(cell)}
                aria-label="Long running activity"
              >
                i
              </span>
            </>
          )}
        </button>

        {hasRecords && (cell.status === "done" || cell.status === "failed") && (
          <button
            type="button"
            className="activity-cell-copy-btn"
            onClick={(e) => {
              e.stopPropagation();
              copyOutput();
            }}
            title={copied ? "已复制" : "复制输出"}
            aria-label={copied ? "已复制" : "复制输出"}
          >
            <Copy size={10} />
          </button>
        )}
      </div>

      {/* Expanded: detailed records */}
      {isExpanded && hasRecords && (
        <div className="activity-cell-expanded">
          {recordDetails.map(({ label, target, targetKind, count, durationMs }, i) => (
            <div key={`${label}-${target}-${i}`} className="activity-cell-detail-row">
              <span className="activity-cell-detail-dot">⎿</span>
              <span className="activity-cell-detail-name">{label}</span>
              <DetailTarget target={target} targetKind={targetKind} />
              {count > 1 && <span className="activity-cell-detail-count">{`x${count}`}</span>}
              {developerMode && durationMs != null && durationMs > 0 && (
                <span className="activity-cell-detail-duration">{durationMs}ms</span>
              )}
            </div>
          ))}
        </div>
      )}

      {!isActive && isExpanded && hasOutputPreview(cell.toolCallRecords) && (
        <div className="activity-cell-output-preview">
          <pre className="activity-cell-output-pre">{getOutputPreview(cell.toolCallRecords)}</pre>
        </div>
      )}

      {/* Inline output preview for running commands */}
      {isActive && cell.activityKind === "commandExecution" && hasOutputPreview(cell.toolCallRecords) && (
        <div className="activity-cell-output-preview">
          <pre className="activity-cell-output-pre">{getOutputPreview(cell.toolCallRecords)}</pre>
        </div>
      )}

      {/* Failed tool actions */}
      {isFailed && !isActive && (
        <div className="activity-cell-failed-actions">
          <button
            type="button"
            className="cell-action-btn activity-cell-action-retry"
            onClick={() => handleRetry(cell)}
            title="重试此操作"
          >
            <RefreshCw size={12} /> 重试
          </button>
          <button
            type="button"
            className="cell-action-btn"
            onClick={() => setShowErrorDetail((v) => !v)}
            title={showErrorDetail ? "隐藏错误详情" : "查看错误详情"}
          >
            {showErrorDetail ? <EyeOff size={12} /> : <Eye size={12} />} {showErrorDetail ? "隐藏详情" : "查看详情"}
          </button>
        </div>
      )}

      {/* Error detail panel */}
      {isFailed && showErrorDetail && hasRecords && (
        <div className="activity-cell-error-detail">
          {cell.toolCallRecords!.map((record, i) => {
            const error = record.errorInfo?.user_summary || record.outputPreview || "";
            if (!error) return null;
            return (
              <div key={`error-${i}`} className="activity-cell-error-item">
                <div className="activity-cell-error-label">{record.name}</div>
                <pre className="activity-cell-error-pre">{error}</pre>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
});

// ── DetailTarget sub-component ─────────────────────────────

function DetailTarget({
  target,
  targetKind,
}: {
  target: string;
  targetKind: ActivityDetail["targetKind"];
}) {
  const text = target.trim();
  if (!text) return null;

  if (targetKind === "url" && isHttpUrl(text)) {
    return (
      <a
        className="activity-cell-detail-path activity-cell-detail-link activity-cell-detail-link-url"
        href={text}
        target="_blank"
        rel="noreferrer"
        title={text}
        onClick={(event) => event.stopPropagation()}
      >
        {text}
      </a>
    );
  }

  if (targetKind === "file") {
    return (
      <button
        type="button"
        className="activity-cell-detail-path activity-cell-detail-link activity-cell-detail-link-file"
        title={text}
        aria-label={`Open ${text}`}
        onClick={(event) => {
          event.stopPropagation();
          useAppStore.getState().openEditorFile(text, fileLabel(text));
        }}
      >
        {text}
      </button>
    );
  }

  return (
    <span className="activity-cell-detail-path" title={text}>
      {text}
    </span>
  );
}

// ── Retry handler (stub) ───────────────────────────────────

async function handleRetry(cell: ActivityCellState) {
  const { pushToast } = await import("../../overlays/ToastContainer");
  pushToast("重试功能正在开发中，敬请期待", "info", 3000);
}
