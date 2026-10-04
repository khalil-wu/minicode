import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  Globe,
  Image as ImageIcon,
  LoaderCircle,
  RotateCw,
} from "lucide-react";
import { memo, useEffect, useMemo, useRef, useState } from "react";
import { useSharedSecondTick } from "../../lib/shared-tick";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import { toolCleanupNotice } from "../../lib/tool-call-reducer";
import {
  extractToolFilePath,
  ToolGlyph,
} from "../toolUtils";
import { useAppStore } from "../../stores";
import type { ViewMode } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";
import { openWebInBrowser } from "../openWebInBrowser";
import { openArtifactPreview } from "../openAttachmentPreview";
import { CommandToolRenderer } from "./renderers/CommandRenderer";
import { WebSearchResultsView } from "./renderers/WebSearchRenderer";
import { getRecordOutputText, isBrowserRecord, isCodeModeRecord, isHttpUrl, readableRecordLabel, recordInputTarget } from "../cells/activityCellHelpers";
import { InlineDiff } from "../diff/InlineDiff";
import { workspaceRelativeDiffPath } from "../diffPaths";
import { getWebSocket } from "../../hooks/useWebSocket";
import {
  artifactResourceUrl,
  inlineImageResourceUrl,
  withPreviewCacheBust,
} from "../../lib/artifact-resource";
import {
  artifactMediaTypeForProjection,
  artifactFallbackLabel,
  canonicalArtifactKind,
  recordHasImageArtifact,
} from "../../lib/artifact-projection";

const LOCAL_URL_RE = /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:[/?#][^\s'"<>]*)?/i;

function isWebSearchRecord(record: ToolCallRecord): boolean {
  return record.resultKind === "search" || record.name === "web_search" || record.name === "websearch";
}

function evidenceLabel(record: ToolCallRecord): string {
  if (record.evidenceType === "candidate") return "候选来源";
  if (record.evidenceType === "fetched") return "已获取证据";
  return "";
}

function normalizeLocalUrl(url: string): string {
  return url.replace(/^https?:\/\/0\.0\.0\.0/i, (prefix) => prefix.replace("0.0.0.0", "localhost"));
}

const Spinner = () => (
  <LoaderCircle size={12} className="animate-spin shrink-0" aria-hidden="true" />
);

function phaseLabel(record: ToolCallRecord): string {
  if (record.status === "failed") return "失败";
  if (record.status === "timeout") return "超时";
  if (record.status === "blocked") return "已阻止";
  if (record.status === "partial") return "部分完成";
  if (record.status === "cancelled") return "已取消";
  if (isCodeModeRecord(record) && ["Script yielded", "Script running", "脚本仍在运行"].includes(record.displaySummary || "")) return "运行中";
  if (record.status === "success") return "已完成";
  const transition = String(record.transition || "").toLowerCase();
  if (transition === "waiting_approval" || record.waitingOn === "approval") return "等待审批";
  if (record.waitingOn === "user" || record.waitingOn === "user_input") return "等待用户输入";
  if (transition === "queued" || record.waitingOn === "dispatch") return "排队中";
  if (transition === "prepared") return "准备中";
  if (transition === "streaming_output") return "输出中";
  if (record.status === "pending") return "准备中";
  if (record.status === "running") return "运行中";
  return "已完成";
}

function shouldAutoOpen(viewMode: ViewMode): boolean {
  return viewMode === "verbose";
}

const SmallAction = ({
  children,
  label,
  onClick,
}: {
  children: React.ReactNode;
  label: string;
  onClick: () => void;
}) => (
  <button
    type="button"
    title={label}
    aria-label={label}
    onClick={(event) => {
      event.stopPropagation();
      onClick();
    }}
    className="h-[22px] inline-flex items-center gap-[5px] px-[7px] border border-[var(--border-subtle)] rounded bg-[var(--surface-base)] text-[var(--text-secondary)] cursor-pointer text-xs shrink-0"
  >
    {children}
  </button>
);

export const ToolCallCard = memo(({
  record,
  viewMode = "normal",
  compact = false,
  workspaceDirectory = "",
  conversationId,
}: {
  record: ToolCallRecord;
  viewMode?: ViewMode;
  compact?: boolean;
  workspaceDirectory?: string;
  /** Explicit transcript owner; side/history views must not borrow the active chat. */
  conversationId?: string;
}) => {
  const hasFailure = ["failed", "blocked", "timeout"].includes(record.status);
  const cleanupNotice = toolCleanupNotice(record.cleanupReceipt);
  const [open, setOpen] = useState(() => shouldAutoOpen(viewMode) || hasFailure);
  const [outputExpanded, setOutputExpanded] = useState(false);
  const userToggled = useRef(false);
  const isActive = record.status === "running" || record.status === "pending";
  // Shared 1s tick — one interval for all running tool cards, not N.
  const now = useSharedSecondTick(isActive);
  useEffect(() => {
    if (viewMode === "verbose") {
      // Verbose auto-expands, but a user collapse must stick instead of being
      // forced open again on every record update.
      if (!userToggled.current) setOpen(true);
      return;
    }
    if (!userToggled.current) {
      setOpen(shouldAutoOpen(viewMode) || hasFailure);
    }
  }, [hasFailure, record, record.status, viewMode]);
  const duration =
    record.finishedAt && record.startedAt
      ? `${((record.finishedAt - record.startedAt) / 1000).toFixed(1)}s`
      : record.status === "running"
        ? formatElapsed(now - (record.startedAt ?? now))
        : "";
  const filePath = extractToolFilePath(record.args);
  const inputTarget = recordInputTarget(record);
  const displayInput = filePath && inputTarget === filePath
    ? workspaceRelativeDiffPath(filePath, workspaceDirectory) || inputTarget
    : inputTarget;
  const resultText = getRecordOutputText(record)
    || (hasFailure ? record.userSummary || "工具执行失败。" : "");
  const previewUrl = normalizeLocalUrl(resultText.match(LOCAL_URL_RE)?.[0] ?? "");
  const toolLabel = readableRecordLabel(record);
  const phase = phaseLabel(record);
  const showStatus = phase !== "已完成";
  const evidence = evidenceLabel(record);
  const imageArtifact = recordHasImageArtifact(record)
    ? {
        kind: canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record),
        mediaType: artifactMediaTypeForProjection(
          record.artifactMediaType,
          canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record),
        ),
      }
    : null;

  const copyResult = () => {
    // The same user-facing result is displayed and copied. Canonical raw
    // output and orchestration envelopes remain in the record and Inspector.
    void navigator.clipboard.writeText(resultText)
      .then(() => pushToast("已复制工具结果", "success", 1200))
      .catch(() => pushToast("复制失败", "error", 1800));
  };

  const openArtifact = () => {
    if (!record.artifactId) return;
    const ownerConversationId = String(conversationId || "").trim();
    if (!ownerConversationId) return;
    openArtifactPreview({
      artifactId: record.artifactId,
      name: isBrowserRecord(record) && imageArtifact ? "浏览器截图" : artifactFallbackLabel(record.artifactKind, record.artifactMediaType),
      kind: record.artifactKind || record.resultKind,
      mediaType: record.artifactMediaType,
      conversationId: ownerConversationId,
    });
  };

  const openPreviewUrl = () => {
    if (!previewUrl) return;
    openWebInBrowser(previewUrl);
  };

  if (viewMode === "summary") {
    return (
      <div className="grid gap-1 text-xs text-[var(--text-muted)]">
        <div className="flex items-center gap-1.5 py-0.5">
          {isActive ? <Spinner /> : <ToolGlyph kind={record.activityKind || record.resultKind} size={14} className="shrink-0" />}
          <span className="text-[var(--text-secondary)] font-semibold">
            {toolLabel}
          </span>
          {displayInput && <span style={summaryValueStyle}>{displayInput}</span>}
          {showStatus && <span>{phase}</span>}
          {showStatus && duration && <span>{duration}</span>}
          {resultText && <SmallAction label="复制工具结果" onClick={copyResult}><Copy size={14} /></SmallAction>}
        </div>
        {hasFailure && <div className="text-[var(--state-danger)] whitespace-pre-wrap break-words">{resultText}</div>}
      </div>
    );
  }

  return (
    <div
      className="tool-call-enter tool-call-card activity-cell"
      data-testid={`tool-call-${record.id}`}
      data-activity-kind={record.activityKind || record.resultKind || "genericTool"}
      data-status={record.status}
      style={{
        borderLeft: 0,
        border: 0,
        borderRadius: 0,
        background: "transparent",
      }}
    >
      {(record.status === "running" || record.status === "pending") && (
        <div className="progress-bar h-0.5" />
      )}
      <div
        style={{
          width: "100%",
          display: "flex",
          alignItems: "center",
          gap: 8,
          minHeight: compact ? 28 : 34,
          padding: compact ? "4px 7px" : "8px 12px",
          background: "transparent",
          border: 0,
          color: "var(--text-primary)",
          fontSize: compact ? "var(--text-xs)" : "var(--text-sm)",
        }}
      >
        <button
          type="button"
          aria-expanded={open}
          aria-label={`${open ? "收起" : "展开"}${toolLabel}详情`}
          onClick={() => {
            userToggled.current = true;
            setOpen((value) => !value);
          }}
          style={{
            display: "flex",
            flex: "1 1 auto",
            minWidth: 0,
            alignItems: "center",
            gap: 8,
            padding: 0,
            border: 0,
            background: "transparent",
            color: "inherit",
            cursor: "pointer",
            font: "inherit",
            textAlign: "left",
          }}
        >
          {(record.status === "running" || record.status === "pending") ? <Spinner /> : <ToolGlyph kind={record.activityKind || record.resultKind} size={14} className="shrink-0" />}
          {showStatus && <span style={phaseBadgeStyle(record)}>{phase}</span>}
          <span className="text-[var(--accent-primary)] font-semibold">
            {toolLabel}
          </span>
          {displayInput && (
            <span style={toolInputInlineStyle}>
              {displayInput}
            </span>
          )}
          <span className="flex-1" />
          {showStatus && duration && (
            <span className="text-[var(--text-muted)] text-xs shrink-0">
              {duration}
            </span>
          )}
        </button>
        {record.artifactId && (
          <SmallAction label="打开产物预览" onClick={openArtifact}>
            <FileText size={14} />
            产物
          </SmallAction>
        )}
        {evidence && (
          <span style={evidenceBadgeStyle}>
            {evidence}
          </span>
        )}
        {previewUrl && (
          <SmallAction label={`在预览面板中打开 ${previewUrl}`} onClick={openPreviewUrl}>
            <Globe size={14} />
            预览
          </SmallAction>
        )}
        {resultText && (
          <SmallAction label="复制工具结果" onClick={copyResult}>
            <Copy size={14} />
            复制
          </SmallAction>
        )}
        <button
          type="button"
          title={open ? "收起工具详情" : "展开工具详情"}
          aria-label={open ? "收起工具详情" : "展开工具详情"}
          onClick={() => {
            userToggled.current = true;
            setOpen((value) => !value);
          }}
          className="inline-flex h-[22px] w-[22px] shrink-0 items-center justify-center border-0 bg-transparent p-0 text-[var(--text-muted)] cursor-pointer"
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
      </div>
      {cleanupNotice && <div className="text-xs text-[var(--text-secondary)]" role="status">{cleanupNotice}</div>}
      {imageArtifact && record.artifactId && (
        <ToolCallArtifactImage
          record={record}
          conversationId={conversationId}
          onOpen={openArtifact}
        />
      )}
      {open && (
        <div className="border-t border-[var(--border-subtle)] bg-[var(--surface-base)] overflow-y-auto"
          style={{
            maxHeight: compact ? 260 : 400,
          }}
        >
          {record.diff?.patch && (
            <>
              <div className="flex items-center pt-1 px-3.5 pb-0 gap-2 text-[var(--text-muted)] text-xs">
                <span className="flex-1 min-w-0 font-semibold">Diff</span>
              </div>
              <InlineDiff patch={record.diff.patch} contextLines={1} />
            </>
          )}
          <div className="grid gap-2 p-2.5 px-3.5 font-mono text-xs text-[var(--text-secondary)] whitespace-pre-wrap break-words">
            {record.diff && (record.diff.plus > 0 || record.diff.minus > 0) && (
              <div className="flex items-center gap-2">
                {record.diff.plus > 0 && <span className="text-[var(--state-success)]">+{record.diff.plus}</span>}
                {record.diff.minus > 0 && <span className="text-[var(--state-danger)]">-{record.diff.minus}</span>}
              </div>
            )}
            {record.limitation && (
              <div style={limitationBadgeStyle}>
                {record.limitation}
              </div>
            )}
            {resultText && (
              <div>
                {record.name !== "monitor" && (record.resultKind === "command" || record.activityKind === "commandExecution" || record.name === "run_command") ? (
                  <CommandToolRenderer record={record} resultSummary={resultText} />
                ) : isWebSearchRecord(record) ? (
                  <WebSearchResultsView text={resultText} />
                ) : (
                  <>
                    {!record.diff && <div className="text-[var(--text-muted)] mb-1 font-medium">结果</div>}
                    {resultText.length > 500 && !outputExpanded ? (
                      <>
                        <div>{resultText.slice(0, 500)}...</div>
                        <button
                          type="button"
                          onClick={() => setOutputExpanded(true)}
                          className="mt-2 text-[var(--accent-primary)] text-xs font-medium cursor-pointer bg-transparent border-0 p-0"
                        >
                          显示更多
                        </button>
                      </>
                    ) : (
                      <div>{resultText}</div>
                    )}
                  </>
                )}
              </div>
            )}
            {record.sourceUrl && isHttpUrl(record.sourceUrl) && record.sourceUrl !== inputTarget && !resultText.includes(record.sourceUrl) && (
              <div style={sourceUrlStyle}>
                来源：{record.sourceUrl}
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
});

ToolCallCard.displayName = "ToolCallCard";

function ToolCallArtifactImage({
  record,
  conversationId,
  onOpen,
}: {
  record: ToolCallRecord;
  conversationId?: string;
  onOpen: () => void;
}) {
  const isConnected = useAppStore((state) => state.isConnected);
  const sessionId = isConnected ? String(getWebSocket()?.sessionId || "").trim() : "";
  const artifactId = String(record.artifactId || "").trim();
  const ownerConversationId = String(conversationId || "").trim();
  const kind = canonicalArtifactKind(record.artifactKind, record.artifactMediaType, record);
  const mediaType = artifactMediaTypeForProjection(record.artifactMediaType, kind) || "image/png";
  const inlineUrl = inlineImageResourceUrl(record.sourceUrl);
  const [reloadNonce, setReloadNonce] = useState(0);
  const [loadState, setLoadState] = useState<"loading" | "loaded" | "error">("loading");
  const baseUrl = useMemo(() => artifactResourceUrl({
    artifactId,
    conversationId: ownerConversationId,
    sessionId,
    source: "artifact",
    originalUrl: inlineUrl,
    isConnected,
  }), [artifactId, inlineUrl, isConnected, ownerConversationId, sessionId, reloadNonce]);

  useEffect(() => {
    setReloadNonce(0);
    setLoadState("loading");
  }, [artifactId, inlineUrl, isConnected, ownerConversationId, mediaType, sessionId]);

  const imageUrl = withPreviewCacheBust(baseUrl, reloadNonce);

  const retry = () => {
    setLoadState("loading");
    setReloadNonce((value) => value + 1);
  };

  if (!imageUrl || loadState === "error") {
    return (
      <div
        data-artifact-id={artifactId}
        data-artifact-conversation-id={ownerConversationId || undefined}
        style={toolArtifactFallbackStyle}
      >
        <ImageIcon size={16} aria-hidden="true" />
        <span>{!ownerConversationId
          ? "截图未关联会话"
          : !isConnected || !sessionId
            ? "连接恢复后载入截图"
            : "截图加载失败"}</span>
        {imageUrl && loadState === "error" && (
          <button type="button" onClick={retry} style={toolArtifactRetryStyle}>
            <RotateCw size={13} aria-hidden="true" />
            重试
          </button>
        )}
      </div>
    );
  }

  return (
    <div
      data-artifact-id={artifactId}
      data-artifact-conversation-id={ownerConversationId || undefined}
      style={toolArtifactImageWrapStyle}
    >
      <button
        type="button"
        onClick={(event) => {
          event.stopPropagation();
          onOpen();
        }}
        aria-label="打开图片预览"
        style={toolArtifactImageButtonStyle}
      >
        <img
          key={`${imageUrl}:${reloadNonce}`}
          src={imageUrl}
          alt="工具生成图片"
          style={toolArtifactImageStyle}
          data-load-state={loadState}
          onLoad={() => setLoadState("loaded")}
          onError={() => setLoadState("error")}
        />
      </button>
    </div>
  );
}

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}s`;
}

const summaryValueStyle: React.CSSProperties = {
  minWidth: 0,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  fontFamily: "var(--font-mono)",
};

const toolInputInlineStyle: React.CSSProperties = {
  minWidth: 0,
  maxWidth: "min(420px, 42vw)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
};

const evidenceBadgeStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  minHeight: 22,
  padding: "0 7px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-base)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
  flexShrink: 0,
};

const phaseBadgeStyle = (record: ToolCallRecord): React.CSSProperties => {
  const status = record.status;
  const tone = status === "failed" || status === "timeout" || status === "blocked"
    ? "var(--state-warning)"
    : status === "success"
      ? "var(--state-success)"
      : "var(--text-muted)";
  return {
    display: "inline-flex",
    alignItems: "center",
    minHeight: 20,
    padding: "0 6px",
    border: "1px solid var(--border-subtle)",
    borderRadius: "var(--radius-sm, 4px)",
    background: "var(--surface-base)",
    color: tone,
    fontSize: "var(--text-3xs)",
    fontWeight: "var(--fw-bold)",
    textTransform: "uppercase",
    letterSpacing: 0,
    flexShrink: 0,
  };
};

const limitationBadgeStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  width: "fit-content",
  padding: "5px 8px",
  border: "1px solid color-mix(in oklch, var(--state-warning) 35%, var(--border-subtle))",
  borderRadius: "var(--radius-sm, 4px)",
  background: "color-mix(in oklch, var(--state-warning) 9%, var(--surface-soft))",
  color: "var(--text-secondary)",
  fontSize: "var(--text-xs)",
  fontFamily: "var(--font-mono)",
};

const sourceUrlStyle: React.CSSProperties = {
  color: "var(--text-muted)",
  fontFamily: "var(--font-mono)",
  wordBreak: "break-all",
};

const toolArtifactImageWrapStyle: React.CSSProperties = {
  margin: "0 12px 8px",
  overflow: "hidden",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-soft)",
};

const toolArtifactImageButtonStyle: React.CSSProperties = {
  display: "block",
  width: "100%",
  padding: 0,
  border: 0,
  background: "transparent",
  cursor: "zoom-in",
};

const toolArtifactImageStyle: React.CSSProperties = {
  display: "block",
  width: "100%",
  maxHeight: 220,
  objectFit: "contain",
};

const toolArtifactFallbackStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 7,
  margin: "0 12px 8px",
  padding: "10px 12px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 6px)",
  background: "var(--surface-soft)",
  color: "var(--text-muted)",
  fontSize: "var(--text-xs)",
};

const toolArtifactRetryStyle: React.CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  marginLeft: "auto",
  padding: "2px 7px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "transparent",
  color: "var(--accent-primary)",
  cursor: "pointer",
  font: "inherit",
};
