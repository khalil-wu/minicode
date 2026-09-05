import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  StreamingAssistantTailCellState,
} from "../../chat/cells/cellTypes";
import type { ViewMode } from "../../stores/types";

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
  hasCompleteFinalAnswer: boolean;
  hasProcessContent: boolean;
  durationMs: number | null;
  failureMessage?: string;
  processDetailMode: ViewMode;
  initialProcessExpanded: boolean;
}

export function projectChatTurnToAgentLoop(
  turn: ChatTurnState,
  committedCells: ChatTurnState["committedCells"] = turn.committedCells,
  processDetailMode: ViewMode = "normal",
): AgentLoopTurnProjection {
  const activeAnswerCell =
    turn.activeCell?.kind === "streaming_assistant_tail" ? turn.activeCell : null;
  const activeAnswerMarkdownCell = activeAnswerCell
    ? activeTailToAssistantMarkdownCell(activeAnswerCell, `${turn.id}-final`)
    : null;
  const finalAnswerHasContent = Boolean(
    turn.finalAnswerCell?.markdownSource.trim()
    || turn.finalAnswerCell?.artifacts?.length
    || turn.finalAnswerCell?.imageProgress?.length,
  );
  const answerCell = finalAnswerHasContent && turn.finalAnswerCell
    ? activeAnswerCell
      ? appendActiveTail(turn.finalAnswerCell, activeAnswerCell)
      : turn.finalAnswerCell
    : activeAnswerMarkdownCell;
  const processCells = committedCells;

  // AgentTimeline is the one grouping/disclosure path. Keep every ordered
  // cell intact so live work stays immediate and only a closed semantic
  // segment is folded.
  const projectedProcessCells = processCells;
  const hasNarration = processCells.some((cell) => cell.kind === "thinking"
    && !cell.collapsible && cell.source !== "provider" && cell.source !== "reasoning");
  const durationMs = turnDurationMs(turn);
  const hasCompleteFinalAnswer = Boolean(
    turn.status === "completed"
    && finalAnswerHasContent
    && turn.finalAnswerCell
    && !turn.finalAnswerCell.isStreaming
    && !activeAnswerCell,
  );

  return {
    id: turn.id,
    status: mapStatus(turn.status),
    userCell: turn.userCell,
    processCells: projectedProcessCells,
    answerCell,
    activeAnswerCell,
    answerIsStreaming: Boolean(activeAnswerCell) || Boolean(turn.finalAnswerCell?.isStreaming),
    hasCompleteFinalAnswer,
    hasProcessContent:
      projectedProcessCells.length > 0
      || (turn.status === "streaming" && !answerCell),
    durationMs,
    failureMessage: turn.finalAnswerCell?.failureMessage
      || turn.committedCells.find((cell) => cell.kind === "error")?.message,
    processDetailMode,
    // The setting controls the work-area disclosure itself, not only the
    // contents of individual tool cards:
    // - summary: compact transcript
    // - normal: keep narration in order; individual completed work groups fold
    // - verbose: keep the complete work trace visible
    initialProcessExpanded:
      processDetailMode === "verbose"
      || (processDetailMode === "normal" && hasNarration)
      || !hasCompleteFinalAnswer,
  };
}

function turnDurationMs(turn: ChatTurnState): number | null {
  if (typeof turn.durationMs === "number" && Number.isFinite(turn.durationMs) && turn.durationMs >= 0) {
    return turn.durationMs;
  }
  if (
    turn.status !== "streaming"
    && typeof turn.completedAt === "number"
    && Number.isFinite(turn.completedAt)
    && Number.isFinite(turn.startedAt)
  ) {
    return Math.max(0, turn.completedAt - turn.startedAt);
  }
  return null;
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

function appendActiveTail(
  cell: AssistantMarkdownCellState,
  tail: StreamingAssistantTailCellState,
): AssistantMarkdownCellState {
  const hasArtifactBoundary = Boolean(
    cell.artifacts?.length
    || cell.imageProgress?.length
    || cell.markdownBeforeArtifacts !== undefined
    || cell.markdownAfterArtifacts !== undefined,
  );
  return {
    ...cell,
    markdownSource: `${cell.markdownSource}${tail.partialMarkdown}`,
    ...(hasArtifactBoundary
      ? {
          markdownBeforeArtifacts: cell.markdownBeforeArtifacts ?? cell.markdownSource,
          markdownAfterArtifacts: `${cell.markdownAfterArtifacts ?? ""}${tail.partialMarkdown}`,
        }
      : {}),
    isStreaming: true,
  };
}

function mapStatus(status: ChatTurnState["status"]): AgentTurnStatus {
  if (status === "streaming") return "running";
  if (status === "partial") return "partial";
  if (status === "interrupted") return "stopped";
  if (status === "failed") return "failed";
  return "completed";
}
