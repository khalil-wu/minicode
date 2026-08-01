import type { ReactNode } from "react";
import type { AgentLoopProcessCell } from "../projection/project-turn";
import type { RenderAgentCell } from "./AgentTurn";
import { withStableRenderKeys } from "./renderKeys";

export function AgentTimeline({
  cells,
  renderCell,
  streamingIndicator,
}: {
  cells: AgentLoopProcessCell[];
  renderCell: RenderAgentCell;
  streamingIndicator: ReactNode;
}) {
  return (
    <div className="chat-turn-process-stack agent-loop-timeline">
      {withStableRenderKeys(cells).map(({ cell, key }) =>
        renderCell({
          key,
          cell,
          isActive: cell.kind === "streaming_assistant_narration",
          className: "chat-turn-process-cell agent-loop-process-cell",
        }),
      )}
      {streamingIndicator}
    </div>
  );
}
