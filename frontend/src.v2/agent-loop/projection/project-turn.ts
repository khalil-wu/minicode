import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  StreamingAssistantTailCellState,
} from "../../chat/cells/cellTypes";
import { collapseReadSearchCells } from "./collapse-read-search";

export type AgentLoopProcessCell = ChatTurnState["committedCells"][number];
export type AgentLoopAnswerCell = AssistantMarkdownCellState;
export type AgentTurnStatus = "running" | "completed" | "partial" | "failed" | "stopped";

export interface AgentLoopTurnProjection {
  id: string;
  status: AgentTurnStatus;
  userCell: ChatTurnState["userCell"];
  processCells: AgentLoopProcessCell[];
  answerCell: AgentLoopAnswerCell | null;
  activeAnswerCell: StreamingAssistantTailCellState | null;
  answerIsStreaming: boolean;
  hasProcessContent: boolean;
  initialProcessExpanded: boolean;
  durationLabel: string;
}

export function projectChatTurnToAgentLoop(
  turn: ChatTurnState,
  committedCells: ChatTurnState["committedCells"] = turn.committedCells,
): AgentLoopTurnProjection {
  const activeAnswerCell =
    turn.activeCell?.kind === "streaming_assistant_tail" ? turn.activeCell : null;
  const activeAnswerMarkdownCell = activeAnswerCell
    ? activeTailToAssistantMarkdownCell(activeAnswerCell, `${turn.id}-final`)
    : null;
  const answerCell = turn.finalAnswerCell?.markdownSource.trim()
    ? turn.finalAnswerCell
    : activeAnswerMarkdownCell;
  const processCells = [...committedCells];
  if (turn.activeCell?.kind === "streaming_assistant_narration") {
    processCells.push(turn.activeCell);
  }

  const visibleProcessCells = collapseReadSearchCells(processCells);

  return {
    id: turn.id,
    status: mapStatus(turn.status),
    userCell: turn.userCell,
    processCells: visibleProcessCells,
    answerCell,
    activeAnswerCell,
    answerIsStreaming: Boolean(activeAnswerCell) || Boolean(turn.finalAnswerCell?.isStreaming),
    hasProcessContent: visibleProcessCells.length > 0 || (turn.status === "streaming" && !answerCell),
    // Tool items are part of the transcript, as in Codex/pi/Claude Code. Keep
    // the existing summary toggle for users who want a compact history, but do
    // not hide completed commands and file targets by default.
    initialProcessExpanded: visibleProcessCells.length > 0 || turn.status === "streaming",
    durationLabel: formatTurnDuration(turn),
  };
}

function activeTailToAssistantMarkdownCell(
  cell: StreamingAssistantTailCellState,
  id: string,
): AssistantMarkdownCellState {
  return {
    kind: "assistant_markdown",
    id,
    messageId: id,
    markdownSource: cell.partialMarkdown,
    phase: "final",
    copyable: true,
    createdAt: cell.updatedAt,
    isStreaming: true,
    source: "stream",
  };
}

function mapStatus(status: ChatTurnState["status"]): AgentTurnStatus {
  if (status === "streaming") return "running";
  if (status === "partial") return "partial";
  if (status === "interrupted") return "stopped";
  if (status === "failed") return "failed";
  return "completed";
}

function formatTurnDuration(turn: ChatTurnState): string {
  if (turn.durationMs != null && Number.isFinite(turn.durationMs) && turn.durationMs >= 0) {
    return formatDurationMs(turn.durationMs);
  }
  const start = turn.startedAt;
  const end = turn.completedAt;
  if (end == null || !Number.isFinite(start) || !Number.isFinite(end) || end <= start) return "";
  return formatDurationMs(end - start);
}

function formatDurationMs(durationMs: number): string {
  const totalSeconds = Math.max(1, Math.floor(durationMs / 1000));
  if (totalSeconds < 60) return `${totalSeconds}s`;
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  if (minutes < 60) return seconds > 0 ? `${minutes}m ${seconds}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remainMinutes = minutes % 60;
  return remainMinutes > 0 ? `${hours}h ${remainMinutes}m` : `${hours}h`;
}
