import { memo, useCallback, useMemo } from "react";
import type {
  ChatTurnState,
  HistoryCellState,
  UserMessageCellState,
} from "../cells/cellTypes";
import { AgentTurn } from "../../agent-loop/components/AgentTurn";
import type { RenderAgentCellArgs } from "../../agent-loop/components/AgentTurn";
import { projectChatTurnToAgentLoop } from "../../agent-loop/projection/project-turn";
import {
  ActivityCell,
  AssistantMarkdownCell,
  CollaborationCell,
  DiffCell,
  ErrorCell,
  ExecCell,
  StatusNoticeCell,
  ThinkingCell,
  UserMessageCell,
} from "../cells";
import { useAppStore } from "../../stores";
import { sendClientCommand } from "../../protocol/ws-outbox";
import { buildInterruptCommand } from "../../lib/interrupt-command";

// ── ChatTurn ────────────────────────────────────────────────────────

export const ChatTurn = memo(function ChatTurn({
  turn,
  wide = false,
  defaultProcessExpanded,
  isTranscriptMode = false,
  conversationId,
  workspaceRoot,
}: {
  turn: ChatTurnState;
  wide?: boolean;
  defaultProcessExpanded?: boolean;
  isTranscriptMode?: boolean;
  /** Conversation that owns artifacts in this transcript.  This is explicit
   * because transcript cells can be rendered while another conversation is
   * active (for example, a child-agent replay). */
  conversationId?: string;
  /** Workspace that owns file paths projected into this transcript. */
  workspaceRoot?: string;
}) {
  const committedCells = turn.committedCells;
  const processDetailMode = useAppStore((state) => state.viewMode);
  const stopActiveRun = useCallback(() => {
    const state = useAppStore.getState();
    const command = buildInterruptCommand(state);
    sendClientCommand(command);
  }, []);
  const agentTurn = useMemo(
    () => projectChatTurnToAgentLoop(turn, committedCells, processDetailMode),
    [turn, committedCells, processDetailMode],
  );
  const renderCell = useCallback(
    ({ key, cell, isActive = false, className }: RenderAgentCellArgs) => (
      <div key={key} className={className} style={{ position: "relative" }}>
        <HistoryCellRenderer
          cell={cell}
          isActive={isActive}
          onStopExecution={isTranscriptMode ? undefined : stopActiveRun}
          isTranscriptMode={isTranscriptMode}
          conversationId={conversationId}
          workspaceRoot={workspaceRoot}
        />
      </div>
    ),
    [conversationId, isTranscriptMode, stopActiveRun, workspaceRoot],
  );

  return (
    <AgentTurn
      turn={agentTurn}
      wide={wide}
      renderCell={renderCell}
      defaultProcessExpanded={defaultProcessExpanded}
    />
  );
});

// ── HistoryCellRenderer ─────────────────────────────────────────────

export function HistoryCellRenderer({
  cell,
  isActive = false,
  onStopExecution,
  isTranscriptMode = false,
  conversationId,
  workspaceRoot,
}: {
  cell: HistoryCellState;
  isActive?: boolean;
  onStopExecution?: () => void;
  isTranscriptMode?: boolean;
  conversationId?: string;
  workspaceRoot?: string;
}) {
  switch (cell.kind) {
    case "user_message":
      return <UserMessageCell cell={cell} isTranscriptMode={isTranscriptMode} conversationId={conversationId} />;

    case "status_notice":
      return <StatusNoticeCell cell={cell} />;

    case "thinking":
      return <ThinkingCell cell={cell} isStreaming={cell.isStreaming || isActive} />;

    case "collaboration":
      return <CollaborationCell cell={cell} />;

    case "activity":
      return <ActivityCell cell={cell} isActive={isActive} conversationId={conversationId} />;

    case "exec":
      return <ExecCell cell={cell} isActive={isActive} onStop={isTranscriptMode ? undefined : onStopExecution} />;

    case "diff":
      return <DiffCell cell={cell} showActions={!isTranscriptMode} />;

    case "error":
      return <ErrorCell cell={cell} />;

    case "assistant_markdown":
      return <AssistantMarkdownCell
        cell={cell}
        isTranscriptMode={isTranscriptMode}
        conversationId={conversationId}
        workspaceRoot={workspaceRoot}
      />;

    default:
      // Live assistant text never reaches this renderer: the turn projection
      // routes a provisional item into the work timeline as process text and
      // the settled answer into the reply area as assistant markdown.
      return null;
  }
}
