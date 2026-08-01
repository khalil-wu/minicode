import type { ChatTurnState, ActivityCellState } from "../../chat/cells/cellTypes";
import {
  isFileReadToolRecord,
  isWorkspaceSearchToolRecord,
  type ToolCallRecord,
} from "../../lib/tool-call-reducer";

type ReadSearchKind = "read" | "search";
type ProcessCell = ChatTurnState["committedCells"][number];

function readSearchKind(record: ToolCallRecord): ReadSearchKind | null {
  if (isWorkspaceSearchToolRecord(record)) {
    return "search";
  }
  if (isFileReadToolRecord(record)) {
    return "read";
  }
  return null;
}

function collapsibleRecords(cell: ProcessCell): ToolCallRecord[] | null {
  if (cell.kind !== "activity" || !cell.toolCallRecords?.length) return null;
  if (cell.status !== "running" && cell.status !== "done") return null;
  return cell.toolCallRecords.every((record) => readSearchKind(record) !== null)
    ? cell.toolCallRecords
    : null;
}

function collapsedCell(cells: ActivityCellState[]): ActivityCellState {
  const records = cells.flatMap((cell) => cell.toolCallRecords ?? []);
  const readCount = records.filter((record) => readSearchKind(record) === "read").length;
  const searchCount = records.length - readCount;
  const running = cells.some((cell) => cell.status === "running");
  const parts: string[] = [];
  if (readCount) parts.push(`${running ? "正在读取" : "已读取"} ${readCount} 项`);
  if (searchCount) parts.push(`${running ? "正在搜索" : "已搜索"} ${searchCount} 次`);
  return {
    kind: "activity",
    id: `read-search-${cells[0].id}`,
    activityKind: searchCount ? "workspaceSearch" : "fileRead",
    title: parts.join("，"),
    subtitle: records.at(-1)?.displayHint || records.at(-1)?.inputSummary || undefined,
    status: running ? "running" : "done",
    collapsed: !running,
    toolCallRecords: records,
    startedAt: Math.min(...cells.map((cell) => cell.startedAt)),
    completedAt: running
      ? undefined
      : Math.max(...cells.map((cell) => cell.completedAt ?? cell.startedAt)),
  };
}

/**
 * CC-style render-only grouping. Only adjacent typed read/search activities
 * collapse; any text, command, write, approval, error, or status cell ends the
 * group. The source cells and transcript remain untouched.
 */
export function collapseReadSearchCells(cells: ProcessCell[]): ProcessCell[] {
  const result: ProcessCell[] = [];
  let group: ActivityCellState[] = [];

  const flush = () => {
    if (group.length === 1) result.push(group[0]);
    else if (group.length > 1) result.push(collapsedCell(group));
    group = [];
  };

  for (const cell of cells) {
    if (collapsibleRecords(cell)) {
      group.push(cell as ActivityCellState);
      continue;
    }
    flush();
    result.push(cell);
  }
  flush();
  return result;
}
