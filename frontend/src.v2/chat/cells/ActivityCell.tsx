import { memo, useEffect, useMemo, useRef, useState, type MouseEvent } from "react";
import { Check, ChevronDown, ChevronRight, Circle, Copy, Pencil, Wifi, WifiOff } from "lucide-react";
import type { ActivityCellState } from "./cellTypes";
import { useAppStore } from "../../stores";
import {
  type ActivityDetail,
  type ActivityToolRecord,
  readableTimelineTitle,
  describeRecordDetail,
  describeRecordDetails,
  getOutputPreview,
  getRecordOutputPreview,
  isHttpUrl,
  fileLabel,
  recordInputTarget,
  readableRecordLabel,
  planUpdateSteps,
  type PlanUpdateStep,
  isWebFetchActivity,
  isWebFetchRecord,
  webFetchEvidenceLabel,
  isBrowserRecord,
  browserFailureGuidance,
} from "./activityCellHelpers";
import { getToolDiffStats, toolCleanupNotice } from "../../lib/tool-call-reducer";
import {
  activityCellStatus,
  formatCellDuration,
  isRunningCellStatus,
} from "./cellStatus";
import { readableToolLabel } from "../toolDisplayName";
import { ToolGlyph } from "../toolUtils";
import { openWebTarget } from "../openWebTarget";
import { normalizeAgentErrorMessage, normalizeToolErrorMessage, purifyToolErrorText } from "../errorMessages";
import { RollingNumber } from "../../components/RollingNumber";
import { InlineDiff } from "../diff/InlineDiff";
import { getWebSocket } from "../../hooks/useWebSocket";
import { openArtifactPreview, openWorkspaceFilePreview } from "../openAttachmentPreview";
import { workspaceRootsEqual } from "../../lib/workspace-path";
import {
  artifactResourceUrl,
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
import { useTranscriptSearch } from "../TranscriptSearchContext";

/**
 * ActivityCell — compact action + original target, with evidence on disclosure.
 *
 * Success needs no second status label. Failures, partial completion,
 * interruptions and approvals remain visible without opening the details.
 */
export const ActivityCell = memo(function ActivityCell({
  cell,
  conversationId,
  workspaceRoot,
}: {
  cell: ActivityCellState;
  /** Explicit owner for artifacts in this cell.  Cells can be rendered from a
   * historical or child transcript while the app's active conversation differs. */
  conversationId?: string;
  workspaceRoot?: string;
}) {
  const developerMode = useAppStore((s) => s.viewMode === "verbose");
  const activeWorkspace = useAppStore((s) => s.workingDirectory);
  const workingDirectory = workspaceRoot ?? activeWorkspace;
  const ownerConversationId = String(conversationId || "").trim();
  const records = useMemo(() => cell.toolCallRecords ?? [], [cell.toolCallRecords]);
  const hasRecords = records.length > 0;
  const browserRecords = records.filter(isBrowserRecord);
  const isBrowserAction = browserRecords.length > 0;
  const isFileChange = cell.activityKind === "fileChange";
  const isRead = cell.activityKind === "fileRead";
  const isWorkspaceSearch = cell.activityKind === "workspaceSearch";
  const isWorkspaceList = cell.activityKind === "workspaceList";
  const isWebAction = cell.activityKind === "webSearch";
  const isWebFetchAction = isWebFetchActivity(cell);
  const isInlineAction = isRead || isWorkspaceSearch || isWorkspaceList || isWebAction;
  const status = activityCellStatus(cell.status);
  const isRunning = isRunningCellStatus(status);
  const fileChangeStats = useMemo(() => {
    if (!isFileChange) return undefined;
    return records.reduce((stats, record) => {
      if (!record.diff) return stats;
      const diff = getToolDiffStats(record.diff);
      return { plus: stats.plus + diff.plus, minus: stats.minus + diff.minus };
    }, { plus: 0, minus: 0 });
  }, [isFileChange, records]);
  const fileChangeTarget = isFileChange && records.length === 1
    ? recordInputTarget(records[0])
    : "";
  const shouldAutoExpand = !cell.collapsed;
  const [expansionPreference, setIsExpanded] = useState(shouldAutoExpand);
  const isExpanded = useTranscriptSearch() || expansionPreference;
  const [copiedPatch, setCopiedPatch] = useState<string | null>(null);
  const userToggled = useRef(false);
  const previousId = useRef(cell.id);

  useEffect(() => {
    if (previousId.current !== cell.id) userToggled.current = false;
    if (!userToggled.current) setIsExpanded(shouldAutoExpand);
    previousId.current = cell.id;
  }, [cell.id, shouldAutoExpand]);

  const isFailed = cell.status === "failed" || cell.status === "interrupted";
  const isPartial = cell.status === "partial";
  const needsApproval = records.some((record) => record.transition === "waiting_approval" || record.waitingOn === "approval");
  const attentionLabel = needsApproval ? "Awaiting approval"
    : cell.status === "interrupted" ? "Interrupted"
    : isPartial ? "Partial"
    : [
    records.some((record) => record.status === "cancelled") ? "Interrupted" : "",
    records.some((record) => record.status === "failed")
      || (cell.status === "failed" && !records.some((record) => ["blocked", "timeout"].includes(record.status))) ? "Failed" : "",
    records.some((record) => record.status === "blocked") ? "Blocked" : "",
    records.some((record) => record.status === "timeout") ? "Timed out" : "",
    isPartial || records.some((record) => record.status === "partial") ? "Partial" : "",
  ].filter(Boolean).join(" · ");
  const recordDetails = useMemo(
    () => hasRecords
      ? describeRecordDetails(records.filter(record => record.name !== "update_plan" && (!isBrowserRecord(record) || records.length > 1)), developerMode)
      : [],
    [records, developerMode, hasRecords],
  );
  const planRecords = useMemo(
    () => records.filter((record) => planUpdateSteps(record).length > 0),
    [records],
  );
  const nonPlanRecords = useMemo(
    () => records.filter((record) => record.name !== "update_plan" && !isBrowserRecord(record)),
    [records],
  );
  const imageArtifactRecords = useMemo(
    () => records.filter(isImageArtifactRecord),
    [records],
  );
  const outputPreview = getOutputPreview(nonPlanRecords);
  const showOutputPreview = !isInlineAction
    && !isFileChange
    && !isFailed
    && !isPartial
    && !isRunning
    && Boolean(outputPreview.trim());
  const inlineDisclosureRecords = useMemo(() => {
    if (!isInlineAction) return [];
    const showTargets = records.length > 1;
    return records.filter((record) => !isBrowserRecord(record)).flatMap((record) => {
      const target = recordInputTarget(record);
      const output = getRecordOutputPreview(record);
      const visibleOutput = output.trim() === target.trim() ? "" : output;
      if (!(showTargets && target.trim()) && !visibleOutput.trim()) return [];
      return [{ record, target: showTargets ? target : "", output: visibleOutput }];
    });
  }, [isInlineAction, records]);
  const name = readableTimelineTitle(cell);
  const concreteToolTarget = records.length === 1
    ? recordInputTarget(records[0])
    : isBrowserAction ? [...new Set(browserRecords.map(recordInputTarget).filter(Boolean))].join(", ") : "";
  const detailValue = hasRecords ? concreteToolTarget : cell.subtitle?.trim();
  const detail = detailValue === name ? undefined : detailValue;
  const visibleRecordDetails = recordDetails.filter((row) => row.label !== name || row.target !== (detail || "") || row.lineInfo || row.count > 1);
  const showDetailRows = !isFileChange && !isInlineAction
    && (visibleRecordDetails.length > 0 || planRecords.length > 0);
  const singleInlineDetail = isInlineAction && records.length === 1 ? describeRecordDetail(records[0], developerMode) : null;
  const changeDetails = useMemo(
    () => isFileChange ? buildChangeDetails(records) : [],
    [isFileChange, records],
  );
  const fetchBodyRecords = records.filter((record) => isWebFetchRecord(record) && record.artifactId);
  const browserDetails = browserRecords.flatMap((record) => {
    const output = getRecordOutputPreview(record);
    const rawError = purifyToolErrorText(record.stderrPreview || (!output ? record.userSummary || record.errorInfo?.user_summary || record.errorInfo?.user_message : "") || "");
    const error = rawError && !output.includes(rawError) ? rawError : "";
    const expression = record.args.action === "evaluate" && typeof record.args.expression === "string" ? record.args.expression : "";
    return expression.trim() || output.trim() || error.trim() ? [{ record, expression, output, error }] : [];
  });
  const errorDetails = records.flatMap((record) => {
    if (isBrowserRecord(record) || !["failed", "blocked", "timeout", "cancelled", "partial"].includes(record.status)) return [];
    const rawError = purifyToolErrorText(record.userSummary || record.errorInfo?.user_summary || getRecordOutputPreview(record) || record.stderrPreview || "");
    if (isInlineAction && rawError.trim() === getRecordOutputPreview(record).trim()) return [];
    const error = record.providerErrorType
      ? normalizeAgentErrorMessage(rawError, { includeProviderDetails: false })
      : normalizeToolErrorMessage(rawError);
    return error.trim() ? [{ error, label: readableRecordLabel(record) }] : [];
  });
  const hasChangeEvidence = isFileChange && changeDetails.length > 0;
  const hasInlineEvidence = isInlineAction && inlineDisclosureRecords.length > 0;
  const hasArtifactEvidence = !isFileChange && imageArtifactRecords.length > 0;
  const hasGenericEvidence = !isFileChange
    && !isInlineAction
    && (showDetailRows || showOutputPreview);
  const hasErrorEvidence = !hasInlineEvidence && errorDetails.length > 0;
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
  const progressLabel = readableToolLabel(cell.progress?.text, isRunning);
  const settledDuration = formatCellDuration(
    cell.completedAt != null && cell.startedAt != null
      ? cell.completedAt - cell.startedAt
      : records.reduce(
          (total, record) => total + (record.durationMs ?? 0),
          0,
        ) || undefined,
  );
  const canToggle = hasChangeEvidence || hasInlineEvidence || hasArtifactEvidence
    || hasGenericEvidence || hasErrorEvidence
    || fetchBodyRecords.length > 0 || browserDetails.length > 0
    || Boolean(hasRecords && settledDuration);
  const showUnifiedEvidence = isExpanded && canToggle;

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
              <span className="activity-cell-name" data-failed={isFailed}>{name}</span>
              {fileChangeTarget && <span className="activity-cell-file-change-target" title={fileChangeTarget}>{fileChangeTarget}</span>}
              {fileChangeStats && (
                <span className="activity-cell-file-change-stats">
                  <RollingNumber value={fileChangeStats?.plus ?? 0} prefix="+" className="activity-cell-added" />
                  <RollingNumber value={fileChangeStats?.minus ?? 0} prefix="-" className="activity-cell-removed" />
                </span>
              )}
            </>
          ) : (
            <span
              className="activity-cell-name"
              data-failed={isFailed}
              aria-live={isProviderRetry ? "polite" : undefined}
            >
              {providerLabel || name}
            </span>
          )}

          {!isFileChange && detail && (
            <span className={`activity-cell-detail${isRead ? " activity-cell-read-target" : ""}`} title={detail}>
              {detail}
            </span>
          )}
          {singleInlineDetail?.lineInfo && <span className="activity-cell-detail-meta">{singleInlineDetail.lineInfo}</span>}

          {attentionLabel && (
            <span className="activity-cell-detail-meta" role="status">{attentionLabel}</span>
          )}

          {isRunning && progressLabel && !providerLabel && progressLabel !== name && (
            <span className="activity-cell-progress">{progressLabel}</span>
          )}

          {canToggle && (
            <span className="activity-cell-toggle">
              {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
            </span>
          )}
        </button>

      </div>

      {records.map((record) => {
        const notice = toolCleanupNotice(record.cleanupReceipt);
        return notice ? <div key={`cleanup-${record.id}`} className="activity-cell-detail-meta" role="status">{notice}</div> : null;
      })}
      {records.filter(isWebFetchRecord).map((record) => {
        const evidence = webFetchEvidenceLabel(record);
        return evidence ? <div key={`fetch-evidence-${record.id}`} className="activity-cell-detail-meta" role="status">{evidence}</div> : null;
      })}
      {browserRecords.filter((record) => ["failed", "blocked", "timeout", "cancelled", "partial"].includes(record.status)
        || record.transition === "waiting_approval" || record.waitingOn === "approval").map((record) => (
        <BrowserRecordNotice key={`browser-notice-${record.id}`} record={record} conversationId={ownerConversationId} showAction={browserRecords.length > 1} />
      ))}

      {/* Disclosure is task evidence, not runtime provenance. Canonical call
          ids, source, script and raw envelopes remain in Inspector/replay. */}
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
                {change.patch && <button type="button" className="cell-action-btn activity-cell-copy-patch"
                  aria-label={copiedPatch === `${cell.id}:${index}` ? "已复制修改" : "复制修改"}
                  onClick={() => void navigator.clipboard.writeText(change.patch!).then(() => {
                    setCopiedPatch(`${cell.id}:${index}`);
                    window.setTimeout(() => setCopiedPatch(null), 1200);
                  })}>
                  {copiedPatch === `${cell.id}:${index}` ? <Check size={14} /> : <Copy size={14} />}
                </button>}
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
                      <DetailTarget target={target} targetKind={recordDetail.targetKind} workspaceRoot={workingDirectory} conversationId={conversationId} />
                    {recordDetail.lineInfo && <span className="activity-cell-detail-meta">{recordDetail.lineInfo}</span>}
                  </div>
                )}
                {output.trim() && <pre className="activity-cell-inline-output">{output}</pre>}
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
              {showDetailRows && visibleRecordDetails.map(({ label, target, targetKind, lineInfo, count }, i) => (
                <div key={`${label}-${target}-${i}`} className="activity-cell-detail-row">
                  <span className="activity-cell-detail-name">{label}</span>
                  {/* The header row already shows this cell's target. Repeating it
                      verbatim one line below is the duplication that made browser
                      and command cells read as two stacked boxes. */}
                  {target !== detail && <DetailTarget target={target} targetKind={targetKind} workspaceRoot={workingDirectory} conversationId={conversationId} />}
                  {lineInfo && <span className="activity-cell-detail-meta">{lineInfo}</span>}
                  {count > 1 && <span className="activity-cell-detail-count">{`x${count}`}</span>}
                </div>
              ))}
              {showOutputPreview && (
                <pre className="activity-cell-output-pre">{outputPreview}</pre>
              )}
            </>
          )}

          {browserDetails.map(({ record, expression, output, error }) => (
            <div key={`browser-details-${record.id}`} className="activity-cell-tool-detail-card">
              {expression.trim() && <pre className="activity-cell-inline-output" aria-label="JavaScript">{expression}</pre>}
              {output.trim() && <pre className="activity-cell-output-pre" aria-label="操作结果">{output}</pre>}
              {error.trim() && <pre className="activity-cell-error-pre" aria-label="错误详情">{error}</pre>}
            </div>
          ))}

          {/* Failed records use the same evidence frame as every other tool. */}
          {hasErrorEvidence && (
            <div className="activity-cell-error-detail">
              {errorDetails.map(({ error, label }, i) => (
                <div key={`error-${i}`} className="activity-cell-error-item">
                  {records.length > 1 && label && <div className="activity-cell-error-label">{label}</div>}
                  <pre className="activity-cell-error-pre">{error}</pre>
                </div>
              ))}
            </div>
          )}
          {(fetchBodyRecords.length > 0 || settledDuration) && (
            <div className="activity-cell-evidence-footer">
              {fetchBodyRecords.map((record) => (
                <button key={`fetch-body-${record.id}`} type="button" className="activity-cell-evidence-button" onClick={() => openArtifactPreview({
                  artifactId: record.artifactId!, name: "页面正文", kind: "text",
                  mediaType: record.artifactMediaType || "text/plain", conversationId: ownerConversationId,
                })}>查看页面正文</button>
              ))}
              {settledDuration && <span className="activity-cell-detail-duration">{settledDuration}</span>}
            </div>
          )}
        </div>
      )}
    </div>
  );
});

function BrowserRecordNotice({ record, conversationId, showAction }: {
  record: ActivityToolRecord;
  conversationId: string;
  showAction: boolean;
}) {
  // Historical and child cells must not borrow the active task's preview.
  const workbench = useAppStore((state) => conversationId ? state.conversationWorkbenchStates[conversationId] : undefined);
  const activeProcesses = useAppStore((state) => conversationId && conversationId === state.conversationId ? state.previewLaunchProcesses : undefined);
  const activeVerification = useAppStore((state) => conversationId && conversationId === state.conversationId ? state.previewVerification : undefined);
  const guidance = browserFailureGuidance(record, workbench || (activeProcesses && {
    previewLaunchProcesses: activeProcesses,
    previewVerification: activeVerification ?? null,
  }));
  return (
    <div className="activity-cell-error-detail browser-record-notice" role="status" data-browser-call-id={record.id}>
      <div className="activity-cell-error-item">
        {showAction && <div className="activity-cell-error-label">{readableRecordLabel(record)}</div>}
        <div className="error-cell-message">{guidance.reason}</div>
        {guidance.previewState && <div className="error-cell-message">{guidance.previewState}</div>}
        {guidance.previewUrls?.map((url) => <div key={url} className="activity-cell-detail-row">
          <span className="activity-cell-detail-meta">本会话预览 URL</span>
          <span className="activity-cell-detail-path" title={url}>{url}</span>
        </div>)}
        <div className="error-cell-suggestion"><span>下一步：{guidance.nextStep}</span></div>
      </div>
    </div>
  );
}

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
    artifactResourceUrl({
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

type ChangeDetail = {
  path: string;
  patch?: string;
  additions: number;
  deletions: number;
};

function buildChangeDetails(records: ActivityToolRecord[]): ChangeDetail[] {
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
          path: file.path,
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
        path: path || "Edited file",
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
  workspaceRoot,
  conversationId,
}: {
  target: string;
  targetKind: ActivityDetail["targetKind"];
  workspaceRoot: string;
  conversationId?: string;
}) {
  const text = target;
  if (!text.trim()) return null;

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
          const store = useAppStore.getState();
          if (workspaceRootsEqual(workspaceRoot, store.workingDirectory)) {
            store.openEditorFile(text, fileLabel(text));
          } else {
            openWorkspaceFilePreview({ path: text, name: fileLabel(text), workspaceRoot, conversationId });
          }
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
