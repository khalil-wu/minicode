import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  StreamingAssistantTailCellState,
} from "../../chat/cells/cellTypes";
import type { ViewMode } from "../../stores/types";
import { proposedPlanParts } from "../../lib/proposed-plan";

export type AgentLoopProcessCell = ChatTurnState["committedCells"][number];
export type AgentLoopAnswerCell = AssistantMarkdownCellState;
export type AgentTurnStatus = "running" | "completed" | "partial" | "failed" | "stopped";

export interface AgentLoopTurnProjection {
  id: string;
  status: AgentTurnStatus;
  userCell: ChatTurnState["userCell"];
  workItems?: ChatTurnState["workItems"];
  processCells: AgentLoopProcessCell[];
  answerCell: AgentLoopAnswerCell | null;
  activeAnswerCell: StreamingAssistantTailCellState | null;
  answerIsStreaming: boolean;
  hasCompleteFinalAnswer: boolean;
  hasProcessContent: boolean;
  durationMs: number | null;
  startedAt?: number;
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
  // AgentTimeline is the one grouping/disclosure path. Keep every ordered
  // cell intact so live work stays immediate and only a closed semantic
  // segment is folded.
  const durationMs = turnDurationMs(turn);
  const proposedPlan = turn.finalAnswerCell ? proposedPlanParts(turn.finalAnswerCell.markdownSource) : null;
  const hasCompleteFinalAnswer = Boolean(
    (turn.status === "completed" || turn.status === "streaming")
    && finalAnswerHasContent
    && turn.finalAnswerCell
    && !turn.finalAnswerCell.isStreaming
    && (!proposedPlan || proposedPlan.complete)
    && (turn.finalAnswerCell.imageProgress ?? []).every((progress) => progress.status === "completed")
    && !activeAnswerCell,
  );

  return {
    id: turn.id,
    status: mapStatus(turn.status),
    userCell: turn.userCell,
    workItems: turn.workItems,
    processCells: committedCells,
    answerCell,
    activeAnswerCell,
    answerIsStreaming: Boolean(activeAnswerCell) || Boolean(turn.finalAnswerCell?.isStreaming),
    hasCompleteFinalAnswer,
    hasProcessContent:
      committedCells.length > 0
      || Boolean(turn.toolPage?.remaining)
      || turn.status === "interrupted"
      || (turn.status === "streaming" && !answerCell),
    durationMs,
    startedAt: turn.startedAt,
    failureMessage: turn.finalAnswerCell?.failureMessage
      || turn.committedCells.find((cell) => cell.kind === "error")?.message,
    processDetailMode,
    // Detail mode controls the visible trace, not the completion boundary.
    initialProcessExpanded: !hasCompleteFinalAnswer,
  };
}

function turnDurationMs(turn: ChatTurnState): number | null {
  if (typeof turn.durationMs === "number" && Number.isFinite(turn.durationMs) && turn.durationMs >= 0) {
    return turn.durationMs;
  }
  // The worked-for end is the original start + the authoritative duration.
  // A viewer's observation/commit time cannot supply a missing execution end.
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
