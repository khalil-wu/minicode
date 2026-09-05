import { memo, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Check, ChevronDown, ChevronRight, Circle, Pencil, Wifi, WifiOff } from "lucide-react";
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
  isWebFetchActivity,
  isWebFetchRecord,
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
import { getWebSocket } from "../../hooks/useWebSocket";
import { openArtifactPreview } from "../openAttachmentPreview";
import {
  artifactImageResourceUrl,
  withPreviewCacheBust,
} from "../../lib/artifact-resource";
import {
  artifactMediaTypeForProjection,
  artifactSummaryForRecord,
  canonicalArtifactKind,
  recordHasImageArtifact,
} from "../../lib/artifact-projection";
import {
  isProviderRetryProgress,
  providerProgressLabel,
  type ProviderProgressSnapshot,
} from "../../lib/provider-progress";
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
  conversationId,
}: {
  cell: ActivityCellState;
  isActive?: boolean;
  /** Explicit owner for artifacts in this cell.  Cells can be rendered from a
   * historical or child transcript while the app's active conversation differs. */
  conversationId?: string;
}) {
  const developerMode = useAppStore((s) => s.viewMode === "verbose");
  const workingDirectory = useAppStore((s) => s.workingDirectory);
  const ownerConversationId = String(conversationId || "").trim();
  const records = useMemo(() => cell.toolCallRecords ?? [], [cell.toolCallRecords]);
  const hasRecords = records.length > 0;
  const isFileChange = cell.activityKind === "fileChange";
  const isRead = cell.activityKind === "fileRead";
  const isWorkspaceSearch = cell.activityKind === "workspaceSearch";
  const isWorkspaceList = cell.activityKind === "workspaceList";
  const isWebAction = cell.activityKind === "webSearch";
  const isWebFetchAction = isWebFetchActivity(cell);
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
  const userToggled = useRef(false);
  const previousId = useRef(cell.id);

  useEffect(() => {
    if (previousId.current !== cell.id) userToggled.current = false;
    if (!userToggled.current) setIsExpanded(shouldAutoExpand);
    previousId.current = cell.id;
  }, [cell.id, shouldAutoExpand]);

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
  const imageArtifactRecords = useMemo(
    () => records.filter(isImageArtifactRecord),
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
  const hasChangeEvidence = isExpanded && isFileChange && changeDetails.length > 0;
  const hasInlineEvidence = isExpanded && isInlineAction && inlineDisclosureRecords.length > 0;
  const hasArtifactEvidence = isExpanded && !isFileChange && imageArtifactRecords.length > 0;
  const hasGenericEvidence = isExpanded
    && !isFileChange
    && !isInlineAction
    && (showDetailRows || showOutputPreview);
  const hasErrorEvidence = isExpanded && isFailed && hasRecords && showGenericErrorDetail;
  const showUnifiedEvidence = hasChangeEvidence
    || hasInlineEvidence
    || hasArtifactEvidence
    || hasGenericEvidence
    || hasErrorEvidence;
  const glyphKind = activityGlyphKind(cell.activityKind, records[0]);
  const useToolIcon = !isFileChange && cell.activityKind !== "genericTool";
  const providerProgress: ProviderProgressSnapshot | undefined = cell.progress && {
    id: cell.id,
    status: isRunning
      ? "running"
      : isFailed
        ? "failed"
        : isPartial
          ? "partial"
          : "completed",
    retryAttempt: cell.progress.retryAttempt,
    maxRetries: cell.progress.maxRetries,
    message: cell.progress.text || "",
    providerState: cell.progress.providerState,
  };
  const isProviderRetry = isProviderRetryProgress(providerProgress);
  const providerLabel = providerProgressLabel(providerProgress);
  const providerIsDisconnected = isFailed
    || providerProgress?.providerState === "failed"
    || providerProgress?.providerState === "interrupted";
  const liveLabel = providerLabel || (isRunning
    ? isWebFetchAction
      ? "正在获取网页"
      : cell.activityKind === "webSearch"
        ? "正在搜索网页"
        : `正在${activityVerb(cell.activityKind)}`
    : "");
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
      data-web-action={isWebAction ? (isWebFetchAction ? "fetch" : "search") : undefined}
      data-provider-retry={isProviderRetry ? "true" : undefined}
      data-provider-state={isProviderRetry ? providerProgress?.providerState : undefined}
      data-retry-attempt={isProviderRetry && providerProgress?.retryAttempt != null ? String(providerProgress.retryAttempt) : undefined}
      data-max-retries={isProviderRetry && providerProgress?.maxRetries != null ? String(providerProgress.maxRetries) : undefined}
    >
      <div className="activity-cell-line">
        <button
          type="button"
          aria-label={canToggle ? (isExpanded ? "收起活动详情" : "展开活动详情") : undefined}
          aria-expanded={canToggle ? isExpanded : undefined}
          disabled={!canToggle}
          data-clickable={canToggle}
          className="activity-cell-main-button"
          onClick={() => {
            if (!canToggle) return;
            userToggled.current = true;
            setIsExpanded((value) => !value);
          }}
        >
          {isFileChange ? (
            <span className="activity-cell-file-change-icon" aria-hidden="true">
              <Pencil size={14} />
            </span>
          ) : isProviderRetry ? (
            <span
              className="activity-cell-provider-icon"
              data-running={isRunning}
              data-failed={providerIsDisconnected}
              data-partial={isPartial}
              aria-hidden="true"
            >
              {providerIsDisconnected ? <WifiOff size={16} /> : <Wifi size={16} />}
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
            <span
              className="activity-cell-name"
              data-failed={isFailed}
              aria-live={isProviderRetry ? "polite" : undefined}
            >
              {liveLabel || name}
            </span>
          )}

          {!isFileChange && detail && (
            <span className={`activity-cell-detail${isRead ? " activity-cell-read-target" : ""}`}>
              {detail}
            </span>
          )}
          {singleInlineDetail?.lineInfo && <span className="activity-cell-detail-meta">{singleInlineDetail.lineInfo}</span>}

          {isRunning && cell.progress?.text && !providerLabel && (
            <span className="activity-cell-progress">{readableToolLabel(cell.progress.text)}</span>
          )}

          {elapsed && !isProviderRetry && <span className="activity-cell-elapsed">{elapsed}</span>}

          {canToggle && (
            <span className="activity-cell-toggle">
              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          )}

          {isRunning && !isProviderRetry && isLongRunning(cell.startedAt) && (
            <span className="activity-cell-long-running">仍在运行</span>
          )}
        </button>

      </div>

      {/* One frame per cell: identity and duration live in the header row, so
          all records, screenshots, output, and errors share one evidence
          surface instead of stacking several nested cards. */}
      {showUnifiedEvidence && (
        <div className={[
          "activity-cell-expanded",
          hasChangeEvidence ? "activity-cell-file-change-expanded" : "",
          hasInlineEvidence ? "activity-cell-tool-expanded" : "",
          hasArtifactEvidence ? "activity-cell-artifact-gallery" : "",
        ].filter(Boolean).join(" ")}>
          {hasChangeEvidence && changeDetails.map((change, index) => (
            <div key={`${change.path}-${index}`} className="activity-cell-change-card">
              <div className="activity-cell-change-card-header">
                <span className="activity-cell-change-card-path" title={change.path}>{change.path}</span>
                <span className="activity-cell-file-change-stats">
                  <span className="activity-cell-added">+{change.additions}</span>
                  <span className="activity-cell-removed">-{change.deletions}</span>
                </span>
              </div>
              {change.patch && <InlineDiff patch={change.patch} contextLines={1} />}
            </div>
          ))}

          {hasInlineEvidence && inlineDisclosureRecords.map(({ record, target, output }, index) => {
            const recordDetail = describeRecordDetail(record, developerMode);
            return (
              <div key={record.id || `${record.name}-${index}`} className="activity-cell-tool-detail-card">
                {target && recordDetail && (
                  <div className="activity-cell-tool-record-target">
                    <DetailTarget target={target} targetKind={recordDetail.targetKind} />
                    {recordDetail.lineInfo && <span className="activity-cell-detail-meta">{recordDetail.lineInfo}</span>}
                  </div>
                )}
                {output && <pre className="activity-cell-inline-output">{output}</pre>}
              </div>
            );
          })}

          {hasArtifactEvidence && imageArtifactRecords.map((record) => (
            <ToolArtifactImage
              key={record.artifactId}
              record={record}
              conversationId={ownerConversationId}
            />
          ))}

          {hasGenericEvidence && (
            <>
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
            </>
          )}

          {/* Failed records use the same evidence frame as every other tool. */}
          {hasErrorEvidence && (
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
      )}
    </div>
  );
});

/**
 * A few durable transcripts predate artifact_kind and only retain the MIME
 * type.  Keep those screenshots in the browser activity instead of silently
 * dropping them from the projection.
 */
const isImageArtifactRecord = (record: ActivityToolRecord): boolean => {
  return recordHasImageArtifact(record);
};

const artifactMediaType = (record: ActivityToolRecord): string => {
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record);
  return artifactMediaTypeForProjection(record.artifactMediaType, kind) || "image/png";
};

function ToolArtifactImage({
  record,
  conversationId,
}: {
  record: ActivityToolRecord;
  conversationId: string;
}) {
  // The websocket handle is installed by an effect after the first render.
  // Subscribe to the connection projection so an already-mounted historical
  // cell rebuilds its signed artifact URL when that handle becomes available
  // or a reconnect completes.
  const isConnected = useAppStore((s) => s.isConnected);
  const artifactId = String(record.artifactId || "").trim();
  const ownerConversationId = conversationId.trim();
  const mediaType = artifactMediaType(record);
  const sessionId = getWebSocket()?.sessionId?.trim() || "";
  const [reloadNonce, setReloadNonce] = useState(0);
  const [loadState, setLoadState] = useState<"loading" | "loaded" | "error">("loading");

  useEffect(() => {
    setReloadNonce(0);
    setLoadState("loading");
  }, [artifactId, ownerConversationId, mediaType, sessionId, isConnected]);

  const imageUrl = useMemo(() => withPreviewCacheBust(
    artifactImageResourceUrl({
      artifactId,
      conversationId: ownerConversationId,
      sessionId,
      source: "artifact",
      isConnected,
    }),
    reloadNonce,
  ), [artifactId, ownerConversationId, isConnected, reloadNonce, sessionId]);
  const label = readableToolLabel(artifactSummaryForRecord(record));
  const scopeMessage = !ownerConversationId
    ? "截图未关联到会话，暂时无法预览。"
    : !isConnected || !sessionId
      ? "连接已断开，重连后可预览截图。"
      : !imageUrl
        ? "截图预览地址不可用。"
        : "截图加载失败。";
  const canOpen = Boolean(imageUrl);

  const open = () => {
    if (!canOpen || !ownerConversationId) return;
    openArtifactPreview({
      artifactId,
      name: label,
      summary: record.summary,
      kind: canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record),
      mediaType,
      conversationId: ownerConversationId,
    });
  };

  const retry = (event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    setLoadState("loading");
    setReloadNonce((value) => value + 1);
  };

  return (
    <div
      className="activity-cell-artifact-card"
      data-artifact-id={artifactId}
      data-artifact-conversation-id={ownerConversationId || undefined}
      data-load-state={loadState}
    >
      <button
        type="button"
        className="activity-cell-artifact-button"
        aria-label={`打开${label}`}
        aria-disabled={!canOpen}
        disabled={!canOpen}
        onClick={open}
      >
        {imageUrl && loadState !== "error" ? (
          <img
            key={`${imageUrl}:${reloadNonce}`}
            className="activity-cell-artifact-image"
            src={imageUrl}
            alt={label}
            loading="lazy"
            onLoad={() => setLoadState("loaded")}
            onError={() => setLoadState("error")}
          />
        ) : (
          <span className="activity-cell-artifact-placeholder">
            {scopeMessage}
          </span>
        )}
        <span className="activity-cell-artifact-caption">
          <span>{label}</span>
          {record.artifactBytes != null && <span>{formatArtifactBytes(record.artifactBytes)}</span>}
        </span>
      </button>
      {loadState === "error" && imageUrl && (
        <div className="activity-cell-artifact-error" role="status" aria-live="polite">
          <span>{scopeMessage}</span>
          <button type="button" className="activity-cell-artifact-retry" onClick={retry}>
            重试
          </button>
        </div>
      )}
    </div>
  );
}

function formatArtifactBytes(value: number): string {
  if (value < 1024) return `${value} B`;
  return `${(value / 1024).toFixed(1)} KB`;
}

function activityGlyphKind(
  activityKind: ActivityCellState["activityKind"],
  record?: ActivityToolRecord,
): string {
  if (activityKind === "webSearch" && record && isWebFetchRecord(record)) {
    return "web";
  }
  return activityKind || record?.resultKind || "genericTool";
}

function activityVerb(activityKind: ActivityCellState["activityKind"]): string {
  if (activityKind === "fileRead") return "读取";
  if (activityKind === "workspaceList") return "列出文件";
  if (activityKind === "workspaceSearch") return "搜索";
  if (activityKind === "webSearch") return "获取网页";
  if (activityKind === "browser") return "操作浏览器";
  if (activityKind === "skill") return "使用技能";
  return "执行工具";
}

function inlineActionLabel(
  record: ActivityToolRecord,
  activityKind: ActivityCellState["activityKind"],
): string {
  // The turn projection already owns tool classification. Render from that
  // canonical activity kind instead of reclassifying broad result metadata
  // such as resultKind="file", which also appears on list_files results.
  if (activityKind === "workspaceList") return "List";
  if (activityKind === "workspaceSearch") return "Search";
  if (activityKind === "fileRead") return "Read";
  if (activityKind === "webSearch") {
    if (isWebFetchRecord(record)) return "Fetch";
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
