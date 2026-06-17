import type { RenderAgentCell } from "./AgentTurn";
import type { AgentLoopAnswerCell } from "../projection/project-turn";

export function FinalAnswer({
  cell,
  isStreaming,
  isActive,
  renderCell,
}: {
  cell: AgentLoopAnswerCell;
  isStreaming: boolean;
  isActive: boolean;
  renderCell: RenderAgentCell;
}) {
  return (
    <section
      className="chat-turn-answer agent-loop-final-answer"
      data-streaming={isStreaming ? "true" : "false"}
      aria-label="Assistant response"
    >
      {renderCell({
        cell,
        isActive,
        className: "chat-turn-answer-cell agent-loop-answer-cell",
      })}
    </section>
  );
}
