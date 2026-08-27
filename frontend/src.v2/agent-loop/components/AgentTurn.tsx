import { memo, useEffect, useRef, useState } from "react";
import type React from "react";
import type { HistoryCellState } from "../../chat/cells/cellTypes";
import type { AgentLoopTurnProjection } from "../projection/project-turn";
import { AgentProcessSummary } from "./AgentProcessSummary";
import { AgentTimeline } from "./AgentTimeline";
import { FinalAnswer } from "./FinalAnswer";

export type RenderAgentCellArgs = {
  key?: React.Key;
  cell: HistoryCellState;
  isActive?: boolean;
  className?: string;
};

export type RenderAgentCell = (args: RenderAgentCellArgs) => React.ReactNode;

export const AgentTurn = memo(function AgentTurn({
  turn,
  wide = false,
  renderCell,
  defaultProcessExpanded,
}: {
  turn: AgentLoopTurnProjection;
  wide?: boolean;
  renderCell: RenderAgentCell;
  defaultProcessExpanded?: boolean;
}) {
  // Incomplete, interrupted, failed, or answer-less turns are evidence, not a
  // disclosure preference. They stay visible until a complete final answer
  // establishes the only valid collapse boundary for the turn.
  const initialProcessExpanded = turn.hasCompleteFinalAnswer
    ? defaultProcessExpanded ?? turn.initialProcessExpanded
    : true;
  const [processExpanded, setProcessExpanded] = useState(initialProcessExpanded);
  const previousTurnId = useRef(turn.id);
  const previousDetailMode = useRef(turn.processDetailMode);
  const previousDefaultProcessExpanded = useRef(defaultProcessExpanded);
  const previousStatus = useRef(turn.status);
  const previousHasCompleteFinalAnswer = useRef(turn.hasCompleteFinalAnswer);
  const userToggled = useRef(false);

  useEffect(() => {
    const changedTurn = previousTurnId.current !== turn.id;
    const changedMode = previousDetailMode.current !== turn.processDetailMode;
    const changedDefault = previousDefaultProcessExpanded.current !== defaultProcessExpanded;
    const reachedCompleteAnswer =
      !previousHasCompleteFinalAnswer.current
      && turn.hasCompleteFinalAnswer;
    const enteredRunning =
      previousStatus.current !== "running"
      && turn.status === "running";

    if (changedTurn || changedMode) {
      userToggled.current = false;
      setProcessExpanded(initialProcessExpanded);
    } else if (changedDefault && turn.hasCompleteFinalAnswer && !userToggled.current) {
      setProcessExpanded(initialProcessExpanded);
    } else if (
      reachedCompleteAnswer
      && turn.processDetailMode === "normal"
      && defaultProcessExpanded !== true
      && !userToggled.current
    ) {
      setProcessExpanded(false);
    } else if (
      enteredRunning
      && turn.processDetailMode === "normal"
      && defaultProcessExpanded === undefined
      && !userToggled.current
    ) {
      setProcessExpanded(true);
    }

    previousTurnId.current = turn.id;
    previousDetailMode.current = turn.processDetailMode;
    previousDefaultProcessExpanded.current = defaultProcessExpanded;
    previousStatus.current = turn.status;
    previousHasCompleteFinalAnswer.current = turn.hasCompleteFinalAnswer;
  }, [
    turn.id,
    initialProcessExpanded,
    turn.processDetailMode,
    turn.status,
    turn.hasCompleteFinalAnswer,
    defaultProcessExpanded,
  ]);

  // A settled file mutation is an outcome, not another activity row. Keep it
  // in the authoritative process projection for metrics, but render it after
  // the reply so the user sees the complete change set at the end of the turn.
  const timelineCells = turn.processCells.filter((cell) => cell.kind !== "diff");
  const diffCells = turn.processCells.filter((cell) => cell.kind === "diff");
  const hasTimelineItems = turn.processCells.length > 0;
  const failureIsTimelineEvidence = turn.processCells.some((cell) => cell.kind === "error");
  const showProcessStack =
    turn.hasProcessContent &&
    (timelineCells.length > 0 || diffCells.length > 0) &&
    processExpanded;
  return (
    <div
      className="chat-turn agent-loop-turn"
      data-status={turn.status}
      style={turnStyle(wide)}
    >
      {turn.userCell && renderCell({
        cell: turn.userCell,
        className: "chat-turn-user-cell agent-loop-user-cell",
      })}

      {turn.hasProcessContent && (
        <section
          className="chat-turn-process agent-loop-process agent-loop-work-area"
          data-zone="work"
          data-active={turn.status === "running" ? "true" : "false"}
          data-collapsed={!processExpanded ? "true" : "false"}
          aria-label="Agent 处理进度"
        >
          {turn.hasProcessContent && (
            <AgentProcessSummary
              status={turn.status}
              processExpanded={processExpanded}
              hasTimelineItems={hasTimelineItems}
              durationMs={turn.durationMs}
              failureMessage={failureIsTimelineEvidence ? undefined : turn.failureMessage}
              canCollapse={turn.hasCompleteFinalAnswer}
              onToggle={() => {
                userToggled.current = true;
                setProcessExpanded((value) => !value);
              }}
            />
          )}

          {showProcessStack && (
            <AgentTimeline
              cells={timelineCells}
              renderCell={renderCell}
              showAllOpenWork={turn.status !== "running" && !turn.hasCompleteFinalAnswer}
            />
          )}

        </section>
      )}

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
          />
        </section>
      )}

      {diffCells.length > 0 && turn.status !== "running" && (
        <section
          className="chat-turn-diff-zone agent-loop-diff-area"
          data-zone="diff"
          aria-label="文件修改"
        >
          {diffCells.map((cell) => renderCell({
            key: cell.id,
            cell,
          }))}
        </section>
      )}

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
