import type {
  ActivityCellState,
  AssistantMarkdownCellState,
  ChatTurnState,
  DiffCellState,
  ErrorCellState,
  ExecCellState,
  HistoryCellState,
  PlanCellState,
  StatusNoticeCellState,
  StreamingAssistantTailCellState,
  ThinkingCellState,
  TurnSummaryCellState,
} from "../../chat/cells/cellTypes";
import type { ToolCallRecord } from "../../lib/tool-call-reducer";
import type {
  ActivityDetail,
  ActivityGroupItem,
  AgentTimelineItem,
  AgentTurnStatus,
  BrowserPreviewItem,
  ProcessItem,
  SystemStatusItem,
} from "../types";

export type AgentLoopProcessCell = ChatTurnState["committedCells"][number];
export type AgentLoopAnswerCell = AssistantMarkdownCellState | StreamingAssistantTailCellState;

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
  const answerCell = turn.finalAnswerCell ?? activeAnswerCell;
  const answerIsStreaming =
    Boolean(activeAnswerCell) ||
    Boolean(turn.finalAnswerCell?.isStreaming);
  const artifactCells: DiffCellState[] = [];
  const hideInlineSummary = Boolean(answerCell) || turn.status === "streaming";
  const processCells = committedCells.filter((cell) => (
    !(hideInlineSummary && isTurnSummaryCell(cell))
  ));
  const timelineItems = buildAgentTimelineItems(processCells);
  const hasProcessContent =
    processCells.length > 0 ||
    (turn.status === "streaming" && !answerCell);
  const shouldCollapseProcess =
    processCells.length > 0 &&
    Boolean(turn.finalAnswerCell) &&
    !answerIsStreaming &&
    turn.status !== "streaming";

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

export function buildAgentTimelineItems(cells: AgentLoopProcessCell[]): AgentTimelineItem[] {
  const items: AgentTimelineItem[] = [];
  let seq = 0;

  for (const cell of cells) {
    const item = projectProcessCell(cell, seq);
    if (!item) continue;
    const projectedItems = Array.isArray(item) ? item : [item];
    items.push(...projectedItems);
    seq += projectedItems.length;
  }

  return items;
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
  const totalSeconds = Math.max(1, Math.floor((end - start) / 1000));
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
  if (sourceCount > 0) items.push({ kind: "source", label: `已收集 ${sourceCount} 个来源` });
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
    case "activity":
      return projectActivityCell(cell, seq);
    case "activity_group":
      return activityGroupItemFromActivities(cell.id, cell.cells, seq);
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
      return systemStatusFromPlan(cell, seq);
    case "diff":
      return fileChangesItemFromDiffCell(cell, seq);
    case "assistant_markdown":
      return processItem(cell.id, seq, cell.markdownSource, "model", "process_text");
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
      status: file.changeType === "created"
        ? "created"
        : file.changeType === "deleted"
          ? "deleted"
          : "modified",
    })),
    actions: {
      canReview: cell.files.some((file) => Boolean(file.patch)),
      canUndo: cell.files.some((file) => Boolean(file.path)),
    },
  };
}

function processItemFromThinking(cell: ThinkingCellState, seq: number): ProcessItem | null {
  const content = cell.content.trim();
  if (!content) return null;
  return processItem(
    cell.id,
    seq,
    content,
    cell.source === "runtime" ? "runtime" : "model",
    cell.source === "runtime" ? "action_summary" : "process_text",
  );
}

function projectActivityCell(cell: ActivityCellState, seq: number): AgentTimelineItem | null {
  if (isProcessActivity(cell)) {
    const content = [cell.title, cell.subtitle].filter(Boolean).join("：").trim();
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
      ? "有命令未通过"
      : isTestGroup
        ? "测试命令"
        : "Shell";

  return {
    id,
    type: "activity_group",
    activityKind: isTestGroup ? "test" : "command",
    seq,
    title,
    summary,
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
    title: cell.status === "failed" ? "Shell failed" : "Shell",
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
  if (cells.length === 0) return null;
  if (cells.some(isBrowserPreviewActivity)) {
    return browserPreviewItemFromActivities(id, cells, seq);
  }
  const actionItems = actionTimelineItemsFromActivityCells(id, cells, seq);
  if (actionItems.length > 0) return actionItems.length === 1 ? actionItems[0] : actionItems;
  const status = activityStatus(cells);
  const activityKind = agentActivityKind(cells);
  const count = activityRecordTotal(cells);
  const title = activityTitle(cells, activityKind, status, count);
  const details = cells.flatMap(activityDetailsFromCell);

  return {
    id,
    type: "activity_group",
    activityKind,
    seq,
    title,
    summary: activitySummary(activityKind, count),
    status,
    details: details.length > 0 ? details : fallbackActivityDetails(cells),
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
  return cells.flatMap((cell, index) => {
    const records = cell.toolCallRecords ?? [];
    if (records.length === 0) return [];
    const details = records.map((record) => detailFromToolRecord(cell, record));
    const status = activityStatus([cell]);
    const activityKind = agentActivityKind([cell]);
    const title = actionChainTitle([cell], records, activityKind, status);
    return [{
      id: cells.length === 1 ? id : `${id}:${cell.id}`,
      type: "activity_group" as const,
      activityKind,
      seq: seq + index,
      title,
      summary: actionChainSummary(records, activityKind),
      status,
      details,
      defaultCollapsed: status !== "running",
      emphasis: "inline" as const,
    }];
  });
}

function actionChainTitle(
  cells: ActivityCellState[],
  records: ToolCallRecord[],
  activityKind: ActivityGroupItem["activityKind"],
  status: ActivityGroupItem["status"],
): string {
  if (activityKind === "command" || activityKind === "test") {
    if (records.length === 1) return status === "running" ? "正在运行命令" : "已运行命令";
    return status === "running" ? `正在运行 ${records.length} 条命令` : `已运行 ${records.length} 条命令`;
  }
  if (activityKind === "web_search") {
    const query = firstRecordQuery(records);
    if (records.length === 1 && query) return status === "running" ? `正在搜索 ${query}` : `已搜索 ${query}`;
    return status === "running" ? `正在搜索 ${records.length} 次` : `已搜索 ${records.length} 次`;
  }
  if (activityKind === "web_read") {
    const target = firstRecordUrl(records);
    if (records.length === 1 && target) return status === "running" ? `正在打开 ${shortUrlLabel(target)}` : `已打开 ${shortUrlLabel(target)}`;
    return status === "running" ? `正在打开 ${records.length} 个网页` : `已打开 ${records.length} 个网页`;
  }
  if (activityKind === "file_read") {
    const target = firstRecordPathOrQuery(records);
    if (records.length === 1 && target) return status === "running" ? `正在查看 ${target}` : `已查看 ${target}`;
    return status === "running" ? `正在查看 ${records.length} 个来源` : `已查看 ${records.length} 个来源`;
  }
  if (activityKind === "mcp") {
    return status === "running" ? `正在调用 ${records.length} 个工具` : `已调用 ${records.length} 个工具`;
  }
  const title = cells[0]?.title?.trim();
  return title || (status === "running" ? `正在处理 ${records.length} 项` : `已处理 ${records.length} 项`);
}

function actionChainSummary(records: ToolCallRecord[], activityKind: ActivityGroupItem["activityKind"]): string {
  const first = records[0];
  if (activityKind === "web_search") return firstRecordQuery(records) || `${records.length} 次搜索`;
  if (activityKind === "web_read") return firstRecordUrl(records) ? shortUrlLabel(firstRecordUrl(records)) : `${records.length} 个页面`;
  if (activityKind === "file_read") return firstRecordPathOrQuery(records) || `${records.length} 个来源`;
  if (activityKind === "command" || activityKind === "test") return `${records.length} 条命令`;
  return first?.displaySummary || first?.inputSummary || first?.summary || `${records.length} 项`;
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

function detailFromToolRecord(cell: ActivityCellState, record: ToolCallRecord): ActivityDetail {
  const name = record.name.toLowerCase();
  const args = record.args ?? {};
  const command = stringArg(args.command ?? args.cmd);
  if (cell.activityKind === "commandExecution" || /run_command|bash|powershell|terminal|shell/i.test(name)) {
    return {
      kind: "shell",
      title: "Shell",
      command: command || record.displaySummary || record.inputSummary || record.summary || record.name,
      output: record.outputPreview || record.stdoutPreview || record.stderrPreview || "",
    };
  }

  const url = stringArg(args.url ?? args.source_url ?? record.sourceUrl) || firstHttpUrl(record.displaySummary || record.summary);
  if (url) {
    const excerpt = cleanWebExcerpt(
      record.contentPreview ||
      record.displaySummary ||
      record.summary ||
      "",
    );
    return {
      kind: "source",
      title: webDetailTitle(record),
      url,
      excerpt,
    };
  }

  const query = stringArg(args.query ?? args.q ?? args.pattern ?? args.glob);
  if (query) {
    return {
      kind: "source",
      title: searchDetailTitle(record),
      query,
      excerpt: record.displaySummary || record.summary || query,
    };
  }

  const path = stringArg(args.file_path ?? args.path ?? args.target ?? args.filename);
  if (path) {
    return {
      kind: "source",
      title: fileDetailTitle(record),
      path,
      excerpt: writeFileExcerpt(record) || path,
    };
  }

  return {
    kind: "text",
    title: readableToolName(record.name),
    content: record.displaySummary || record.inputSummary || record.summary || record.contentPreview || record.name,
  };
}

function cleanWebExcerpt(value: string): string | undefined {
  const text = value
    .replace(/<script[\s\S]*?<\/script>/gi, " ")
    .replace(/<style[\s\S]*?<\/style>/gi, " ")
    .replace(/<[^>]+>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (!text) return undefined;
  if (looksLikeCssOrHtmlDump(text)) return undefined;
  if (/^https?:\/\//i.test(text)) return undefined;
  return truncateMiddle(text, 220);
}

function looksLikeCssOrHtmlDump(text: string): boolean {
  const sample = text.slice(0, 800);
  const cssSignals = (sample.match(/[{};]/g) ?? []).length;
  if (cssSignals > 18 && /\.(?:[a-z0-9_-]+)\s*\{/i.test(sample)) return true;
  if (/<\/?(?:html|head|body|div|span|style|script|meta|link)\b/i.test(sample)) return true;
  if (/(?:width|height|background|font-size|line-height|margin|padding|position|z-index)\s*:/i.test(sample) && cssSignals > 8) return true;
  if (/function\s*\(|document\.|window\.|var\s+\w+\s*=|const\s+\w+\s*=|let\s+\w+\s*=/i.test(sample) && cssSignals > 8) return true;
  return false;
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

function systemStatusFromPlan(cell: PlanCellState, seq: number): SystemStatusItem {
  const completed = cell.steps.filter((step) => step.status === "completed").length;
  const total = cell.steps.length;
  return {
    id: cell.id,
    type: "system_status",
    seq,
    content: cell.title,
    detail: total > 0 ? `${completed}/${total} 步完成` : undefined,
    tone: cell.status === "cancelled" ? "warning" : "subtle",
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
  if (cells.some(isBrowserPreviewActivity)) return "browser";
  const kinds = new Set(cells.map((cell) => cell.activityKind));
  if (kinds.size === 1 && kinds.has("commandExecution")) return "command";
  if (kinds.size === 1 && kinds.has("webSearch")) {
    return cells.some((cell) => cell.toolCallRecords?.some(recordHasUrl)) ? "web_read" : "web_search";
  }
  if (kinds.size === 1 && kinds.has("mcpToolCall")) return "mcp";
  if ([...kinds].every((kind) => kind === "fileRead" || kind === "workspaceSearch")) return "file_read";
  if ([...kinds].every((kind) => kind === "fileRead" || kind === "workspaceSearch" || kind === "webSearch" || kind === "mcpToolCall")) {
    return "file_read";
  }
  return "unknown";
}

function activityTitle(
  cells: ActivityCellState[],
  kind: ActivityGroupItem["activityKind"],
  status: ActivityGroupItem["status"],
  count: number,
): string {
  if (kind === "browser") {
    const title = cells[0]?.title?.trim();
    return title || (status === "running" ? "正在打开浏览器预览" : "已打开浏览器预览");
  }
  if (kind === "command") {
    if (count === 1) return status === "running" ? "正在运行命令" : "已运行命令";
    return status === "running" ? `正在运行 ${count} 条命令` : `已运行 ${count} 条命令`;
  }
  if (kind === "mcp") {
    return status === "running" ? `正在调用 ${count} 个 MCP 工具` : `已调用 ${count} 个 MCP 工具`;
  }
  if (kind === "web_search" || kind === "web_read" || kind === "file_read") {
    return status === "running" ? "正在收集上下文" : count === 1 ? "已收集上下文" : `已收集 ${count} 个上下文来源`;
  }
  const title = cells[0]?.title?.trim();
  return title || (status === "running" ? `正在处理 ${count} 项` : `已处理 ${count} 项`);
}

function activitySummary(kind: ActivityGroupItem["activityKind"], count: number): string {
  switch (kind) {
    case "command":
    case "test":
      return `${count} 条命令`;
    case "web_search":
      return `${count} 次搜索`;
    case "web_read":
      return `${count} 个页面`;
    case "file_read":
      return `${count} 个来源`;
    case "mcp":
      return `${count} 个工具`;
    case "browser":
      return "预览";
    default:
      return `${count} 项`;
  }
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
  return Math.max(1, cells.reduce((sum, cell) => sum + activityRecordCount(cell), 0));
}

function activityRecordCount(cell: ActivityCellState): number {
  // Count comes from the tool-call records, never from digits scraped out of
  // localized display strings (which produced wrong counts like "文件 ×7").
  return Math.max(1, cell.toolCallRecords?.length ?? 1);
}

function recordHasUrl(record: ToolCallRecord): boolean {
  if (/search/i.test(record.name)) return false;
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
    const path = stringArg(args.file_path ?? args.path ?? args.target ?? args.filename);
    if (path) return truncatePath(path);
    const query = stringArg(args.query ?? args.q ?? args.pattern ?? args.glob);
    if (query) return truncateMiddle(query, 80);
  }
  return "";
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
  return name.replace(/^mcp__[^_]+__/i, "").replace(/_/g, " ");
}

function webDetailTitle(record: ToolCallRecord): string {
  if (/search/i.test(record.name)) return "搜索结果";
  return /fetch|read|open/i.test(record.name) ? "打开网页" : "来源";
}

function searchDetailTitle(record: ToolCallRecord): string {
  return /grep|glob|list|workspace|file/i.test(record.name) ? "搜索工作区" : "搜索网页";
}

function fileDetailTitle(record: ToolCallRecord): string {
  if (/write/i.test(record.name)) return "写入文件";
  if (/edit/i.test(record.name)) return "编辑文件";
  return "读取文件";
}

function writeFileExcerpt(record: ToolCallRecord): string | undefined {
  const isWriteEdit = /write|edit|patch|apply/i.test(record.name);
  const diff = record.diff;
  const lines: string[] = [];
  if (diff) {
    lines.push(`+${diff.plus} -${diff.minus}`);
    if (diff.patch) lines.push(diff.patch);
  } else if (isWriteEdit) {
    const preview = record.contentPreview || record.summary || "";
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
  const toolNames = (cell.toolCallRecords ?? []).map((record) => record.name).join(" ");
  if (/browser|playwright|preview/i.test(toolNames)) return true;

  const text = [
    cell.activityKind,
    cell.title,
    cell.subtitle,
    ...(cell.toolCallRecords ?? []).flatMap((record) => [
      record.displaySummary,
      record.summary,
      typeof record.args.url === "string" ? record.args.url : "",
    ]),
  ].join(" ");
  return /browser|playwright|preview|预览|页面检查|打开页面/i.test(text);
}
