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
  const artifactCells = committedCells.filter(isDiffCell);
  const hideInlineSummary = Boolean(answerCell) || turn.status === "streaming";
  const processCells = committedCells.filter((cell) => (
    !isDiffCell(cell) &&
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
    turn.status === "completed";

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
    items.push(item);
    seq += 1;
  }

  return items;
}

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

function isDiffCell(cell: HistoryCellState): cell is DiffCellState {
  return cell.kind === "diff";
}

function isTurnSummaryCell(cell: HistoryCellState): cell is TurnSummaryCellState {
  return cell.kind === "turn_summary";
}

function projectProcessCell(cell: AgentLoopProcessCell, seq: number): AgentTimelineItem | null {
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
    case "assistant_markdown":
      return processItem(cell.id, seq, cell.markdownSource, "model", "process_text");
    default:
      return null;
  }
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
  return activityGroupItemFromActivities(cell.id, [cell], seq);
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
    status === "running"
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
    defaultCollapsed: true,
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
): ActivityGroupItem | BrowserPreviewItem | null {
  if (cells.length === 0) return null;
  if (cells.some(isBrowserPreviewActivity)) {
    return browserPreviewItemFromActivities(id, cells, seq);
  }
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
    defaultCollapsed: true,
  };
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
    return {
      kind: "source",
      title: webDetailTitle(record),
      url,
      excerpt: record.contentPreview || record.displaySummary || record.summary,
    };
  }

  const query = stringArg(args.query ?? args.q ?? args.pattern ?? args.glob);
  if (query) {
    return {
      kind: "source",
      title: searchDetailTitle(record),
      query,
      excerpt: query,
    };
  }

  const path = stringArg(args.file_path ?? args.path ?? args.target ?? args.filename);
  if (path) {
    return {
      kind: "source",
      title: fileDetailTitle(record),
      path,
      excerpt: path,
    };
  }

  return {
    kind: "text",
    title: readableToolName(record.name),
    content: record.displaySummary || record.inputSummary || record.summary || record.contentPreview || record.name,
  };
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
  return Math.max(1, cell.toolCallRecords?.length ?? Number(cell.subtitle?.match(/\d+/)?.[0] || cell.title.match(/\d+/)?.[0] || 1));
}

function recordHasUrl(record: ToolCallRecord): boolean {
  const args = record.args ?? {};
  return Boolean(
    stringArg(args.url ?? args.source_url ?? record.sourceUrl) ||
    firstHttpUrl(record.displaySummary || record.summary),
  );
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
  return /fetch|read/i.test(record.name) ? "读取网页" : "来源";
}

function searchDetailTitle(record: ToolCallRecord): string {
  return /grep|glob|list|workspace/i.test(record.name) ? "搜索工作区" : "搜索";
}

function fileDetailTitle(record: ToolCallRecord): string {
  if (/write/i.test(record.name)) return "写入文件";
  if (/edit/i.test(record.name)) return "编辑文件";
  return "读取文件";
}

function sourceContribution(cell: Extract<HistoryCellState, { kind: "activity" }>): number {
  if (!["fileRead", "workspaceSearch", "webSearch", "mcpToolCall"].includes(cell.activityKind)) {
    return 0;
  }
  return Math.max(1, cell.toolCallRecords?.length ?? 1);
}

function isBrowserPreviewActivity(cell: Extract<HistoryCellState, { kind: "activity" }>): boolean {
  const text = [
    cell.activityKind,
    cell.title,
    cell.subtitle,
    ...(cell.toolCallRecords ?? []).flatMap((record) => [
      record.name,
      record.displaySummary,
      record.summary,
      typeof record.args.url === "string" ? record.args.url : "",
    ]),
  ].join(" ");
  return /browser|playwright|preview|预览|页面检查|打开页面/i.test(text);
}
