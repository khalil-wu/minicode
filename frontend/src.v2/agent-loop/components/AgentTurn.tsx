import { memo, useEffect, useState } from "react";
import type React from "react";
import type { HistoryCellState } from "../../chat/cells/cellTypes";
import type { AgentLoopTurnProjection } from "../projection/project-turn";
import { AgentProcessSummary } from "./AgentProcessSummary";
import { AgentTimeline } from "./AgentTimeline";
import { ArtifactCards } from "./ArtifactCards";
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

  const showProcessStack =
    turn.hasProcessContent &&
    (!turn.shouldCollapseProcess || processExpanded);
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
          className="chat-turn-process agent-loop-process"
          data-active={turn.status === "running" ? "true" : "false"}
          data-collapsed={turn.shouldCollapseProcess && !processExpanded ? "true" : "false"}
          aria-label="Agent progress"
        >
          <AgentProcessSummary
            status={turn.status}
            shouldCollapseProcess={turn.shouldCollapseProcess}
            processExpanded={processExpanded}
            durationLabel={turn.durationLabel}
            summaryItems={turn.summaryItems}
            onToggle={() => setProcessExpanded((value) => !value)}
          />

          {showProcessStack && (
            <AgentTimeline
              items={turn.timelineItems}
              cells={turn.processCells}
              renderCell={renderCell}
              streamingIndicator={null}
            />
          )}
        </section>
      )}

      {turn.answerCell && (
        <FinalAnswer
          cell={turn.answerCell}
          isStreaming={turn.answerIsStreaming}
          isActive={Boolean(turn.activeAnswerCell)}
          renderCell={renderCell}
        />
      )}

      <ArtifactCards cells={turn.artifactCells} />
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
