import { useEffect, useRef, useState, type ReactNode } from "react";
import {
  ChevronDown,
  ChevronRight,
  FileSearch,
  Globe,
  Monitor,
  PencilLine,
  Puzzle,
  Search,
  SquareTerminal,
  Terminal,
  type LucideIcon,
} from "lucide-react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import type {
  ActivityDetail,
  ActivityGroupItem,
  AgentTimelineItem,
  BrowserPreviewItem,
  LiveNarrationItem,
  ProcessItem,
  SystemStatusItem,
} from "../types";
import type { RenderAgentCell } from "./AgentTurn";
import { useAppStore } from "../../stores";
import { MarkdownRenderer } from "../../chat/messages/MarkdownRenderer";
import { openWebInPreview } from "../../chat/openWebInPreview";
import { withStableRenderKeys } from "./renderKeys";
import {
  diffFileChangeType,
  diffFileChangeTypeLabel,
} from "../../chat/cells/diffCellLabels";
import { RollingNumber } from "../../components/RollingNumber";
import { StatusIcon } from "../../components/icons";
import { InlineDiffPreview } from "./FileChangesCard";
import { workspaceRelativeDiffPath } from "../../chat/diffPaths";
import { smoothedLiveNarrationMarkdown } from "./liveNarrationSmoothing";

export function AgentTimeline({
  items,
  cells,
  renderCell,
  streamingIndicator,
}: {
  items?: AgentTimelineItem[];
  cells: AgentLoopProcessCell[];
  renderCell: RenderAgentCell;
  streamingIndicator: ReactNode;
}) {
  const hasStructuredItems = Array.isArray(items);

  return (
    <div className="chat-turn-process-stack agent-loop-timeline">
      {hasStructuredItems
        ? withStableRenderKeys(items ?? []).map(({ cell: item, key }) => (
            <AgentTimelineStructuredItem key={key} item={item} renderCell={renderCell} />
          ))
        : withStableRenderKeys(cells).map(({ cell, key }) =>
            renderCell({
              key,
              cell,
              className: "chat-turn-process-cell agent-loop-process-cell",
            }),
          )}
      {streamingIndicator}
    </div>
  );
}

function AgentTimelineStructuredItem({
  item,
  renderCell,
}: {
  item: AgentTimelineItem;
  renderCell: RenderAgentCell;
}) {
  switch (item.type) {
    case "process":
      return <ProcessNote item={item} />;
    case "live_narration":
      return <LiveNarrationNote item={item} />;
    case "activity_group":
      return <StructuredActivityGroup item={item} />;
    case "browser_preview":
      return <BrowserPreview item={item} />;
    case "system_status":
      return <SystemStatusNote item={item} />;
    case "file_changes":
      return <FileChangesTimelineItem item={item} />;
    case "plan":
      return renderCell({
        cell: item.cell,
        className: "chat-turn-process-cell agent-loop-process-cell",
      });
    default:
      return null;
  }
}

const TIMELINE_FILE_LIMIT = 4;

function FileChangesTimelineItem({ item }: { item: Extract<AgentTimelineItem, { type: "file_changes" }> }) {
  const defaultExpanded = item.files.length === 1;
  const [expanded, setExpanded] = useState(defaultExpanded);
  const workingDirectory = useAppStore((state) => state.workingDirectory);
  const files = item.files.map((file) => ({
    ...file,
    path: workspaceRelativeDiffPath(file.path, workingDirectory) || file.path,
  }));
  const visibleFiles = files.slice(0, TIMELINE_FILE_LIMIT);
  const hiddenCount = files.length - visibleFiles.length;

  return (
    <section className="agent-loop-file-inline" data-expanded={expanded ? "true" : "false"}>
      <button
        type="button"
        className="agent-loop-file-inline-row"
        aria-label={expanded ? "Collapse edited files" : "Expand edited files"}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="agent-loop-file-inline-icon" aria-hidden="true">
          <PencilLine size={14} />
        </span>
        <span className="agent-loop-file-inline-title">{timelineFileChangesTitle(item.cell)}</span>
        <span className="agent-loop-file-inline-stats">
          <RollingNumber value={item.added} prefix="+" className="agent-loop-file-added" animateOnMount />
          <RollingNumber value={item.removed} prefix="-" className="agent-loop-file-removed" animateOnMount />
        </span>
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>

      {expanded && (
        <div className="agent-loop-file-inline-panel">
          <div className="agent-loop-file-inline-list">
            {visibleFiles.map((file, index) => (
              <TimelineFileRow
                key={file.path || index}
                file={file}
                defaultDiffOpen={defaultExpanded}
              />
            ))}
            {hiddenCount > 0 && (
              <div className="agent-loop-file-inline-more">还有 {hiddenCount} 个文件在尾部改动卡中查看</div>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

function TimelineFileRow({
  file,
  defaultDiffOpen = false,
}: {
  file: Extract<AgentTimelineItem, { type: "file_changes" }>["files"][number];
  defaultDiffOpen?: boolean;
}) {
  const [diffOpen, setDiffOpen] = useState(Boolean(defaultDiffOpen && file.patch));
  const cellFile = {
    path: file.path,
    additions: file.added,
    deletions: file.removed,
    changeType: file.status === "modified" ? "updated" as const : file.status,
  };
  const openFile = () => {
    const store = useAppStore.getState();
    store.openEditorFile(file.path, basename(file.path));
  };

  return (
    <div className="agent-loop-file-inline-file-wrap">
      <button
        type="button"
        className="agent-loop-file-inline-file"
        onClick={file.patch ? () => setDiffOpen((value) => !value) : openFile}
        title={file.path}
        aria-label={file.patch ? `${diffOpen ? "收起" : "展开"} ${file.path} diff` : `打开 ${file.path}`}
        aria-expanded={file.patch ? diffOpen : undefined}
      >
        <span className="agent-loop-file-inline-path">{shortTimelinePath(file.path)}</span>
        <span className={`agent-loop-file-kind agent-loop-file-kind-${diffFileChangeType(cellFile)}`}>
          {diffFileChangeTypeLabel(cellFile)}
        </span>
        <span className="agent-loop-file-row-stats">
          <RollingNumber value={file.added} prefix="+" className="agent-loop-file-added" animateOnMount />
          <RollingNumber value={file.removed} prefix="-" className="agent-loop-file-removed" animateOnMount />
        </span>
        {file.patch && (diffOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />)}
      </button>
      {diffOpen && file.patch && (
        <InlineDiffPreview
          path={file.path}
          patch={file.patch}
          additions={file.added}
          deletions={file.removed}
        />
      )}
    </div>
  );
}

function timelineFileChangesTitle(cell: Extract<AgentTimelineItem, { type: "file_changes" }>["cell"]): string {
  const count = cell.summary.modifiedFiles || cell.files.length;
  const types = new Set(cell.files.map(diffFileChangeType));
  const verb =
    types.size === 1 && types.has("created")
      ? "已创建"
      : types.size === 1 && types.has("deleted")
        ? "已删除"
        : types.size === 1 && types.has("updated")
          ? "已编辑"
          : "已更改";
  return `${verb} ${count} 个文件`;
}

function shortTimelinePath(path: string): string {
  const parts = path.replace(/\\/g, "/").split("/").filter(Boolean);
  const value = parts.length <= 3 ? parts.join("/") : `${parts[0]}/…/${parts.slice(-2).join("/")}`;
  return value.length > 72 ? `${value.slice(0, 69)}...` : value;
}

function ProcessNote({ item }: { item: ProcessItem }) {
  if (isFilePrepareProcess(item)) {
    return null;
  }
  return (
    <div
      className="agent-loop-process-note"
      data-source={item.source}
      data-kind={item.kind}
      data-status={item.status}
    >
      <MarkdownRenderer content={item.content} isStreaming={item.status === "running"} />
    </div>
  );
}

function LiveNarrationNote({ item }: { item: LiveNarrationItem }) {
  const content = smoothedLiveNarrationMarkdown(item.partialMarkdown, item.isStreaming);
  if (!content.trim()) return null;
  return (
    <div className="agent-loop-process-note agent-loop-live-narration" data-status={item.isStreaming ? "running" : "completed"}>
      <MarkdownRenderer content={content} isStreaming={item.isStreaming} />
    </div>
  );
}

function isFilePrepareProcess(item: ProcessItem): boolean {
  const text = [item.title, item.content].filter(Boolean).join("\n").trim();
  if (!text) return false;
  const lines = text.split(/\n+/).map(normalizeProcessLine).filter(Boolean);
  return lines.length > 0 && lines.every(isLowValueFilePrepareLine);
}

function normalizeProcessLine(line: string): string {
  return line
    .trim()
    .replace(/^[-*]\s+/, "")
    .replace(/^`(.+)`$/, "$1")
    .trim();
}

function isLowValueFilePrepareLine(line: string): boolean {
  if (!line) return false;
  if (/^(?:已准备|已写入|已创建|已生成|已更新|已修改|已编辑|准备好了|prepared|wrote|written|created|generated|updated|modified|edited)\s+/i.test(line) && hasFilePathToken(line)) {
    return true;
  }
  return /^(?:准备|正在|已准备)(?:写入|修改|编辑)(?:\s+\S+)?$/i.test(line);
}

function hasFilePathToken(value: string): boolean {
  return /(?:^|[\s`"'(（])(?:[A-Za-z]:[\\/])?[^\s`"',，。)）:：]+(?:[\\/][^\s`"',，。)）:：]+)*\.[A-Za-z0-9]{1,12}(?:$|[\s`"',，。)）:：])/i.test(value);
}

function StructuredActivityGroup({ item }: { item: ActivityGroupItem }) {
  const [expanded, setExpanded] = useState(() => !item.defaultCollapsed);
  const userToggledRef = useRef(false);
  const itemIdRef = useRef(item.id);
  const isCommandLike = item.activityKind === "command" || item.activityKind === "test";
  const sourceLike = isSourceLikeActivity(item.activityKind);
  const showStatusIcon = item.status !== "completed";
  const expandLabel = isCommandLike ? "Expand command details" : "Expand activity details";
  const collapseLabel = isCommandLike ? "Collapse command details" : "Collapse activity details";

  useEffect(() => {
    if (itemIdRef.current !== item.id) {
      itemIdRef.current = item.id;
      userToggledRef.current = false;
      setExpanded(!item.defaultCollapsed);
      return;
    }
    if (!userToggledRef.current) {
      setExpanded(!item.defaultCollapsed);
    }
  }, [item.defaultCollapsed, item.id]);

  return (
    <section
      className="agent-loop-activity-group"
      data-kind={item.activityKind}
      data-status={item.status}
    >
      <button
        type="button"
        className="agent-loop-activity-row"
        data-source-like={sourceLike ? "true" : "false"}
        data-status-icon={showStatusIcon ? "true" : "false"}
        data-has-summary={!expanded && item.summary ? "true" : "false"}
        aria-label={expanded ? collapseLabel : expandLabel}
        aria-expanded={expanded}
        onClick={() => {
          userToggledRef.current = true;
          setExpanded((value) => !value);
        }}
      >
        {showStatusIcon && <ActivityStatusIcon status={item.status} />}
        <span className="agent-loop-activity-kind-icon" aria-hidden="true">
          <ActivityKindIcon kind={item.activityKind} />
        </span>
        <span className="agent-loop-activity-copy">
          <span className="agent-loop-activity-title">{item.title}</span>
          {!expanded && item.summary && (
            <span className="agent-loop-activity-summary">{item.summary}</span>
          )}
        </span>
        {!sourceLike && (
          <span
            className="agent-loop-activity-duration"
            data-empty={item.durationLabel ? "false" : "true"}
            aria-hidden={item.durationLabel ? undefined : "true"}
          >
            {item.durationLabel ? `已持续 ${item.durationLabel}` : ""}
          </span>
        )}
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
      </button>

      {expanded && item.details.length > 0 && (
        <div className="agent-loop-activity-details" data-open="true">
          {item.details.map((detail, index) => (
            <ActivityDetailView key={`${detail.kind}:${detail.title}:${index}`} detail={detail} />
          ))}
        </div>
      )}
    </section>
  );
}

const OUTPUT_COLLAPSE_LINES = 8;
const OUTPUT_PREVIEW_LINES = 3;
const EXCERPT_COLLAPSE_CHARS = 160;
const COMMAND_MAX_CHARS = 140;

function countLines(value: string): number {
  if (!value) return 0;
  return value.split("\n").length;
}

function firstLines(value: string, max: number): string {
  const lines = value.split("\n");
  return lines.length <= max ? value : lines.slice(0, max).join("\n");
}

function truncateCommand(command: string): string {
  const single = command.replace(/\s*\n\s*/g, " ").trim();
  if (single.length <= COMMAND_MAX_CHARS) return single;
  return `${single.slice(0, COMMAND_MAX_CHARS - 1)}…`;
}

function basename(path: string): string {
  return path.split(/[/\\]/).filter(Boolean).pop() ?? path;
}

function ActivityDetailView({ detail }: { detail: ActivityDetail }) {
  if (detail.kind === "shell") {
    return <ShellDetail detail={detail} />;
  }

  if (detail.kind === "source") {
    if (isFileChangeSourceDetail(detail)) {
      return <FileChangeSourceDetail detail={detail} />;
    }
    return <SourceDetail detail={detail} />;
  }

  return <TextDetail detail={detail} />;
}

function DetailChevron({ open }: { open: boolean }) {
  return open ? <ChevronDown size={12} /> : <ChevronRight size={12} />;
}

function ShellDetail({ detail }: { detail: Extract<ActivityDetail, { kind: "shell" }> }) {
  const output = detail.output?.replace(/\s+$/, "") ?? "";
  const lineCount = countLines(output);
  const hasBody = output.length > 0;
  const collapsible = lineCount > OUTPUT_COLLAPSE_LINES;
  const [expanded, setExpanded] = useState(false);
  const showFull = !collapsible || expanded;
  const hiddenLines = collapsible ? lineCount - OUTPUT_PREVIEW_LINES : 0;
  const exit = detail.exitCode;
  const body = showFull ? output : firstLines(output, OUTPUT_PREVIEW_LINES);
  const actionLabel = exit == null ? "已运行" : exit === 0 ? "已运行" : "运行失败";

  return (
    <section className="agent-loop-shell-detail" data-expanded={showFull} aria-label={detail.title}>
      <button
        type="button"
        className="agent-loop-detail-header"
        aria-expanded={collapsible ? showFull : undefined}
        disabled={!collapsible}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="agent-loop-detail-chevron" aria-hidden="true">
          {collapsible ? <DetailChevron open={showFull} /> : <Terminal size={12} />}
        </span>
        <span className="agent-loop-detail-title">{actionLabel}</span>
        <code className="agent-loop-shell-command">{truncateCommand(detail.command)}</code>
        <span className="agent-loop-detail-meta">
          {collapsible && !showFull && (
            <span className="agent-loop-detail-hint" title={`${hiddenLines} hidden lines`}>+{hiddenLines}</span>
          )}
          {exit != null && (
            <span className="agent-loop-detail-exit" data-ok={exit === 0}>
              {exit === 0 ? "成功" : `exit ${exit}`}
            </span>
          )}
        </span>
      </button>
      {hasBody ? (
        <pre className="agent-loop-shell-output">{body}</pre>
      ) : (
        <div className="agent-loop-detail-empty">无输出</div>
      )}
    </section>
  );
}

function SourceDetail({ detail }: { detail: Extract<ActivityDetail, { kind: "source" }> }) {
  const target = detail.url || detail.path || detail.query || "";
  const sourceType = detail.url ? "web" : detail.path ? "file" : "query";
  const targetLabel = detail.path ? basename(detail.path) : detail.url ? shortUrlDisplay(detail.url) : target;
  const excerpt =
    detail.excerpt && detail.excerpt !== target ? detail.excerpt : undefined;
  const collapsible = Boolean(excerpt) && (excerpt?.length ?? 0) > EXCERPT_COLLAPSE_CHARS;
  const [expanded, setExpanded] = useState(!collapsible);
  const openable = Boolean(detail.url || detail.path);

  const openTarget = () => {
    const store = useAppStore.getState();
    if (detail.url) {
      openWebInPreview(detail.url);
      return;
    }
    if (detail.path) {
      store.openEditorFile(detail.path, basename(detail.path));
      store.setRightStackTab("inspector");
      return;
    }
    store.setRightStackTab("inspector");
  };
  const SourceIcon = sourceType === "web" ? Globe : sourceType === "query" ? Search : FileSearch;

  return (
    <section
      className="agent-loop-source-detail"
      data-expanded={expanded}
      data-source-type={sourceType}
      aria-label={detail.title}
    >
      <div className="agent-loop-source-row">
        <span className="agent-loop-detail-chevron" aria-hidden="true">
          <SourceIcon size={12} />
        </span>
        <span className="agent-loop-detail-title">{detail.title}</span>
        {targetLabel && (
          <button
            type="button"
            className="agent-loop-source-link"
            onClick={openTarget}
            disabled={!openable}
            title={target}
          >
            {targetLabel}
          </button>
        )}
        {detail.lineInfo && (
          <span className="agent-loop-source-meta">{detail.lineInfo}</span>
        )}
        {excerpt && (
          <button
            type="button"
            className="agent-loop-detail-toggle"
            aria-label={expanded ? "Collapse detail" : "Expand detail"}
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            <span className="agent-loop-detail-chevron" aria-hidden="true">
              <DetailChevron open={expanded} />
            </span>
          </button>
        )}
      </div>
      {expanded && excerpt && <div className="agent-loop-detail-text">{excerpt}</div>}
    </section>
  );
}

function shortUrlDisplay(url: string): string {
  try {
    const parsed = new URL(url);
    const path = parsed.pathname === "/" ? "" : parsed.pathname.replace(/\/$/, "");
    const compactPath = path.length > 42 ? `${path.slice(0, 41)}…` : path;
    return `${parsed.hostname.replace(/^www\./i, "")}${compactPath}`;
  } catch {
    return url;
  }
}

function TextDetail({ detail }: { detail: Extract<ActivityDetail, { kind: "text" }> }) {
  const collapsible = detail.content.length > EXCERPT_COLLAPSE_CHARS;
  const [expanded, setExpanded] = useState(!collapsible);
  const shownContent = expanded || !collapsible ? detail.content : `${detail.content.slice(0, EXCERPT_COLLAPSE_CHARS)}…`;
  return (
    <section className="agent-loop-text-detail" data-expanded={expanded} aria-label={detail.title}>
      <div className="agent-loop-text-row">
        <span className="agent-loop-detail-chevron" aria-hidden="true">
          <Search size={12} />
        </span>
        <span className="agent-loop-detail-title">{detail.title}</span>
        <span className="agent-loop-detail-text">{shownContent}</span>
        {collapsible && (
          <button
            type="button"
            className="agent-loop-detail-toggle"
            aria-label={expanded ? "Collapse detail" : "Expand detail"}
            aria-expanded={expanded}
            onClick={() => setExpanded((value) => !value)}
          >
            <span className="agent-loop-detail-chevron" aria-hidden="true">
              <DetailChevron open={expanded} />
            </span>
          </button>
        )}
      </div>
    </section>
  );
}

function BrowserPreview({ item }: { item: BrowserPreviewItem }) {
  const openPreview = () => {
    if (!item.url) {
      useAppStore.getState().setRightStackTab("preview");
      return;
    }
    openWebInPreview(item.url);
  };
  return (
    <button
      type="button"
      className="agent-loop-browser-preview"
      data-status={item.status}
      aria-label="Open browser preview"
      title={item.url}
      onClick={openPreview}
    >
      <Monitor size={14} />
      <span>{item.title}</span>
      {item.url && <span className="agent-loop-browser-url">打开预览</span>}
    </button>
  );
}

function SystemStatusNote({ item }: { item: SystemStatusItem }) {
  return (
    <div
      className="agent-loop-system-status"
      data-tone={item.tone}
      aria-label={item.ariaLabel}
      title={item.detail}
    >
      <span>{item.content}</span>
      {item.detail && !item.ariaLabel && (
        <span className="agent-loop-system-status-detail">{item.detail}</span>
      )}
    </div>
  );
}

function ActivityStatusIcon({ status }: { status: ActivityGroupItem["status"] }) {
  if (status === "running") return <StatusIcon status="running" size={14} spinningClassName="agent-loop-spin-icon" />;
  if (status === "failed") return <StatusIcon status="failed" size={14} />;
  return <StatusIcon status="done" size={14} />;
}

function isFileChangeSourceDetail(detail: Extract<ActivityDetail, { kind: "source" }>): boolean {
  return Boolean(detail.path && detail.changeType && (detail.additions != null || detail.deletions != null));
}

function FileChangeSourceDetail({ detail }: { detail: Extract<ActivityDetail, { kind: "source" }> }) {
  const openFile = () => {
    if (!detail.path) return;
    const store = useAppStore.getState();
    store.openEditorFile(detail.path, basename(detail.path));
  };
  const changeType = detail.changeType ?? "updated";
  const added = detail.additions ?? 0;
  const removed = detail.deletions ?? 0;

  return (
    <button
      type="button"
      className="agent-loop-file-detail-row"
      data-change-type={changeType}
      onClick={openFile}
      title={detail.path}
      aria-label={`打开 ${detail.path}`}
    >
      <span className="agent-loop-file-detail-icon" aria-hidden="true">
        <PencilLine size={13} />
      </span>
      <span className="agent-loop-file-detail-action">{fileChangeSourceLabel(changeType)}</span>
      <span className="agent-loop-file-detail-path">{detail.path ? shortTimelinePath(detail.path) : ""}</span>
      <span className="agent-loop-file-row-stats">
        <RollingNumber value={added} prefix="+" className="agent-loop-file-added" animateOnMount />
        <RollingNumber value={removed} prefix="-" className="agent-loop-file-removed" animateOnMount />
      </span>
    </button>
  );
}

function isSourceLikeActivity(kind: ActivityGroupItem["activityKind"]): boolean {
  return kind === "web_search" || kind === "web_read" || kind === "file_read";
}

function fileChangeSourceLabel(changeType: Extract<ActivityDetail, { kind: "source" }>["changeType"]): string {
  if (changeType === "created") return "已创建";
  if (changeType === "deleted") return "已删除";
  return "已编辑";
}

function ActivityKindIcon({ kind }: { kind: ActivityGroupItem["activityKind"] }) {
  const Icon = ACTIVITY_KIND_ICONS[kind] ?? Search;
  return <Icon size={14} />;
}

const ACTIVITY_KIND_ICONS: Record<ActivityGroupItem["activityKind"], LucideIcon> = {
  command: SquareTerminal,
  test: SquareTerminal,
  web_search: Globe,
  web_read: Globe,
  file_read: Search,
  file_change: PencilLine,
  browser: Monitor,
  mcp: Puzzle,
  unknown: Search,
};
