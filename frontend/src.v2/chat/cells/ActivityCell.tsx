import { memo, useEffect, useMemo, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
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
  readableRecordLabel,
} from "./activityCellHelpers";
import { subscribeSecondTick } from "../../lib/shared-tick";
import { openWebTarget } from "../openWebTarget";
import { normalizeAgentErrorMessage, purifyToolErrorText } from "../errorMessages";
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

  useEffect(() => {
    setIsExpanded(shouldAutoExpand);
  }, [cell.id, cell.status, cell.collapsed, shouldAutoExpand]);

  const hasRecords = Boolean(cell.toolCallRecords?.length);
  const canToggle = !isActive && hasRecords;
  const isRunning = isActive || cell.status === "running";
  const isFailed = cell.status === "failed" || cell.status === "interrupted";
  const isPartial = cell.status === "partial";
  const elapsed = useElapsedTime(cell.startedAt, isRunning);
  const recordDetails = useMemo(
    () => isExpanded && hasRecords
      ? describeRecordDetails(cell.toolCallRecords!, developerMode)
      : [],
    [cell.toolCallRecords, developerMode, hasRecords, isExpanded],
  );

  const name = readableTimelineTitle(cell);
  const detail = cell.subtitle;

  const cellStateClass = isRunning
    ? "activity-cell-running"
    : isFailed
      ? "activity-cell-failed"
      : isPartial
        ? "activity-cell-partial"
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
            data-partial={isPartial}
            data-completed={!isRunning && !isFailed && !isPartial}
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
              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          )}

          {isRunning && isLongRunning(cell.startedAt) && (
            <>
              <span className="activity-cell-long-running">仍在运行</span>
              <span
                className="activity-cell-info-icon"
                title={getLongRunningExplanation()}
                aria-label="Long running activity"
              >
                i
              </span>
            </>
          )}
        </button>

      </div>

      {/* Expanded: detailed records */}
      {isExpanded && hasRecords && (
        <div className="activity-cell-expanded">
          {recordDetails.map(({ label, target, targetKind, lineInfo, count, durationMs }, i) => (
            <div key={`${label}-${target}-${i}`} className="activity-cell-detail-row">
              <span className="activity-cell-detail-dot">⎿</span>
              <span className="activity-cell-detail-name">{label}</span>
              <DetailTarget target={target} targetKind={targetKind} />
              {lineInfo && <span className="activity-cell-detail-meta">{lineInfo}</span>}
              {count > 1 && <span className="activity-cell-detail-count">{`x${count}`}</span>}
              {developerMode && durationMs != null && durationMs > 0 && (
                <span className="activity-cell-detail-duration">{durationMs}ms</span>
              )}
            </div>
          ))}
        </div>
      )}

      {!isFailed && !isActive && isExpanded && hasOutputPreview(cell.toolCallRecords) && (
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

      {/* Failed records use the same disclosure row as every other tool. */}
      {isFailed && isExpanded && hasRecords && (
        <div className="activity-cell-error-detail">
          {cell.toolCallRecords!.map((record, i) => {
            const rawError = purifyToolErrorText(
              developerMode
                ? record.developerDetail || record.outputPreview || record.userSummary || record.errorInfo?.user_summary || ""
                : record.outputPreview || record.userSummary || record.errorInfo?.user_summary || "",
            );
            const error = developerMode
              ? rawError
              : normalizeAgentErrorMessage(rawError, { includeProviderDetails: false });
            if (!error) return null;
            const label = developerMode ? record.name : readableRecordLabel(record);
            return (
              <div key={`error-${i}`} className="activity-cell-error-item">
                {label && <div className="activity-cell-error-label">{label}</div>}
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
        rel="noreferrer"
        title={text}
        onClick={(event) => {
          event.stopPropagation();
          if (openWebTarget(text)) event.preventDefault();
        }}
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

