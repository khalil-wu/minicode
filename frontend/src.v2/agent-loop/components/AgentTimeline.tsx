import { useState, type ReactNode } from "react";
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
      return null;
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
  const [expanded, setExpanded] = useState(false);
  const isCommandLike = item.activityKind === "command" || item.activityKind === "test";
  const expandLabel = isCommandLike ? "Expand command details" : "Expand activity details";
  const collapseLabel = isCommandLike ? "Collapse command details" : "Collapse activity details";

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
        {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        <ActivityStatusIcon status={item.status} />
        <ActivityKindIcon kind={item.activityKind} />
        <span className="agent-loop-activity-title">{item.title}</span>
        {!expanded && item.summary && (
          <span className="agent-loop-activity-summary">{item.summary}</span>
        )}
      </button>

      {expanded && item.details.length > 0 && (
        <div className="agent-loop-activity-details">
          {item.details.map((detail, index) => (
            <ActivityDetailView key={`${detail.kind}:${detail.title}:${index}`} detail={detail} />
          ))}
        </div>
      )}
    </section>
  );
}

function ActivityDetailView({ detail }: { detail: ActivityDetail }) {
  if (detail.kind === "shell") {
    return (
      <section className="agent-loop-shell-detail" aria-label={detail.title}>
        <div className="agent-loop-shell-header">
          <span>{detail.title}</span>
          {detail.exitCode != null && (
            <span>{detail.exitCode === 0 ? "成功" : `exit ${detail.exitCode}`}</span>
          )}
        </div>
        <pre className="agent-loop-shell-command">{`$ ${detail.command}`}</pre>
        {detail.output ? (
          <pre className="agent-loop-shell-output">{detail.output}</pre>
        ) : (
          <div className="agent-loop-detail-empty">无输出</div>
        )}
      </section>
    );
  }

  if (detail.kind === "source") {
    const openDetail = () => {
      const store = useAppStore.getState();
      if (detail.url) {
        store.openLivePreview(detail.url);
        return;
      }
      if (detail.path) {
        const label = detail.path.split(/[/\\]/).filter(Boolean).pop() ?? detail.path;
        store.openEditorFile(detail.path, label);
        store.setRightStackTab("inspector");
        return;
      }
      store.setRightStackTab("inspector");
    };
    const targetText = detail.url || detail.path || detail.query || detail.excerpt || "";
    return (
      <section className="agent-loop-source-detail">
        <div className="agent-loop-detail-title">{detail.title}</div>
        {targetText ? (
          <button type="button" className="agent-loop-source-link" onClick={openDetail}>
            {targetText}
          </button>
        ) : null}
        {detail.excerpt && detail.excerpt !== targetText ? (
          <div className="agent-loop-detail-text">{detail.excerpt}</div>
        ) : null}
      </section>
    );
  }

  return (
    <section className="agent-loop-text-detail">
      <div className="agent-loop-detail-title">{detail.title}</div>
      <div className="agent-loop-detail-text">{detail.content}</div>
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
