import type {
  AssistantMarkdownCellState,
  ChatTurnState,
  StreamingAssistantTailCellState,
} from "../../chat/cells/cellTypes";
import type { ViewMode } from "../../stores/types";

export type AgentLoopProcessCell = ChatTurnState["committedCells"][number];
export type AgentLoopAnswerCell = AssistantMarkdownCellState;
export type AgentTurnStatus = "running" | "completed" | "partial" | "failed" | "stopped";

export interface AgentProcessMetrics {
  toolCallCount: number;
  commandCount: number;
  fileCount: number;
  additions: number;
  deletions: number;
  subagentCount: number;
  failureCount: number;
  inputTokens: number;
  outputTokens: number;
  reasoningTokens: number;
}

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
  processSummary: AgentProcessMetrics;
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
  const processSummary = summarizeProcess(turn, projectedProcessCells);
  const hasSummaryFacts = hasProcessSummaryFacts(processSummary);
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
      || hasSummaryFacts
      || (turn.status === "streaming" && !answerCell),
    processSummary,
    durationMs,
    failureMessage: turn.finalAnswerCell?.failureMessage
      || turn.committedCells.find((cell) => cell.kind === "error")?.message,
    processDetailMode,
    // The setting controls the work-area disclosure itself, not only the
    // contents of individual tool cards:
    // - summary: compact transcript
    // - normal: follow live work, collapse only after a complete final answer
    // - verbose: keep the complete work trace visible
    initialProcessExpanded:
      processDetailMode === "verbose"
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

function summarizeProcess(
  turn: ChatTurnState,
  cells: AgentLoopProcessCell[],
): AgentProcessMetrics {
  let toolCallCount = 0;
  let commandCount = 0;
  let additions = 0;
  let deletions = 0;
  let failureCount = 0;
  const files = new Set<string>();
  const subagents = new Set<string>();

  for (const cell of cells) {
    if (cell.kind === "activity") {
      const records = cell.toolCallRecords ?? [];
      toolCallCount += records.length;
      const recordFailures = records.filter((record) =>
        ["failed", "blocked", "timeout", "cancelled"].includes(String(record.status)),
      ).length;
      failureCount += recordFailures || (
        cell.status === "failed" || cell.status === "interrupted" ? 1 : 0
      );
      continue;
    }
    if (cell.kind === "exec") {
      toolCallCount += 1;
      commandCount += 1;
      if (cell.status === "failed" || cell.status === "cancelled") failureCount += 1;
      continue;
    }
    if (cell.kind === "diff") {
      toolCallCount += Math.max(0, cell.toolCallCount ?? 0);
      additions += Math.max(0, cell.summary.added);
      deletions += Math.max(0, cell.summary.deleted);
      for (const file of cell.files) {
        const path = file.path.trim();
        if (path) files.add(path);
      }
      continue;
    }
    if (cell.kind === "collaboration") {
      // A collaboration cell is projected from one authoritative control tool
      // record; its entries may represent several children in a parallel task.
      toolCallCount += 1;
      for (const entry of cell.entries) {
        const id = entry.agentId.trim();
        if (id) subagents.add(id);
      }
      continue;
    }
    if (cell.kind === "error") failureCount += 1;
  }

  return {
    toolCallCount,
    commandCount,
    fileCount: files.size,
    additions,
    deletions,
    subagentCount: subagents.size,
    failureCount,
    inputTokens: nonnegativeMetric(turn.usage?.input),
    outputTokens: nonnegativeMetric(turn.usage?.output),
    reasoningTokens: nonnegativeMetric(turn.usage?.reasoning),
  };
}

function nonnegativeMetric(value: number | undefined): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0 ? value : 0;
}

function hasProcessSummaryFacts(summary: AgentProcessMetrics): boolean {
  return Boolean(
    summary.toolCallCount
    || summary.commandCount
    || summary.fileCount
    || summary.additions
    || summary.deletions
    || summary.subagentCount
    || summary.failureCount
    || summary.inputTokens
    || summary.outputTokens
    || summary.reasoningTokens,
  );
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
