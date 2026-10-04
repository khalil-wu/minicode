import { memo, useEffect, useId, useMemo, useRef, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, PencilLine, TerminalSquare } from "lucide-react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import type { RenderAgentCell } from "./AgentTurn";
import { withStableRenderKeys } from "./renderKeys";
import { ToolGlyph } from "../../chat/toolUtils";
import {
  isWebFetchActivity,
} from "../../chat/cells/activityCellHelpers";
import { isBrowserScreenshotRecord } from "../../lib/artifact-projection";
import { isProviderReasoningSummary } from "../../lib/provider-reasoning";
import { useTranscriptSearch } from "../../chat/TranscriptSearchContext";

type TimelineGroupKind = "work" | "thinking" | "narration" | "context" | "notice";
type TimelineGroup = {
  kind: TimelineGroupKind;
  cells: AgentLoopProcessCell[];
  segment?: number;
  closed: boolean;
};

const needsAttention = (cell: AgentLoopProcessCell): boolean => {
  if (cell.kind === "error") return true;
  if (cell.kind === "thinking") return Boolean(cell.isStreaming);
  return "status" in cell && ["running", "pending", "pending_approval", "failed", "partial", "blocked", "timeout"].includes(cell.status);
};

function visibleAttention(cells: AgentLoopProcessCell[]): Set<AgentLoopProcessCell> {
  const visible = new Set<AgentLoopProcessCell>();
  let failures = 0;
  for (let index = cells.length - 1; index >= 0; index--) {
    const cell = cells[index];
    if (!needsAttention(cell)) continue;
    const active = cell.kind === "thinking" || ("status" in cell && ["running", "pending", "pending_approval"].includes(cell.status));
    if (active || failures++ < 4) visible.add(cell);
  }
  return visible;
}

export const isProcessNarration = (cell: AgentLoopProcessCell): boolean => cell.kind === "thinking"
  && !cell.collapsible && !["provider", "reasoning"].includes(cell.source);

const timelineGroupKind = (cell: AgentLoopProcessCell): TimelineGroupKind => {
  if (cell.kind === "thinking") {
    return isProcessNarration(cell) ? "narration" : "thinking";
  }
  if (cell.kind === "status_notice" && /压缩|compac/i.test(`${cell.title} ${cell.message || ""}`)) return "context";
  if (cell.kind === "status_notice") return "notice";
  return "work";
};

const cellSegment = (cell: AgentLoopProcessCell): number | undefined => {
  if (cell.kind === "activity" || cell.kind === "exec" || cell.kind === "thinking" || cell.kind === "collaboration") return cell.segment;
  return undefined;
};

const cellSegmentClosed = (cell: AgentLoopProcessCell): boolean => {
  if (cell.kind === "activity" || cell.kind === "exec" || cell.kind === "thinking" || cell.kind === "collaboration") return Boolean(cell.segmentClosed);
  return true;
};

const groupTimelineCells = (cells: AgentLoopProcessCell[]): TimelineGroup[] => {
  const groups: TimelineGroup[] = [];
  for (const cell of cells) {
    const kind = timelineGroupKind(cell);
    const segment = cellSegment(cell);
    const previous = groups.at(-1);
    const joinsPrevious = previous?.kind === kind && (
      kind !== "work"
      || (segment !== undefined && previous.segment === segment)
    );
    if (joinsPrevious) {
      previous.cells.push(cell);
      previous.closed = previous.closed && cellSegmentClosed(cell);
    } else {
      groups.push({ kind, cells: [cell], segment, closed: cellSegmentClosed(cell) });
    }
  }
  return groups;
};

type WorkLabel = "Edit" | "Run" | "Read" | "List" | "Search" | "Fetch" | "Browse" | "Collaborate" | "Tool calls";

const workLabel = (cell: AgentLoopProcessCell): WorkLabel => {
  if (cell.kind === "exec") return "Run";
  if (cell.kind === "diff" || (cell.kind === "activity" && cell.activityKind === "fileChange")) return "Edit";
  if (cell.kind === "activity" && cell.activityKind === "workspaceList") return "List";
  if (cell.kind === "activity" && cell.activityKind === "workspaceSearch") return "Search";
  if (cell.kind === "activity" && cell.activityKind === "fileRead") return "Read";
  if (cell.kind === "activity" && isWebFetchActivity(cell)) return "Fetch";
  if (cell.kind === "activity" && cell.activityKind === "webSearch") {
    const names = (cell.toolCallRecords ?? []).map((record) => String(record.name || "").toLowerCase());
    if (names.length > 0 && names.every((name) => /web_search|websearch/.test(name))) return "Search";
  }
  if (cell.kind === "activity" && cell.activityKind === "browser") return "Browse";
  if (cell.kind === "collaboration") return "Collaborate";
  return "Tool calls";
};

const timelineGroupTitle = (group: TimelineGroup, live = false): string => {
  if (group.kind === "context") {
    const notice = group.cells.at(-1);
    return notice?.kind === "status_notice" ? notice.title : "上下文压缩";
  }
  if (group.kind === "notice") return "状态";
  const labels: WorkLabel[] = [];
  for (const cell of group.cells) {
    const label = workLabel(cell);
    if (!labels.includes(label)) labels.push(label);
  }
  const failed = group.cells.some((cell) => cell.kind === "error" || ("status" in cell && cell.status === "failed"));
  const interrupted = group.cells.some((cell) => "status" in cell && ["partial", "cancelled", "interrupted"].includes(cell.status));
  if (labels.every((label) => ["Read", "List", "Search"].includes(label))) {
    return live ? "Exploring" : failed ? "Exploration failed" : interrupted ? "Exploration interrupted" : "Explored";
  }
  if (labels.length === 1 && labels[0] === "Run") {
    return failed ? "Run · Failed" : interrupted ? "Run · Interrupted" : `${live ? "Running" : "Ran"} ${group.cells.length} commands`;
  }
  if (labels.length === 1 && labels[0] === "Edit") {
    return failed ? "Edit · Failed" : interrupted ? "Edit · Interrupted" : live ? "Editing files" : "Edited files";
  }
  return `${labels.join(" · ")}${failed ? " · Failed" : interrupted ? " · Interrupted" : ""}`;
};

const latestWorkGlyph = (cell: AgentLoopProcessCell | undefined): React.ReactNode => {
  if (!cell) return <TerminalSquare size={15} />;
  if (cell.kind === "exec") return <TerminalSquare size={15} />;
  if (cell.kind === "activity") return <ToolGlyph kind={cell.activityKind} size={15} />;
  if (cell.kind === "diff") return <PencilLine size={15} />;
  return <TerminalSquare size={15} />;
};

function WorkGroup({ group, renderCell, expandWorkGroups, isRunning }: { group: TimelineGroup; renderCell: RenderAgentCell; expandWorkGroups: boolean; isRunning: boolean }) {
  const searching = useTranscriptSearch();
  const [visibleCount, setVisibleCount] = useState(40);
  const previousWindow = useRef({ first: group.cells[0]?.id, count: group.cells.length });
  useEffect(() => {
    const previous = previousWindow.current;
    if (previous.first !== group.cells[0]?.id && group.cells.length > previous.count) {
      setVisibleCount((count) => count + group.cells.length - previous.count);
      setExpanded(true);
    }
    previousWindow.current = { first: group.cells[0]?.id, count: group.cells.length };
  }, [group.cells]);
  const containsScreenshot = group.cells.some((cell) => (
    cell.kind === "activity"
    && cell.toolCallRecords?.some((record) => Boolean(record.artifactId) && isBrowserScreenshotRecord(record))
  ));
  const containsFailure = group.cells.some((cell) => cell.kind === "error"
    || ((cell.kind === "exec" || cell.kind === "activity" || cell.kind === "collaboration")
      && (cell.status === "failed" || cell.status === "partial")));
  const defaultExpanded = containsFailure || (isRunning && (!group.closed || containsScreenshot)) || expandWorkGroups;
  const [expansionPreference, setExpanded] = useState(defaultExpanded);
  const expanded = searching || expansionPreference;
  const userToggled = useRef(false);
  const detailId = useId();
  useEffect(() => {
    if (!userToggled.current) setExpanded(defaultExpanded);
  }, [defaultExpanded]);
  const latest = group.cells.at(-1);
  const liveGroup = isRunning && !group.closed;
  const title = timelineGroupTitle(group, liveGroup);
  const labels = group.cells.map(workLabel);
  const groupGlyph = labels.includes("Edit")
    ? <PencilLine size={15} />
    : labels.includes("Run")
      ? <TerminalSquare size={15} />
      : (() => {
          const firstActivity = group.cells.find((cell) => cell.kind === "activity");
          return firstActivity?.kind === "activity"
            ? <ToolGlyph kind={firstActivity.activityKind} size={15} />
            : <TerminalSquare size={15} />;
        })();
  const attention = useMemo(() => visibleAttention(group.cells), [group.cells]);
  const visibleCells = group.cells.filter((cell, index) => searching || index >= group.cells.length - visibleCount || attention.has(cell));
  const hiddenCount = group.cells.length - visibleCells.length;
  const hiddenFailures = group.cells.filter((cell, index) => index < group.cells.length - visibleCount && !attention.has(cell) && needsAttention(cell)).length;
  const keyed = withStableRenderKeys(visibleCells);
  return (
    <section className={`agent-loop-timeline-group agent-loop-timeline-group-work${liveGroup ? " agent-loop-open-work-group" : ""}`} data-group-kind="work" data-group-open={liveGroup} data-group-expanded={expanded} aria-label={title}>
      <button type="button" className="agent-loop-timeline-group-title" data-group-kind="work" aria-expanded={expanded} aria-controls={detailId} onClick={() => {
        userToggled.current = true;
        setExpanded((value) => !value);
      }}>
        <span className="agent-loop-timeline-group-icon" aria-hidden="true">{liveGroup ? latestWorkGlyph(latest) : groupGlyph}</span>
        <span className={liveGroup ? "agent-loop-timeline-group-live-title" : "agent-loop-timeline-group-label"} title={title}>{title}</span>
        <span className="agent-loop-timeline-group-chevron" aria-hidden="true">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {expanded && (
        <div id={detailId} className="agent-loop-timeline-group-items">
          {hiddenCount > 0 && <button type="button" className="agent-loop-timeline-group-title" onClick={() => setVisibleCount((count) => count + 40)}>显示更早的操作（{hiddenCount}{hiddenFailures > 0 ? `，含 ${hiddenFailures} 项失败` : ""}）</button>}
          {keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }))}
        </div>
      )}
    </section>
  );
}

function CollapsibleThinkingCell({ cell, renderCell }: { cell: Extract<AgentLoopProcessCell, { kind: "thinking" }>; renderCell: RenderAgentCell }) {
  const [expansionPreference, setExpanded] = useState(false);
  const expanded = useTranscriptSearch() || expansionPreference;
  return (
    <section className="agent-loop-thinking-disclosure" data-thinking-expanded={expanded}>
      <button
        type="button"
        className="agent-loop-timeline-group-title agent-loop-thinking-toggle"
        aria-label="Thinking"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span>Thinking</span>
        <span className="agent-loop-timeline-group-chevron" aria-hidden="true">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {expanded && (
        <div className="agent-loop-timeline-group-items">
          {renderCell({ cell, key: cell.id, className: "chat-turn-process-cell agent-loop-process-cell" })}
        </div>
      )}
    </section>
  );
}

export const AgentTimeline = memo(function AgentTimeline({ cells, renderCell, showAllOpenWork = false, expandWorkGroups = false, isRunning = false, loadedToolItems }: { cells: AgentLoopProcessCell[]; renderCell: RenderAgentCell; showAllOpenWork?: boolean; expandWorkGroups?: boolean; isRunning?: boolean; loadedToolItems?: number }) {
  const searching = useTranscriptSearch();
  const groups = useMemo(() => groupTimelineCells(cells), [cells]);
  const [visibleGroupCount, setVisibleGroupCount] = useState(40);
  const previouslyLoaded = useRef(loadedToolItems);
  useEffect(() => {
    if (loadedToolItems !== undefined && previouslyLoaded.current !== undefined && loadedToolItems > previouslyLoaded.current) {
      const added = loadedToolItems - previouslyLoaded.current;
      setVisibleGroupCount((count) => count + added);
    }
    previouslyLoaded.current = loadedToolItems;
  }, [loadedToolItems]);
  const attention = useMemo(() => visibleAttention(cells), [cells]);
  const visibleGroups = groups.filter((group, index) => searching || index >= groups.length - visibleGroupCount || group.cells.some((cell) => attention.has(cell)));
  const hiddenGroupCount = groups.length - visibleGroups.length;
  const hiddenFailures = groups.filter((group, index) => index < groups.length - visibleGroupCount && !group.cells.some((cell) => attention.has(cell)))
    .reduce((sum, group) => sum + group.cells.filter(needsAttention).length, 0);
  return (
    <div className="chat-turn-process-stack agent-loop-timeline">
      {hiddenGroupCount > 0 && <button type="button" className="agent-loop-timeline-group-title" onClick={() => setVisibleGroupCount((count) => count + 40)}>显示更早的处理过程（{hiddenGroupCount}{hiddenFailures > 0 ? `，含 ${hiddenFailures} 项失败` : ""}）</button>}
      {visibleGroups.map((group, groupIndex) => {
        const keyed = withStableRenderKeys(group.cells);
        if (group.kind === "thinking") {
          return group.cells
            .filter((cell) => (
              cell.kind === "thinking"
              && (cell.isStreaming || isProviderReasoningSummary(cell) || cell.collapsible)
            ))
            .map((cell) => cell.kind === "thinking" && cell.collapsible
              ? <CollapsibleThinkingCell key={cell.id} cell={cell} renderCell={renderCell} />
              : renderCell({ key: cell.id, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
        }
        if (group.kind === "work") {
          if (group.cells.length > 1) {
            return <WorkGroup key={`timeline-group-work-${group.segment === undefined ? `unscoped-${groupIndex}` : `segment-${group.segment}`}`} group={group} renderCell={renderCell} isRunning={isRunning} expandWorkGroups={expandWorkGroups || showAllOpenWork} />;
          }
          return keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
        }
        if (group.kind === "narration" || (group.kind !== "context" && group.cells.length === 1)) {
          return keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
        }
        return (
          <section key={`timeline-group-${group.kind}-${groupIndex}`} className={`agent-loop-timeline-group agent-loop-timeline-group-${group.kind}`} data-group-kind={group.kind} aria-label={timelineGroupTitle(group)}>
            <div className="agent-loop-timeline-group-title">{timelineGroupTitle(group)}</div>
            <div className="agent-loop-timeline-group-items">
              {keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }))}
            </div>
          </section>
        );
      })}
    </div>
  );
});
