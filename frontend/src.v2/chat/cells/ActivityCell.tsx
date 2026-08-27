import { memo, useEffect, useMemo, useState } from "react";
import { Check, ChevronDown, ChevronRight, Circle, Pencil } from "lucide-react";
import type { ActivityCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import {
  type ActivityDetail,
  type ActivityToolRecord,
  readableTimelineTitle,
  describeRecordDetail,
  describeRecordDetails,
  hasOutputPreview,
  getOutputPreview,
  getRecordOutputPreview,
  isLongRunning,
  isHttpUrl,
  fileLabel,
  recordInputTarget,
  readableRecordLabel,
  planUpdateSteps,
  type PlanUpdateStep,
} from "./activityCellHelpers";
import { getToolDiffStats } from "../../lib/tool-call-reducer";
import {
  activityCellStatus,
  formatCellDuration,
  isRunningCellStatus,
} from "./cellStatus";
import { readableToolLabel } from "../toolDisplayName";
import { ToolGlyph } from "../toolUtils";
import { subscribeSecondTick } from "../../lib/shared-tick";
import { openWebTarget } from "../openWebTarget";
import { normalizeAgentErrorMessage, purifyToolErrorText } from "../errorMessages";
import { RollingNumber } from "../../components/RollingNumber";
import { InlineDiff } from "../diff/InlineDiff";
import { workspaceRelativeDiffPath } from "../diffPaths";
import "./cells.css";

/** Real-time elapsed timer — ticks every second while tool is running.
 * Subscribes to the shared 1s tick so N running cells share one interval. */
function useElapsedTime(startedAt: number | undefined, isRunning: boolean): string {
  const [elapsed, setElapsed] = useState("");

  useEffect(() => {
    if (!startedAt || !isRunning) return;
    const update = (now: number) => setElapsed(formatCellDuration(now - startedAt));
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
}: {
  cell: ActivityCellState;
  isActive?: boolean;
}) {
  const developerMode = useAppStore((s) => s.viewMode === "verbose");
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const records = useMemo(() => cell.toolCallRecords ?? [], [cell.toolCallRecords]);
  const hasRecords = records.length > 0;
  const isFileChange = cell.activityKind === "fileChange";
  const isRead = cell.activityKind === "fileRead";
  const isWorkspaceSearch = cell.activityKind === "workspaceSearch";
  const isWorkspaceList = cell.activityKind === "workspaceList";
  const isWebAction = cell.activityKind === "webSearch";
  const isInlineAction = isRead || isWorkspaceSearch || isWorkspaceList || isWebAction;
  const status = activityCellStatus(cell.status);
  const isRunning = isActive || isRunningCellStatus(status);
  const fileChangeStats = useMemo(() => {
    if (!isFileChange) return undefined;
    return records.reduce((stats, record) => {
      if (!record.diff) return stats;
      const diff = getToolDiffStats(record.diff);
      return { plus: stats.plus + diff.plus, minus: stats.minus + diff.minus };
    }, { plus: 0, minus: 0 });
  }, [isFileChange, records]);
  const fileChangeTarget = isFileChange
    ? workspaceRelativeDiffPath(recordInputTarget(records[0]), workingDirectory)
    : "";
  const shouldAutoExpand = !cell.collapsed;
  const [isExpanded, setIsExpanded] = useState(shouldAutoExpand);

  useEffect(() => {
    setIsExpanded(shouldAutoExpand);
  }, [cell.id, cell.status, cell.collapsed, shouldAutoExpand]);

  const isFailed = cell.status === "failed" || cell.status === "interrupted";
  const isPartial = cell.status === "partial";
  const recordDetails = useMemo(
    () => isExpanded && hasRecords
      ? describeRecordDetails(records, developerMode)
      : [],
    [records, developerMode, hasRecords, isExpanded],
  );
  const planRecords = useMemo(
    () => records.filter((record) => planUpdateSteps(record).length > 0),
    [records],
  );
  const nonPlanRecords = useMemo(
    () => records.filter((record) => record.name !== "update_plan"),
    [records],
  );
  const showDetailRows = hasRecords
    && !isFileChange
    && !isInlineAction
    && (recordDetails.length > 0 || planRecords.length > 0);
  const showOutputPreview = !isInlineAction
    && !isFileChange
    && !isFailed
    && !isActive
    && hasOutputPreview(nonPlanRecords);
  const inlineDisclosureRecords = useMemo(() => {
    if (!isInlineAction) return [];
    const showTargets = records.length > 1;
    return records.flatMap((record) => {
      const target = recordInputTarget(record);
      const output = getRecordOutputPreview(record);
      const visibleOutput = output.trim() === target.trim() ? "" : output;
      if (!showTargets && !visibleOutput) return [];
      return [{ record, target: showTargets ? target : "", output: visibleOutput }];
    });
  }, [isInlineAction, records]);
  const hasInlineFailureEvidence = isInlineAction
    && inlineDisclosureRecords.some(({ output }) => Boolean(output.trim()));
  const showGenericErrorDetail = !isInlineAction || !hasInlineFailureEvidence;
  const canToggle = hasRecords && (
    !isInlineAction
    || isFailed
    || inlineDisclosureRecords.length > 0
  );

  const name = isInlineAction && records.length === 1
    ? cell.activityKind === "webSearch"
      ? readableTimelineTitle(cell)
      : inlineActionLabel(records[0], cell.activityKind)
    : readableTimelineTitle(cell);
  const inlineTarget = isInlineAction && records.length === 1
    ? recordInputTarget(records[0])
    : "";
  const concreteToolTarget = !isFailed && records.length === 1
    ? recordInputTarget(records[0])
    : "";
  const detail = inlineTarget || cell.subtitle?.trim() || concreteToolTarget;
  const singleInlineDetail = isInlineAction && records.length === 1 ? describeRecordDetail(records[0], developerMode) : null;
  const changeDetails = useMemo(
    () => isFileChange ? buildChangeDetails(records, workingDirectory) : [],
    [isFileChange, records, workingDirectory],
  );
  const glyphKind = activityGlyphKind(cell.activityKind, records[0]);
  const useToolIcon = !isFileChange && cell.activityKind !== "genericTool";
  const liveLabel = isRunning ? `正在${activityVerb(cell.activityKind)}` : "";
  // Every cell reports duration the same way: live elapsed while running, the
  // settled tool duration afterwards. A tool must not silently drop its timing
  // just because it settled between ticks.
  const liveElapsed = useElapsedTime(cell.startedAt, isRunning);
  const settledDuration = formatCellDuration(
    cell.completedAt != null && cell.startedAt != null
      ? cell.completedAt - cell.startedAt
      : records.reduce(
          (total, record) => total + (record.durationMs ?? 0),
          0,
        ) || undefined,
  );
  const elapsed = isRunning ? liveElapsed : settledDuration;

  const cellStateClass = isRunning
    ? "activity-cell-running"
    : isFailed
      ? "activity-cell-failed"
      : isPartial
        ? "activity-cell-partial"
        : "activity-cell-completed";

  return (
    <div
      className={`activity-cell ${cellStateClass}`}
      data-activity-kind={cell.activityKind}
    >
      <div className="activity-cell-line">
        <button
          type="button"
          aria-label={canToggle ? (isExpanded ? "收起活动详情" : "展开活动详情") : undefined}
          aria-expanded={canToggle ? isExpanded : undefined}
          disabled={!canToggle}
          data-clickable={canToggle}
          className="activity-cell-main-button"
          onClick={() => { if (canToggle) setIsExpanded((v) => !v); }}
        >
          {isFileChange ? (
            <span className="activity-cell-file-change-icon" aria-hidden="true">
              <Pencil size={14} />
            </span>
          ) : useToolIcon ? (
            <span
              className="activity-cell-tool-icon"
              data-running={isRunning}
              data-failed={isFailed}
              data-partial={isPartial}
              aria-hidden="true"
            >
              <ToolGlyph kind={glyphKind} size={15} />
            </span>
          ) : (
            <span
              className="activity-cell-dot"
              data-running={isRunning}
              data-failed={isFailed}
              data-partial={isPartial}
              data-completed={!isRunning && !isFailed && !isPartial}
            >
              ●
            </span>
          )}

          {isFileChange ? (
            <>
              <span className="activity-cell-name" data-failed={isFailed}>{isRunning ? "正在编辑" : "已编辑"}</span>
              {fileChangeTarget && <span className="activity-cell-file-change-target">{fileChangeTarget}</span>}
              {fileChangeStats && (
                <span className="activity-cell-file-change-stats">
                  <RollingNumber value={fileChangeStats?.plus ?? 0} prefix="+" className="activity-cell-added" animateOnMount />
                  <RollingNumber value={fileChangeStats?.minus ?? 0} prefix="-" className="activity-cell-removed" animateOnMount />
                </span>
              )}
            </>
          ) : (
            <span className="activity-cell-name" data-failed={isFailed}>{liveLabel || name}</span>
          )}

          {!isFileChange && detail && (
            <span className={`activity-cell-detail${isRead ? " activity-cell-read-target" : ""}`}>
              {detail}
            </span>
          )}
          {singleInlineDetail?.lineInfo && <span className="activity-cell-detail-meta">{singleInlineDetail.lineInfo}</span>}

          {isRunning && cell.progress?.text && (
            <span className="activity-cell-progress">{readableToolLabel(cell.progress.text)}</span>
          )}

          {elapsed && <span className="activity-cell-elapsed">{elapsed}</span>}

          {canToggle && (
            <span className="activity-cell-toggle">
              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          )}

          {isRunning && isLongRunning(cell.startedAt) && (
            <span className="activity-cell-long-running">仍在运行</span>
          )}
        </button>

      </div>

      {isExpanded && isFileChange && changeDetails.length > 0 && (
        <div className="activity-cell-expanded activity-cell-file-change-expanded">
          {changeDetails.map((detail, index) => (
            <div key={`${detail.path}-${index}`} className="activity-cell-change-card">
              <div className="activity-cell-change-card-header">
                <span className="activity-cell-change-card-path" title={detail.path}>{detail.path}</span>
                <span className="activity-cell-file-change-stats">
                  <span className="activity-cell-added">+{detail.additions}</span>
                  <span className="activity-cell-removed">-{detail.deletions}</span>
                </span>
              </div>
              {detail.patch && <InlineDiff patch={detail.patch} contextLines={1} />}
            </div>
          ))}
        </div>
      )}

      {isExpanded && isInlineAction && inlineDisclosureRecords.length > 0 && (
        <div className="activity-cell-expanded activity-cell-tool-expanded">
          {inlineDisclosureRecords.map(({ record, target, output }, index) => {
            const detail = describeRecordDetail(record, developerMode);
            return (
              <div key={record.id || `${record.name}-${index}`} className="activity-cell-tool-detail-card">
                {target && (
                  <div className="activity-cell-tool-record-target">
                    <DetailTarget target={target} targetKind={detail!.targetKind} />
                    {detail!.lineInfo && <span className="activity-cell-detail-meta">{detail!.lineInfo}</span>}
                  </div>
                )}
                {output && <pre className="activity-cell-inline-output">{output}</pre>}
              </div>
            );
          })}
        </div>
      )}

      {/* One frame per cell: identity and duration live in the header row, so
          the panel carries the records and their output together instead of
          stacking a metadata box on top of an output box. */}
      {isExpanded && (showDetailRows || showOutputPreview) && (
          <div className="activity-cell-expanded">
            {showDetailRows && planRecords.map((record, i) => (
              <PlanUpdateDetail
                key={`plan-update-${record.id || i}`}
                steps={planUpdateSteps(record)}
              />
            ))}
            {showDetailRows && recordDetails.map(({ label, target, targetKind, lineInfo, count }, i) => (
              <div key={`${label}-${target}-${i}`} className="activity-cell-detail-row">
                <span className="activity-cell-detail-name">{label}</span>
                {/* The header row already shows this cell's target. Repeating it
                    verbatim one line below is the duplication that made browser
                    and command cells read as two stacked boxes. */}
                {target !== detail && <DetailTarget target={target} targetKind={targetKind} />}
                {lineInfo && <span className="activity-cell-detail-meta">{lineInfo}</span>}
                {count > 1 && <span className="activity-cell-detail-count">{`x${count}`}</span>}
              </div>
            ))}
            {showOutputPreview && (
              <pre className="activity-cell-output-pre">{getOutputPreview(nonPlanRecords)}</pre>
            )}
          </div>
      )}

      {/* Failed records use the same disclosure row as every other tool. */}
      {isFailed && isExpanded && hasRecords && showGenericErrorDetail && (
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
            const label = developerMode
              ? readableToolLabel(record.displayHint || record.name)
              : readableRecordLabel(record);
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

function activityGlyphKind(
  activityKind: ActivityCellState["activityKind"],
  record?: ActivityToolRecord,
): string {
  const name = String(record?.name || "").toLowerCase();
  if (activityKind === "webSearch" && (
    String(record?.resultKind || "").toLowerCase() === "web"
    || name === "web_fetch"
    || name === "webfetch"
  )) {
    return "web";
  }
  return activityKind || record?.resultKind || "genericTool";
}

function activityVerb(activityKind: ActivityCellState["activityKind"]): string {
  if (activityKind === "fileRead") return "读取";
  if (activityKind === "workspaceList") return "列出文件";
  if (activityKind === "workspaceSearch") return "搜索";
  if (activityKind === "webSearch") return "获取网页";
  if (activityKind === "skill") return "使用技能";
  return "执行工具";
}

function inlineActionLabel(
  record: ActivityToolRecord,
  activityKind: ActivityCellState["activityKind"],
): string {
  const name = String(record.name || "").toLowerCase();
  const resultKind = String(record.resultKind || "").toLowerCase();
  // The turn projection already owns tool classification. Render from that
  // canonical activity kind instead of reclassifying broad result metadata
  // such as resultKind="file", which also appears on list_files results.
  if (activityKind === "workspaceList") return "List";
  if (activityKind === "workspaceSearch") return "Search";
  if (activityKind === "fileRead") return "Read";
  if (activityKind === "webSearch") {
    if (name === "web_fetch" || name === "webfetch" || resultKind === "web") return "Fetch";
    return "Search";
  }
  return readableToolLabel(record.displayHint || record.name);
}

type ChangeDetail = {
  path: string;
  patch?: string;
  additions: number;
  deletions: number;
};

function buildChangeDetails(records: ActivityToolRecord[], workingDirectory: string): ChangeDetail[] {
  const details: ChangeDetail[] = [];

  for (const record of records) {
    const structured = record.diff?.files ?? [];
    if (structured.length > 0) {
      for (const file of structured) {
        const stats = getToolDiffStats({
          plus: file.plus,
          minus: file.minus,
          patch: file.patch,
        });
        details.push({
          path: workspaceRelativeDiffPath(file.path, workingDirectory) || file.path,
          patch: file.patch,
          additions: stats.plus,
          deletions: stats.minus,
        });
      }
      continue;
    }
    const path = recordInputTarget(record);
    if (path || record.diff) {
      const stats = record.diff ? getToolDiffStats(record.diff) : { plus: 0, minus: 0 };
      details.push({
        path: workspaceRelativeDiffPath(path, workingDirectory) || path || "已编辑文件",
        patch: record.diff?.patch,
        additions: stats.plus,
        deletions: stats.minus,
      });
    }
  }
  return details;
}

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
        aria-label={`打开 ${text}`}
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

function PlanUpdateDetail({
  steps,
}: {
  steps: PlanUpdateStep[];
}) {
  const completed = steps.filter((step) => step.status === "completed").length;
  return (
    <div className="activity-cell-plan-detail" aria-label="更新后的计划">
      <div className="activity-cell-plan-summary">
        <span>更新后的计划</span>
        <span>{completed}/{steps.length} 已完成</span>
      </div>
      {steps.map((step, index) => {
        const Icon = step.status === "completed" ? Check : Circle;
        return (
          <div
            key={`${index}-${step.step}`}
            className="activity-cell-plan-step"
            data-status={step.status}
          >
            <Icon size={14} aria-hidden="true" />
            <span>{step.step}</span>
          </div>
        );
      })}
    </div>
  );
}
