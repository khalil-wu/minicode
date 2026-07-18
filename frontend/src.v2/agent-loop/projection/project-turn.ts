import type {
  ActivityCellState,
  AssistantMarkdownCellState,
  ChatTurnState,
  DiffCellState,
  ErrorCellState,
  ExecCellState,
  HistoryCellState,
  StatusNoticeCellState,
  StreamingAssistantNarrationCellState,
  StreamingAssistantTailCellState,
  ThinkingCellState,
  TurnSummaryCellState,
} from "../../chat/cells/cellTypes";
import { isGenericProcessPlaceholder } from "../../lib/turn-projection";
import { smoothedLiveNarrationMarkdown } from "../components/liveNarrationSmoothing";
import {
  getToolDiffStats,
  isAgentControlToolName,
  isBrowserToolRecord,
  isCommandToolRecord,
  isFileChangeToolRecord,
  isFileReadToolRecord,
  isWebFetchToolRecord,
  isWebSearchToolRecord,
  isWorkspaceSearchToolRecord,
  type ToolCallRecord,
} from "../../lib/tool-call-reducer";
import type {
  ActivityDetail,
  ActivityGroupItem,
  AgentTimelineItem,
  AgentTurnStatus,
  BrowserPreviewItem,
  LiveNarrationItem,
  ProcessItem,
  SystemStatusItem,
} from "../types";

export type AgentLoopProcessCell = ChatTurnState["committedCells"][number];
export type AgentLoopAnswerCell = AssistantMarkdownCellState;

export interface AgentLoopSummaryItem {
  kind: "command" | "diff" | "test" | "browser" | "source";
  label: string;
}

export interface AgentLoopTurnProjection {
  id: string;
  status: AgentTurnStatus;
  userCell: ChatTurnState["userCell"];
  timelineItems: AgentTimelineItem[];
  processCells: AgentLoopProcessCell[];
  artifactCells: DiffCellState[];
  answerCell: AgentLoopAnswerCell | null;
  activeAnswerCell: StreamingAssistantTailCellState | null;
  answerIsStreaming: boolean;
  hasProcessContent: boolean;
  shouldCollapseProcess: boolean;
  initialProcessExpanded: boolean;
  durationLabel: string;
  summaryItems: AgentLoopSummaryItem[];
}

export function projectChatTurnToAgentLoop(
  turn: ChatTurnState,
  committedCells: ChatTurnState["committedCells"] = turn.committedCells,
): AgentLoopTurnProjection {
  const activeAnswerCell =
    turn.activeCell?.kind === "streaming_assistant_tail" ? turn.activeCell : null;
  const activeNarrationCell =
    turn.activeCell?.kind === "streaming_assistant_narration" ? turn.activeCell : null;
  const activeAnswerMarkdownCell = activeAnswerCell
    ? activeTailToAssistantMarkdownCell(activeAnswerCell, `${turn.id}-final`)
    : null;
  const answerCell = turn.finalAnswerCell?.markdownSource.trim()
    ? turn.finalAnswerCell
    : activeAnswerMarkdownCell;
  const answerIsStreaming =
    Boolean(activeAnswerCell) ||
    Boolean(turn.finalAnswerCell?.isStreaming);
  const hideInlineSummary = Boolean(answerCell) || turn.status === "streaming";
  const artifactCells =
    turn.status !== "streaming" && Boolean(turn.finalAnswerCell)
      ? buildArtifactDiffCells(committedCells)
      : [];
  const answerText = answerCellText(answerCell);
  const processCells = committedCells.filter((cell) => (
    !(hideInlineSummary && isTurnSummaryCell(cell)) &&
    !isSilentProcessCell(cell) &&
    !duplicatesAnswerCell(cell, answerText)
  ));
  const timelineItems = buildAgentTimelineItems(
    activeNarrationCell ? [...processCells, activeNarrationCell] : processCells,
  );
  const hasProcessContent =
    timelineItems.length > 0 ||
    (turn.status === "streaming" && !answerCell);
  const shouldCollapseProcess = false;

  return {
    id: turn.id,
    status: mapStatus(turn.status),
    userCell: turn.userCell,
    timelineItems,
    processCells,
    artifactCells,
    answerCell,
    activeAnswerCell,
    answerIsStreaming,
    hasProcessContent,
    shouldCollapseProcess,
    initialProcessExpanded: !shouldCollapseProcess,
    durationLabel: formatTurnDuration(turn),
    summaryItems: buildAgentLoopSummaryItems(committedCells),
  };
}

function isSilentProcessCell(cell: ChatTurnState["committedCells"][number]): boolean {
  return (
    cell.kind === "activity" &&
    (
      cell.activityKind === "skill" ||
      (ACTION_CHAIN_ACTIVITY_KINDS.has(cell.activityKind) && (cell.toolCallRecords?.length ?? 0) === 0)
    )
  );
}

export function buildAgentTimelineItems(cells: AgentLoopProcessCell[]): AgentTimelineItem[] {
  const items: AgentTimelineItem[] = [];
  let seq = 0;

  for (let index = 0; index < cells.length; index += 1) {
    const cell = cells[index];
    if (cell.kind === "activity" && isAgentControlActivity(cell)) {
      const controlCells = [cell];
      while (true) {
        const nextCell = cells[index + 1];
        if (!nextCell || nextCell.kind !== "activity" || !isAgentControlActivity(nextCell)) break;
        controlCells.push(nextCell);
        index += 1;
      }
      const controlItem = agentControlProcessItem(cell.id, controlCells, seq);
      if (controlItem) {
        items.push(controlItem);
        seq += 1;
      }
      continue;
    }
    const item = projectProcessCell(cell, seq);
    if (!item) continue;
    const projectedItems = Array.isArray(item) ? item : [item];
    items.push(...projectedItems);
    seq += projectedItems.length;
  }

  return items;
}

function buildArtifactDiffCells(cells: ChatTurnState["committedCells"]): DiffCellState[] {
  const diffCells = cells.filter((cell): cell is DiffCellState => cell.kind === "diff");
  if (diffCells.length === 0) return [];
  if (diffCells.length === 1) return diffCells;

  const filesByPath = new Map<string, DiffCellState["files"][number]>();
  for (const diffCell of diffCells) {
    for (const file of diffCell.files) {
      const existing = filesByPath.get(file.path);
      if (!existing) {
        filesByPath.set(file.path, { ...file });
        continue;
      }
      filesByPath.set(file.path, {
        ...existing,
        ...file,
        additions: existing.additions + file.additions,
        deletions: existing.deletions + file.deletions,
        patch: file.patch ?? existing.patch,
        isLarge: Boolean(existing.isLarge || file.isLarge),
        isTruncated: Boolean(existing.isTruncated || file.isTruncated),
        changeType: mergeArtifactChangeType(existing.changeType, file.changeType),
      });
    }
  }

  const files = [...filesByPath.values()];
  return [{
    kind: "diff",
    id: `artifact-${diffCells.map((cell) => cell.id).join("-")}`,
    status: diffCells.some((cell) => cell.status === "created") ? "created" : "updated",
    files,
    summary: {
      added: files.reduce((sum, file) => sum + file.additions, 0),
      deleted: files.reduce((sum, file) => sum + file.deletions, 0),
      modifiedFiles: files.length,
    },
    collapsed: true,
    createdAt: Math.max(...diffCells.map((cell) => cell.createdAt).filter(Number.isFinite)),
  }];
}

function mergeArtifactChangeType(
  previous: DiffCellState["files"][number]["changeType"],
  next: DiffCellState["files"][number]["changeType"],
): DiffCellState["files"][number]["changeType"] {
  if (!previous) return next;
  if (!next || previous === next) return previous;
  if (previous === "created" && next !== "deleted") return "created";
  if (next === "deleted") return "deleted";
  return "updated";
}

const ACTION_CHAIN_ACTIVITY_KINDS = new Set<ActivityCellState["activityKind"]>([
  "webSearch",
  "workspaceSearch",
  "fileRead",
  "commandExecution",
  "fileChange",
  "mcpToolCall",
  "genericTool",
]);

export function formatTurnDuration(turn: ChatTurnState): string {
  const start = turn.startedAt;
  const end = turn.completedAt;
  if (end == null || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) {
    return "";
  }
  return formatDurationMs(end - start);
}

function formatDurationMs(durationMs: number): string {
  if (!Number.isFinite(durationMs) || durationMs <= 0) return "";
  const totalSeconds = Math.max(1, Math.floor(durationMs / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainMinutes = minutes % 60;
  return remainMinutes > 0 ? `${hours}h ${remainMinutes}m` : `${hours}h`;
}

export function buildAgentLoopSummaryItems(cells: ChatTurnState["committedCells"]): AgentLoopSummaryItem[] {
  let commandCount = 0;
  let diffFileCount = 0;
  let completedTestGroups = 0;
  let browserPreviewCount = 0;
  let sourceCount = 0;
  let lastWasCompletedTestCommand = false;

  const visitExec = (cell: ExecCellState) => {
    commandCount += 1;
    const isCompletedTest = cell.status === "success" && isTestCommand(cell.command);
    if (isCompletedTest && !lastWasCompletedTestCommand) completedTestGroups += 1;
    lastWasCompletedTestCommand = isCompletedTest;
  };

  for (const cell of cells) {
    if (cell.kind === "exec") {
      visitExec(cell);
      continue;
    }
    if (cell.kind === "exec_group") {
      for (const execCell of cell.cells) visitExec(execCell);
      continue;
    }
    lastWasCompletedTestCommand = false;

    if (cell.kind === "diff") {
      diffFileCount += cell.summary.modifiedFiles;
      continue;
    }
    if (cell.kind === "activity" && isBrowserPreviewActivity(cell)) {
      browserPreviewCount += 1;
      continue;
    }
    if (cell.kind === "activity") {
      sourceCount += sourceContribution(cell);
      continue;
    }
    if (cell.kind === "activity_group") {
      browserPreviewCount += cell.cells.filter(isBrowserPreviewActivity).length;
      sourceCount += cell.cells.reduce((count, item) => count + sourceContribution(item), 0);
    }
  }

  const items: AgentLoopSummaryItem[] = [];
  if (commandCount > 0) items.push({ kind: "command", label: `已运行 ${commandCount} 条命令` });
  if (diffFileCount > 0) items.push({ kind: "diff", label: `已编辑 ${diffFileCount} 个文件` });
  if (completedTestGroups > 0) items.push({ kind: "test", label: `已完成 ${completedTestGroups} 组测试` });
  if (browserPreviewCount > 0) {
    items.push({ kind: "browser", label: `已打开 ${browserPreviewCount} 次浏览器预览` });
  }
  return items;
}

export function isTestCommand(command: string): boolean {
  return /\b(?:pytest|vitest|jest|npm\s+(?:run\s+)?test|pnpm\s+(?:run\s+)?test|yarn\s+(?:run\s+)?test|tsc\b|typecheck)\b/i.test(command);
}

function mapStatus(status: ChatTurnState["status"]): AgentTurnStatus {
  if (status === "streaming") return "running";
  if (status === "interrupted") return "stopped";
  if (status === "failed") return "failed";
  return "completed";
}

function isTurnSummaryCell(cell: HistoryCellState): cell is TurnSummaryCellState {
  return cell.kind === "turn_summary";
}

function projectProcessCell(cell: AgentLoopProcessCell, seq: number): AgentTimelineItem | AgentTimelineItem[] | null {
  switch (cell.kind) {
    case "thinking":
      return processItemFromThinking(cell, seq);
    case "streaming_assistant_narration":
      return liveNarrationItemFromCell(cell, seq);
    case "activity":
      return projectActivityCell(cell, seq);
    case "activity_group":
      return agentControlProcessItem(cell.id, cell.cells, seq)
        ?? activityGroupItemFromActivities(cell.id, cell.cells, seq);
    case "exec":
      return activityGroupItemFromExecs(cell.id, [cell], seq);
    case "exec_group":
      return activityGroupItemFromExecs(cell.id, cell.cells, seq);
    case "status_notice":
      return systemStatusFromNotice(cell, seq);
    case "turn_summary":
      return systemStatusFromTurnSummary(cell, seq);
    case "error":
      return systemStatusFromError(cell, seq);
    case "plan":
      return { id: cell.id, type: "plan", seq, cell };
    case "diff":
      return fileChangesItemFromDiffCell(cell, seq);
    case "assistant_markdown":
      {
        const content = smoothedLiveNarrationMarkdown(cell.markdownSource, Boolean(cell.isStreaming));
        if (!content.trim()) return null;
        const item = processItem(cell.id, seq, content, "model", "process_text");
        if (cell.isStreaming) item.status = "running";
        return item;
      }
    default:
      return null;
  }
}

function fileChangesItemFromDiffCell(cell: DiffCellState, seq: number): AgentTimelineItem {
  return {
    id: cell.id,
    type: "file_changes",
    seq,
    cell,
    added: cell.summary.added,
    removed: cell.summary.deleted,
    files: cell.files.map((file) => ({
      path: file.path,
      added: file.additions,
      removed: file.deletions,
      patch: file.patch,
      status: file.changeType === "created"
        ? "created"
        : file.changeType === "deleted"
          ? "deleted"
          : "modified",
    })),
  };
}

function liveNarrationItemFromCell(cell: StreamingAssistantNarrationCellState, seq: number): LiveNarrationItem | null {
  if (!cell.partialMarkdown.trim()) return null;
  return {
    id: cell.id,
    type: "live_narration",
    seq,
    partialMarkdown: cell.partialMarkdown,
    isStreaming: cell.isStreaming,
    updatedAt: cell.updatedAt,
  };
}

function processItemFromThinking(cell: ThinkingCellState, seq: number): ProcessItem | null {
  const content = cell.content.trim();
  if (!content || isGenericProcessPlaceholder(content)) return null;
  if (cell.isRawProviderReasoning) return null;
  if (cell.source === "runtime") return null;
  if (cell.source === "model_preamble" && cell.phase !== "public_output") return null;
  const item = processItem(
    cell.id,
    seq,
    content,
    "model",
    "process_text",
  );
  if (cell.isStreaming) item.status = "running";
  return item;
}

function projectActivityCell(cell: ActivityCellState, seq: number): AgentTimelineItem | null {
  if (cell.canonical?.visibility === "developer") return null;
  if (cell.activityKind === "skill") {
    return null;
  }
  if (isAgentControlActivity(cell)) {
    return agentControlProcessItem(cell.id, [cell], seq);
  }
  if (ACTION_CHAIN_ACTIVITY_KINDS.has(cell.activityKind) && (cell.toolCallRecords?.length ?? 0) === 0) {
    return null;
  }
  if (isProcessActivity(cell)) {
    const content = [cell.title || cell.canonical?.title, cell.subtitle || cell.canonical?.summary]
      .filter(Boolean)
      .join("：")
      .trim();
    if (!content) return null;
    return processItem(cell.id, seq, content, "runtime", "observation");
  }
  if (isBrowserPreviewActivity(cell)) {
    return browserPreviewItemFromActivities(cell.id, [cell], seq);
  }
  const actionItems = actionTimelineItemsFromActivityCell(cell, seq);
  if (actionItems.length > 0) return actionItems[0] ?? null;
  const item = activityGroupItemFromActivities(cell.id, [cell], seq);
  return Array.isArray(item) ? item[0] ?? null : item;
}

function processItem(
  id: string,
  seq: number,
  content: string,
  source: ProcessItem["source"],
  kind: ProcessItem["kind"],
): ProcessItem {
  return {
    id,
    type: "process",
    kind,
    source,
    seq,
    content,
    status: "completed",
  };
}

function activityGroupItemFromExecs(
  id: string,
  cells: ExecCellState[],
  seq: number,
): ActivityGroupItem | null {
  if (cells.length === 0) return null;
  const status = execActivityStatus(cells);
  const count = cells.length;
  const isTestGroup = cells.every((cell) => isTestCommand(cell.command));
  const title =
    count === 1
      ? status === "running" ? "正在运行命令" : "已运行命令"
      : status === "running"
        ? `正在运行 ${count} 条命令`
        : `已运行 ${count} 条命令`;
  const summary =
    status === "failed"
      ? isTestGroup
        ? "测试命令"
        : execCommandTargetSummary(cells)
      : isTestGroup
        ? "测试命令"
        : execCommandTargetSummary(cells);

  return {
    id,
    type: "activity_group",
    activityKind: isTestGroup ? "test" : "command",
    seq,
    title,
    summary,
    durationLabel: commandDurationFromExecCells(cells),
    status,
    details: cells.map(shellDetailFromExec),
    defaultCollapsed: status !== "running",
    emphasis: "group",
  };
}

function shellDetailFromExec(cell: ExecCellState): ActivityDetail {
  const stdout = (cell.stdoutFull ?? cell.stdoutPreview.join("\n")).trimEnd();
  const stderr = (cell.stderrFull ?? cell.stderrPreview.join("\n")).trimEnd();
  const output = [stdout, stderr ? `[stderr]\n${stderr}` : ""].filter(Boolean).join("\n");
  return {
    kind: "shell",
    title: "Shell",
    command: cell.command,
    output,
    exitCode: cell.exitCode,
  };
}

function activityGroupItemFromActivities(
  id: string,
  cells: ActivityCellState[],
  seq: number,
): ActivityGroupItem | ActivityGroupItem[] | BrowserPreviewItem | null {
  const visibleCells = cells.filter((cell) => !isAgentControlActivity(cell));
  if (visibleCells.length === 0) return null;
  if (visibleCells.some(isBrowserPreviewActivity)) {
    return browserPreviewItemFromActivities(id, visibleCells, seq);
  }
  const actionItems = actionTimelineItemsFromActivityCells(id, visibleCells, seq);
  if (actionItems.length > 0) return actionItems.length === 1 ? actionItems[0] : actionItems;
  const status = activityStatus(visibleCells);
  const activityKind = agentActivityKind(visibleCells);
  const count = activityRecordTotal(visibleCells);
  const title = activityTitle(visibleCells, activityKind, status, count);
  const details = visibleCells.flatMap(activityDetailsFromCell);

  return {
    id,
    type: "activity_group",
    activityKind,
    seq,
    title,
    summary: activitySummary(activityKind, count),
    status,
    details: details.length > 0 ? details : fallbackActivityDetails(visibleCells),
    defaultCollapsed: status !== "running",
    emphasis: "group",
  };
}

function actionTimelineItemsFromActivityCell(cell: ActivityCellState, seq: number): ActivityGroupItem[] {
  return actionTimelineItemsFromActivityCells(cell.id, [cell], seq);
}

function actionTimelineItemsFromActivityCells(
  id: string,
  cells: ActivityCellState[],
  seq: number,
): ActivityGroupItem[] {
  if (!cells.every((cell) => ACTION_CHAIN_ACTIVITY_KINDS.has(cell.activityKind))) return [];
  const items: ActivityGroupItem[] = [];
  for (const cell of cells) {
    const segments = activityRecordSegments(cell);
    for (const segment of segments) {
      const segmentCell = activityCellWithRecords(cell, segment.records, segment.id);
      const details = segment.records.map((record) => detailFromToolRecord(segmentCell, record));
      const status = activityStatusFromRecords(segment.records, activityStatus([cell]));
      const activityKind = agentActivityKind([segmentCell]);
      const title = actionChainTitle([segmentCell], segment.records, activityKind, status);
      const itemId = cell.activityKind === "webSearch" || cells.length > 1 || segments.length > 1
        ? segment.id
        : id;
      items.push({
        id: itemId,
        type: "activity_group" as const,
        activityKind,
        seq: seq + items.length,
        title,
        summary: actionChainSummary(segment.records, activityKind),
        durationLabel: (activityKind === "command" || activityKind === "test") ? activityDurationFromCells([segmentCell]) : undefined,
        status,
        details,
        defaultCollapsed: status !== "running",
        emphasis: "inline" as const,
      });
    }
  }
  return items;
}

function activityRecordSegments(cell: ActivityCellState): { id: string; records: ToolCallRecord[] }[] {
  const records = cell.toolCallRecords ?? [];
  if (records.length === 0) return [];
  if (cell.activityKind !== "webSearch") {
    return [{ id: stableActivitySegmentId(cell, records, 0), records }];
  }

  const segments: { id: string; records: ToolCallRecord[] }[] = [];
  let current: ToolCallRecord[] = [];
  let currentKind = "";

  records.forEach((record) => {
    const nextKind = webRecordSegmentKind(record);
    if (current.length > 0 && nextKind !== currentKind) {
      segments.push({ id: stableActivitySegmentId(cell, current, segments.length), records: current });
      current = [];
    }
    currentKind = nextKind;
    current.push(record);
  });
  if (current.length > 0) {
    segments.push({ id: stableActivitySegmentId(cell, current, segments.length), records: current });
  }
  return segments;
}

function webRecordSegmentKind(record: ToolCallRecord): "search" | "read" {
  return recordHasUrl(record) ? "read" : "search";
}

function stableActivitySegmentId(cell: ActivityCellState, records: ToolCallRecord[], index: number): string {
  const firstRecord = records[0];
  const firstId = firstRecord?.id?.trim();
  const kind = firstRecord ? webRecordSegmentKind(firstRecord) : "segment";
  return `${cell.id}:${kind}:${firstId || index}`;
}

function activityCellWithRecords(
  cell: ActivityCellState,
  records: ToolCallRecord[],
  id: string,
): ActivityCellState {
  return {
    ...cell,
    id,
    title: "",
    subtitle: undefined,
    status: mapRecordStatusForActivity(records, cell.status),
    toolCallRecords: records,
    startedAt: Math.min(...records.map((record) => record.startedAt).filter(Number.isFinite), cell.startedAt),
    completedAt: latestRecordFinishedAt(records) ?? cell.completedAt,
  };
}

function mapRecordStatusForActivity(records: ToolCallRecord[], fallback: ActivityCellState["status"]): ActivityCellState["status"] {
  if (records.some((record) => record.status === "failed" || record.status === "blocked")) return "failed";
  if (records.some((record) => record.status === "running" || record.status === "pending")) return "running";
  return fallback;
}

function activityStatusFromRecords(
  records: ToolCallRecord[],
  fallback: ActivityGroupItem["status"],
): ActivityGroupItem["status"] {
  if (records.some((record) => record.status === "failed" || record.status === "blocked")) return "failed";
  if (records.some((record) => record.status === "running" || record.status === "pending")) return "running";
  return fallback;
}

function latestRecordFinishedAt(records: ToolCallRecord[]): number | undefined {
  const finished = records
    .map((record) => record.finishedAt)
    .filter((value): value is number => Number.isFinite(value));
  return finished.length ? Math.max(...finished) : undefined;
}

function commandDurationFromExecCells(cells: ExecCellState[]): string | undefined {
  const durations = cells.map((cell) => {
    if (Number.isFinite(cell.durationMs)) return cell.durationMs ?? 0;
    if (Number.isFinite(cell.completedAt) && Number.isFinite(cell.createdAt) && (cell.completedAt ?? 0) > cell.createdAt) {
      return (cell.completedAt ?? 0) - cell.createdAt;
    }
    return 0;
  }).filter((value) => value > 0);
  const total = durations.reduce((sum, value) => sum + value, 0);
  return total > 0 ? formatDurationMs(total) : undefined;
}

function execCommandTargetSummary(cells: ExecCellState[]): string {
  return compactSummaryList(
    cells
      .map((cell) => truncateMiddle(cell.command.replace(/\s+/g, " "), 58))
      .filter(Boolean),
    2,
  );
}

function normalizeVisibleText(value: string | undefined): string {
  return (value || "").replace(/\s+/g, " ").trim();
}

function answerCellText(cell: AgentLoopAnswerCell | null): string {
  if (!cell) return "";
  return normalizeVisibleText(cell.markdownSource);
}

function activeTailToAssistantMarkdownCell(
  cell: StreamingAssistantTailCellState,
  id: string,
): AssistantMarkdownCellState | null {
  if (!cell.partialMarkdown.trim()) return null;
  return {
    kind: "assistant_markdown",
    id,
    markdownSource: cell.partialMarkdown,
    phase: "final",
    copyable: false,
    isStreaming: true,
    source: "partial",
    createdAt: cell.updatedAt,
  };
}

function processCellText(cell: ChatTurnState["committedCells"][number]): string {
  if (cell.kind === "thinking") return normalizeVisibleText(cell.content);
  if (cell.kind === "assistant_markdown") return normalizeVisibleText(cell.markdownSource);
  if (cell.kind === "streaming_assistant_narration") return normalizeVisibleText(cell.partialMarkdown);
  return "";
}

function duplicatesAnswerCell(cell: ChatTurnState["committedCells"][number], answerText: string): boolean {
  if (!answerText) return false;
  const processText = processCellText(cell);
  return Boolean(processText) && processText === answerText;
}

function activityDurationFromCells(cells: ActivityCellState[]): string | undefined {
  const starts = cells.map((cell) => cell.startedAt).filter(Number.isFinite);
  const ends = cells.map((cell) => cell.completedAt).filter((value): value is number => Number.isFinite(value));
  if (starts.length === 0 || ends.length === 0) return undefined;
  const start = Math.min(...starts);
  const end = Math.max(...ends);
  return end > start ? formatDurationMs(end - start) : undefined;
}

function commandTargetSummary(records: ToolCallRecord[]): string {
  const commands = records
    .map((record) => stringArg(record.args?.command ?? record.args?.cmd ?? record.args?.script))
    .filter(Boolean)
    .map((command) => truncateMiddle(command.replace(/\s+/g, " "), 58));
  return compactSummaryList(commands, 2);
}

function fileTargetSummary(records: ToolCallRecord[]): string {
  const targets = records
    .map((record) => firstRecordPathOrQuery([record]))
    .filter(Boolean);
  return compactSummaryList(targets, 2);
}

function toolTargetSummary(records: ToolCallRecord[]): string {
  const targets = records
    .map((record) => {
      const args = record.args ?? {};
      return (
        stringArg(record.displayHint) ||
        stringArg(record.inputSummary) ||
        stringArg(record.displaySummary) ||
        cleanToolPath(args.file_path ?? args.path ?? args.directory ?? args.dir ?? args.cwd ?? args.target ?? args.filename) ||
        stringArg(args.url ?? args.source_url) ||
        stringArg(args.query ?? args.q ?? args.pattern ?? args.glob) ||
        stringArg(args.description ?? args.title ?? args.objective ?? args.task_id ?? args.workflow_id ?? args.name) ||
        stringArg(args.command ?? args.cmd)
      );
    })
    .filter(Boolean)
    .map((target) => truncateMiddle(target, 64));
  return compactSummaryList(targets, 2);
}

function compactSummaryList(items: string[], visibleCount: number): string {
  const unique = Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)));
  if (unique.length === 0) return "";
  const shown = unique.slice(0, visibleCount).join(" · ");
  const hidden = unique.length - visibleCount;
  return hidden > 0 ? `${shown} · +${hidden}` : shown;
}

type ActivityRenderKind = ActivityGroupItem["activityKind"];

interface ActivityRenderContext {
  cells: ActivityCellState[];
  records: ToolCallRecord[];
  status: ActivityGroupItem["status"];
  count: number;
  inline: boolean;
}

interface ActivityRenderer {
  matches: (cells: ActivityCellState[], kinds: Set<ActivityCellState["activityKind"]>) => boolean;
  title: (context: ActivityRenderContext) => string;
  summary: (context: ActivityRenderContext) => string;
}

const ACTIVITY_RENDERERS: Record<ActivityRenderKind, ActivityRenderer> = {
  command: {
    matches: (_cells, kinds) => kinds.size === 1 && kinds.has("commandExecution"),
    title: ({ records, status, count }) => {
      const total = records.length || count;
      if (total === 1) return status === "running" ? "正在运行命令" : "已运行命令";
      return status === "running" ? `正在运行 ${total} 条命令` : `已运行 ${total} 条命令`;
    },
    summary: ({ records }) => commandTargetSummary(records),
  },
  test: {
    matches: () => false,
    title: ({ records, status, count }) => {
      const total = records.length || count;
      if (total === 1) return status === "running" ? "正在运行命令" : "已运行命令";
      return status === "running" ? `正在运行 ${total} 条命令` : `已运行 ${total} 条命令`;
    },
    summary: ({ records }) => commandTargetSummary(records),
  },
  web_search: {
    matches: (cells, kinds) => (
      kinds.size === 1 &&
      kinds.has("webSearch") &&
      !cells.some((cell) => cell.toolCallRecords?.some(recordHasUrl))
    ),
    title: ({ records, status, count }) => {
      const total = records.length || count;
      const query = firstRecordQuery(records);
      if (total === 1 && query) return status === "running" ? `正在搜索 ${query}` : `已搜索 ${query}`;
      if (status === "running") return total > 0 ? `正在搜索 ${total} 次` : "正在搜索";
      return total === 1 ? "已搜索 1 次" : `已搜索 ${total} 次`;
    },
    summary: () => "",
  },
  web_read: {
    matches: (cells, kinds) => (
      kinds.size === 1 &&
      kinds.has("webSearch") &&
      cells.some((cell) => cell.toolCallRecords?.some(recordHasUrl))
    ),
    title: ({ records, status, count, inline }) => {
      const total = records.length || count;
      const target = firstRecordUrl(records);
      if (inline && total === 1 && target) return status === "running" ? `正在打开 ${shortUrlLabel(target)}` : `已打开 ${shortUrlLabel(target)}`;
      if (inline && status === "running" && total > 0) return `正在打开 ${total} 个网页`;
      if (inline && status !== "running" && total > 0) return `已打开 ${total} 个网页`;
      if (status === "running") return total > 0 ? `正在打开 ${total} 个网页` : "正在打开网页";
      return total === 1 ? "已打开网页" : `已打开 ${total} 个网页`;
    },
    summary: () => "",
  },
  file_read: {
    matches: (_cells, kinds) => (
      [...kinds].every((kind) => kind === "fileRead" || kind === "workspaceSearch") ||
      [...kinds].every((kind) => kind === "fileRead" || kind === "workspaceSearch" || kind === "webSearch" || kind === "mcpToolCall")
    ),
    title: ({ records, status, count }) => {
      const total = records.length || count;
      const target = firstRecordPathOrQuery(records);
      const hasSearch = records.some(isWorkspaceSearchToolRecord);
      const verb = hasSearch ? "搜索" : "读取";
      if (total === 1 && target) return status === "running" ? `正在${verb} ${target}` : `已${verb} ${target}`;
      if (status === "running") return total > 0 ? `正在${verb} ${total} 项` : `正在${verb}`;
      return total === 1 ? `已${verb} 1 项` : `已${verb} ${total} 项`;
    },
    summary: ({ records }) => fileTargetSummary(records),
  },
  file_change: {
    matches: (_cells, kinds) => kinds.size === 1 && kinds.has("fileChange"),
    title: ({ records, status, count }) => {
      const total = records.length || count;
      const target = firstRecordPathOrQuery(records);
      const verb = "修改";
      if (total === 1 && target) return status === "running" ? `正在${verb} ${target}` : `已${verb} ${target}`;
      return status === "running" ? `正在${verb} ${total} 个文件` : `已${verb} ${total} 个文件`;
    },
    summary: ({ records, count }) => firstRecordPathOrQuery(records) || `${records.length || count} 个文件`,
  },
  browser: {
    matches: (cells) => cells.some(isBrowserPreviewActivity),
    title: ({ cells, status }) => {
      const title = cells[0]?.title?.trim();
      return title || (status === "running" ? "正在打开浏览器预览" : "已打开浏览器预览");
    },
    summary: () => "预览",
  },
  mcp: {
    matches: (_cells, kinds) => kinds.size === 1 && kinds.has("mcpToolCall"),
    title: ({ records, status, count, inline }) => {
      const total = records.length || count;
      if (!inline) return status === "running" ? `正在调用 ${total} 个 MCP 工具` : `已调用 ${total} 个 MCP 工具`;
      return status === "running" ? `正在调用 ${total} 个工具` : `已调用 ${total} 个工具`;
    },
    summary: ({ records, count }) => toolTargetSummary(records) || `${records.length || count} 个工具`,
  },
  unknown: {
    matches: () => true,
    title: ({ cells, records, status, count }) => {
      const title = cells[0]?.title?.trim();
      const total = records.length || count;
      return title || (status === "running" ? `正在运行 ${total} 项` : `已处理 ${total} 项`);
    },
    summary: ({ records, count }) => toolTargetSummary(records) || `${records.length || count} 项`,
  },
};

const ACTIVITY_CLASSIFIER_ORDER: ActivityRenderKind[] = [
  "browser",
  "command",
  "web_read",
  "web_search",
  "mcp",
  "file_change",
  "file_read",
  "unknown",
];

function renderActivity(kind: ActivityRenderKind): ActivityRenderer {
  return ACTIVITY_RENDERERS[kind] ?? ACTIVITY_RENDERERS.unknown;
}

function actionChainTitle(
  cells: ActivityCellState[],
  records: ToolCallRecord[],
  activityKind: ActivityGroupItem["activityKind"],
  status: ActivityGroupItem["status"],
): string {
  return renderActivity(activityKind)
    .title({ cells, records, status, count: records.length, inline: true });
}

function actionChainSummary(records: ToolCallRecord[], activityKind: ActivityGroupItem["activityKind"]): string {
  const context = { cells: [] as ActivityCellState[], records, status: "completed" as const, count: records.length, inline: true };
  return renderActivity(activityKind).summary(context);
}

function browserPreviewItemFromActivities(
  id: string,
  cells: ActivityCellState[],
  seq: number,
): BrowserPreviewItem {
  const status = activityStatus(cells);
  const url = firstUrlFromActivities(cells);
  const title = cells.find((cell) => cell.title.trim())?.title.trim() || "已打开浏览器预览";
  return {
    id,
    type: "browser_preview",
    seq,
    title,
    url,
    status,
  };
}

function activityDetailsFromCell(cell: ActivityCellState): ActivityDetail[] {
  const records = cell.toolCallRecords ?? [];
  if (records.length === 0) {
    return fallbackActivityDetails([cell]);
  }
  return records.map((record) => detailFromToolRecord(cell, record));
}

interface ToolDetailRenderContext {
  cell: ActivityCellState;
  record: ToolCallRecord;
  name: string;
  args: Record<string, unknown>;
  command: string;
  url: string;
  query: string;
  path: string;
}

interface ToolDetailRenderer {
  matches: (context: ToolDetailRenderContext) => boolean;
  render: (context: ToolDetailRenderContext) => ActivityDetail;
}

const TOOL_DETAIL_RENDERERS: ToolDetailRenderer[] = [
  {
    matches: ({ cell, record }) => cell.activityKind === "commandExecution" || isCommandToolRecord(record),
    render: ({ command, record }) => ({
      kind: "shell",
      title: "Shell",
      command: command || stringArg(record.args?.script) || record.name,
      output: record.outputPreview || record.stdoutPreview || record.stderrPreview || "",
    }),
  },
  {
    matches: ({ url }) => Boolean(url),
    render: ({ record, url }) => ({
      kind: "source",
      title: webDetailTitle(record),
      url,
    }),
  },
  {
    matches: ({ query }) => Boolean(query),
    render: ({ record, query }) => ({
      kind: "source",
      title: searchDetailTitle(record),
      query,
    }),
  },
  {
    matches: ({ path }) => Boolean(path),
    render: ({ record, path }) => {
      const diffStats = record.diff ? getToolDiffStats(record.diff) : undefined;
      return {
        kind: "source",
        title: fileDetailTitle(record),
        path,
        excerpt: writeFileExcerpt(record) || path,
        lineInfo: readFileLineInfoLabel(record) || undefined,
        additions: diffStats?.plus,
        deletions: diffStats?.minus,
        changeType: fileDetailChangeType(record),
      };
    },
  },
  {
    matches: () => true,
    render: ({ record }) => ({
      kind: "text",
      title: readableToolName(record.name),
      content: toolTargetSummary([record]) || record.contentPreview || record.name,
    }),
  },
];

function detailFromToolRecord(cell: ActivityCellState, record: ToolCallRecord): ActivityDetail {
  const args = record.args ?? {};
  const context: ToolDetailRenderContext = {
    cell,
    record,
    name: record.name.toLowerCase(),
    args,
    command: stringArg(args.command ?? args.cmd),
    url: stringArg(args.url ?? args.source_url ?? record.sourceUrl) || firstHttpUrl(record.displaySummary || record.summary),
    query: stringArg(args.query ?? args.q ?? args.pattern ?? args.glob),
    path: cleanToolPath(args.file_path ?? args.path ?? args.target ?? args.filename),
  };
  return (TOOL_DETAIL_RENDERERS.find((renderer) => renderer.matches(context)) ?? TOOL_DETAIL_RENDERERS[TOOL_DETAIL_RENDERERS.length - 1]!)
    .render(context);
}

function fallbackActivityDetails(cells: ActivityCellState[]): ActivityDetail[] {
  return cells
    .map((cell) => [cell.title, cell.subtitle].filter(Boolean).join("：").trim())
    .filter(Boolean)
    .map((content) => ({ kind: "text", title: "Activity", content }));
}

function systemStatusFromNotice(cell: StatusNoticeCellState, seq: number): SystemStatusItem {
  return {
    id: cell.id,
    type: "system_status",
    seq,
    content: cell.title,
    detail: cell.message,
    tone: cell.tone === "danger" ? "error" : cell.tone === "warning" ? "warning" : "subtle",
  };
}

function systemStatusFromTurnSummary(cell: TurnSummaryCellState, seq: number): SystemStatusItem {
  const detail = cell.items
    .map((item) => [item.label, item.detail].filter(Boolean).join(" "))
    .join(" · ");
  return {
    id: cell.id,
    type: "system_status",
    seq,
    content: `已处理 ${cell.items.length} 项`,
    detail,
    ariaLabel: "Turn activity summary",
    tone: cell.status === "failed" ? "error" : "subtle",
  };
}

function systemStatusFromError(cell: ErrorCellState, seq: number): SystemStatusItem {
  return {
    id: cell.id,
    type: "system_status",
    seq,
    content: cell.title,
    detail: cell.message,
    tone: "error",
  };
}

function execActivityStatus(cells: ExecCellState[]): ActivityGroupItem["status"] {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "cancelled")) return "failed";
  if (cells.some((cell) => cell.status === "running" || cell.status === "pending_approval")) return "running";
  return "completed";
}

function activityStatus(cells: ActivityCellState[]): ActivityGroupItem["status"] {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "interrupted")) return "failed";
  if (cells.some((cell) => cell.status === "running")) return "running";
  return "completed";
}

function agentActivityKind(cells: ActivityCellState[]): ActivityGroupItem["activityKind"] {
  const kinds = new Set(cells.map((cell) => cell.activityKind));
  for (const kind of ACTIVITY_CLASSIFIER_ORDER) {
    if (renderActivity(kind).matches(cells, kinds)) {
      return kind;
    }
  }
  return "unknown";
}

function activityTitle(
  cells: ActivityCellState[],
  kind: ActivityGroupItem["activityKind"],
  status: ActivityGroupItem["status"],
  count: number,
): string {
  const records = cells.flatMap((cell) => cell.toolCallRecords ?? []);
  const context = { cells, records, status, count, inline: false };
  return renderActivity(kind).title(context);
}

function activitySummary(kind: ActivityGroupItem["activityKind"], count: number): string {
  const context = { cells: [] as ActivityCellState[], records: [] as ToolCallRecord[], status: "completed" as const, count, inline: false };
  return renderActivity(kind).summary(context);
}

function isProcessActivity(cell: ActivityCellState): boolean {
  if ((cell.toolCallRecords?.length ?? 0) > 0) return false;
  return [
    "reasoning",
    "planning",
    "processNote",
    "providerReasoning",
    "agentMessage",
    "progress",
  ].includes(cell.activityKind);
}

function activityRecordTotal(cells: ActivityCellState[]): number {
  return cells.reduce((sum, cell) => sum + activityRecordCount(cell), 0);
}

function activityRecordCount(cell: ActivityCellState): number {
  // Count comes from the tool-call records, never from digits scraped out of
  // localized display strings (which produced wrong counts like "文件 ×7").
  return cell.toolCallRecords?.length ?? 0;
}

function recordHasUrl(record: ToolCallRecord): boolean {
  if (isWebSearchToolRecord(record)) return false;
  const args = record.args ?? {};
  return Boolean(
    stringArg(args.url ?? args.source_url ?? record.sourceUrl) ||
    firstHttpUrl(record.displaySummary || record.summary),
  );
}

function firstRecordQuery(records: ToolCallRecord[]): string {
  for (const record of records) {
    const args = record.args ?? {};
    const query = stringArg(args.query ?? args.q ?? args.pattern ?? args.glob);
    if (query) return truncateMiddle(query, 80);
  }
  return "";
}

function firstRecordUrl(records: ToolCallRecord[]): string {
  for (const record of records) {
    const args = record.args ?? {};
    const url = stringArg(args.url ?? args.source_url ?? record.sourceUrl) ||
      firstHttpUrl(record.displaySummary || record.summary);
    if (url) return url;
  }
  return "";
}

function firstRecordPathOrQuery(records: ToolCallRecord[]): string {
  for (const record of records) {
    const args = record.args ?? {};
    const path = cleanToolPath(args.file_path ?? args.path ?? args.directory ?? args.dir ?? args.cwd ?? args.target ?? args.filename);
    if (path) return `${truncatePath(path)}${readFileLineInfoSuffix(record)}`;
    const query = stringArg(args.query ?? args.q ?? args.pattern ?? args.glob);
    if (query) return truncateMiddle(query, 80);
  }
  return "";
}

function readFileLineInfoSuffix(record: ToolCallRecord): string {
  const lineInfo = readFileLineInfoLabel(record);
  return lineInfo ? ` ${lineInfo}` : "";
}

function readFileLineInfoLabel(record: ToolCallRecord): string {
  const range = readFileLineRangeLabel(record);
  if (range) return range;
  const totalLines = readFileTotalLineCount(record);
  return totalLines ? `L1-L${totalLines}` : "";
}

function readFileLineRangeLabel(record: ToolCallRecord): string {
  if (!/^read_file$/i.test(record.name)) return "";
  const args = record.args ?? {};
  const start = positiveIntArg(args.start_line ?? args.startLine ?? args.line);
  const end = positiveIntArg(args.end_line ?? args.endLine);
  if (start && end) return `L${start}-L${end}`;
  if (start) return `L${start}+`;
  if (end) return `L1-L${end}`;
  return "";
}

function readFileTotalLineCount(record: ToolCallRecord): number | null {
  if (!/^read_file$/i.test(record.name)) return null;
  for (const text of [record.summary, record.contentPreview]) {
    const count = readFileTotalLineCountFromText(text);
    if (count) return count;
  }
  return null;
}

function readFileTotalLineCountFromText(text: string | undefined): number | null {
  if (!text) return null;

  const artifactHeader = text.match(/^File .+ \((\d+) lines, approx [^)]+\) was saved as an artifact\./m);
  const artifactLines = positiveIntArg(artifactHeader?.[1]);
  if (artifactLines) return artifactLines;

  let maxLineNumber = 0;
  for (const match of text.matchAll(/^\s*(\d+)→/gm)) {
    const lineNumber = positiveIntArg(match[1]);
    if (lineNumber && lineNumber > maxLineNumber) maxLineNumber = lineNumber;
  }
  if (maxLineNumber > 0) return maxLineNumber;

  const inlineContent = text.match(/^([\s\S]*?)\n\n\[(?:content_hash|range_hash):[^\]]+\](?:\n\[range only;[^\]]+\])?\s*$/);
  if (inlineContent?.[1] != null) {
    return inlineContent[1].split(/\r?\n/).length;
  }
  return null;
}

function positiveIntArg(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) {
    const intValue = Math.trunc(value);
    return intValue > 0 ? intValue : null;
  }
  if (typeof value === "string" && value.trim()) {
    const parsed = Number.parseInt(value.trim(), 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  }
  return null;
}

function truncateMiddle(value: string, max = 96): string {
  const text = value.replace(/\s+/g, " ").trim();
  if (text.length <= max) return text;
  const head = Math.max(12, Math.floor(max * 0.58));
  const tail = Math.max(8, max - head - 3);
  return `${text.slice(0, head)}...${text.slice(-tail)}`;
}

function truncatePath(value: string): string {
  const normalized = value.replace(/\\/g, "/");
  const parts = normalized.split("/").filter(Boolean);
  if (parts.length <= 3) return truncateMiddle(value, 84);
  return truncateMiddle(`${parts[0]}/.../${parts.slice(-2).join("/")}`, 84);
}

function shortUrlLabel(value: string): string {
  try {
    const url = new URL(value);
    const host = url.hostname.replace(/^www\./i, "");
    const path = url.pathname && url.pathname !== "/" ? url.pathname.replace(/\/$/, "") : "";
    return truncateMiddle(`${host}${path}`, 72);
  } catch {
    return truncateMiddle(value, 72);
  }
}

function stringArg(value: unknown): string {
  return typeof value === "string" ? value.trim() : "";
}

function cleanToolPath(value: unknown): string {
  const text = stringArg(value);
  if (!text) return "";
  const normalized = text.replace(/\\/g, "/").trim();
  if (normalized === "." || normalized === ".." || /^\.{1,2}\/?$/.test(normalized)) return "";
  if (/^[./\\]+$/.test(text)) return "";
  return normalized;
}

function firstHttpUrl(value?: string): string {
  return value?.match(/https?:\/\/[^\s)]+/i)?.[0] ?? "";
}

function firstUrlFromActivities(cells: ActivityCellState[]): string | undefined {
  for (const cell of cells) {
    for (const record of cell.toolCallRecords ?? []) {
      const args = record.args ?? {};
      const url = stringArg(args.url ?? args.source_url ?? record.sourceUrl) ||
        firstHttpUrl(record.displaySummary || record.summary);
      if (url) return url;
    }
    const fallback = firstHttpUrl(cell.subtitle || cell.title);
    if (fallback) return fallback;
  }
  return undefined;
}

function readableToolName(name: string): string {
  const normalized = name.replace(/^mcp__[^_]+__/i, "").toLowerCase();
  if (normalized === "task") return "子代理";
  if (normalized === "workflow") return "工作流";
  if (normalized.startsWith("task_")) return "协作任务";
  if (normalized.startsWith("team_")) return "代理团队";
  if (normalized === "send_message" || normalized === "message_list") return "代理消息";
  return name.replace(/^mcp__[^_]+__/i, "").replace(/_/g, " ");
}

function webDetailTitle(record: ToolCallRecord): string {
  return isWebFetchToolRecord(record) ? "已打开" : "已搜索";
}

function agentControlProcessItem(
  id: string,
  cells: ActivityCellState[],
  seq: number,
): ProcessItem | null {
  if (cells.length === 0 || !cells.every(isAgentControlActivity)) return null;
  const records = cells.flatMap((cell) => cell.toolCallRecords ?? []);
  const started = records.filter((record) => record.name.trim().toLowerCase() === "task");
  const statusChecks = records.filter((record) => /^task_status$/i.test(record.name.trim()));
  const stopped = records.filter((record) => /^task_stop$/i.test(record.name.trim()));
  const messages = records.filter((record) => /^send_message$/i.test(record.name.trim()));
  const parts: string[] = [];
  if (started.length > 0) parts.push(`发起子任务 ${started.length} 次`);
  if (statusChecks.length > 0) parts.push(`检查进度 ${statusChecks.length} 次`);
  if (messages.length > 0) parts.push(`同步消息 ${messages.length} 次`);
  if (stopped.length > 0) parts.push(`停止 ${stopped.length} 个子任务`);
  if (parts.length === 0) parts.push(`已执行 ${records.length} 次协作操作`);
  return processItem(id, seq, parts.join("，"), "runtime", "action_summary");
}

function isAgentControlActivity(cell: ActivityCellState): boolean {
  const records = cell.toolCallRecords ?? [];
  return cell.activityKind === "genericTool"
    && records.length > 0
    && records.every((record) => isAgentControlToolName(record.name));
}

function searchDetailTitle(record: ToolCallRecord): string {
  return isWorkspaceSearchToolRecord(record) ? "已搜索" : "已处理";
}

function fileDetailTitle(record: ToolCallRecord): string {
  if (isFileChangeToolRecord(record)) return "修改文件";
  return isFileReadToolRecord(record) ? "读取文件" : "文件";
}

function fileDetailChangeType(record: ToolCallRecord): "created" | "updated" | "deleted" | undefined {
  const patch = record.diff?.patch ?? "";
  if (/^(?:deleted file mode\b|\+\+\+\s+\/dev\/null$)/m.test(patch)) return "deleted";
  if (/^(?:new file mode\b|---\s+\/dev\/null$)/m.test(patch)) return "created";
  if (isFileChangeToolRecord(record)) return "updated";
  return undefined;
}

function writeFileExcerpt(record: ToolCallRecord): string | undefined {
  const isWriteEdit = isFileChangeToolRecord(record);
  const diff = record.diff;
  const lines: string[] = [];
  if (diff) {
    lines.push(`+${diff.plus} -${diff.minus}`);
    if (diff.patch) lines.push(diff.patch);
  } else if (isWriteEdit) {
    const preview = record.contentPreview || "";
    if (preview) lines.push(preview);
  }
  const text = lines.join("\n").trim();
  return text || undefined;
}

function sourceContribution(cell: Extract<HistoryCellState, { kind: "activity" }>): number {
  if (cell.activityKind === "webSearch") {
    return (cell.toolCallRecords ?? []).filter(recordHasUrl).length;
  }
  if (!["fileRead", "workspaceSearch", "mcpToolCall"].includes(cell.activityKind)) {
    return 0;
  }
  return Math.max(1, cell.toolCallRecords?.length ?? 1);
}

function isBrowserPreviewActivity(cell: Extract<HistoryCellState, { kind: "activity" }>): boolean {
  if (cell.activityKind === "webSearch" || cell.activityKind === "fileRead" || cell.activityKind === "workspaceSearch") {
    return false;
  }
  if ((cell.toolCallRecords ?? []).some(isBrowserToolRecord)) return true;
  return false;
}
