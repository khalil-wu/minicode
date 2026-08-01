import { memo, useEffect, useState } from "react";
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
}: {
  turn: AgentLoopTurnProjection;
  wide?: boolean;
  renderCell: RenderAgentCell;
}) {
  const [processExpanded, setProcessExpanded] = useState(turn.initialProcessExpanded);

  useEffect(() => {
    setProcessExpanded(turn.initialProcessExpanded);
  }, [turn.id, turn.initialProcessExpanded]);

  const hasTimelineItems = turn.processCells.length > 0;
  const showProcessStack =
    turn.hasProcessContent &&
    hasTimelineItems &&
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
          aria-label="Agent progress"
        >
          {(hasTimelineItems || turn.status === "running") && (
            <AgentProcessSummary
              status={turn.status}
              processExpanded={processExpanded}
              durationLabel={turn.durationLabel}
              hasTimelineItems={hasTimelineItems}
              onToggle={() => setProcessExpanded((value) => !value)}
            />
          )}

          {showProcessStack && (
            <AgentTimeline
              cells={turn.processCells}
              renderCell={renderCell}
              streamingIndicator={null}
            />
          )}

        </section>
      )}

      {turn.answerCell && (
        <section
          className="chat-turn-answer-zone agent-loop-reply-area"
          data-zone="reply"
          aria-label="Agent reply"
        >
          <FinalAnswer
            cell={turn.answerCell}
            isStreaming={turn.answerIsStreaming}
            isActive={Boolean(turn.activeAnswerCell)}
            renderCell={renderCell}
          />
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
