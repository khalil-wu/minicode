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
  const knownFilePaths = useMemo(() => [...new Set(agentTurn.processCells.flatMap((cell) => {
    if (cell.kind === "diff") return cell.files.filter((file) => file.changeType !== "deleted").map((file) => file.path);
    if (cell.kind !== "activity") return [];
    return (cell.toolCallRecords ?? []).filter((record) => record.status === "success").flatMap((record) => [
      ...(typeof record.args.file_path === "string" ? [record.args.file_path] : []),
      ...(record.diff?.files ?? []).filter((file) => file.status !== "deleted").map((file) => file.path),
      ...(record.outputFiles ?? []).map((file) => file.path),
    ]);
  }))], [agentTurn.processCells]);
  const renderCell = useCallback(
    ({ key, cell, isActive = false, className, afterContent }: RenderAgentCellArgs) => (
      <div key={key} className={className} style={{ position: "relative" }}>
        <HistoryCellRenderer
          cell={cell}
          isActive={isActive}
          onStopExecution={isTranscriptMode ? undefined : stopActiveRun}
          isTranscriptMode={isTranscriptMode}
          conversationId={conversationId}
          workspaceRoot={workspaceRoot}
          knownFilePaths={knownFilePaths}
          afterContent={afterContent}
        />
      </div>
    ),
    [conversationId, isTranscriptMode, stopActiveRun, workspaceRoot, knownFilePaths],
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

export const HistoryCellRenderer = memo(function HistoryCellRenderer({
  cell,
  isActive = false,
  onStopExecution,
  isTranscriptMode = false,
  conversationId,
  workspaceRoot,
  knownFilePaths,
  afterContent,
}: {
  cell: HistoryCellState;
  isActive?: boolean;
  onStopExecution?: () => void;
  isTranscriptMode?: boolean;
  conversationId?: string;
  workspaceRoot?: string;
  knownFilePaths?: string[];
  afterContent?: React.ReactNode;
}) {
  switch (cell.kind) {
    case "user_message":
      return <UserMessageCell cell={cell} isTranscriptMode={isTranscriptMode} conversationId={conversationId} />;

    case "status_notice":
      return <StatusNoticeCell cell={cell} />;

    case "thinking":
      return <ThinkingCell cell={cell} isStreaming={cell.isStreaming || isActive} conversationId={conversationId} workspaceRoot={workspaceRoot} knownFilePaths={knownFilePaths} />;

    case "collaboration":
      return <CollaborationCell cell={cell} />;

    case "activity":
      return <ActivityCell cell={cell} conversationId={conversationId} workspaceRoot={workspaceRoot} />;

    case "exec":
      return <ExecCell cell={cell} isActive={isActive} onStop={isTranscriptMode ? undefined : onStopExecution} />;

    case "diff":
      return <DiffCell cell={cell} showActions={!isTranscriptMode} conversationId={conversationId} workspaceRoot={workspaceRoot} />;

    case "error":
      return <ErrorCell cell={cell} />;

    case "assistant_markdown":
      return <AssistantMarkdownCell
        cell={cell}
        isTranscriptMode={isTranscriptMode}
        conversationId={conversationId}
        workspaceRoot={workspaceRoot}
        knownFilePaths={knownFilePaths}
        afterContent={afterContent}
      />;

    default:
      // Live assistant text never reaches this renderer: the turn projection
      // routes a provisional item into the work timeline as process text and
      // the settled answer into the reply area as assistant markdown.
      return null;
  }
});
