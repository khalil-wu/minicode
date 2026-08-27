import {
  ChevronDown,
  ChevronRight,
  Copy,
  FileText,
  Globe,
  LoaderCircle,
  TerminalSquare,
} from "lucide-react";
import { memo, useEffect, useRef, useState } from "react";
import { useSharedSecondTick } from "../../lib/shared-tick";
import { isFileChangeToolRecord, type ToolCallRecord, type ToolCallStatus } from "../../lib/tool-call-reducer";
import {
  extractToolFilePath,
  ToolGlyph,
} from "../toolUtils";
import { StatusIcon } from "../../components/icons";
import { useAppStore } from "../../stores";
import type { ViewMode } from "../../stores/types";
import { pushToast } from "../../overlays/ToastContainer";
import { openWebInBrowser } from "../openWebInBrowser";
import { purifyToolErrorText } from "../errorMessages";
import { readableToolLabel } from "../toolDisplayName";
import { openArtifactPreview } from "../openAttachmentPreview";
import {
  CommandToolRenderer,
  FileChangeToolRenderer,
  WebSearchToolRenderer,
} from "./renderers";
import { InlineDiff } from "../diff/InlineDiff";
import { workspaceRelativeDiffPath } from "../diffPaths";

const LOCAL_URL_RE = /https?:\/\/(?:localhost|127\.0\.0\.1|0\.0\.0\.0):\d+(?:[/?#][^\s'"<>]*)?/i;

function isCommandRecord(record: ToolCallRecord): boolean {
  return record.resultKind === "command" || record.activityKind === "commandExecution";
}

function isWebRecord(record: ToolCallRecord): boolean {
  return record.resultKind === "web" || record.resultKind === "search";
}

function evidenceLabel(record: ToolCallRecord): string {
  const parts: string[] = [];
  if (record.evidenceType === "candidate") parts.push("候选来源");
  else if (record.evidenceType === "fetched") parts.push("已获取证据");
  else if (record.evidenceType) parts.push(record.evidenceType);
  if (record.extractionStatus) parts.push(`提取状态：${record.extractionStatus}`);
  return parts.join(" - ");
}

const RawJsonDetails = ({ args }: { args: Record<string, unknown> }) => {
  if (Object.keys(args).length === 0) return null;
  return (
    <details className="border-t border-[var(--border-subtle)] pt-2">
      <summary className="cursor-pointer text-[var(--text-muted)] text-xs inline-flex items-center gap-[5px]">
        原始 JSON
      </summary>
      <pre className="mt-[7px] mb-0 p-2 border border-[var(--border-subtle)] rounded bg-[var(--surface-soft)] text-[var(--text-secondary)] whitespace-pre-wrap break-words">
        {JSON.stringify(args, null, 2)}
      </pre>
    </details>
  );
};

function normalizeLocalUrl(url: string): string {
  return url.replace(/^https?:\/\/0\.0\.0\.0/i, (prefix) => prefix.replace("0.0.0.0", "localhost"));
}

const Spinner = () => (
  <LoaderCircle size={12} className="animate-spin shrink-0" aria-hidden="true" />
);

function phaseLabel(record: ToolCallRecord): string {
  const transition = String(record.transition || "").toLowerCase();
  if (transition === "waiting_approval") return "等待授权";
  if (transition === "queued") return "排队中";
  if (transition === "prepared") return "准备中";
  if (transition === "streaming_output") return "输出中";
  if (record.status === "pending") return "准备中";
  if (record.status === "running") return "运行中";
  if (record.status === "failed" || record.status === "timeout") return "需要处理";
  if (record.status === "blocked") return "已阻止";
  if (record.status === "partial") return "部分完成";
  if (record.status === "cancelled") return "已取消";
  return "已完成";
}

function shouldAutoOpen(viewMode: ViewMode): boolean {
  return viewMode === "verbose";
}

function waitingOnLabel(record: ToolCallRecord): string {
  if (record.blockingReason) return record.blockingReason;
  if (record.waitingOn) {
    const waitingOn = record.waitingOn.toLowerCase();
    if (waitingOn === "approval") return "等待审批";
    if (waitingOn === "dispatch") return "等待调度";
    if (waitingOn === "user" || waitingOn === "user_input") return "等待用户输入";
    return `等待 ${record.waitingOn.replace(/^waiting(?: on)?\s*/i, "")}`;
  }
  if (record.transition === "waiting_approval") return "等待审批";
  if (record.transition === "queued") return "等待调度";
  if (record.status === "pending") return "等待调度";
  return "";
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
}: {
  record: ToolCallRecord;
  viewMode?: ViewMode;
  compact?: boolean;
  workspaceDirectory?: string;
}) => {
  const [open, setOpen] = useState(() => shouldAutoOpen(viewMode));
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
      setOpen(shouldAutoOpen(viewMode));
    }
  }, [record, record.status, viewMode]);
  const duration =
    record.finishedAt && record.startedAt
      ? `${((record.finishedAt - record.startedAt) / 1000).toFixed(1)}s`
      : record.status === "running"
        ? formatElapsed(now - (record.startedAt ?? now))
        : "";
  const waitingOn = waitingOnLabel(record);

  const filePath = extractToolFilePath(record.args);
  const inputSummary = record.inputSummary || "";
  const displayFilePath = filePath
    ? workspaceRelativeDiffPath(filePath, workspaceDirectory) || filePath
    : null;
  const displayInput = displayFilePath || inputSummary;
  const previewUrl = record.summary ? normalizeLocalUrl(record.summary.match(LOCAL_URL_RE)?.[0] ?? "") : "";
  const rawResultSummary = purifyToolErrorText(record.summary?.trim() || "");
  const resultSummary = record.displaySummary || rawResultSummary;
  const readableName = readableToolLabel(record.name);
  const hasReadableProtocolName = Boolean(record.name && readableName !== record.name);
  const toolLabel = hasReadableProtocolName
    ? readableName
    : readableToolLabel(
        viewMode === "verbose"
          ? record.displayHint || record.name
          : record.displayHint || record.displaySummary || record.name || "工具",
      );
  const evidence = evidenceLabel(record);

  const copyResult = () => {
    // Copy the actual bounded tool output, not the humanized display summary
    // (cc copies the real result; the summary may drop or rephrase content).
    const text = record.outputPreview
      || record.stdoutPreview
      || record.stderrPreview
      || resultSummary
      || record.summary
      || "";
    if (!text) return;
    void navigator.clipboard?.writeText(text)
      .then(() => pushToast("已复制工具结果", "success", 1200))
      .catch(() => pushToast("复制失败", "error", 1800));
  };

  const openArtifact = () => {
    if (!record.artifactId) return;
    const store = useAppStore.getState();
    openArtifactPreview({
      artifactId: record.artifactId,
      name: record.displaySummary || record.summary || "生成文件",
      summary: record.displaySummary || record.summary,
      kind: record.resultKind,
      conversationId: store.conversationId || undefined,
    });
  };

  const openPreviewUrl = () => {
    if (!previewUrl) return;
    openWebInBrowser(previewUrl);
  };

  if (viewMode === "summary") {
    return (
      <div className="flex items-center gap-1.5 py-0.5 text-xs text-[var(--text-muted)]">
        <StatusIcon status={record.status} size={14} />
        <ToolGlyph kind={record.activityKind || record.resultKind} size={14} className="shrink-0" />
        <span className="text-[var(--text-secondary)] font-semibold">
          {toolLabel}
        </span>
        {displayFilePath && (
          <span style={summaryValueStyle}>{displayFilePath}</span>
        )}
        {!filePath && inputSummary && <span style={summaryValueStyle}>{inputSummary}</span>}
        {duration && <span>{duration}</span>}
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
          className={record.status === "running" ? "anim-tool-running" : undefined}
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
          <span style={phaseBadgeStyle(record)}>{phaseLabel(record)}</span>
          <span className="text-[var(--accent-primary)] font-semibold">
            {toolLabel}
          </span>
          {displayInput && (
            <span style={toolInputInlineStyle}>
              {displayInput}
            </span>
          )}
          <span className="flex-1" />
          {duration && (
            <span className="text-[var(--text-muted)] text-xs shrink-0">
              {record.status === "running" ? `运行中 · ${duration}` : record.status === "pending" ? `准备中 · ${duration}` : duration}
            </span>
          )}
          {waitingOn && (
            <span className="text-[var(--text-muted)] text-xs shrink-0">
              {waitingOn}
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
        {resultSummary && (
          <SmallAction label="复制工具结果" onClick={copyResult}>
            <Copy size={14} />
            复制
          </SmallAction>
        )}
        <StatusIcon status={record.status} size={14} />
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
            {inputSummary && (
              <div style={humanSummaryStyle}>
            {isCommandRecord(record) ? <TerminalSquare size={14} /> : <ToolGlyph kind={record.activityKind || record.resultKind} size={14} />}
            <span>{inputSummary}</span>
          </div>
        )}
            {record.limitation && (
              <div style={limitationBadgeStyle}>
                {record.limitation}
              </div>
            )}
            {resultSummary && (
              <div>
                {isCommandRecord(record) ? (
                  <CommandToolRenderer record={record} resultSummary={resultSummary} />
                ) : isWebRecord(record) ? (
                  <WebSearchToolRenderer
                    record={record}
                    resultSummary={resultSummary}
                    rawResultSummary={rawResultSummary}
                  />
                ) : isFileChangeToolRecord(record) ? (
                  <FileChangeToolRenderer
                    record={record}
                    inputSummary={displayFilePath || inputSummary}
                    resultSummary={resultSummary}
                  />
                ) : (
                  <>
                    <div className="text-[var(--text-muted)] mb-1 font-medium">结果</div>
                    {resultSummary.length > 500 && !outputExpanded ? (
                      <>
                        <div>{resultSummary.slice(0, 500)}...</div>
                        <button
                          type="button"
                          onClick={() => setOutputExpanded(true)}
                          className="mt-2 text-[var(--accent-primary)] text-xs font-medium cursor-pointer bg-transparent border-0 p-0"
                        >
                          显示更多
                        </button>
                      </>
                    ) : (
                      <div>{resultSummary}</div>
                    )}
                  </>
                )}
              </div>
            )}
            {record.sourceUrl && (
              <div style={sourceUrlStyle}>
                来源：{record.sourceUrl}
              </div>
            )}
            {record.contentPreview && purifyToolErrorText(record.contentPreview) !== resultSummary && !isWebRecord(record) && (
              <div>
                <div className="text-[var(--text-muted)] mb-1 font-medium">内容预览</div>
                <div>{purifyToolErrorText(record.contentPreview)}</div>
              </div>
            )}
            {viewMode === "verbose" && <RawJsonDetails args={record.args} />}
          </div>
        </div>
      )}
    </div>
  );
});

ToolCallCard.displayName = "ToolCallCard";

function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}s`;
}

const humanSummaryStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 6,
  padding: "6px 8px",
  border: "1px solid var(--border-subtle)",
  borderRadius: "var(--radius-sm, 4px)",
  background: "var(--surface-soft)",
  color: "var(--text-secondary)",
  overflow: "hidden",
};

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
