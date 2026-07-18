import type { ActivityCellState } from "./cellTypes";

// Single source of truth for activity grouping/counting rules, shared by:
// - ChatTurn.tsx groupActivityCells (membership: which adjacent cells fold)
// - ActivityGroupCell.tsx (chat-cell rendering: title/counts)
// - agent-loop/projection/project-turn.ts (AgentLoop timeline projection)

export type ActivityGroupStatus = "running" | "done" | "failed";
export type ActivityGroupMembershipKey = "context" | "command" | "change" | "tool" | "solo";
export type ActivityTitleKind = "context" | "command" | "change" | "tool" | "mixed";

const CONTEXT_KINDS = new Set<ActivityCellState["activityKind"]>([
  "fileRead",
  "workspaceSearch",
  "webSearch",
  "mcpToolCall",
]);

/**
 * Membership rule: decides whether adjacent activity cells fold into one
 * group. Strict — a "context" run must consist ONLY of context kinds.
 * "solo" cells are never grouped.
 */
export function activityGroupMembershipKey(cells: ActivityCellState[]): ActivityGroupMembershipKey {
  const kinds = new Set(cells.map((cell) => cell.activityKind));
  if ([...kinds].every((kind) => CONTEXT_KINDS.has(kind))) return "context";
  if (kinds.size === 1 && kinds.has("commandExecution")) return "command";
  if (kinds.size === 1 && kinds.has("fileChange")) return "change";
  if (kinds.size === 1 && kinds.has("genericTool")) return "tool";
  return "solo";
}

export function activityGroupStatus(cells: ActivityCellState[]): ActivityGroupStatus {
  if (cells.some((cell) => cell.status === "failed" || cell.status === "interrupted")) return "failed";
  if (cells.some((cell) => cell.status === "running")) return "running";
  return "done";
}

/**
 * Count comes from the tool-call records, never from digits scraped out of
 * localized display strings (which produced wrong counts like "文件 ×7").
 */
export function activityRecordCount(cell: ActivityCellState): number {
  return Math.max(1, cell.toolCallRecords?.length ?? 1);
}

export function activityRecordTotal(cells: ActivityCellState[]): number {
  return Math.max(1, cells.reduce((sum, cell) => sum + activityRecordCount(cell), 0));
}

export function activityCounts(
  cells: ActivityCellState[],
): Partial<Record<ActivityCellState["activityKind"], number>> {
  return cells.reduce((counts, cell) => {
    counts[cell.activityKind] = (counts[cell.activityKind] ?? 0) + activityRecordCount(cell);
    return counts;
  }, {} as Partial<Record<ActivityCellState["activityKind"], number>>);
}

/**
 * Titling rule: broader than membership — a group containing any context
 * kind (plus e.g. reasoning) and no command/edit still titles as "context".
 */
export function activityTitleKind(cells: ActivityCellState[]): ActivityTitleKind {
  const counts = activityCounts(cells);
  const total = activityRecordTotal(cells);
  const contextTotal =
    (counts.fileRead ?? 0) + (counts.workspaceSearch ?? 0) + (counts.webSearch ?? 0) + (counts.mcpToolCall ?? 0);
  if (contextTotal > 0 && (counts.commandExecution ?? 0) === 0 && (counts.fileChange ?? 0) === 0) return "context";
  if ((counts.commandExecution ?? 0) > 0 && counts.commandExecution === total) return "command";
  if ((counts.fileChange ?? 0) > 0 && counts.fileChange === total) return "change";
  if ((counts.genericTool ?? 0) > 0 && counts.genericTool === total) return "tool";
  return "mixed";
}

/** Shared title strings, consumed by both the chat cells and the AgentLoop projection. */
export function collectedContextTitle(running: boolean, total: number): string {
  if (running) return "正在收集上下文";
  return total === 1 ? "已收集上下文" : `已收集 ${total} 个上下文来源`;
}

export function ranCommandsTitle(running: boolean, total: number): string {
  if (running) return total === 1 ? "正在运行命令" : `正在运行 ${total} 条命令`;
  return total === 1 ? "已运行命令" : `已运行 ${total} 条命令`;
}

export function activityGroupTitle(cells: ActivityCellState[], status: ActivityGroupStatus): string {
  if (status === "failed") return "需要处理";

  const kind = activityTitleKind(cells);
  const total = activityRecordTotal(cells);

  if (kind === "context") return collectedContextTitle(status === "running", total);
  if (kind === "command") return ranCommandsTitle(status === "running", total);
  if (kind === "change") {
    return total === 1 ? "已编辑文件" : `已编辑 ${total} 个文件`;
  }
  if (kind === "tool") {
    const title = cells[0]?.title?.trim();
    return total === 1 && title ? title : `已处理 ${total} 项`;
  }

  const firstKind = cells[0]?.activityKind;
  switch (firstKind) {
    case "fileRead":
      return total === 1 ? "已读取文件" : `已读取 ${total} 个文件`;
    case "workspaceSearch":
      return total === 1 ? "已搜索工作区" : `已搜索 ${total} 次`;
    case "webSearch":
      return total === 1 ? "已搜索网页" : `已搜索网页 ${total} 次`;
    case "commandExecution":
      return total === 1 ? "已运行命令" : `已运行 ${total} 条命令`;
    case "fileChange":
      return total === 1 ? "已编辑文件" : `已编辑 ${total} 个文件`;
    case "mcpToolCall":
      return total === 1 ? "已调用 MCP" : `已调用 ${total} 个 MCP 工具`;
    case "reasoning":
    case "planning":
    case "providerReasoning":
      return "思考过程";
    default:
      return `已处理 ${total} 项`;
  }
}
