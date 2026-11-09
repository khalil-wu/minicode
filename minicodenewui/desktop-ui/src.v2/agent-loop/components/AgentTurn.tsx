import { memo, useCallback, useEffect, useMemo, useRef } from "react";
import type React from "react";
import type { HistoryCellState } from "../../chat/cells/cellTypes";
import type { AgentLoopTurnProjection } from "../projection/project-turn";
import { AgentProcessSummary } from "./AgentProcessSummary";
import { AgentTimeline } from "./AgentTimeline";
import { FinalAnswer } from "./FinalAnswer";
import { useTranscriptSearch } from "../../chat/TranscriptSearchContext";
import { ProcessDetailReadingContext, useProcessDetailReading, useTranscriptReadingPreference } from "../../chat/transcriptReadingState";

export type RenderAgentCellArgs = {
  key?: React.Key;
  cell: HistoryCellState;
  isActive?: boolean;
  className?: string;
  afterContent?: React.ReactNode;
};

export type RenderAgentCell = (args: RenderAgentCellArgs) => React.ReactNode;

export const AgentTurn = memo(function AgentTurn({
  turn,
  wide = false,
  renderCell,
  defaultProcessExpanded,
  hasMoreHistory = false,
  onLoadHistory,
  persistentContent,
  collapsedContent,
}: {
  turn: AgentLoopTurnProjection;
  wide?: boolean;
  renderCell: RenderAgentCell;
  defaultProcessExpanded?: boolean;
  hasMoreHistory?: boolean;
  onLoadHistory?: () => void;
  persistentContent?: React.ReactNode;
  collapsedContent?: React.ReactNode;
}) {
  // Live and incomplete work starts open. Only a complete final answer folds
  // it automatically. The parent's toggle and explicit raw exec/MCP reading
  // retain the parent; ordinary child groups keep independent preferences.
  const initialProcessExpanded = !turn.hasCompleteFinalAnswer
    ? true
    : defaultProcessExpanded ?? turn.initialProcessExpanded;
  const [processPreference, setProcessPreference, userToggled] = useTranscriptReadingPreference("process", {
    complete: turn.hasCompleteFinalAnswer,
    expanded: initialProcessExpanded,
  });
  const searching = useTranscriptSearch();
  const [protectedDetailReading, protectDetailReading] = useProcessDetailReading(turn.id);
  const reachedCompleteAnswer = turn.hasCompleteFinalAnswer && !processPreference.complete;
  const lostCompleteAnswer = !turn.hasCompleteFinalAnswer && processPreference.complete;
  // Completion belongs to the saved reading choice, so it also folds when a
  // virtualized row first mounts after the final item arrived offscreen.
  const nextExpanded = reachedCompleteAnswer && !userToggled.current ? defaultProcessExpanded ?? false
    : lostCompleteAnswer && !userToggled.current ? true
    : processPreference.expanded;
  const processExpanded = searching || nextExpanded
    || turn.hasCompleteFinalAnswer && protectedDetailReading && !userToggled.current;
  const previouslyExpanded = useRef(false);
  useEffect(() => {
    if (processExpanded && !previouslyExpanded.current) onLoadHistory?.();
    previouslyExpanded.current = processExpanded;
  }, [processExpanded, onLoadHistory]);
  const setProcessExpanded = useCallback((next: boolean | ((value: boolean) => boolean)) => {
    setProcessPreference((previous) => ({ ...previous, expanded: typeof next === "function" ? next(previous.expanded) : next }));
  }, [setProcessPreference]);

  useEffect(() => {
    if (!reachedCompleteAnswer && !lostCompleteAnswer) return;
    const next = {
      complete: turn.hasCompleteFinalAnswer,
      expanded: nextExpanded,
    };
    setProcessPreference(next);
  }, [
    turn.hasCompleteFinalAnswer,
    reachedCompleteAnswer,
    lostCompleteAnswer,
    nextExpanded,
    setProcessPreference,
  ]);

  // A settled file mutation is an outcome, not another activity row. Keep it
  // in the authoritative process projection for metrics, but render it after
  // the reply so the user sees the complete change set at the end of the turn.
  const timelineCells = useMemo(() => turn.processCells.filter((cell) => cell.kind !== "diff"), [turn.processCells]);
  const diffCells = useMemo(() => turn.processCells.filter((cell) => cell.kind === "diff"), [turn.processCells]);
  const visibleTimelineCells = useMemo(() => processExpanded
    ? timelineCells
    : [], [processExpanded, timelineCells]);
  const hasTimelineItems = turn.processCells.length > 0 || hasMoreHistory;
  const failureIsTimelineEvidence = useMemo(() => turn.processCells.some((cell) => cell.kind === "error"), [turn.processCells]);
  const showProcessStack =
    turn.hasProcessContent &&
    processExpanded && (visibleTimelineCells.length > 0 || hasMoreHistory);
  const standaloneNotice = !turn.userCell && turn.status === "completed"
    && turn.processCells.length > 0
    && turn.processCells.every((cell) => cell.kind === "status_notice" || cell.kind === "context_compaction");
  const processSummary = standaloneNotice ? null : (
    <AgentProcessSummary
      status={turn.status}
      processExpanded={processExpanded}
      hasTimelineItems={hasTimelineItems}
      durationMs={turn.durationMs}
      startedAt={turn.startedAt}
      failureMessage={failureIsTimelineEvidence && processExpanded ? undefined : turn.failureMessage}
      position="top"
      onToggle={() => {
        userToggled.current = true;
        setProcessExpanded(!processExpanded);
      }}
    />
  );
  const fileChanges = diffCells.length > 0 && turn.status !== "running" ? (
    <section className="chat-turn-diff-zone agent-loop-diff-area" data-zone="diff" aria-label="文件修改">
      {diffCells.map((cell) => renderCell({ key: cell.id, cell }))}
    </section>
  ) : null;
  return (
    <div
      className="chat-turn agent-loop-turn"
      data-message-id={turn.id}
      data-status={turn.status}
      style={turnStyle(wide)}
    >
      {turn.userCell && renderCell({
        cell: turn.userCell,
        className: "chat-turn-user-cell agent-loop-user-cell",
      })}

      {(turn.hasProcessContent || Boolean(turn.workItems?.length)) && (
        <ProcessDetailReadingContext.Provider value={protectDetailReading}>
        <section
          className="chat-turn-process agent-loop-process agent-loop-work-area"
          data-zone="work"
          data-active={turn.status === "running" && !turn.hasCompleteFinalAnswer ? "true" : "false"}
          data-collapsed={!processExpanded ? "true" : "false"}
          aria-label="Agent 处理进度"
        >
          {turn.hasProcessContent && processSummary}
          {!processExpanded && collapsedContent}

          {turn.workItems ? turn.workItems.map((item) => item.kind === "user"
            ? renderCell({ key: item.id, cell: item.cell, className: "chat-turn-user-cell agent-loop-user-cell" })
            : processExpanded && (
              <AgentTimeline
                key={item.id}
                cells={item.cells}
                renderCell={renderCell}
                isRunning={turn.status === "running" && item === turn.workItems?.at(-1)}
                expandWorkGroups={false}
                showCompletedReasoning={processExpanded}
                showAllOpenWork={turn.status !== "running" && !turn.hasCompleteFinalAnswer}
                onHistoryStartReached={item === turn.workItems?.find((entry) => entry.kind === "process") ? onLoadHistory : undefined}
              />
            )) : showProcessStack && (
            <AgentTimeline
              cells={visibleTimelineCells}
              renderCell={renderCell}
              isRunning={turn.status === "running"}
              expandWorkGroups={false}
              showCompletedReasoning={processExpanded}
              showAllOpenWork={turn.status !== "running" && !turn.hasCompleteFinalAnswer}
              onHistoryStartReached={onLoadHistory}
            />
          )}

        </section>
        </ProcessDetailReadingContext.Provider>
      )}

      {persistentContent}

      {turn.answerCell && (
        <section
          className="chat-turn-answer-zone agent-loop-reply-area"
          data-zone="reply"
          aria-label="Agent 回复"
        >
          <FinalAnswer
            cell={turn.answerCell}
            isStreaming={turn.answerIsStreaming}
            isActive={Boolean(turn.activeAnswerCell)}
            renderCell={renderCell}
            afterContent={fileChanges}
          />
        </section>
      )}

      {!turn.answerCell && fileChanges}

    </div>
  );
});

const turnStyle = (wide: boolean): React.CSSProperties => ({
  display: "flex",
  flexDirection: "column",
  gap: 8,
  width: wide ? "var(--chat-wide-axis-width)" : "var(--chat-axis-width)",
  margin: "0 auto",
});
