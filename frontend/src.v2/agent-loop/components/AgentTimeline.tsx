import { useEffect, useState, type ReactNode } from "react";
import {
  CheckCircle2,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  FileSearch,
  Globe2,
  Loader2,
  Monitor,
  Puzzle,
  Search,
  Terminal,
} from "lucide-react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import type {
  ActivityDetail,
  ActivityGroupItem,
  AgentTimelineItem,
  BrowserPreviewItem,
  ProcessItem,
  SystemStatusItem,
} from "../types";
import type { RenderAgentCell } from "./AgentTurn";
import { useAppStore } from "../../stores";
import { FileChangesCard } from "./FileChangesCard";

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
  const hasStructuredItems = Boolean(items?.length);

  return (
    <div className="chat-turn-process-stack agent-loop-timeline">
      {hasStructuredItems
        ? withStableRenderKeys(items ?? []).map(({ cell: item, key }) => (
            <AgentTimelineStructuredItem key={key} item={item} />
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

export function withStableRenderKeys<T extends { id: string; kind?: string; type?: string }>(cells: T[]) {
  const seen = new Map<string, number>();
  return cells.map((cell) => {
    const base = `${cell.kind ?? cell.type ?? "item"}:${cell.id}`;
    const count = seen.get(base) ?? 0;
    seen.set(base, count + 1);
    return {
      cell,
      key: count === 0 ? base : `${base}:${count}`,
    };
  });
}

function AgentTimelineStructuredItem({ item }: { item: AgentTimelineItem }) {
  switch (item.type) {
    case "process":
      return <ProcessNote item={item} />;
    case "activity_group":
      return <StructuredActivityGroup item={item} />;
    case "browser_preview":
      return <BrowserPreview item={item} />;
    case "system_status":
      return <SystemStatusNote item={item} />;
    case "file_changes":
      return <FileChangesCard cell={item.cell} />;
    default:
      return null;
  }
}

function ProcessNote({ item }: { item: ProcessItem }) {
  return (
    <div className="agent-loop-process-note" data-source={item.source} data-kind={item.kind}>
      {item.content}
    </div>
  );
}

function StructuredActivityGroup({ item }: { item: ActivityGroupItem }) {
  const [expanded, setExpanded] = useState(() => !item.defaultCollapsed);
  const isCommandLike = item.activityKind === "command" || item.activityKind === "test";
  const expandLabel = isCommandLike ? "Expand command details" : "Expand activity details";
  const collapseLabel = isCommandLike ? "Collapse command details" : "Collapse activity details";

  useEffect(() => {
    setExpanded(!item.defaultCollapsed);
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
        aria-label={expanded ? collapseLabel : expandLabel}
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <ActivityStatusIcon status={item.status} />
        <span className="agent-loop-activity-kind-icon" aria-hidden="true">
          <ActivityKindIcon kind={item.activityKind} />
        </span>
        <span className="agent-loop-activity-title">{item.title}</span>
        {!expanded && item.summary && (
          <span className="agent-loop-activity-summary">{item.summary}</span>
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
        <code className="agent-loop-shell-command">$ {truncateCommand(detail.command)}</code>
        <span className="agent-loop-detail-meta">
          {collapsible && !showFull && (
            <span className="agent-loop-detail-hint">+{hiddenLines} 行 · 展开</span>
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
  const targetLabel = detail.path ? basename(detail.path) : target;
  const excerpt =
    detail.excerpt && detail.excerpt !== target ? detail.excerpt : undefined;
  const collapsible = Boolean(excerpt) && (excerpt?.length ?? 0) > EXCERPT_COLLAPSE_CHARS;
  const [expanded, setExpanded] = useState(!collapsible);
  const openable = Boolean(detail.url || detail.path);

  const openTarget = () => {
    const store = useAppStore.getState();
    if (detail.url) {
      store.openLivePreview(detail.url);
      return;
    }
    if (detail.path) {
      store.openEditorFile(detail.path, basename(detail.path));
      store.setRightStackTab("inspector");
      return;
    }
    store.setRightStackTab("inspector");
  };

  return (
    <section className="agent-loop-source-detail" data-expanded={expanded} aria-label={detail.title}>
      <div className="agent-loop-detail-header">
        <button
          type="button"
          className="agent-loop-detail-toggle"
          aria-label={expanded ? "Collapse detail" : "Expand detail"}
          aria-expanded={excerpt ? expanded : undefined}
          disabled={!excerpt}
          onClick={() => excerpt && setExpanded((value) => !value)}
        >
          <span className="agent-loop-detail-chevron" aria-hidden="true">
            {excerpt ? <DetailChevron open={expanded} /> : <FileSearch size={12} />}
          </span>
          <span className="agent-loop-detail-title">{detail.title}</span>
        </button>
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
        {collapsible && !expanded && (
          <span className="agent-loop-detail-hint">展开</span>
        )}
      </div>
      {expanded && excerpt && <div className="agent-loop-detail-text">{excerpt}</div>}
    </section>
  );
}

function TextDetail({ detail }: { detail: Extract<ActivityDetail, { kind: "text" }> }) {
  const collapsible = detail.content.length > EXCERPT_COLLAPSE_CHARS;
  const [expanded, setExpanded] = useState(!collapsible);
  return (
    <section className="agent-loop-text-detail" data-expanded={expanded} aria-label={detail.title}>
      <button
        type="button"
        className="agent-loop-detail-header"
        aria-expanded={collapsible ? expanded : undefined}
        disabled={!detail.content}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="agent-loop-detail-chevron" aria-hidden="true">
          <DetailChevron open={expanded} />
        </span>
        <span className="agent-loop-detail-title">{detail.title}</span>
        {collapsible && !expanded && <span className="agent-loop-detail-hint">展开</span>}
      </button>
      {expanded && <div className="agent-loop-detail-text">{detail.content}</div>}
    </section>
  );
}

function BrowserPreview({ item }: { item: BrowserPreviewItem }) {
  const openPreview = () => {
    if (!item.url) {
      useAppStore.getState().setRightStackTab("preview");
      return;
    }
    useAppStore.getState().openLivePreview(item.url);
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
  if (status === "running") return <Loader2 size={14} className="agent-loop-spin-icon" />;
  if (status === "failed") return <CircleAlert size={14} className="agent-loop-failed-icon" />;
  return <CheckCircle2 size={14} className="agent-loop-done-icon" />;
}

function ActivityKindIcon({ kind }: { kind: ActivityGroupItem["activityKind"] }) {
  switch (kind) {
    case "command":
    case "test":
      return <Terminal size={14} />;
    case "web_search":
      return <Search size={14} />;
    case "web_read":
      return <Globe2 size={14} />;
    case "file_read":
      return <FileSearch size={14} />;
    case "browser":
      return <Monitor size={14} />;
    case "mcp":
      return <Puzzle size={14} />;
    default:
      return <Search size={14} />;
  }
}
