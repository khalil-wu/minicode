import { useEffect, useRef, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, PencilLine, TerminalSquare } from "lucide-react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import type { RenderAgentCell } from "./AgentTurn";
import { withStableRenderKeys } from "./renderKeys";
import { ToolGlyph } from "../../chat/toolUtils";
import {
  isWebFetchActivity,
  readableTimelineTitle,
  recordInputTarget,
  shortCommand,
} from "../../chat/cells/activityCellHelpers";
import { isBrowserScreenshotRecord } from "../../lib/artifact-projection";
import { isProviderReasoningSummary } from "../../lib/provider-reasoning";

type TimelineGroupKind = "work" | "thinking" | "narration" | "context" | "notice";
type TimelineGroup = {
  kind: TimelineGroupKind;
  cells: AgentLoopProcessCell[];
  segment?: number;
  closed: boolean;
};

const timelineGroupKind = (cell: AgentLoopProcessCell): TimelineGroupKind => {
  if (cell.kind === "thinking") {
    return ["provider", "reasoning"].includes(cell.source) ? "thinking" : "narration";
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

type WorkLabel = "编辑了文件" | "运行了命令" | "读取了文件" | "列出了文件" | "搜索了内容" | "获取网页" | "搜索网页" | "操作浏览器" | "协作任务" | "处理步骤";

const workLabel = (cell: AgentLoopProcessCell): WorkLabel => {
  if (cell.kind === "exec") return "运行了命令";
  if (cell.kind === "diff" || (cell.kind === "activity" && cell.activityKind === "fileChange")) return "编辑了文件";
  if (cell.kind === "activity" && cell.activityKind === "workspaceList") return "列出了文件";
  if (cell.kind === "activity" && cell.activityKind === "workspaceSearch") return "搜索了内容";
  if (cell.kind === "activity" && cell.activityKind === "fileRead") return "读取了文件";
  if (cell.kind === "activity" && isWebFetchActivity(cell)) return "获取网页";
  if (cell.kind === "activity" && cell.activityKind === "webSearch") {
    const names = (cell.toolCallRecords ?? []).map((record) => String(record.name || "").toLowerCase());
    if (names.length > 0 && names.every((name) => /web_search|websearch/.test(name))) return "搜索网页";
  }
  if (cell.kind === "activity" && cell.activityKind === "browser") return "操作浏览器";
  if (cell.kind === "collaboration") return "协作任务";
  return "处理步骤";
};

const timelineGroupTitle = (group: TimelineGroup): string => {
  if (group.kind === "context") return "上下文已自动压缩";
  if (group.kind === "notice") return "状态";
  const labels: WorkLabel[] = [];
  for (const cell of group.cells) {
    const label = workLabel(cell);
    if (!labels.includes(label)) labels.push(label);
  }
  if (labels.length === 1 && labels[0] === "运行了命令" && group.cells.length > 1) {
    return `运行了 ${group.cells.length} 条命令`;
  }
  return labels.join("并");
};

const latestWorkTitle = (cell: AgentLoopProcessCell | undefined): string => {
  if (!cell) return "处理步骤";
  if (cell.kind === "exec") {
    const command = shortCommand(cell.command);
    return command ? `运行命令 ${command}` : "运行命令";
  }
  if (cell.kind === "activity") {
    const title = readableTimelineTitle(cell);
    const target = cell.toolCallRecords?.[0] ? recordInputTarget(cell.toolCallRecords[0]) : "";
    return [title, target].filter(Boolean).join(" ") || title;
  }
  if (cell.kind === "collaboration") return "协作任务";
  if (cell.kind === "error") return cell.title || "处理失败";
  return workLabel(cell);
};

const latestWorkGlyph = (cell: AgentLoopProcessCell | undefined): React.ReactNode => {
  if (!cell) return <TerminalSquare size={15} />;
  if (cell.kind === "exec") return <TerminalSquare size={15} />;
  if (cell.kind === "activity") return <ToolGlyph kind={cell.activityKind} size={15} />;
  if (cell.kind === "diff") return <PencilLine size={15} />;
  return <TerminalSquare size={15} />;
};

function ClosedWorkGroup({ group, groupIndex, renderCell }: { group: TimelineGroup; groupIndex: number; renderCell: RenderAgentCell }) {
  const containsScreenshot = group.cells.some((cell) => (
    cell.kind === "activity"
    && cell.toolCallRecords?.some((record) => Boolean(record.artifactId) && isBrowserScreenshotRecord(record))
  ));
  const [expanded, setExpanded] = useState(containsScreenshot);
  const previousGroupKey = useRef(`${group.segment ?? "none"}:${group.cells[0]?.id ?? groupIndex}`);
  const groupKey = `${group.segment ?? "none"}:${group.cells[0]?.id ?? groupIndex}`;
  useEffect(() => {
    if (previousGroupKey.current !== groupKey) setExpanded(containsScreenshot);
    previousGroupKey.current = groupKey;
  }, [containsScreenshot, groupKey]);
  const title = timelineGroupTitle(group);
  const labels = group.cells.map(workLabel);
  const groupGlyph = labels.includes("编辑了文件")
    ? <PencilLine size={15} />
    : labels.includes("运行了命令")
      ? <TerminalSquare size={15} />
      : (() => {
          const firstActivity = group.cells.find((cell) => cell.kind === "activity");
          return firstActivity?.kind === "activity"
            ? <ToolGlyph kind={firstActivity.activityKind} size={15} />
            : <TerminalSquare size={15} />;
        })();
  const keyed = withStableRenderKeys(group.cells);
  return (
    <section className="agent-loop-timeline-group agent-loop-timeline-group-work" data-group-kind="work" data-group-expanded={expanded} aria-label={title}>
      <button type="button" className="agent-loop-timeline-group-title" data-group-kind="work" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>
        <span className="agent-loop-timeline-group-icon" aria-hidden="true">{groupGlyph}</span>
        <span>{title}</span>
        <span className="agent-loop-timeline-group-chevron" aria-hidden="true">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {expanded && (
        <div className="agent-loop-timeline-group-items">
          {keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }))}
        </div>
      )}
    </section>
  );
}

function OpenWorkGroup({ group, renderCell }: { group: TimelineGroup; renderCell: RenderAgentCell }) {
  const [expanded, setExpanded] = useState(true);
  const latest = group.cells.at(-1);
  const title = latestWorkTitle(latest);
  const keyed = withStableRenderKeys(group.cells);
  const latestId = latest?.id;
  return (
    <section
      className="agent-loop-open-work-group agent-loop-timeline-group agent-loop-timeline-group-work"
      data-group-kind="work"
      data-group-open="true"
      data-group-expanded={expanded}
      data-group-latest-id={latest?.id}
      aria-label={title}
    >
      <button
        type="button"
        className="agent-loop-timeline-group-title agent-loop-timeline-group-title-live"
        data-group-kind="work"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="agent-loop-timeline-group-icon" aria-hidden="true">{latestWorkGlyph(latest)}</span>
        <span className="agent-loop-timeline-group-live-title">{title}</span>
        <span className="agent-loop-timeline-group-chevron" aria-hidden="true">
          {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </span>
      </button>
      {expanded && (
        <div className="agent-loop-timeline-group-items">
          {keyed.map(({ cell, key }) => renderCell({
            key,
            cell,
            isActive: cell.id === latestId,
            className: "chat-turn-process-cell agent-loop-process-cell",
          }))}
        </div>
      )}
    </section>
  );
}

export function AgentTimeline({ cells, renderCell, showAllOpenWork = false }: { cells: AgentLoopProcessCell[]; renderCell: RenderAgentCell; showAllOpenWork?: boolean }) {
  const groups = groupTimelineCells(cells);
  return (
    <div className="chat-turn-process-stack agent-loop-timeline">
      {groups.map((group, groupIndex) => {
        const keyed = withStableRenderKeys(group.cells);
        if (group.kind === "thinking") {
          return group.cells
            .filter((cell) => (
              cell.kind === "thinking"
              && (cell.isStreaming || isProviderReasoningSummary(cell))
            ))
            .map((cell) => renderCell({ key: cell.id, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
        }
        if (group.kind === "work" && group.closed) {
          if (group.cells.length === 1) {
            return keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
          }
          return <ClosedWorkGroup key={`timeline-group-work-${group.segment ?? groupIndex}-${groupIndex}`} group={group} groupIndex={groupIndex} renderCell={renderCell} />;
        }
        if (group.kind === "work") {
          if (!showAllOpenWork && group.cells.length > 1) {
            return <OpenWorkGroup key={`timeline-group-open-work-${group.segment ?? groupIndex}-${groupIndex}`} group={group} renderCell={renderCell} />;
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
}
