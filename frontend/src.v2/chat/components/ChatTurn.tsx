import { memo, useCallback, useMemo, useState } from "react";
import { loadEarlierToolItems } from "../historyPagination";
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
import { knownFilePathsForCell } from "../cells/activityCellHelpers";

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
  const [loadingTools, setLoadingTools] = useState(false);
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
  const resourceKey = turn.resourceKey ?? agentTurn.processCells;
  const knownFilePaths = useMemo(() => [...new Set(agentTurn.processCells.flatMap(knownFilePathsForCell))], [resourceKey]);
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
      loadedToolItems={turn.toolPage ? turn.toolPage.total - turn.toolPage.remaining : undefined}
      historyControl={turn.toolPage?.remaining && conversationId ? (
        <button type="button" className="agent-loop-timeline-group-title" disabled={loadingTools}
          onClick={() => {
            setLoadingTools(true);
            void loadEarlierToolItems(conversationId, turn.id).finally(() => setLoadingTools(false));
          }}>
          {loadingTools ? "正在加载更早的步骤…" : `加载更早的工具步骤（${turn.toolPage.remaining}）`}
        </button>
      ) : undefined}
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
