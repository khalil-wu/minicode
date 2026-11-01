import { memo, useCallback, useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react";
import type React from "react";
import { ChevronDown, ChevronRight, PencilLine, TerminalSquare } from "../../lib/icons";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import type { RenderAgentCell } from "./AgentTurn";
import { withStableRenderKeys } from "./renderKeys";
import { guardianActionSummary, GuardianReviewGlyph } from "../../chat/cells/GuardianReviewCell";
import { ToolGlyph } from "../../chat/toolUtils";
import {
  isWebFetchActivity,
  commandActionKind,
  commandActionTarget,
  fileLabel,
  readableTimelineTitle,
  recordInputTarget,
} from "../../chat/cells/activityCellHelpers";
import { isProviderReasoningSummary } from "../../lib/provider-reasoning";
import { useTranscriptSearch } from "../../chat/TranscriptSearchContext";
import { useTranscriptReadingPreference } from "../../chat/transcriptReadingState";
import { useProcessDisclosure } from "./useProcessDisclosure";
import { useActivitySummary } from "./useActivitySummary";
import { useI18n, type Translate, type TranslationKey, type Locale } from "../../i18n";

type TimelineGroupKind = "work" | "thinking" | "narration" | "context" | "notice";
type TimelineGroup = {
  key: string;
  kind: TimelineGroupKind;
  cells: AgentLoopProcessCell[];
  segment?: number;
};

export const isProcessNarration = (cell: AgentLoopProcessCell): boolean => cell.kind === "thinking"
  && !cell.collapsible && !["provider", "reasoning"].includes(cell.source);

const timelineGroupKind = (cell: AgentLoopProcessCell): TimelineGroupKind => {
  if (cell.kind === "thinking") {
    return isProcessNarration(cell) ? "narration" : "thinking";
  }
  if (cell.kind === "guardian_review" && cell.review.status !== "in_progress" || cell.kind === "guardian_warning") return "notice";
  if (cell.kind === "context_compaction") return "context";
  if (cell.kind === "status_notice" && /压缩|compac/i.test(`${cell.title} ${cell.message || ""}`)) return "context";
  if (cell.kind === "status_notice") return "notice";
  return "work";
};

const cellSegment = (cell: AgentLoopProcessCell): number | undefined => {
  if (cell.kind === "activity" || cell.kind === "exec" || cell.kind === "thinking" || cell.kind === "collaboration" || cell.kind === "guardian_review") return cell.segment;
  return undefined;
};

const groupTimelineCells = (cells: AgentLoopProcessCell[]): TimelineGroup[] => {
  const groups: TimelineGroup[] = [];
  const groupCounts = new Map<string, number>();
  for (const cell of cells) {
    const kind = timelineGroupKind(cell);
    const segment = cellSegment(cell);
    const previous = groups.at(-1);
    const joinsPrevious = previous?.kind === kind && (
      kind !== "work"
      || (segment !== undefined && previous.segment === segment
        && cell.kind !== "collaboration" && (cell.kind === "guardian_review" || workLabel(cell) !== undefined)
        && previous.cells[0].kind !== "collaboration" && (previous.cells[0].kind === "guardian_review" || workLabel(previous.cells[0]) !== undefined))
    );
    if (joinsPrevious) {
      previous.cells.push(cell);
    } else {
      const scope = `${kind}:${segment ?? cell.id}`;
      const ordinal = groupCounts.get(scope) ?? 0;
      groupCounts.set(scope, ordinal + 1);
      groups.push({ key: `${scope}:${ordinal}`, kind, cells: [cell], segment });
    }
  }
  return groups;
};

type WorkLabel = "Edit" | "Run" | "Read" | "List" | "Search" | "WebSearch" | "Fetch" | "Browse";
const reviewingAction = (cell: AgentLoopProcessCell) => cell.kind === "activity" || cell.kind === "exec"
  ? cell.automaticApprovalReviews?.find(review => review.status === "in_progress") : undefined;
const WORK_COPY = {
  Edit: ["编辑文件", "编辑了文件", "编辑了文件"], Run: ["运行命令", "运行了命令", "运行了命令"],
  Read: ["读取文件", "已读取文件", "读取文件"], List: ["读取文件", "已读取文件", "读取文件"],
  Search: ["读取文件", "已读取文件", "读取文件"], WebSearch: ["搜索网页", "已搜索网页", "搜索了网页"],
  Fetch: ["读取网页", "已读取网页", "读取了网页"], Browse: ["浏览网页", "已浏览网页", "浏览了网页"],
} as const satisfies Record<WorkLabel, readonly [TranslationKey, TranslationKey, TranslationKey]>;

const workLabel = (cell: AgentLoopProcessCell): WorkLabel | undefined => {
  if (cell.kind === "exec") {
    const kind = commandActionKind(cell.commandActions);
    return kind === "fileRead" ? "Read" : kind === "workspaceSearch" ? "Search" : kind === "workspaceList" ? "List" : "Run";
  }
  if (cell.kind === "activity" && cell.activityKind === "commandExecution") return "Run";
  if (cell.kind === "diff" || (cell.kind === "activity" && cell.activityKind === "fileChange")) return "Edit";
  if (cell.kind === "activity" && cell.activityKind === "workspaceList") return "List";
  if (cell.kind === "activity" && cell.activityKind === "workspaceSearch") return "Search";
  if (cell.kind === "activity" && cell.activityKind === "fileRead") return "Read";
  if (cell.kind === "activity" && isWebFetchActivity(cell)) return "Fetch";
  if (cell.kind === "activity" && cell.activityKind === "webSearch") {
    const names = (cell.toolCallRecords ?? []).map((record) => String(record.name || "").toLowerCase());
    if (names.length > 0 && names.every((name) => /web_search|websearch/.test(name))) return "WebSearch";
  }
  if (cell.kind === "activity" && cell.activityKind === "browser") return "Browse";
  return undefined;
};

const timelineGroupTitle = (group: TimelineGroup, t: Translate, locale: Locale): string => {
  const labels: WorkLabel[] = [];
  for (const cell of group.cells) {
    const label = workLabel(cell);
    const summaryLabel = label === "List" || label === "Search" ? "Read" : label;
    if (summaryLabel !== undefined && !labels.includes(summaryLabel)) labels.push(summaryLabel);
  }
  const summaryOrder: WorkLabel[] = ["Edit", "Read", "Run", "WebSearch", "Fetch", "Browse"];
  labels.sort((left, right) => summaryOrder.indexOf(left) - summaryOrder.indexOf(right));
  const captions = labels.map((label, index) => {
    const [action, first, following] = WORK_COPY[label];
    const operations = group.cells.filter((cell) => {
      const kind = workLabel(cell);
      return kind === label || label === "Read" && (kind === "List" || kind === "Search");
    });
    const statuses = operations.map((cell) => "status" in cell ? cell.status : "done");
    const review = operations.map(reviewingAction).find(Boolean);
    if (review) return guardianActionSummary(review, t);
    const patchFiles = operations.flatMap(cell => cell.kind === "activity" ? cell.guardianPatchFiles ?? [] : []);
    if (patchFiles.length && statuses.some(status => ["pending", "pending_approval"].includes(status))) return patchFiles.map(fileLabel).join(", ");
    const actionLabel = locale === "en-US" ? t(action).toLowerCase() : t(action);
    if (statuses.includes("pending_approval")) return t("等待批准{action}", { action: actionLabel });
    if (statuses.includes("pending")) return t("准备{action}", { action: actionLabel });
    let completed: TranslationKey = index === 0 ? first : following;
    if (label === "Edit") {
      const files = new Set(operations.flatMap(cell => cell.kind === "diff" ? cell.files.map(file => file.path)
        : cell.kind === "activity" ? (cell.toolCallRecords ?? []).flatMap(record =>
          record.diff?.files?.length ? record.diff.files.map(file => file.path) : [recordInputTarget(record)]).filter(Boolean)
        : []));
      if (files.size > 0) completed = files.size === 1 ? "编辑了一个文件" : index === 0 ? "编辑了文件" : "编辑了多个文件";
    } else if (label === "Run") {
      const commands = new Set(operations.flatMap(cell => cell.kind === "exec" ? [cell.id]
        : cell.kind === "activity" ? (cell.toolCallRecords ?? []).map(record => record.id) : []));
      if (commands.size === 1) completed = "运行了一条命令";
    }
    const caption = t(completed);
    return locale === "en-US" && index > 0 ? caption.toLowerCase() : caption;
  });
  const statusBoundary = group.cells.some((cell) => "status" in cell
    && ["pending", "pending_approval"].includes(cell.status));
  if (!captions.length && group.cells[0].kind === "guardian_review") return guardianActionSummary(group.cells[0].review, t);
  // The unit list format keeps Chinese free of separators while English uses
  // the locale's comma-separated list.
  return statusBoundary ? captions.join(" · ") : new Intl.ListFormat(locale, { type: "unit" }).format(captions);
};

const workGlyph = (cell: AgentLoopProcessCell | undefined): React.ReactNode => {
  if (cell?.kind === "guardian_review") return <GuardianReviewGlyph />;
  if (!cell) return <TerminalSquare size={15} />;
  if (reviewingAction(cell)) return <GuardianReviewGlyph />;
  if (cell.kind === "exec") return <ToolGlyph kind={commandActionKind(cell.commandActions)} size={cell.commandPresentation ? 16 : 15} />;
  if (cell.kind === "activity") return <ToolGlyph kind={cell.activityKind} size={15} />;
  if (cell.kind === "diff") return <PencilLine size={15} />;
  return <TerminalSquare size={15} />;
};

const liveWorkTitle = (cell: AgentLoopProcessCell, t: Translate): string => {
  if (cell.kind === "guardian_review") return guardianActionSummary(cell.review, t);
  const review = reviewingAction(cell);
  if (review) return guardianActionSummary(review, t);
  if (cell.kind === "exec") {
    const kind = commandActionKind(cell.commandActions);
    const action = t(kind === "fileRead" ? "正在读取" : kind === "workspaceSearch" ? "正在搜索" : kind === "workspaceList" ? "正在查看目录" : "正在运行");
    const target = kind === "commandExecution" ? cell.command : commandActionTarget(cell.commandActions!);
    return [action, target].filter(Boolean).join(" ");
  }
  if (cell.kind === "activity") {
    const record = cell.toolCallRecords?.at(-1);
    const target = record ? recordInputTarget(record) : cell.subtitle;
    return [readableTimelineTitle(cell, t), target && cell.activityKind === "fileRead" ? fileLabel(target) : target].filter(Boolean).join(" ");
  }
  return t(cell.kind === "diff" ? "正在编辑文件" : "正在思考");
};

const WorkGroup = memo(function WorkGroup({ group, renderCell, expandWorkGroups, isRunning, onHistoryStartReached }: { group: TimelineGroup; renderCell: RenderAgentCell; expandWorkGroups: boolean; isRunning: boolean; onHistoryStartReached?: () => void }) {
  const { t, locale } = useI18n();
  const searching = useTranscriptSearch();
  const defaultExpanded = expandWorkGroups;
  const [expansionPreference, setExpanded, userToggled] = useTranscriptReadingPreference(`work:${group.key}`, defaultExpanded);
  const expanded = searching || expansionPreference;
  const disclosure = useProcessDisclosure(expanded);
  const detailId = useId();
  const scrollList = useRef<HTMLDivElement>(null);
  const scrollPosition = useRef({ firstId: group.cells[0].id, height: 0, top: 0 });
  const [overflowBottom, setOverflowBottom] = useState(false);
  const measureOverflow = useCallback(() => {
    const list = scrollList.current!;
    setOverflowBottom(list.scrollTop + list.clientHeight < list.scrollHeight);
    scrollPosition.current.height = list.scrollHeight;
    scrollPosition.current.top = list.scrollTop;
  }, []);
  useLayoutEffect(() => {
    if (!expanded || !disclosure.mounted) return;
    const list = scrollList.current!;
    const previous = scrollPosition.current;
    if (previous.firstId !== group.cells[0].id && group.cells.findIndex((cell) => cell.id === previous.firstId) > 0) {
      list.scrollTop = previous.top + list.scrollHeight - previous.height;
    }
    scrollPosition.current.firstId = group.cells[0].id;
    measureOverflow();
    const observer = new ResizeObserver(measureOverflow);
    observer.observe(list);
    for (const row of list.children) observer.observe(row);
    return () => observer.disconnect();
  }, [expanded, disclosure.mounted, group.cells, measureOverflow]);
  useEffect(() => {
    if (!userToggled.current) setExpanded(defaultExpanded);
  }, [defaultExpanded]);
  const latestRunning = group.cells.filter((cell) => reviewingAction(cell) || "status" in cell && cell.status === "running").at(-1);
  const liveGroup = isRunning && Boolean(latestRunning);
  const currentTitle = liveGroup ? liveWorkTitle(latestRunning!, t) : timelineGroupTitle(group, t, locale);
  const summary = useActivitySummary(currentTitle, liveGroup ? latestRunning!.id : "summary", liveGroup);
  const title = summary.title;
  const summaryIconCell = group.cells.find((cell) => workLabel(cell) === "Edit")
    ?? group.cells.find((cell) => ["Read", "List", "Search"].includes(workLabel(cell) || ""))
    ?? group.cells[0];
  const keyed = useMemo(() => disclosure.mounted ? withStableRenderKeys(group.cells) : [], [disclosure.mounted, group.cells]);
  return (
    <section className={`agent-loop-timeline-group agent-loop-timeline-group-work${liveGroup ? " agent-loop-open-work-group" : ""}`} data-group-kind="work" data-group-open={liveGroup} data-group-expanded={expanded} data-processing={liveGroup} aria-label={title}>
      <button type="button" className="agent-loop-timeline-group-title" data-group-kind="work" aria-expanded={expanded} aria-controls={detailId} onClick={() => {
        userToggled.current = true;
        setExpanded((value) => !value);
      }}>
        <span className="agent-loop-timeline-group-icon" aria-hidden="true">{workGlyph(liveGroup && summary.key !== "summary" ? group.cells.find((cell) => cell.id === summary.key) : summaryIconCell)}</span>
        <span className={liveGroup ? "agent-loop-timeline-group-live-title" : "agent-loop-timeline-group-label"} title={title}>{title}</span>
        <span className="agent-loop-timeline-group-chevron" aria-hidden="true">
          <ChevronRight size={14} className="mc-process-disclosure" data-expanded={expanded} />
        </span>
      </button>
      {disclosure.mounted && (
        <div ref={disclosure.element} className="agent-loop-timeline-group-disclosure" aria-hidden={!expanded} style={{ pointerEvents: expanded ? "auto" : "none" }}>
        <div ref={scrollList} id={detailId} className="agent-loop-timeline-group-items" data-overflow-bottom={overflowBottom}
          onScroll={() => { measureOverflow(); if (scrollList.current!.scrollTop === 0) onHistoryStartReached?.(); }}
          onWheel={(event) => { if (event.deltaY < 0 && scrollList.current!.scrollTop === 0) onHistoryStartReached?.(); }}>
          {keyed.map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }))}
        </div>
        </div>
      )}
    </section>
  );
}, (previous, next) => previous.renderCell === next.renderCell && previous.expandWorkGroups === next.expandWorkGroups
  && previous.isRunning === next.isRunning && previous.onHistoryStartReached === next.onHistoryStartReached
  && previous.group.key === next.group.key && previous.group.kind === next.group.kind && previous.group.segment === next.group.segment
  && previous.group.cells.length === next.group.cells.length && previous.group.cells.every((cell, index) => cell === next.group.cells[index]));

function CollapsibleThinkingCell({ cell, renderCell }: { cell: Extract<AgentLoopProcessCell, { kind: "thinking" }>; renderCell: RenderAgentCell }) {
  const [expansionPreference, setExpanded, userToggled] = useTranscriptReadingPreference(`thinking:${cell.id}`, false);
  const expanded = useTranscriptSearch() || expansionPreference;
  const caption = cell.content.trim().split(/\r?\n/)[0];
  return (
    <section className="agent-loop-thinking-disclosure" data-thinking-expanded={expanded}>
      <button
        type="button"
        className="agent-loop-timeline-group-title agent-loop-thinking-toggle"
        aria-label={caption}
        aria-expanded={expanded}
        onClick={() => { userToggled.current = true; setExpanded((value) => !value); }}
      >
        <span>{caption}</span>
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

export const AgentTimeline = memo(function AgentTimeline({ cells, renderCell, showAllOpenWork = false, expandWorkGroups = false, showCompletedReasoning = false, isRunning = false, onHistoryStartReached }: { cells: AgentLoopProcessCell[]; renderCell: RenderAgentCell; showAllOpenWork?: boolean; expandWorkGroups?: boolean; showCompletedReasoning?: boolean; isRunning?: boolean; onHistoryStartReached?: () => void }) {
  const groups = useMemo(() => groupTimelineCells(cells), [cells]);
  const firstWorkGroup = groups.find((group) => group.kind === "work");
  // Completed reasoning summaries stay in the expanded transcript; the live
  // status row carries the heading while a turn is running. Rendering a
  // settled provider heading in the ordinary process stack would create a
  // second projection.
  const showSettledReasoning = isRunning || showAllOpenWork || expandWorkGroups || showCompletedReasoning;
  return (
    <div className="chat-turn-process-stack agent-loop-timeline" onWheelCapture={(event) => {
      if (event.deltaY < 0 && !(event.target as Element).closest(".agent-loop-timeline-group-items")) onHistoryStartReached?.();
    }}>
      {groups.map((group) => {
        if (group.kind === "thinking") {
          return group.cells
            .filter((cell) => (
              cell.kind === "thinking"
              && (cell.isStreaming || (isProviderReasoningSummary(cell) && showSettledReasoning) || cell.collapsible)
            ))
            .map((cell) => cell.kind === "thinking" && cell.collapsible
              ? <CollapsibleThinkingCell key={cell.id} cell={cell} renderCell={renderCell} />
              : renderCell({ key: cell.id, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
        }
        if (group.kind === "work") {
          const activeSingleItem = group.cells.length === 1 && isRunning
            && "status" in group.cells[0] && group.cells[0].status === "running";
          if ((group.cells.length > 1 || activeSingleItem) && group.cells.some((cell) => cell.kind === "guardian_review" || workLabel(cell) !== undefined)) {
            const activeGroup = isRunning && group === groups.at(-1)
              && group.cells.some((cell) => (cell.kind === "exec" || cell.kind === "activity" || cell.kind === "guardian_review") && cell.segmentClosed !== true);
            return <WorkGroup key={group.key} group={group} renderCell={renderCell} isRunning={activeGroup} expandWorkGroups={expandWorkGroups || showAllOpenWork} onHistoryStartReached={group === firstWorkGroup ? onHistoryStartReached : undefined} />;
          }
          return withStableRenderKeys(group.cells).map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
        }
        return withStableRenderKeys(group.cells).map(({ cell, key }) => renderCell({ key, cell, className: "chat-turn-process-cell agent-loop-process-cell" }));
      })}
    </div>
  );
});
